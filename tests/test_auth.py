from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import hashlib
from threading import Event

import pytest

from mail_manager import create_app
from mail_manager.auth import (
    LoginOutcome,
    PostgresAuthRepository,
    constant_time_compare,
    derive_csrf_token,
    hash_token,
)
from mail_manager.config import Settings
from tests.fakes import FakeAuthRepository, MutableClock, RecordingConnection


VALID_MAPPING = {
    "DATABASE_URL": "postgresql://mail:secret@db.example.test/mail_manager",
    "ACCESS_PASSWORD": "correct-horse-battery-staple",
    "SESSION_SECRET": "s" * 32,
    "CREDENTIALS_ENCRYPTION_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    "PUBLIC_ORIGIN": "https://mail.example.test",
}
NOW = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)


@pytest.fixture
def auth_app():
    repository = FakeAuthRepository()
    clock = MutableClock(NOW)
    app = create_app(
        {
            "TESTING": True,
            "AUTH_REPOSITORY": repository,
            "AUTH_CLOCK": clock,
        },
        settings=Settings.from_mapping(VALID_MAPPING),
    )
    return app, repository, clock


def _login(client, password=VALID_MAPPING["ACCESS_PASSWORD"], **kwargs):
    return client.post("/api/login", json={"password": password}, **kwargs)


def _csrf_headers(csrf_token, origin=VALID_MAPPING["PUBLIC_ORIGIN"]):
    return {"Origin": origin, "X-CSRF-Token": csrf_token}


def test_hash_and_constant_time_comparison_helpers():
    secret = "secret-value"

    assert hash_token(secret) == hashlib.sha256(secret.encode()).hexdigest()
    assert constant_time_compare(secret, secret) is True
    assert constant_time_compare(secret, "wrong-value") is False
    assert secret not in hash_token(secret)


def test_check_auth_reports_anonymous_requests(auth_app):
    app, _, _ = auth_app

    response = app.test_client().get("/api/check-auth")

    assert response.status_code == 200
    assert response.get_json() == {"authenticated": False}


def test_login_sets_hardened_cookie_and_stores_only_hashes(auth_app):
    app, repository, _ = auth_app
    client = app.test_client()

    response = _login(client)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["authenticated"] is True
    csrf_token = payload["csrfToken"]
    cookie = client.get_cookie("mail_session")
    assert cookie is not None
    assert cookie.value
    set_cookie = response.headers["Set-Cookie"]
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie
    assert "Path=/" in set_cookie
    assert "Max-Age=28800" in set_cookie
    assert set(repository.sessions) == {hash_token(cookie.value)}
    stored_csrf_hash, expires_at = repository.sessions[hash_token(cookie.value)]
    assert stored_csrf_hash == hash_token(csrf_token)
    assert expires_at == NOW + timedelta(hours=8)
    assert csrf_token == derive_csrf_token(VALID_MAPPING["SESSION_SECRET"], cookie.value)
    assert cookie.value not in repr(repository.sessions)
    assert csrf_token not in repr(repository.sessions)

    checked = client.get("/api/check-auth")
    assert checked.get_json() == {
        "authenticated": True,
        "csrfToken": csrf_token,
    }


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": VALID_MAPPING["PUBLIC_ORIGIN"]},
        {"Origin": "https://attacker.example.test", "X-CSRF-Token": "wrong"},
    ],
)
def test_logout_rejects_missing_or_wrong_origin_and_csrf(auth_app, headers):
    app, repository, _ = auth_app
    client = app.test_client()
    login = _login(client)
    session_count = len(repository.sessions)

    response = client.post("/api/logout", headers=headers)

    assert response.status_code == 403
    assert response.is_json
    assert len(repository.sessions) == session_count
    assert VALID_MAPPING["ACCESS_PASSWORD"] not in response.get_data(as_text=True)
    assert login.get_json()["authenticated"] is True


def test_logout_revokes_session_and_expires_cookie(auth_app):
    app, repository, _ = auth_app
    client = app.test_client()
    login = _login(client)
    csrf_token = login.get_json()["csrfToken"]
    session_hash = next(iter(repository.sessions))

    response = client.post("/api/logout", headers=_csrf_headers(csrf_token))

    assert response.status_code == 200
    assert response.get_json() == {"authenticated": False}
    assert session_hash in repository.deleted_sessions
    assert repository.sessions == {}
    assert "mail_session=;" in response.headers["Set-Cookie"]
    assert client.get("/api/check-auth").get_json() == {"authenticated": False}


def test_protected_api_requires_session(auth_app):
    app, _, _ = auth_app

    @app.get("/api/protected")
    def protected():
        return {"ok": True}

    response = app.test_client().get("/api/protected")

    assert response.status_code == 401
    assert response.is_json


