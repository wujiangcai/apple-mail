from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
import json
import sys
from typing import Protocol

from flask import (
    Blueprint,
    Flask,
    Response,
    current_app,
    jsonify,
    request,
    stream_with_context,
)

from mail_manager import db
from mail_manager.crypto import CredentialCipher
from mail_manager.models import (
    AccountMetadata,
    EncryptedAccount,
    EncryptedCredentials,
    RevealedCredentials,
)


BATCH_DELETE_LIMIT = 1000
EXPORT_BATCH_SIZE = 100
_IMPORT_FIELDS = frozenset({"email", "password", "clientId", "refreshToken"})
_UPDATE_FIELDS = frozenset({"bound", "token_valid", "rentable"})


class InvalidAccountPayload(ValueError):
    def __init__(self, *, too_large: bool = False):
        super().__init__("invalid account payload")
        self.too_large = too_large


class AccountNotFound(LookupError):
    pass


class ExportLimitError(ValueError):
    pass


class AccountRepository(Protocol):
    def list_accounts(self) -> list[AccountMetadata]: ...

    def upsert_accounts(self, accounts: Sequence[EncryptedAccount]) -> int: ...

    def get_credentials(self, account_id: int) -> EncryptedCredentials | None: ...

    def iter_encrypted_credentials(
        self, limit: int, batch_size: int = EXPORT_BATCH_SIZE
    ) -> Iterator[EncryptedCredentials]: ...

    def update_flags(
        self, account_id: int, changes: Mapping[str, bool], now: datetime
    ) -> AccountMetadata | None: ...

    def delete_account(self, account_id: int) -> bool: ...

    def delete_accounts(self, account_ids: Sequence[int]) -> bool: ...


class _DecryptingCredentialIterator:
    def __init__(
        self,
        encrypted: Iterator[EncryptedCredentials],
        decrypt: Callable[[EncryptedCredentials], RevealedCredentials],
    ):
        self._encrypted = encrypted
        self._decrypt = decrypt

    def __iter__(self):
        return self

    def __next__(self) -> RevealedCredentials:
        return self._decrypt(next(self._encrypted))

    def close(self) -> None:
        close = getattr(self._encrypted, "close", None)
        if close is not None:
            close()


