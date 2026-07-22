from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
import hashlib
import hmac
import math
import secrets
from typing import Protocol

from flask import Blueprint, Flask, current_app, g, jsonify, request
from werkzeug.exceptions import HTTPException

from mail_manager import db


SESSION_COOKIE = "mail_session"
SESSION_LIFETIME = timedelta(hours=8)
LOCK_DURATION = timedelta(minutes=15)
MAX_FAILURES = 5
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_PUBLIC_API_PATHS = frozenset({"/api/login", "/api/check-auth"})


@dataclass(frozen=True)
class AuthSession:
    session_hash: str
    csrf_hash: str
    expires_at: datetime


class LoginOutcome(Enum):
    AUTHENTICATED = "authenticated"
    INVALID = "invalid"
    LOCKED = "locked"


@dataclass(frozen=True)
class LoginDecision:
    outcome: LoginOutcome
    locked_until: datetime | None = None


class AuthRepository(Protocol):
    def attempt_login(
        self,
        client_key: str,
        now: datetime,
        password_valid: bool,
        session_hash: str,
        csrf_hash: str,
        expires_at: datetime,
    ) -> LoginDecision: ...

    def find_session(self, session_hash: str, now: datetime) -> AuthSession | None: ...

    def delete_session(self, session_hash: str) -> None: ...


class PostgresAuthRepository:
    def __init__(self, connection: object | Callable[[], object] = db.get_db):
        self._connection_source = connection

    def _connection(self):
        source = self._connection_source
        return source() if callable(source) else source

    def attempt_login(
        self,
        client_key: str,
        now: datetime,
        password_valid: bool,
        session_hash: str,
        csrf_hash: str,
        expires_at: datetime,
    ) -> LoginDecision:
        with db.transaction(self._connection()) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (client_key,),
            )
            connection.execute(
                """
                DELETE FROM login_failures
                WHERE updated_at < %s
                  AND (locked_until IS NULL OR locked_until <= %s)
                """,
                (now - LOCK_DURATION, now),
            )
            locked_row = connection.execute(
                """
                SELECT locked_until
                FROM login_failures
                WHERE client_key = %s AND locked_until > %s
                """,
                (client_key, now),
            ).fetchone()
            if locked_row is not None:
                return LoginDecision(LoginOutcome.LOCKED, locked_row[0])

            if not password_valid:
                row = connection.execute(
                    """
                    INSERT INTO login_failures (
                        client_key, failure_count, locked_until, updated_at
                    )
                    VALUES (%s, 1, NULL, %s)
                    ON CONFLICT (client_key) DO UPDATE SET
                        failure_count = CASE
                            WHEN login_failures.locked_until IS NOT NULL
                                 AND login_failures.locked_until
                                     <= EXCLUDED.updated_at
                            THEN 1
                            ELSE login_failures.failure_count + 1
                        END,
                        locked_until = CASE
                            WHEN (
                                CASE
                                    WHEN login_failures.locked_until IS NOT NULL
                                         AND login_failures.locked_until
                                             <= EXCLUDED.updated_at
                                    THEN 1
                                    ELSE login_failures.failure_count + 1
                                END
                            ) >= 5
                            THEN EXCLUDED.updated_at + INTERVAL '15 minutes'
                            ELSE NULL
                        END,
                        updated_at = EXCLUDED.updated_at
                    RETURNING failure_count, locked_until
                    """,
                    (client_key, now),
                ).fetchone()
                if row is None:
                    raise RuntimeError("failed to record login attempt")
                failure_count, locked_until = row
                if failure_count >= MAX_FAILURES:
                    return LoginDecision(LoginOutcome.LOCKED, locked_until)
                return LoginDecision(LoginOutcome.INVALID)

            connection.execute(
                "DELETE FROM login_failures WHERE client_key = %s",
                (client_key,),
            )
            connection.execute(
                """
                INSERT INTO auth_sessions (session_hash, csrf_hash, expires_at)
                VALUES (%s, %s, %s)
                """,
                (session_hash, csrf_hash, expires_at),
            )
            return LoginDecision(LoginOutcome.AUTHENTICATED)

    def find_session(self, session_hash: str, now: datetime) -> AuthSession | None:
        with db.transaction(self._connection()) as connection:
            row = connection.execute(
                """
                SELECT csrf_hash, expires_at
                FROM auth_sessions
                WHERE session_hash = %s
                """,
                (session_hash,),
            ).fetchone()
            if row is None:
                return None
            csrf_hash, expires_at = row
            if expires_at <= now:
                connection.execute(
                    "DELETE FROM auth_sessions WHERE session_hash = %s",
                    (session_hash,),
                )
                return None
        return AuthSession(
            session_hash=session_hash,
            csrf_hash=csrf_hash,
            expires_at=expires_at,
        )

    def delete_session(self, session_hash: str) -> None:
        with db.transaction(self._connection()) as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE session_hash = %s",
                (session_hash,),
            )

blueprint = Blueprint("auth", __name__)


def hash_token(token: str) -> str:
    if not isinstance(token, str):
        raise TypeError("token must be str")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_compare(candidate: str, expected: str) -> bool:
    if not isinstance(candidate, str) or not isinstance(expected, str):
        raise TypeError("compared values must be str")
    return hmac.compare_digest(hash_token(candidate), hash_token(expected))


