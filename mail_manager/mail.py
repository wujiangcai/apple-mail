from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
import math
import re
import secrets
import time
from typing import Protocol

import click
import psycopg
from flask import (
    Blueprint,
    Flask,
    copy_current_request_context,
    current_app,
    has_request_context,
    jsonify,
    request,
)

from mail_manager import db
from mail_manager.crypto import CredentialCipher
from mail_manager.graph import (
    GraphClient,
    InvalidGrant,
    UpstreamProtocolError,
    UpstreamRateLimited,
    UpstreamUnavailable,
)
from mail_manager.models import (
    AccountSummary,
    MailManagerError,
    MailView,
    RefreshClaim,
)


MAX_ACCOUNT_ID = (1 << 63) - 1
MAX_BATCH_SIZE = 20
MAX_MAIL_RESULTS = 100
MAX_KEYWORD_LENGTH = 128
RANDOM_CANDIDATE_LIMIT = 5
BOUND_MESSAGE_LIMIT = 100
RANDOM_BOUND_MESSAGE_LIMIT = 20
RANDOM_DEADLINE_SECONDS = 30
REFRESH_LEASE_TTL = timedelta(seconds=30)
FINALIZE_ATTEMPTS = 3
FINALIZE_BACKOFF_SECONDS = 0.01
_FOLDERS = frozenset({"inbox", "junkemail"})
_CODE_CONTEXTS = (
    "verification code",
    "security code",
    "one-time code",
    "one time code",
    "verify code",
    "please use",
    "expires",
    "验证码",
    "otp",
    "code",
)
_CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]{4,8}(?![A-Za-z0-9]))"
    r"(?=[A-Za-z0-9]*[0-9])([A-Za-z0-9]{4,8})(?![A-Za-z0-9])"
)
_CODE_STOPWORDS = frozenset(
    {"code", "security", "verify", "your", "login", "openai"}
)


class MailError(MailManagerError):
    """Base class for expected mail access failures."""


class InvalidMailPayload(MailError, ValueError):
    pass


class MailAccountNotFound(MailError, LookupError):
    pass


class TokenInvalid(MailError):
    pass


class RefreshBusy(MailError):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, min(30, retry_after))
        super().__init__("account refresh is already in progress")


class RefreshPersistenceError(MailError):
    pass


class VerificationCodeNotFound(MailError, LookupError):
    pass


class NoUnboundAccount(MailError, LookupError):
    pass


class MailRepository(Protocol):
    def claim_refresh(
        self, account_id: int, lease: str, now: datetime, ttl: timedelta
    ) -> RefreshClaim: ...

    def finalize_refresh(
        self, account_id: int, lease: str, ciphertext: str, now: datetime
    ) -> bool: ...

    def invalidate_refresh(
        self, account_id: int, lease: str, now: datetime
    ) -> bool: ...

    def release_refresh(self, account_id: int, lease: str) -> bool: ...

    def set_bound(self, account_id: int, bound: bool, now: datetime) -> bool: ...

    def random_unbound_candidates(self, limit: int) -> list[AccountSummary]: ...


