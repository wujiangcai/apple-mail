from datetime import UTC, datetime, timedelta

from mail_manager import create_app
from mail_manager.auth import hash_token
from mail_manager.config import Settings
from mail_manager.models import PickupContext, PickupSession
from tests.fakes import FakeAuthRepository, FakeCredentialCipher, FakeGraphClient, FakeMailRepository, FakeStoredAccount, MutableClock


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
VALID_MAPPING = {
    "DATABASE_URL": "postgresql://mail:secret@db.example.test/mail_manager",
    "ACCESS_PASSWORD": "correct-horse-battery-staple",
    "SESSION_SECRET": "s" * 32,
    "CREDENTIALS_ENCRYPTION_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    "PUBLIC_ORIGIN": "https://mail.example.test",
}


class FakePickupRepository:
    def __init__(self):
        self.sessions = {}
        self.exchange_calls = []
        self.deleted = []
        self.events = []
        self.deny = False
        self.token_valid = True
        self.password_ciphertext = "password-ciphertext"

    def exchange_code(self, client_key, code_hash, session_hash, csrf_hash, now, limit):
        from mail_manager.pickup import PickupDenied

        self.exchange_calls.append((client_key, code_hash, session_hash, csrf_hash, now, limit))
        if self.deny:
            raise PickupDenied
        context = PickupContext(
            3,
            1,
            "rented@example.test",
            now + timedelta(minutes=30),
            now + timedelta(minutes=30),
            self.token_valid,
        )
        self.sessions[session_hash] = PickupSession(session_hash, csrf_hash, context)
        return context

    def find_session(self, session_hash, now):
        session = self.sessions.get(session_hash)
        if session is None or session.context.expires_at <= now:
            return None
        context = PickupContext(
            session.context.rental_id,
            session.context.account_id,
            session.context.email,
            session.context.expires_at,
            session.context.rental_expires_at,
            self.token_valid,
        )
        return PickupSession(session_hash, session.csrf_hash, context)

    def get_active_password_ciphertext(self, rental_id, account_id, now):
        from mail_manager.pickup import PickupPasswordUnavailable

        if not any(
            session.context.rental_id == rental_id
            and session.context.account_id == account_id
            and session.context.expires_at > now
            for session in self.sessions.values()
        ):
            raise PickupPasswordUnavailable
        return self.password_ciphertext

    def delete_session(self, session_hash):
        self.deleted.append(session_hash)
        self.sessions.pop(session_hash, None)

    def record_event(self, rental_id, event_type, client_key, now, detail=None):
        self.events.append((rental_id, event_type, client_key, now, detail))


class EmptyRentalRepository:
    def list_rentals(self, _now):
        return []


def _app(repository, clock):
    mail_repository = FakeMailRepository([
        FakeStoredAccount(
            id=1,
            email="rented@example.test",
            email_normalized="rented@example.test",
            password_ciphertext="password-ciphertext",
            client_id="client-1",
            refresh_token_ciphertext="refresh-ciphertext-1",
        )
    ])
    cipher = FakeCredentialCipher()
    cipher.add("password-ciphertext", "mailbox-password")
    cipher.add("refresh-ciphertext-1", "refresh-token")
    graph = FakeGraphClient([
        {
            "id": "message-1",
            "subject": "Verification code: 123456",
            "from": {"emailAddress": {"address": "sender@example.test"}},
            "receivedDateTime": "2026-07-22T11:00:00Z",
            "bodyPreview": "Use code 123456",
            "body": {"contentType": "text", "content": "Use code 123456"},
        }
    ])
    return create_app(
        {
            "TESTING": True,
            "AUTH_REPOSITORY": FakeAuthRepository(),
            "AUTH_CLOCK": clock,
            "PICKUP_REPOSITORY": repository,
            "PICKUP_CLOCK": clock,
            "RENTAL_REPOSITORY": EmptyRentalRepository(),
            "RENTAL_CLOCK": clock,
            "MAIL_REPOSITORY": mail_repository,
            "CREDENTIAL_CIPHER": cipher,
            "GRAPH_CLIENT_FACTORY": graph.spawn,
            "MAIL_CLOCK": clock,
        },
        settings=Settings.from_mapping(VALID_MAPPING),
    )


def _login(client):
    response = client.post("/api/pickup/login", json={"code": "one-time-code"})
    assert response.status_code == 200
    return response.get_json()


def _headers(csrf):
    return {"Origin": VALID_MAPPING["PUBLIC_ORIGIN"], "X-CSRF-Token": csrf}


