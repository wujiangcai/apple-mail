from collections.abc import Callable
from datetime import UTC, datetime
import hashlib
import secrets

from flask import Blueprint, Flask, current_app, jsonify, request

from mail_manager import db
from mail_manager.crypto import CredentialCipher
from mail_manager.models import RentalSummary


class RentalError(Exception):
    pass


class InvalidRentalPayload(RentalError, ValueError):
    pass


class NoRentableAccount(RentalError, LookupError):
    pass


class RentalNotFound(RentalError, LookupError):
    pass


class RentalCodeUnavailable(RentalError):
    pass


class RentalInactive(RentalError):
    pass


def hash_access_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_expiry(value: object) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise InvalidRentalPayload
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidRentalPayload from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidRentalPayload
    return parsed.astimezone(UTC)


def _summary(row) -> RentalSummary:
    return RentalSummary(
        id=row[0],
        account_id=row[1],
        email=row[2],
        starts_at=_as_utc(row[3]),
        expires_at=None if row[4] is None else _as_utc(row[4]),
        ended_at=None if row[5] is None else _as_utc(row[5]),
        revoked_at=None if row[6] is None else _as_utc(row[6]),
        last_access_at=None if row[7] is None else _as_utc(row[7]),
        access_count=row[8],
        code_available=bool(row[9]),
        code_rotated_at=None if row[10] is None else _as_utc(row[10]),
    )


class PostgresRentalRepository:
    def __init__(self, connection: object | Callable[[], object] = db.get_db):
        self._connection_source = connection

    def _connection(self):
        source = self._connection_source
        return source() if callable(source) else source

    @staticmethod
    def _expire(connection, now):
        expired = connection.execute(
            """
            UPDATE mailbox_rentals
            SET ended_at = expires_at
            WHERE ended_at IS NULL
              AND expires_at IS NOT NULL
              AND expires_at <= %s
            RETURNING id
            """,
            (now,),
        ).fetchall()
        if expired:
            connection.execute(
                """
                INSERT INTO rental_events (rental_id, event_type, created_at)
                SELECT id, 'expired', %s FROM mailbox_rentals WHERE id = ANY(%s)
                """,
                (now, [row[0] for row in expired]),
            )

    def list_rentals(self, now):
        with db.transaction(self._connection()) as connection:
            self._expire(connection, now)
            rows = connection.execute(
                """
                SELECT r.id, r.account_id, a.email, r.starts_at, r.expires_at,
                       r.ended_at, r.revoked_at, r.last_access_at, r.access_count,
                       r.access_code_ciphertext IS NOT NULL, r.code_rotated_at
                FROM mailbox_rentals AS r
                JOIN accounts AS a ON a.id = r.account_id
                ORDER BY r.id DESC
                """
            ).fetchall()
        return [_summary(row) for row in rows]

    def create_rental(
        self, account_id, access_code_hash, access_code_ciphertext, now, expires_at
    ):
        with db.transaction(self._connection()) as connection:
            self._expire(connection, now)
            if account_id is None:
                account = connection.execute(
                    """
                    SELECT a.id
                    FROM accounts AS a
                    WHERE a.rentable = TRUE
                      AND a.token_valid = TRUE
                      AND NOT EXISTS (
                          SELECT 1 FROM mailbox_rentals AS r
                          WHERE r.account_id = a.id AND r.ended_at IS NULL
                      )
                    ORDER BY a.id
                    FOR UPDATE OF a SKIP LOCKED
                    LIMIT 1
                    """
                ).fetchone()
            else:
                account = connection.execute(
                    """
                    SELECT a.id
                    FROM accounts AS a
                    WHERE a.id = %s
                      AND a.rentable = TRUE
                      AND a.token_valid = TRUE
                      AND NOT EXISTS (
                          SELECT 1 FROM mailbox_rentals AS r
                          WHERE r.account_id = a.id AND r.ended_at IS NULL
                      )
                    FOR UPDATE OF a
                    """,
                    (account_id,),
                ).fetchone()
            if account is None:
                raise NoRentableAccount
            row = connection.execute(
                """
                INSERT INTO mailbox_rentals (
                    account_id, access_code_hash, access_code_ciphertext,
                    starts_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    account[0],
                    access_code_hash,
                    access_code_ciphertext,
                    now,
                    expires_at,
                ),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO rental_events (rental_id, event_type, created_at)
                VALUES (%s, 'created', %s)
                """,
                (row[0], now),
            )
            result = connection.execute(
                """
                SELECT r.id, r.account_id, a.email, r.starts_at, r.expires_at,
                       r.ended_at, r.revoked_at, r.last_access_at, r.access_count,
                       r.access_code_ciphertext IS NOT NULL, r.code_rotated_at
                FROM mailbox_rentals AS r
                JOIN accounts AS a ON a.id = r.account_id
                WHERE r.id = %s
                """,
                (row[0],),
            ).fetchone()
        return _summary(result)

    def get_code_ciphertext(self, rental_id, now):
        with db.transaction(self._connection()) as connection:
            self._expire(connection, now)
            row = connection.execute(
                """
                SELECT access_code_ciphertext
                FROM mailbox_rentals
                WHERE id = %s AND ended_at IS NULL
                """,
                (rental_id,),
            ).fetchone()
            if row is None:
                raise RentalInactive
            connection.execute(
                """
                INSERT INTO rental_events (rental_id, event_type, created_at)
                VALUES (%s, 'code_revealed', %s)
                """,
                (rental_id, now),
            )
        if row[0] is None:
            raise RentalCodeUnavailable
        return row[0]

    def reset_code(self, rental_id, code_hash, ciphertext, now):
        with db.transaction(self._connection()) as connection:
            self._expire(connection, now)
            row = connection.execute(
                """
                UPDATE mailbox_rentals
                SET access_code_hash = %s,
                    access_code_ciphertext = %s,
                    code_rotated_at = %s
                WHERE id = %s AND ended_at IS NULL
                RETURNING id
                """,
                (code_hash, ciphertext, now, rental_id),
            ).fetchone()
            if row is None:
                raise RentalInactive
            connection.execute(
                "DELETE FROM pickup_sessions WHERE rental_id = %s", (rental_id,)
            )
            connection.execute(
                """
                INSERT INTO rental_events (rental_id, event_type, created_at)
                VALUES (%s, 'code_reset', %s)
                """,
                (rental_id, now),
            )

    def revoke(self, rental_id, now):
        with db.transaction(self._connection()) as connection:
            row = connection.execute(
                """
                UPDATE mailbox_rentals
                SET revoked_at = %s, ended_at = %s
                WHERE id = %s AND ended_at IS NULL
                RETURNING id
                """,
                (now, now, rental_id),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "DELETE FROM pickup_sessions WHERE rental_id = %s", (rental_id,)
            )
            connection.execute(
                """
                INSERT INTO rental_events (rental_id, event_type, created_at)
                VALUES (%s, 'revoked', %s)
                """,
                (rental_id, now),
            )
        return True