class PostgresMailRepository:
    def __init__(self, connection: object | Callable[[], object] | None = None):
        self._connection_source = connection

    def _connection(self):
        source = self._connection_source
        if source is None:
            return db.connect_database(current_app.config["DATABASE_URL"]), True
        if callable(source):
            return source(), True
        return source, False

    @contextmanager
    def _transaction(self):
        connection, owned = self._connection()
        primary_error = None
        try:
            with db.transaction(connection):
                yield connection
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if owned:
                db._run_cleanup(connection.close, "database close", primary_error)

    def claim_refresh(
        self,
        account_id: int,
        lease: str,
        now: datetime,
        ttl: timedelta = REFRESH_LEASE_TTL,
    ) -> RefreshClaim:
        _validate_account_id(account_id)
        if not isinstance(lease, str) or not lease:
            raise ValueError("lease must be a non-empty string")
        now = _as_utc(now)
        expires_at = now + ttl
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    UPDATE accounts
                    SET refresh_lease_token = %s,
                        refresh_lease_expires_at = %s
                    WHERE id = %s
                      AND token_valid = TRUE
                      AND (
                          refresh_lease_token IS NULL
                          OR refresh_lease_expires_at <= %s
                      )
                    RETURNING client_id, refresh_token_ciphertext
                    """,
                    (lease, expires_at, account_id, now),
                ).fetchone()
                if row is not None:
                    return RefreshClaim(
                        client_id=row[0], refresh_token_ciphertext=row[1]
                    )
                state = connection.execute(
                    """
                    SELECT token_valid, refresh_lease_expires_at
                    FROM accounts
                    WHERE id = %s
                    """,
                    (account_id,),
                ).fetchone()
        except psycopg.Error:
            raise RefreshPersistenceError("refresh claim failed") from None

        if state is None:
            raise MailAccountNotFound
        token_valid, lease_expires_at = state
        if not token_valid:
            raise TokenInvalid
        retry_after = 1
        if isinstance(lease_expires_at, datetime):
            seconds = (_as_utc(lease_expires_at) - now).total_seconds()
            retry_after = max(1, min(30, math.ceil(seconds)))
        raise RefreshBusy(retry_after)

    def finalize_refresh(
        self,
        account_id: int,
        lease: str,
        ciphertext: str,
        now: datetime,
    ) -> bool:
        _validate_account_id(account_id)
        now = _as_utc(now)
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    UPDATE accounts
                    SET refresh_token_ciphertext = %s,
                        token_valid = TRUE,
                        last_refresh_time = %s,
                        updated_at = %s,
                        refresh_lease_token = NULL,
                        refresh_lease_expires_at = NULL
                    WHERE id = %s AND refresh_lease_token = %s
                    RETURNING id
                    """,
                    (ciphertext, now, now, account_id, lease),
                ).fetchone()
        except psycopg.Error:
            raise RefreshPersistenceError("refresh finalization failed") from None
        return row is not None

    def invalidate_refresh(
        self, account_id: int, lease: str, now: datetime
    ) -> bool:
        _validate_account_id(account_id)
        now = _as_utc(now)
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    UPDATE accounts
                    SET token_valid = FALSE,
                        updated_at = %s,
                        refresh_lease_token = NULL,
                        refresh_lease_expires_at = NULL
                    WHERE id = %s AND refresh_lease_token = %s
                    RETURNING id
                    """,
                    (now, account_id, lease),
                ).fetchone()
        except psycopg.Error:
            raise RefreshPersistenceError("refresh invalidation failed") from None
        return row is not None

    def release_refresh(self, account_id: int, lease: str) -> bool:
        _validate_account_id(account_id)
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    UPDATE accounts
                    SET refresh_lease_token = NULL,
                        refresh_lease_expires_at = NULL
                    WHERE id = %s AND refresh_lease_token = %s
                    RETURNING id
                    """,
                    (account_id, lease),
                ).fetchone()
        except psycopg.Error:
            raise RefreshPersistenceError("refresh lease release failed") from None
        return row is not None

    def maintenance_account_ids(self, limit: int) -> list[int]:
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("limit must be from 1 to 10000")
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT id FROM accounts
                WHERE token_valid = TRUE
                ORDER BY last_refresh_time NULLS FIRST, id
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [row[0] for row in rows]

    def begin_maintenance_run(self, now):
        connection = db.connect_database(current_app.config["DATABASE_URL"])
        acquired = connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
            ("mail-manager-token-maintenance",),
        ).fetchone()[0]
        if not acquired:
            connection.close()
            return None
        row = connection.execute(
            """
            INSERT INTO token_maintenance_runs (started_at, status)
            VALUES (%s, 'running') RETURNING id
            """,
            (now,),
        ).fetchone()
        connection.commit()
        return connection, row[0]

    def finish_maintenance_run(self, handle, now, counts, status="completed"):
        connection, run_id = handle
        try:
            connection.execute(
                """
                UPDATE token_maintenance_runs
                SET finished_at = %s, status = %s, scanned_count = %s,
                    success_count = %s, invalid_count = %s,
                    busy_count = %s, failed_count = %s
                WHERE id = %s
                """,
                (
                    now,
                    status,
                    counts["scanned"],
                    counts["success"],
                    counts["invalid"],
                    counts["busy"],
                    counts["failed"],
                    run_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def set_bound(self, account_id: int, bound: bool, now: datetime) -> bool:
        _validate_account_id(account_id)
        now = _as_utc(now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                UPDATE accounts
                SET bound = %s,
                    bound_at = %s,
                    updated_at = %s
                WHERE id = %s
                RETURNING id
                """,
                (bound, now if bound else None, now, account_id),
            ).fetchone()
        return row is not None

    def random_unbound_candidates(self, limit: int) -> list[AccountSummary]:
        if type(limit) is not int or not 1 <= limit <= RANDOM_CANDIDATE_LIMIT:
            raise ValueError("limit must be from 1 to 5")
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, email
                FROM accounts
                WHERE bound = FALSE AND token_valid = TRUE
                ORDER BY id
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [AccountSummary(id=row[0], email=row[1]) for row in rows]


class TokenCoordinator:
    def __init__(
        self,
        repository,
        graph_client_factory,
        cipher,
        clock=lambda: datetime.now(UTC),
        *,
        sleep=time.sleep,
        lease_factory=lambda: secrets.token_urlsafe(24),
    ):
        self._repository = repository
        self._graph_client_factory = graph_client_factory
        self._cipher = cipher
        self._clock = clock
        self._sleep = sleep
        self._lease_factory = lease_factory

    def access_token(self, account_id: int) -> str:
        _validate_account_id(account_id)
        lease = self._lease_factory()
        try:
            claim = self._repository.claim_refresh(
                account_id, lease, _as_utc(self._clock()), REFRESH_LEASE_TTL
            )
        except RefreshPersistenceError:
            raise UpstreamUnavailable("refresh storage is unavailable") from None

        try:
            refresh_token = self._cipher.decrypt(claim.refresh_token_ciphertext)
            graph = self._graph_client_factory()
            access_token, new_refresh_token = graph.refresh(
                claim.client_id, refresh_token
            )
            new_ciphertext = self._cipher.encrypt(new_refresh_token)
        except InvalidGrant:
            try:
                self._repository.invalidate_refresh(
                    account_id, lease, _as_utc(self._clock())
                )
            except RefreshPersistenceError:
                pass
            raise
        except BaseException:
            self._release_best_effort(account_id, lease)
            raise

        for attempt in range(FINALIZE_ATTEMPTS):
            try:
                finalized = self._repository.finalize_refresh(
                    account_id,
                    lease,
                    new_ciphertext,
                    _as_utc(self._clock()),
                )
            except RefreshPersistenceError:
                if attempt == FINALIZE_ATTEMPTS - 1:
                    raise UpstreamUnavailable(
                        "refresh storage is unavailable"
                    ) from None
                self._sleep(FINALIZE_BACKOFF_SECONDS * (2**attempt))
                continue
            if not finalized:
                raise RefreshBusy(1)
            return access_token
        raise RuntimeError("unreachable refresh finalization state")

    def _release_best_effort(self, account_id: int, lease: str) -> None:
        try:
            self._repository.release_refresh(account_id, lease)
        except BaseException:
            pass


class TokenMaintenanceService:
    def __init__(self, repository, coordinator, clock=lambda: datetime.now(UTC)):
        self._repository = repository
        self._coordinator = coordinator
        self._clock = clock

    def run(self, limit, max_runtime_seconds):
        started = _as_utc(self._clock())
        handle = self._repository.begin_maintenance_run(started)
        if handle is None:
            return None
        counts = {"scanned": 0, "success": 0, "invalid": 0, "busy": 0, "failed": 0}
        deadline = time.monotonic() + max_runtime_seconds
        try:
            for account_id in self._repository.maintenance_account_ids(limit):
                if time.monotonic() >= deadline:
                    counts["failed"] += 1
                    break
                counts["scanned"] += 1
                try:
                    self._coordinator.access_token(account_id)
                    counts["success"] += 1
                except (InvalidGrant, TokenInvalid):
                    counts["invalid"] += 1
                except RefreshBusy:
                    counts["busy"] += 1
                except (UpstreamRateLimited, UpstreamUnavailable, UpstreamProtocolError):
                    counts["failed"] += 1
        except BaseException:
            self._repository.finish_maintenance_run(
                handle, _as_utc(self._clock()), counts, "failed"
            )
            raise
        self._repository.finish_maintenance_run(
            handle, _as_utc(self._clock()), counts
        )
        return counts


class MailService:
    def __init__(
        self,
        repository,
        coordinator,
        graph_client_factory,
        clock=lambda: datetime.now(UTC),
        *,
        monotonic=time.monotonic,
        randomizer=None,
    ):
        self._repository = repository
        self._coordinator = coordinator
        self._graph_client_factory = graph_client_factory
        self._clock = clock
        self._monotonic = monotonic
        self._randomizer = randomizer or secrets.SystemRandom()

    def list_mail(self, account_id: int, folder: str, limit: int):
        _validate_account_id(account_id)
        if folder not in _FOLDERS:
            raise InvalidMailPayload
        _validate_result_limit(limit)
        access_token = self._coordinator.access_token(account_id)
        graph = self._graph_client_factory()
        raw_messages = graph.list_messages(access_token, folder, limit)
        return [_serialize_message(_normalize_message(item)) for item in raw_messages]

    def verify_code(
        self, account_id: int, keyword: str | None, max_results: int
    ) -> str:
        _validate_account_id(account_id)
        if keyword is not None and (
            not isinstance(keyword, str) or len(keyword) > MAX_KEYWORD_LENGTH
        ):
            raise InvalidMailPayload
        _validate_result_limit(max_results)
        messages = self.list_mail(account_id, "inbox", max_results)
        keyword_folded = keyword.casefold() if keyword else None
        for message in messages:
            searchable = "\n".join(
                (message["sender"], message["subject"], message["bodyPreview"])
            )
            if keyword_folded is not None and keyword_folded not in searchable.casefold():
                continue
            code = _extract_contextual_code(
                f"{message['subject']}\n{message['bodyPreview']}"
            )
            if code is not None:
                return code
        raise VerificationCodeNotFound

    def check_bound(
        self, account_id: int, message_limit: int = BOUND_MESSAGE_LIMIT
    ) -> bool:
        _validate_account_id(account_id)
        _validate_result_limit(message_limit)
        messages = self.list_mail(account_id, "inbox", message_limit)
        bound = any(_is_openai_sender(message["sender"]) for message in messages)
        if not self._repository.set_bound(account_id, bound, _as_utc(self._clock())):
            raise MailAccountNotFound
        return bound

    def check_bound_batch(self, account_ids: Sequence[int]):
        ids = _validate_ids(account_ids)
        workers = min(4, len(ids))

        def check(account_id):
            return {"id": account_id, "bound": self.check_bound(account_id)}

        operations = []
        for account_id in ids:
            def operation(account_id=account_id):
                return check(account_id)

            if has_request_context():
                operation = copy_current_request_context(operation)
            operations.append(operation)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(lambda operation: operation(), operations))

    def random_unbound(self):
        deadline = self._monotonic() + RANDOM_DEADLINE_SECONDS
        candidates = self._repository.random_unbound_candidates(
            RANDOM_CANDIDATE_LIMIT
        )
        self._randomizer.shuffle(candidates)
        for candidate in candidates:
            if self._monotonic() >= deadline:
                raise UpstreamUnavailable("random account check timed out")
            try:
                if self.check_bound(candidate.id, RANDOM_BOUND_MESSAGE_LIMIT):
                    return {"id": candidate.id, "email": candidate.email}
            except (InvalidGrant, TokenInvalid, RefreshBusy, MailAccountNotFound):
                continue
        raise NoUnboundAccount


blueprint = Blueprint("mail", __name__)


def _service() -> MailService:
    return current_app.extensions["mail_service"]


def _validate_account_id(account_id: int) -> None:
    if type(account_id) is not int or not 1 <= account_id <= MAX_ACCOUNT_ID:
        raise InvalidMailPayload


def _validate_result_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_MAIL_RESULTS:
        raise InvalidMailPayload


def _validate_ids(value: object) -> list[int]:
    if not isinstance(value, list) or not value or len(value) > MAX_BATCH_SIZE:
        raise InvalidMailPayload
    if any(type(account_id) is not int for account_id in value):
        raise InvalidMailPayload
    for account_id in value:
        _validate_account_id(account_id)
    if len(value) != len(set(value)):
        raise InvalidMailPayload
    return value


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("clock must return datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _string_field(mapping: Mapping, name: str, default="") -> str:
    value = mapping.get(name, default)
    if not isinstance(value, str):
        raise UpstreamProtocolError("Microsoft Graph returned an invalid message")
    return value


def _sender_address(raw_message: Mapping) -> str:
    sender = raw_message.get("from")
    if sender is None:
        return ""
    if not isinstance(sender, Mapping):
        raise UpstreamProtocolError("Microsoft Graph returned an invalid sender")
    email_address = sender.get("emailAddress")
    if not isinstance(email_address, Mapping):
        raise UpstreamProtocolError("Microsoft Graph returned an invalid sender")
    raw_address = email_address.get("address", "")
    if not isinstance(raw_address, str):
        raise UpstreamProtocolError("Microsoft Graph returned an invalid sender")
    _, address = parseaddr(raw_address)
    return address


def _normalize_message(raw_message: object) -> MailView:
    if not isinstance(raw_message, Mapping):
        raise UpstreamProtocolError("Microsoft Graph returned an invalid message")
    body = raw_message.get("body")
    if not isinstance(body, Mapping):
        raise UpstreamProtocolError("Microsoft Graph returned an invalid message body")
    content_type = body.get("contentType")
    content = body.get("content")
    if (
        not isinstance(content_type, str)
        or content_type.casefold() not in {"html", "text"}
        or not isinstance(content, str)
    ):
        raise UpstreamProtocolError("Microsoft Graph returned an invalid message body")
    return MailView(
        id=_string_field(raw_message, "id"),
        subject=_string_field(raw_message, "subject"),
        sender=_sender_address(raw_message),
        date=_string_field(raw_message, "receivedDateTime"),
        body_preview=_string_field(raw_message, "bodyPreview"),
        html=content,
    )


def _serialize_message(message: MailView) -> dict[str, str]:
    return {
        "id": message.id,
        "subject": message.subject,
        "sender": message.sender,
        "date": message.date,
        "bodyPreview": message.body_preview,
        "html": message.html,
    }


def _extract_contextual_code(text: str) -> str | None:
    folded = text.casefold()
    for context in _CODE_CONTEXTS:
        start = 0
        while (context_index := folded.find(context, start)) >= 0:
            after = text[context_index + len(context) : context_index + len(context) + 40]
            after = re.sub(r"^[\s:;,#-]*(?:is\s+)?", "", after, flags=re.I)
            for match in _CODE_PATTERN.finditer(after):
                if match.group(1).casefold() not in _CODE_STOPWORDS:
                    return match.group(1)
            before = text[max(0, context_index - 24) : context_index]
            matches = [
                match
                for match in _CODE_PATTERN.finditer(before)
                if match.group(1).casefold() not in _CODE_STOPWORDS
            ]
            if matches:
                return matches[-1].group(1)
            start = context_index + len(context)
    return None


def _is_openai_sender(sender: str) -> bool:
    _, address = parseaddr(sender)
    if not address or "@" not in address:
        return False
    domain = address.rsplit("@", 1)[1].casefold()
    return domain == "openai.com" or domain.endswith(".openai.com")


def _invalid_response():
    return jsonify(error="invalid request"), 400


def _error_response(error):
    if isinstance(error, (MailAccountNotFound, VerificationCodeNotFound, NoUnboundAccount)):
        return jsonify(error="not found"), 404
    if isinstance(error, (InvalidGrant, TokenInvalid)):
        return jsonify(error="account token invalid"), 409
    if isinstance(error, RefreshBusy):
        response = jsonify(error="account refresh busy")
        response.status_code = 409
        response.headers["Retry-After"] = str(error.retry_after)
        return response
    if isinstance(error, UpstreamRateLimited):
        response = jsonify(error="upstream rate limited")
        response.status_code = 429
        if error.retry_after is not None:
            response.headers["Retry-After"] = str(error.retry_after)
        return response
    if isinstance(error, UpstreamUnavailable):
        return jsonify(error="upstream unavailable"), 503
    if isinstance(error, UpstreamProtocolError):
        return jsonify(error="invalid upstream response"), 502
    raise error


_EXPECTED_ERRORS = (
    MailAccountNotFound,
    TokenInvalid,
    RefreshBusy,
    VerificationCodeNotFound,
    NoUnboundAccount,
    InvalidGrant,
    UpstreamRateLimited,
    UpstreamUnavailable,
    UpstreamProtocolError,
)


@blueprint.post("/api/accounts/<int:account_id>/mail")
def list_mail(account_id: int):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {"folder", "limit"}:
        return _invalid_response()
    try:
        messages = _service().list_mail(
            account_id, payload["folder"], payload["limit"]
        )
    except InvalidMailPayload:
        return _invalid_response()
    except _EXPECTED_ERRORS as error:
        return _error_response(error)
    return jsonify(messages)


@blueprint.post("/api/accounts/<int:account_id>/verify-code")
def verify_code(account_id: int):
    payload = request.get_json(silent=True)
    if (
        not isinstance(payload, dict)
        or "maxResults" not in payload
        or not set(payload) <= {"keyword", "maxResults"}
    ):
        return _invalid_response()
    try:
        code = _service().verify_code(
            account_id, payload.get("keyword"), payload["maxResults"]
        )
    except InvalidMailPayload:
        return _invalid_response()
    except _EXPECTED_ERRORS as error:
        return _error_response(error)
    return jsonify(code=code)


@blueprint.post("/api/pickup/mail")
def pickup_list_mail():
    from mail_manager.pickup import current_pickup_context, record_pickup_event

    context = current_pickup_context()
    if context is None:
        return jsonify(error="pickup authorization required"), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {"folder", "limit"}:
        return _invalid_response()
    try:
        messages = _service().list_mail(
            context.account_id, payload["folder"], payload["limit"]
        )
    except InvalidMailPayload:
        return _invalid_response()
    except _EXPECTED_ERRORS as error:
        return _error_response(error)
    record_pickup_event("mail_viewed", payload["folder"])
    return jsonify(messages)


@blueprint.post("/api/pickup/verify-code")
def pickup_verify_code():
    from mail_manager.pickup import current_pickup_context, record_pickup_event

    context = current_pickup_context()
    if context is None:
        return jsonify(error="pickup authorization required"), 401
    payload = request.get_json(silent=True)
    if (
        not isinstance(payload, dict)
        or "maxResults" not in payload
        or not set(payload) <= {"keyword", "maxResults"}
    ):
        return _invalid_response()
    try:
        code = _service().verify_code(
            context.account_id, payload.get("keyword"), payload["maxResults"]
        )
    except InvalidMailPayload:
        return _invalid_response()
    except _EXPECTED_ERRORS as error:
        return _error_response(error)
    record_pickup_event("code_extracted")
    return jsonify(code=code)


@blueprint.post("/api/accounts/check-bound")
def check_bound():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {"ids"}:
        return _invalid_response()
    try:
        results = _service().check_bound_batch(payload["ids"])
    except InvalidMailPayload:
        return _invalid_response()
    except _EXPECTED_ERRORS as error:
        return _error_response(error)
    return jsonify(results)


@blueprint.post("/api/accounts/random-unbound")
def random_unbound():
    payload = request.get_json(silent=True)
    if payload not in (None, {}) or (
        payload is None and request.content_length not in (None, 0)
    ):
        return _invalid_response()
    try:
        account = _service().random_unbound()
    except _EXPECTED_ERRORS as error:
        return _error_response(error)
    return jsonify(account)


@click.command("refresh-tokens")
def refresh_tokens_command():
    service = current_app.extensions["token_maintenance_service"]
    counts = service.run(
        current_app.config["TOKEN_REFRESH_BATCH_SIZE"],
        current_app.config["TOKEN_REFRESH_MAX_RUNTIME_SECONDS"],
    )
    if counts is None:
        click.echo("Token maintenance is already running.")
        return
    click.echo(
        "Token maintenance completed: "
        f"scanned={counts['scanned']} success={counts['success']} "
        f"invalid={counts['invalid']} busy={counts['busy']} "
        f"failed={counts['failed']}"
    )


def init_app(app: Flask) -> None:
    repository = app.config.get("MAIL_REPOSITORY")
    if repository is None:
        repository = PostgresMailRepository()
    graph_client_factory = app.config.get("GRAPH_CLIENT_FACTORY", GraphClient)
    cipher = app.config.get("CREDENTIAL_CIPHER")
    if cipher is None:
        cipher = CredentialCipher(app.config["CREDENTIALS_ENCRYPTION_KEY"])
    clock = app.config.get("MAIL_CLOCK", lambda: datetime.now(UTC))
    coordinator = TokenCoordinator(repository, graph_client_factory, cipher, clock)
    app.extensions["mail_service"] = MailService(
        repository, coordinator, graph_client_factory, clock
    )
    app.extensions["token_maintenance_service"] = TokenMaintenanceService(
        repository, coordinator, clock
    )
    app.cli.add_command(refresh_tokens_command)
    app.register_blueprint(blueprint)
