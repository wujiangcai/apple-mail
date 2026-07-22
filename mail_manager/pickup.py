from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import hmac
import math
import secrets

from flask import Blueprint, Flask, current_app, g, jsonify, request

from mail_manager import db
from mail_manager.auth import constant_time_compare, derive_csrf_token, hash_token
from mail_manager.models import PickupContext, PickupSession
from mail_manager.rentals import hash_access_code


PICKUP_COOKIE = "pickup_session"
PICKUP_LOCK_DURATION = timedelta(minutes=15)
PICKUP_MAX_FAILURES = 8


class PickupDenied(Exception):
    pass


class PostgresPickupRepository:
    def __init__(self, connection: object | Callable[[], object] = db.get_db):
        self._connection_source = connection

    def _connection(self):
        source = self._connection_source
        return source() if callable(source) else source

    def exchange_code(
        self, client_key, code_hash, session_hash, csrf_hash, now, session_limit
    ):
        with db.transaction(self._connection()) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"pickup:{client_key}",),
            )
            connection.execute(
                """
                DELETE FROM pickup_failures
                WHERE updated_at < %s
                  AND (locked_until IS NULL OR locked_until <= %s)
                """,
                (now - PICKUP_LOCK_DURATION, now),
            )
            failure = connection.execute(
                """
                SELECT failure_count, locked_until
                FROM pickup_failures WHERE client_key = %s
                """,
                (client_key,),
            ).fetchone()
            if failure is not None and failure[1] is not None and failure[1] > now:
                raise PickupDenied

            row = connection.execute(
                """
                SELECT r.id, r.account_id, a.email, r.expires_at
                FROM mailbox_rentals AS r
                JOIN accounts AS a ON a.id = r.account_id
                WHERE r.access_code_hash = %s
                  AND r.ended_at IS NULL
                  AND r.revoked_at IS NULL
                  AND r.starts_at <= %s
                  AND (r.expires_at IS NULL OR r.expires_at > %s)
                FOR UPDATE OF r
                """,
                (code_hash, now, now),
            ).fetchone()
            if row is None:
                count = 1 if failure is None else failure[0] + 1
                locked_until = (
                    now + PICKUP_LOCK_DURATION if count >= PICKUP_MAX_FAILURES else None
                )
                connection.execute(
                    """
                    INSERT INTO pickup_failures (
                        client_key, failure_count, locked_until, updated_at
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (client_key) DO UPDATE SET
                        failure_count = EXCLUDED.failure_count,
                        locked_until = EXCLUDED.locked_until,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (client_key, count, locked_until, now),
                )
                raise PickupDenied

            connection.execute(
                "DELETE FROM pickup_failures WHERE client_key = %s", (client_key,)
            )
            session_expires_at = now + session_limit
            if row[3] is not None:
                session_expires_at = min(row[3], session_expires_at)
            connection.execute(
                """
                INSERT INTO pickup_sessions (
                    session_hash, rental_id, csrf_hash, expires_at, created_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (session_hash, row[0], csrf_hash, session_expires_at, now),
            )
            connection.execute(
                """
                UPDATE mailbox_rentals
                SET last_access_at = %s, access_count = access_count + 1
                WHERE id = %s
                """,
                (now, row[0]),
            )
            connection.execute(
                """
                INSERT INTO rental_events (
                    rental_id, event_type, client_key, created_at
                ) VALUES (%s, 'exchanged', %s, %s)
                """,
                (row[0], client_key, now),
            )
        return PickupContext(
            row[0], row[1], row[2], session_expires_at, row[3]
        )

    def find_session(self, session_hash, now):
        with db.transaction(self._connection()) as connection:
            row = connection.execute(
                """
                SELECT s.csrf_hash, s.expires_at, r.id, r.account_id, a.email,
                       r.expires_at
                FROM pickup_sessions AS s
                JOIN mailbox_rentals AS r ON r.id = s.rental_id
                JOIN accounts AS a ON a.id = r.account_id
                WHERE s.session_hash = %s
                  AND s.expires_at > %s
                  AND r.ended_at IS NULL
                  AND r.revoked_at IS NULL
                  AND (r.expires_at IS NULL OR r.expires_at > %s)
                """,
                (session_hash, now, now),
            ).fetchone()
            if row is None:
                connection.execute(
                    "DELETE FROM pickup_sessions WHERE session_hash = %s",
                    (session_hash,),
                )
                return None
        session_expiry = row[1]
        rental_expiry = row[5]
        return PickupSession(
            session_hash=session_hash,
            csrf_hash=row[0],
            context=PickupContext(
                row[2],
                row[3],
                row[4],
                session_expiry,
                rental_expiry,
            ),
        )

    def delete_session(self, session_hash):
        with db.transaction(self._connection()) as connection:
            connection.execute(
                "DELETE FROM pickup_sessions WHERE session_hash = %s",
                (session_hash,),
            )

    def record_event(self, rental_id, event_type, client_key, now, detail=None):
        with db.transaction(self._connection()) as connection:
            connection.execute(
                """
                INSERT INTO rental_events (
                    rental_id, event_type, client_key, detail, created_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (rental_id, event_type, client_key, detail, now),
            )


blueprint = Blueprint("pickup", __name__)


def _repository():
    return current_app.extensions["pickup_repository"]


def _now():
    return current_app.config["PICKUP_CLOCK"]()


def _client_key():
    return request.remote_addr or "unknown-client"


def _load_pickup():
    if "pickup_session" in g:
        return g.pickup_session
    raw = request.cookies.get(PICKUP_COOKIE)
    if not raw:
        g.pickup_session = None
        return None
    session = _repository().find_session(hash_token(raw), _now())
    if session is None:
        g.pickup_session = None
        return None
    csrf = derive_csrf_token(current_app.config["SESSION_SECRET"], raw)
    if not hmac.compare_digest(hash_token(csrf), session.csrf_hash):
        _repository().delete_session(session.session_hash)
        g.pickup_session = None
        return None
    g.pickup_session = session
    g.pickup_csrf = csrf
    return session


def current_pickup_context():
    session = _load_pickup()
    return None if session is None else session.context


def protect_pickup_api():
    if not request.path.startswith("/api/pickup/"):
        return None
    if request.path in {"/api/pickup/login", "/api/pickup/check-auth"}:
        return None
    if _load_pickup() is None:
        return jsonify(error="pickup authorization required"), 401
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if not constant_time_compare(
            request.headers.get("Origin", ""), current_app.config["PUBLIC_ORIGIN"]
        ) or not constant_time_compare(
            request.headers.get("X-CSRF-Token", ""), g.pickup_csrf
        ):
            return jsonify(error="forbidden"), 403
    return None


@blueprint.post("/api/pickup/login")
def login():
    payload = request.get_json(silent=True)
    code = payload.get("code", "") if isinstance(payload, dict) else ""
    if not isinstance(code, str) or not code or len(code) > 256:
        code = "invalid"
    raw_session = secrets.token_urlsafe(32)
    csrf = derive_csrf_token(current_app.config["SESSION_SECRET"], raw_session)
    try:
        context = _repository().exchange_code(
            _client_key(),
            hash_access_code(code),
            hash_token(raw_session),
            hash_token(csrf),
            _now(),
            timedelta(minutes=current_app.config["PICKUP_SESSION_MINUTES"]),
        )
    except PickupDenied:
        return jsonify(error="invalid or expired pickup code"), 401
    max_age = max(1, math.floor((context.expires_at - _now()).total_seconds()))
    response = jsonify(
        authenticated=True,
        csrfToken=csrf,
        email=context.email,
        expiresAt=context.expires_at.isoformat().replace("+00:00", "Z"),
        rentalExpiresAt=(
            None
            if context.rental_expires_at is None
            else context.rental_expires_at.isoformat().replace("+00:00", "Z")
        ),
        permanent=context.rental_expires_at is None,
    )
    response.set_cookie(
        PICKUP_COOKIE,
        raw_session,
        max_age=max_age,
        secure=bool(current_app.config["COOKIE_SECURE"]),
        httponly=True,
        samesite="Strict",
        path="/",
    )
    return response


@blueprint.get("/api/pickup/check-auth")
def check_auth():
    session = _load_pickup()
    if session is None:
        return jsonify(authenticated=False)
    context = session.context
    return jsonify(
        authenticated=True,
        csrfToken=g.pickup_csrf,
        email=context.email,
        expiresAt=context.expires_at.isoformat().replace("+00:00", "Z"),
        rentalExpiresAt=(
            None
            if context.rental_expires_at is None
            else context.rental_expires_at.isoformat().replace("+00:00", "Z")
        ),
        permanent=context.rental_expires_at is None,
    )


@blueprint.post("/api/pickup/logout")
def logout():
    _repository().delete_session(g.pickup_session.session_hash)
    response = jsonify(authenticated=False)
    response.delete_cookie(
        PICKUP_COOKIE,
        secure=bool(current_app.config["COOKIE_SECURE"]),
        httponly=True,
        samesite="Strict",
        path="/",
    )
    return response


def record_pickup_event(event_type, detail=None):
    context = current_pickup_context()
    if context is not None:
        _repository().record_event(
            context.rental_id, event_type, _client_key(), _now(), detail
        )


def init_app(app: Flask):
    repository = app.config.get("PICKUP_REPOSITORY") or PostgresPickupRepository()
    app.extensions["pickup_repository"] = repository
    app.config.setdefault("PICKUP_CLOCK", lambda: datetime.now(UTC))
    app.register_blueprint(blueprint)
    app.before_request(protect_pickup_api)