class RentalService:
    def __init__(
        self,
        repository,
        cipher,
        clock=lambda: datetime.now(UTC),
        code_factory=None,
    ):
        self._repository = repository
        self._cipher = cipher
        self._clock = clock
        self._code_factory = code_factory or (lambda: secrets.token_urlsafe(18))

    def list_rentals(self):
        return self._repository.list_rentals(_as_utc(self._clock()))

    def create(self, payload, minimum, maximum):
        if not isinstance(payload, dict) or not set(payload) <= {
            "accountId",
            "expiryMode",
            "expiresAt",
        } or "expiryMode" not in payload:
            raise InvalidRentalPayload
        mode = payload["expiryMode"]
        account_id = payload.get("accountId")
        if account_id is not None and (type(account_id) is not int or account_id <= 0):
            raise InvalidRentalPayload
        now = _as_utc(self._clock())
        if mode == "never":
            if "expiresAt" in payload:
                raise InvalidRentalPayload
            expires_at = None
        elif mode == "at":
            if set(payload) < {"expiryMode", "expiresAt"}:
                raise InvalidRentalPayload
            expires_at = _parse_expiry(payload["expiresAt"])
            seconds = (expires_at - now).total_seconds()
            if not minimum * 60 <= seconds <= maximum * 60:
                raise InvalidRentalPayload
        else:
            raise InvalidRentalPayload
        code = self._code_factory()
        rental = self._repository.create_rental(
            account_id,
            hash_access_code(code),
            self._cipher.encrypt(code),
            now,
            expires_at,
        )
        return rental, code

    def reveal_code(self, rental_id):
        ciphertext = self._repository.get_code_ciphertext(
            rental_id, _as_utc(self._clock())
        )
        return self._cipher.decrypt(ciphertext)

    def reset_code(self, rental_id):
        code = self._code_factory()
        self._repository.reset_code(
            rental_id,
            hash_access_code(code),
            self._cipher.encrypt(code),
            _as_utc(self._clock()),
        )
        return code

    def revoke(self, rental_id):
        if not self._repository.revoke(rental_id, _as_utc(self._clock())):
            raise RentalNotFound