def test_state_changing_api_requires_exact_origin_and_csrf(auth_app):
    app, _, _ = auth_app

    @app.post("/api/change")
    def change():
        return {"changed": True}

    client = app.test_client()
    csrf_token = _login(client).get_json()["csrfToken"]

    assert client.post("/api/change").status_code == 403
    assert (
        client.post(
            "/api/change",
            headers=_csrf_headers(csrf_token, "https://attacker.example.test"),
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/change",
            headers=_csrf_headers("not-the-right-token"),
        ).status_code
        == 403
    )
    assert (
        client.post("/api/change", headers=_csrf_headers(csrf_token)).get_json()
        == {"changed": True}
    )


def test_expired_session_is_rejected_and_deleted(auth_app):
    app, repository, clock = auth_app
    client = app.test_client()
    _login(client)
    session_hash = next(iter(repository.sessions))
    clock.advance(hours=8, seconds=1)

    response = client.get("/api/check-auth")

    assert response.get_json() == {"authenticated": False}
    assert session_hash in repository.deleted_sessions
    assert repository.sessions == {}


def test_fifth_bad_password_locks_client_and_returns_retry_after(auth_app):
    app, repository, _ = auth_app
    client = app.test_client()

    for _ in range(4):
        response = _login(client, "wrong-password")
        assert response.status_code == 401

    locked = _login(client, "wrong-password")
    still_locked = _login(client)

    assert locked.status_code == 429
    assert locked.headers["Retry-After"] == "900"
    assert still_locked.status_code == 429
    assert still_locked.headers["Retry-After"] == "900"
    assert repository.failures["127.0.0.1"] == 5


def test_successful_login_clears_previous_failures(auth_app):
    app, repository, _ = auth_app
    client = app.test_client()
    for _ in range(4):
        assert _login(client, "wrong-password").status_code == 401

    assert _login(client).status_code == 200
    assert repository.failures == {}
    assert repository.locked_until_by_client == {}


def test_untrusted_forwarded_for_cannot_rotate_rate_limit_key(auth_app):
    app, repository, _ = auth_app
    client = app.test_client()

    for index in range(5):
        response = _login(
            client,
            "wrong-password",
            headers={"X-Forwarded-For": f"198.51.100.{index + 1}"},
        )

    assert response.status_code == 429
    assert repository.failures == {"127.0.0.1": 5}