class PostgresAccountRepository:
    def __init__(
        self,
        connection: object | Callable[[], object] | None = None,
        export_connection_source: object | Callable[[], object] | None = None,
    ):
        self._connection_source = connection
        self._export_connection_source = export_connection_source

    def _connection(self):
        source = self._connection_source
        if source is None:
            return db.get_db()
        return source() if callable(source) else source

    def _export_connection(self):
        source = self._export_connection_source
        if source is None:
            return db.connect_database(current_app.config["DATABASE_URL"])
        return source() if callable(source) else source

    def list_accounts(self) -> list[AccountMetadata]:
        with db.transaction(self._connection()) as connection:
            rows = connection.execute(
                """
                SELECT a.id, a.email, a.last_refresh_time, a.bound, a.bound_at,
                       a.token_valid, a.rentable,
                       EXISTS (
                           SELECT 1 FROM mailbox_rentals AS r
                           WHERE r.account_id = a.id AND r.ended_at IS NULL
                             AND (r.expires_at IS NULL
                                  OR r.expires_at > CURRENT_TIMESTAMP)
                       ) AS rented
                FROM accounts AS a
                ORDER BY a.id
                """
            ).fetchall()
        return [_metadata_from_row(row) for row in rows]

    def upsert_accounts(self, accounts: Sequence[EncryptedAccount]) -> int:
        pending = list(accounts)
        with db.transaction(self._connection()) as connection:
            for account in pending:
                connection.execute(
                    """
                    INSERT INTO accounts (
                        email,
                        email_normalized,
                        password_ciphertext,
                        client_id,
                        refresh_token_ciphertext
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (email_normalized) DO UPDATE SET
                        email = EXCLUDED.email,
                        password_ciphertext = EXCLUDED.password_ciphertext,
                        client_id = EXCLUDED.client_id,
                        refresh_token_ciphertext =
                            EXCLUDED.refresh_token_ciphertext,
                        token_valid = TRUE,
                        refresh_lease_token = NULL,
                        refresh_lease_expires_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        account.email,
                        account.email_normalized,
                        account.password_ciphertext,
                        account.client_id,
                        account.refresh_token_ciphertext,
                    ),
                )
        return len(pending)

    def get_credentials(self, account_id: int) -> EncryptedCredentials | None:
        with db.transaction(self._connection()) as connection:
            row = connection.execute(
                """
                SELECT email, password_ciphertext, client_id,
                       refresh_token_ciphertext
                FROM accounts
                WHERE id = %s
                """,
                (account_id,),
            ).fetchone()
        return None if row is None else EncryptedCredentials(*row)

    def iter_encrypted_credentials(
        self, limit: int, batch_size: int = EXPORT_BATCH_SIZE
    ) -> Iterator[EncryptedCredentials]:
        stack = ExitStack()
        try:
            connection = self._export_connection()
            stack.callback(connection.close)
            connection = stack.enter_context(db.transaction(connection))
            cursor = stack.enter_context(connection.cursor(name="account_export"))
            cursor.execute(
                """
                SELECT email, password_ciphertext, client_id,
                       refresh_token_ciphertext,
                       COUNT(*) OVER() AS total_count
                FROM accounts
                ORDER BY id
                LIMIT %s
                """,
                (limit + 1,),
            )
            first = cursor.fetchone()
            if first is not None and first[4] > limit:
                raise ExportLimitError
        except BaseException:
            stack.__exit__(*sys.exc_info())
            raise

        if first is None:
            stack.close()
            return iter(())

        def generate() -> Iterator[EncryptedCredentials]:
            error_info = (None, None, None)
            try:
                # Prime cleanup before returning without exposing a data item.
                yield
                yield EncryptedCredentials(*first[:4])
                while rows := cursor.fetchmany(batch_size):
                    for row in rows:
                        yield EncryptedCredentials(*row[:4])
            except BaseException:
                error_info = sys.exc_info()
                raise
            finally:
                stack.__exit__(*error_info)

        records = generate()
        next(records)
        return records

    def update_flags(
        self, account_id: int, changes: Mapping[str, bool], now: datetime
    ) -> AccountMetadata | None:
        unknown = set(changes) - _UPDATE_FIELDS
        if unknown:
            raise ValueError("unsupported account field")
        if not changes:
            raise ValueError("at least one account field is required")

        assignments = []
        parameters: list[object] = []
        if "bound" in changes:
            bound = changes["bound"]
            assignments.extend(("bound = %s", "bound_at = %s"))
            parameters.extend((bound, now if bound else None))
        if "token_valid" in changes:
            assignments.append("token_valid = %s")
            parameters.append(changes["token_valid"])
        if "rentable" in changes:
            assignments.append("rentable = %s")
            parameters.append(changes["rentable"])
        assignments.append("updated_at = CURRENT_TIMESTAMP")
        parameters.append(account_id)

        statement = f"""
            UPDATE accounts
            SET {", ".join(assignments)}
            WHERE id = %s
            RETURNING id, email, last_refresh_time, bound, bound_at, token_valid,
                      rentable, FALSE AS rented
        """
        with db.transaction(self._connection()) as connection:
            row = connection.execute(statement, tuple(parameters)).fetchone()
        return None if row is None else _metadata_from_row(row)

    def delete_account(self, account_id: int) -> bool:
        with db.transaction(self._connection()) as connection:
            row = connection.execute(
                "DELETE FROM accounts WHERE id = %s RETURNING id",
                (account_id,),
            ).fetchone()
        return row is not None

    def delete_accounts(self, account_ids: Sequence[int]) -> bool:
        pending = list(account_ids)
        with db.transaction(self._connection()) as connection:
            rows = connection.execute(
                "DELETE FROM accounts WHERE id = ANY(%s) RETURNING id",
                (pending,),
            ).fetchall()
            if {row[0] for row in rows} != set(pending):
                raise AccountNotFound
        return True


class AccountsService:
    def __init__(
        self,
        repository: AccountRepository,
        cipher: CredentialCipher,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._repository = repository
        self._cipher = cipher
        self._clock = clock

    def list_accounts(self) -> list[AccountMetadata]:
        return self._repository.list_accounts()

    def import_accounts(self, payload: object, limit: int) -> int:
        accounts = _validate_import_payload(payload, limit)
        encrypted = [
            EncryptedAccount(
                email=account["email"].strip(),
                email_normalized=account["email"].strip().casefold(),
                password_ciphertext=self._cipher.encrypt(account["password"]),
                client_id=account["clientId"],
                refresh_token_ciphertext=self._cipher.encrypt(account["refreshToken"]),
            )
            for account in accounts
        ]
        return self._repository.upsert_accounts(encrypted)

    def reveal(self, account_id: int) -> RevealedCredentials:
        credentials = self._repository.get_credentials(account_id)
        if credentials is None:
            raise AccountNotFound
        return self._decrypt(credentials)

    def export(
        self, limit: int, batch_size: int = EXPORT_BATCH_SIZE
    ) -> Iterator[RevealedCredentials]:
        encrypted = self._repository.iter_encrypted_credentials(limit, batch_size)
        return _DecryptingCredentialIterator(encrypted, self._decrypt)

    def update_account(self, account_id: int, payload: object) -> AccountMetadata:
        if (
            not isinstance(payload, dict)
            or not payload
            or not set(payload) <= {"bound", "rentable"}
            or any(type(value) is not bool for value in payload.values())
        ):
            raise InvalidAccountPayload
        updated = self._repository.update_flags(
            account_id, payload, _as_utc(self._clock())
        )
        if updated is None:
            raise AccountNotFound
        return updated

    def update_bound(self, account_id: int, payload: object) -> AccountMetadata:
        return self.update_account(account_id, payload)

    def delete(self, account_id: int) -> None:
        if not self._repository.delete_account(account_id):
            raise AccountNotFound

    def delete_batch(self, payload: object) -> int:
        ids = _validate_delete_payload(payload)
        try:
            deleted = self._repository.delete_accounts(ids)
        except AccountNotFound:
            raise
        if not deleted:
            raise AccountNotFound
        return len(ids)

    def _decrypt(self, credentials: EncryptedCredentials) -> RevealedCredentials:
        return RevealedCredentials(
            email=credentials.email,
            password=self._cipher.decrypt(credentials.password_ciphertext),
            client_id=credentials.client_id,
            refresh_token=self._cipher.decrypt(credentials.refresh_token_ciphertext),
        )


blueprint = Blueprint("accounts", __name__)


def _service() -> AccountsService:
    return current_app.extensions["accounts_service"]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _metadata_from_row(row: Sequence[object]) -> AccountMetadata:
    if len(row) == 6:
        account_id, email, refreshed, bound, bound_at, token_valid = row
        rentable, rented = True, False
    else:
        account_id, email, refreshed, bound, bound_at, token_valid, rentable, rented = row
    return AccountMetadata(
        id=account_id,
        email=email,
        last_refresh_time=None if refreshed is None else _as_utc(refreshed),
        bound=bound,
        bound_at=None if bound_at is None else _as_utc(bound_at),
        token_valid=token_valid,
        rentable=rentable,
        rented=rented,
    )


def _serialize_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _serialize_metadata(account: AccountMetadata) -> dict[str, object]:
    result = {
        "id": account.id,
        "email": account.email,
        "lastRefreshTime": _serialize_timestamp(account.last_refresh_time),
        "bound": account.bound,
        "boundAt": _serialize_timestamp(account.bound_at),
        "tokenValid": account.token_valid,
        "rentable": account.rentable,
        "rented": account.rented,
    }
    return result


def _validate_import_payload(payload: object, limit: int) -> list[dict[str, str]]:
    if not isinstance(payload, dict) or set(payload) != {"accounts"}:
        raise InvalidAccountPayload
    accounts = payload["accounts"]
    if not isinstance(accounts, list) or not accounts:
        raise InvalidAccountPayload
    if len(accounts) > limit:
        raise InvalidAccountPayload(too_large=True)

    limits = {"email": 320, "password": 1024, "clientId": 256, "refreshToken": 8192}
    for account in accounts:
        if not isinstance(account, dict) or set(account) != _IMPORT_FIELDS:
            raise InvalidAccountPayload
        for field, maximum in limits.items():
            value = account[field]
            if not isinstance(value, str) or len(value) > maximum:
                raise InvalidAccountPayload
        if not account["email"].strip():
            raise InvalidAccountPayload
        if not account["clientId"].strip() or not account["refreshToken"].strip():
            raise InvalidAccountPayload
    return accounts


def _validate_delete_payload(payload: object) -> list[int]:
    if not isinstance(payload, dict) or set(payload) != {"ids"}:
        raise InvalidAccountPayload
    ids = payload["ids"]
    if not isinstance(ids, list) or not ids:
        raise InvalidAccountPayload
    if len(ids) > BATCH_DELETE_LIMIT:
        raise InvalidAccountPayload(too_large=True)
    if any(type(account_id) is not int or account_id <= 0 for account_id in ids):
        raise InvalidAccountPayload
    if len(ids) != len(set(ids)):
        raise InvalidAccountPayload
    return ids


def _invalid_payload_response(error: InvalidAccountPayload):
    status = 413 if error.too_large else 400
    return jsonify(
        error="request too large" if error.too_large else "invalid request"
    ), status


def _not_found_response():
    return jsonify(error="not found"), 404


@blueprint.get("/api/accounts")
def list_accounts():
    return jsonify(
        [_serialize_metadata(account) for account in _service().list_accounts()]
    )


@blueprint.post("/api/accounts/import")
def import_accounts():
    try:
        imported = _service().import_accounts(
            request.get_json(silent=True), current_app.config["IMPORT_LIMIT"]
        )
    except InvalidAccountPayload as error:
        return _invalid_payload_response(error)
    return jsonify(imported=imported)


@blueprint.post("/api/accounts/<int:account_id>/reveal")
def reveal_account(account_id: int):
    try:
        credentials = _service().reveal(account_id)
    except AccountNotFound:
        return _not_found_response()
    response = jsonify(
        email=credentials.email,
        password=credentials.password,
        clientId=credentials.client_id,
        refreshToken=credentials.refresh_token,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@blueprint.post("/api/accounts/export")
def export_accounts():
    try:
        credentials = _service().export(current_app.config["EXPORT_LIMIT"])
    except ExportLimitError:
        return jsonify(error="request too large"), 413

    def generate_lines():
        try:
            # Start the WSGI response without forcing the first credential read.
            yield ""
            for record in credentials:
                yield (
                    json.dumps(
                        {
                            "email": record.email,
                            "password": record.password,
                            "clientId": record.client_id,
                            "refreshToken": record.refresh_token,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        finally:
            credentials.close()

    response = Response(
        stream_with_context(generate_lines()),
        content_type="application/x-ndjson; charset=utf-8",
    )
    response.call_on_close(credentials.close)
    response.headers["Content-Disposition"] = "attachment; filename=accounts.jsonl"
    response.headers["Cache-Control"] = "no-store"
    return response


@blueprint.patch("/api/accounts/<int:account_id>")
def update_account(account_id: int):
    try:
        updated = _service().update_account(account_id, request.get_json(silent=True))
    except InvalidAccountPayload as error:
        return _invalid_payload_response(error)
    except AccountNotFound:
        return _not_found_response()
    return jsonify(_serialize_metadata(updated))


@blueprint.delete("/api/accounts/<int:account_id>")
def delete_account(account_id: int):
    try:
        _service().delete(account_id)
    except AccountNotFound:
        return _not_found_response()
    return jsonify(deleted=True)


@blueprint.post("/api/accounts/delete-batch")
def delete_accounts():
    try:
        deleted = _service().delete_batch(request.get_json(silent=True))
    except InvalidAccountPayload as error:
        return _invalid_payload_response(error)
    except AccountNotFound:
        return _not_found_response()
    return jsonify(deleted=deleted)


def init_app(app: Flask) -> None:
    repository = app.config.get("ACCOUNT_REPOSITORY")
    if repository is None:
        repository = PostgresAccountRepository()
    cipher = app.config.get("CREDENTIAL_CIPHER")
    if cipher is None:
        cipher = CredentialCipher(app.config["CREDENTIALS_ENCRYPTION_KEY"])
    clock = app.config.get("ACCOUNTS_CLOCK", lambda: datetime.now(UTC))
    app.extensions["accounts_service"] = AccountsService(repository, cipher, clock)
    app.register_blueprint(blueprint)