blueprint = Blueprint("rentals", __name__)


def _service():
    return current_app.extensions["rental_service"]


def _serialize(rental, now=None):
    def stamp(value):
        return None if value is None else _as_utc(value).isoformat().replace("+00:00", "Z")

    now = _as_utc(now or datetime.now(UTC))
    if rental.revoked_at is not None:
        status = "revoked"
    elif rental.ended_at is not None or (
        rental.expires_at is not None and rental.expires_at <= now
    ):
        status = "expired"
    else:
        status = "active"
    return {
        "id": rental.id,
        "accountId": rental.account_id,
        "email": rental.email,
        "startsAt": stamp(rental.starts_at),
        "expiresAt": stamp(rental.expires_at),
        "expiryMode": "never" if rental.expires_at is None else "at",
        "endedAt": stamp(rental.ended_at),
        "revokedAt": stamp(rental.revoked_at),
        "lastAccessAt": stamp(rental.last_access_at),
        "accessCount": rental.access_count,
        "codeAvailable": rental.code_available,
        "codeRotatedAt": stamp(rental.code_rotated_at),
        "status": status,
    }


@blueprint.get("/api/rentals")
def list_rentals():
    now = _as_utc(current_app.config["RENTAL_CLOCK"]())
    return jsonify([_serialize(item, now) for item in _service().list_rentals()])


@blueprint.post("/api/rentals")
def create_rental():
    try:
        rental, code = _service().create(
            request.get_json(silent=True),
            current_app.config["RENTAL_MIN_MINUTES"],
            current_app.config["RENTAL_MAX_MINUTES"],
        )
    except InvalidRentalPayload:
        return jsonify(error="invalid request"), 400
    except NoRentableAccount:
        return jsonify(error="no rentable account"), 409
    result = _serialize(rental, current_app.config["RENTAL_CLOCK"]())
    result["accessCode"] = code
    return jsonify(result), 201


@blueprint.post("/api/rentals/<int:rental_id>/reveal-code")
def reveal_rental_code(rental_id):
    try:
        code = _service().reveal_code(rental_id)
    except RentalCodeUnavailable:
        return jsonify(error="code unavailable; reset required"), 409
    except RentalInactive:
        return jsonify(error="rental inactive"), 409
    response = jsonify(accessCode=code)
    response.headers["Cache-Control"] = "no-store"
    return response


@blueprint.post("/api/rentals/<int:rental_id>/reset-code")
def reset_rental_code(rental_id):
    try:
        code = _service().reset_code(rental_id)
    except RentalInactive:
        return jsonify(error="rental inactive"), 409
    response = jsonify(accessCode=code)
    response.headers["Cache-Control"] = "no-store"
    return response


@blueprint.post("/api/rentals/<int:rental_id>/revoke")
def revoke_rental(rental_id):
    try:
        _service().revoke(rental_id)
    except RentalNotFound:
        return jsonify(error="not found"), 404
    return jsonify(revoked=True)


def init_app(app: Flask):
    repository = app.config.get("RENTAL_REPOSITORY") or PostgresRentalRepository()
    clock = app.config.get("RENTAL_CLOCK", lambda: datetime.now(UTC))
    cipher = app.config.get("CREDENTIAL_CIPHER")
    if cipher is None:
        cipher = CredentialCipher(app.config["CREDENTIALS_ENCRYPTION_KEY"])
    app.extensions["rental_service"] = RentalService(repository, cipher, clock)
    app.config.setdefault("RENTAL_CLOCK", clock)
    app.register_blueprint(blueprint)