def test_pickup_login_uses_separate_hardened_cookie_and_hashed_storage():
    repository = FakePickupRepository()
    client = _app(repository, MutableClock(NOW)).test_client()

    payload = _login(client)

    cookie = client.get_cookie("pickup_session")
    assert cookie is not None
    assert "HttpOnly" in client.post("/api/pickup/login", json={"code": "second"}).headers["Set-Cookie"]
    assert payload["email"] == "rented@example.test"
    assert payload["expiresAt"] == "2026-07-22T12:30:00Z"
    assert payload["rentalExpiresAt"] == "2026-07-22T12:30:00Z"
    assert payload["permanent"] is False
    assert payload["tokenValid"] is True
    stored_hash = repository.exchange_calls[0][2]
    assert stored_hash == hash_token(cookie.value)
    assert cookie.value not in repr(repository.sessions)
    assert "one-time-code" not in repr(repository.exchange_calls)


def test_pickup_code_failure_is_generic_and_creates_no_cookie():
    repository = FakePickupRepository()
    repository.deny = True
    client = _app(repository, MutableClock(NOW)).test_client()

    response = client.post("/api/pickup/login", json={"code": "wrong"})

    assert response.status_code == 401
    assert response.get_json() == {"error": "invalid or expired pickup code"}
    assert client.get_cookie("pickup_session") is None
    assert "wrong" not in response.get_data(as_text=True)


def test_pickup_login_allows_active_rental_when_authorization_needs_attention():
    repository = FakePickupRepository()
    repository.token_valid = False
    client = _app(repository, MutableClock(NOW)).test_client()

    response = client.post("/api/pickup/login", json={"code": "valid-code"})

    assert response.status_code == 200
    assert response.get_json()["tokenValid"] is False
    assert client.get_cookie("pickup_session") is not None


def test_pickup_password_reveal_requires_session_csrf_and_returns_only_password():
    repository = FakePickupRepository()
    client = _app(repository, MutableClock(NOW)).test_client()

    assert client.post("/api/pickup/reveal-password", json={}).status_code == 401
    auth = _login(client)
    assert client.post("/api/pickup/reveal-password", json={}).status_code == 403
    response = client.post(
        "/api/pickup/reveal-password",
        json={},
        headers=_headers(auth["csrfToken"]),
    )

    assert response.status_code == 200
    assert response.get_json() == {"password": "mailbox-password"}
    assert response.headers["Cache-Control"] == "no-store"
    assert repository.events[-1] == (
        3,
        "password_revealed",
        "127.0.0.1",
        NOW,
        None,
    )


def test_pickup_password_reveal_rejects_client_selected_account_fields():
    repository = FakePickupRepository()
    client = _app(repository, MutableClock(NOW)).test_client()
    auth = _login(client)

    response = client.post(
        "/api/pickup/reveal-password",
        json={"accountId": 2},
        headers=_headers(auth["csrfToken"]),
    )

    assert response.status_code == 400
    assert "mailbox-password" not in response.get_data(as_text=True)


def test_pickup_check_auth_reports_live_token_status():
    repository = FakePickupRepository()
    client = _app(repository, MutableClock(NOW)).test_client()
    _login(client)
    repository.token_valid = False

    response = client.get("/api/pickup/check-auth")

    assert response.status_code == 200
    assert response.get_json()["tokenValid"] is False


def test_pickup_mail_requires_pickup_session_and_exact_csrf():
    repository = FakePickupRepository()
    client = _app(repository, MutableClock(NOW)).test_client()

    assert client.post("/api/pickup/mail", json={"folder": "inbox", "limit": 10}).status_code == 401
    auth = _login(client)
    assert client.post("/api/pickup/mail", json={"folder": "inbox", "limit": 10}).status_code == 403
    response = client.post(
        "/api/pickup/mail",
        json={"folder": "inbox", "limit": 10},
        headers=_headers(auth["csrfToken"]),
    )

    assert response.status_code == 200
    assert response.get_json()[0]["subject"] == "Verification code: 123456"
    assert repository.events[-1][1:] == (
        "mail_viewed",
        "127.0.0.1",
        NOW,
        "inbox",
    )


def test_pickup_session_cannot_access_admin_inventory():
    repository = FakePickupRepository()
    client = _app(repository, MutableClock(NOW)).test_client()
    _login(client)

    response = client.get("/api/accounts")

    assert response.status_code == 401


def test_expired_pickup_session_is_rejected_and_cleared():
    repository = FakePickupRepository()
    clock = MutableClock(NOW)
    client = _app(repository, clock).test_client()
    auth = _login(client)
    session_hash = next(iter(repository.sessions))
    clock.advance(minutes=31)

    response = client.post(
        "/api/pickup/verify-code",
        json={"maxResults": 10},
        headers=_headers(auth["csrfToken"]),
    )

    assert response.status_code == 401
    assert repository.find_session(session_hash, clock()) is None