def test_trusted_proxy_uses_single_forwarded_hop_for_client_key():
    repository = FakeAuthRepository()
    app = create_app(
        {
            "TESTING": True,
            "AUTH_REPOSITORY": repository,
            "AUTH_CLOCK": MutableClock(NOW),
            "TRUST_PROXY": True,
        },
        settings=Settings.from_mapping(VALID_MAPPING),
    )

    _login(
        app.test_client(),
        "wrong-password",
        headers={"X-Forwarded-For": "198.51.100.9"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert repository.failures == {"198.51.100.9": 1}


def test_untrusted_peer_cannot_use_forwarded_headers_when_proxy_mode_is_enabled():
    repository = FakeAuthRepository()
    app = create_app(
        {
            "TESTING": True,
            "AUTH_REPOSITORY": repository,
            "AUTH_CLOCK": MutableClock(NOW),
            "TRUST_PROXY": True,
        },
        settings=Settings.from_mapping(VALID_MAPPING),
    )

    _login(
        app.test_client(),
        "wrong-password",
        headers={
            "X-Forwarded-For": "198.51.100.9",
            "X-Forwarded-Proto": "https",
        },
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )

    assert repository.failures == {"203.0.113.10": 1}


def test_postgres_repository_uses_parameterized_session_queries():
    expires_at = NOW + timedelta(hours=8)
    connection = RecordingConnection(rows=[("csrf-hash", expires_at)])
    repository = PostgresAuthRepository(connection)

    session = repository.find_session("session-hash", NOW)
    repository.delete_session("session-hash")

    assert session.csrf_hash == "csrf-hash"
    assert connection.commits == 2
    for statement, parameters in connection.executed:
        assert "%s" in statement
        assert parameters is not None
        assert "session-hash" not in statement
        assert "csrf-hash" not in statement


def test_postgres_failed_login_is_one_locked_transaction_with_indexed_ttl_cleanup():
    locked_until = NOW + timedelta(minutes=15)
    connection = RecordingConnection(rows=[None, (5, locked_until)])
    repository = PostgresAuthRepository(connection)

    decision = repository.attempt_login(
        "client-key",
        NOW,
        False,
        "session-hash",
        "csrf-hash",
        NOW + timedelta(hours=8),
    )

    statements = [" ".join(sql.lower().split()) for sql, _ in connection.executed]
    assert "select pg_advisory_xact_lock(hashtextextended(%s, 0))" in statements[0]
    assert "delete from login_failures where updated_at < %s" in statements[1]
    assert "locked_until is null or locked_until <= %s" in statements[1]
    assert "select locked_until from login_failures" in statements[2]
    assert "insert into login_failures" in statements[3]
    assert "on conflict (client_key) do update" in statements[3]
    assert "returning failure_count, locked_until" in statements[3]
    assert connection.executed[0][1] == ("client-key",)
    assert connection.executed[1][1] == (NOW - timedelta(minutes=15), NOW)
    assert connection.executed[2][1] == ("client-key", NOW)
    assert connection.executed[3][1] == ("client-key", NOW)
    assert decision.outcome is LoginOutcome.LOCKED
    assert decision.locked_until == locked_until
    assert connection.commits == 1


def test_postgres_success_clears_failures_and_creates_session_in_same_transaction():
    expires_at = NOW + timedelta(hours=8)
    connection = RecordingConnection(rows=[None])
    repository = PostgresAuthRepository(connection)

    decision = repository.attempt_login(
        "client-key",
        NOW,
        True,
        "session-hash",
        "csrf-hash",
        expires_at,
    )

    statements = [" ".join(sql.lower().split()) for sql, _ in connection.executed]
    assert "select pg_advisory_xact_lock(hashtextextended(%s, 0))" in statements[0]
    assert "delete from login_failures where client_key = %s" in statements[3]
    assert "insert into auth_sessions" in statements[4]
    assert connection.executed[4][1] == ("session-hash", "csrf-hash", expires_at)
    assert decision.outcome is LoginOutcome.AUTHENTICATED
    assert connection.commits == 1


def _attempt(repository, client_key, now, password_valid, marker):
    return repository.attempt_login(
        client_key,
        now,
        password_valid,
        f"session-{marker}",
        f"csrf-{marker}",
        now + timedelta(hours=8),
    )


def test_fake_repository_serializes_concurrent_fifth_failure_decisions():
    repository = FakeAuthRepository()
    for marker in range(4):
        assert (
            _attempt(repository, "client-key", NOW, False, marker).outcome
            is LoginOutcome.INVALID
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        decisions = list(
            executor.map(
                lambda marker: _attempt(
                    repository, "client-key", NOW, False, marker
                ),
                (4, 5),
            )
        )

    assert [decision.outcome for decision in decisions] == [
        LoginOutcome.LOCKED,
        LoginOutcome.LOCKED,
    ]
    assert repository.failures == {"client-key": 5}


def test_concurrent_success_does_not_clear_a_newly_established_lock():
    repository = FakeAuthRepository()
    for marker in range(4):
        _attempt(repository, "client-key", NOW, False, marker)

    lock_established = Event()
    release_failure = Event()

    def hold_first_locked_decision(_client_key, decision):
        repository.decision_hook = None
        assert decision.outcome is LoginOutcome.LOCKED
        lock_established.set()
        assert release_failure.wait(timeout=5)

    repository.decision_hook = hold_first_locked_decision
    with ThreadPoolExecutor(max_workers=2) as executor:
        failed = executor.submit(
            _attempt, repository, "client-key", NOW, False, "failed"
        )
        assert lock_established.wait(timeout=5)
        success_started = Event()

        def attempt_success():
            success_started.set()
            return _attempt(repository, "client-key", NOW, True, "success")

        successful = executor.submit(attempt_success)
        assert success_started.wait(timeout=5)
        release_failure.set()
        decisions = (failed.result(timeout=5), successful.result(timeout=5))

    assert [decision.outcome for decision in decisions] == [
        LoginOutcome.LOCKED,
        LoginOutcome.LOCKED,
    ]
    assert repository.failures == {"client-key": 5}
    assert repository.sessions == {}


def test_attempt_login_prunes_expired_failures_for_other_client_keys():
    repository = FakeAuthRepository()
    old = NOW - timedelta(minutes=16)
    _attempt(repository, "expired-client", old, False, "old")

    decision = _attempt(repository, "current-client", NOW, False, "current")

    assert decision.outcome is LoginOutcome.INVALID
    assert repository.failures == {"current-client": 1}


def test_authentication_exceptions_return_redacted_json():
    class FailingRepository(FakeAuthRepository):
        def attempt_login(
            self,
            client_key,
            now,
            password_valid,
            session_hash,
            csrf_hash,
            expires_at,
        ):
            raise RuntimeError("database-secret-marker")

    app = create_app(
        {
            "TESTING": True,
            "AUTH_REPOSITORY": FailingRepository(),
            "AUTH_CLOCK": MutableClock(NOW),
        },
        settings=Settings.from_mapping(VALID_MAPPING),
    )

    response = _login(app.test_client(), "submitted-password-marker")
    body = response.get_data(as_text=True)

    assert response.status_code == 500
    assert response.is_json
    assert "submitted-password-marker" not in body
    assert "database-secret-marker" not in body