def derive_csrf_token(session_secret: str, session_token: str) -> str:
    if not isinstance(session_secret, str) or not isinstance(session_token, str):
        raise TypeError("session secret and token must be str")
    return hmac.new(
        session_secret.encode("utf-8"),
        session_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _matches_hash(token: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), expected_hash)


def _repository() -> AuthRepository:
    return current_app.extensions["auth_repository"]


def _now() -> datetime:
    return current_app.config["AUTH_CLOCK"]()


def _client_key() -> str:
    return request.remote_addr or "unknown-client"


def _retry_response(locked_until: datetime, now: datetime):
    seconds = max(1, math.ceil((locked_until - now).total_seconds()))
    response = jsonify(error="too many login attempts")
    response.status_code = 429
    response.headers["Retry-After"] = str(seconds)
    return response


def _load_session() -> AuthSession | None:
    if "auth_session" in g:
        return g.auth_session

    raw_session = request.cookies.get(SESSION_COOKIE)
    if not raw_session:
        g.auth_session = None
        return None

    session_hash = hash_token(raw_session)
    session = _repository().find_session(session_hash, _now())
    if session is None:
        g.auth_session = None
        return None

    csrf_token = derive_csrf_token(current_app.config["SESSION_SECRET"], raw_session)
    if not _matches_hash(csrf_token, session.csrf_hash):
        _repository().delete_session(session_hash)
        g.auth_session = None
        return None

    g.auth_session = session
    g.raw_session = raw_session
    g.csrf_token = csrf_token
    return session


@blueprint.post("/api/login")
def login():
    now = _now()
    client_key = _client_key()
    payload = request.get_json(silent=True)
    supplied_password = payload.get("password", "") if isinstance(payload, dict) else ""
    if not isinstance(supplied_password, str):
        supplied_password = ""
    password_valid = constant_time_compare(
        supplied_password, current_app.config["ACCESS_PASSWORD"]
    )
    raw_session = secrets.token_urlsafe(32)
    csrf_token = derive_csrf_token(
        current_app.config["SESSION_SECRET"], raw_session
    )
    expires_at = now + SESSION_LIFETIME
    decision = _repository().attempt_login(
        client_key,
        now,
        password_valid,
        hash_token(raw_session),
        hash_token(csrf_token),
        expires_at,
    )
    if decision.outcome is LoginOutcome.LOCKED:
        if decision.locked_until is None:
            raise RuntimeError("locked login decision requires an expiry")
        return _retry_response(decision.locked_until, now)
    if decision.outcome is LoginOutcome.INVALID:
        return jsonify(error="invalid credentials"), 401

    response = jsonify(authenticated=True, csrfToken=csrf_token)
    response.set_cookie(
        SESSION_COOKIE,
        raw_session,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        secure=bool(current_app.config["COOKIE_SECURE"]),
        httponly=True,
        samesite="Strict",
        path="/",
    )
    return response


@blueprint.get("/api/check-auth")
def check_auth():
    if _load_session() is None:
        return jsonify(authenticated=False)
    return jsonify(authenticated=True, csrfToken=g.csrf_token)


@blueprint.post("/api/logout")
def logout():
    _repository().delete_session(g.auth_session.session_hash)
    response = jsonify(authenticated=False)
    response.delete_cookie(
        SESSION_COOKIE,
        secure=bool(current_app.config["COOKIE_SECURE"]),
        httponly=True,
        samesite="Strict",
        path="/",
    )
    return response


def protect_api():
    if request.path.startswith("/api/pickup/"):
        return None
    if not request.path.startswith("/api/") or request.path in _PUBLIC_API_PATHS:
        return None
    if _load_session() is None:
        return jsonify(error="authentication required"), 401
    if request.method not in _STATE_CHANGING_METHODS:
        return None

    origin = request.headers.get("Origin", "")
    csrf_token = request.headers.get("X-CSRF-Token", "")
    if not constant_time_compare(origin, current_app.config["PUBLIC_ORIGIN"]):
        return jsonify(error="forbidden"), 403
    if not constant_time_compare(csrf_token, g.csrf_token):
        return jsonify(error="forbidden"), 403
    return None


def _redact_api_error(error: Exception):
    if not request.path.startswith("/api/"):
        if isinstance(error, HTTPException):
            return error
        raise error
    status_code = error.code if isinstance(error, HTTPException) else 500
    messages = {
        401: "authentication required",
        403: "forbidden",
        404: "not found",
        405: "method not allowed",
        429: "too many requests",
    }
    return jsonify(error=messages.get(status_code, "internal server error")), status_code


def init_app(app: Flask) -> None:
    repository = app.config.get("AUTH_REPOSITORY")
    if repository is None:
        repository = PostgresAuthRepository()
    app.extensions["auth_repository"] = repository
    app.config.setdefault("AUTH_CLOCK", lambda: datetime.now(UTC))
    app.register_blueprint(blueprint)
    app.before_request(protect_api)
    app.register_error_handler(Exception, _redact_api_error)
