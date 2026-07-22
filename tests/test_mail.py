from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Lock
import time

import psycopg
import pytest
from flask import current_app

from mail_manager import create_app, graph
from mail_manager.config import Settings
from mail_manager.mail import (
    MailAccountNotFound,
    MailService,
    NoUnboundAccount,
    PostgresMailRepository,
    RefreshBusy,
    RefreshPersistenceError,
    TokenCoordinator,
    TokenInvalid,
)
from tests.fakes import (
    FakeAuthRepository,
    FakeCredentialCipher,
    FakeGraphClient,
    FakeMailRepository,
    FakeStoredAccount,
    MutableClock,
    RecordingConnection,
)


VALID_MAPPING = {
    "DATABASE_URL": "postgresql://mail:secret@db.example.test/mail_manager",
    "ACCESS_PASSWORD": "correct-horse-battery-staple",
    "SESSION_SECRET": "s" * 32,
    "CREDENTIALS_ENCRYPTION_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    "PUBLIC_ORIGIN": "https://mail.example.test",
}
NOW = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)


class EventConnection(RecordingConnection):
    def __init__(self, rows, events):
        super().__init__(rows=rows)
        self.events = events

    def execute(self, statement, parameters=None):
        self.events.append(("sql", " ".join(statement.split()), parameters))
        return super().execute(statement, parameters)

    def commit(self):
        self.events.append(("commit",))
        super().commit()

    def rollback(self):
        self.events.append(("rollback",))
        super().rollback()


def _stored(account_id=1, **overrides):
    values = {
        "id": account_id,
        "email": f"person-{account_id}@example.test",
        "email_normalized": f"person-{account_id}@example.test",
        "password_ciphertext": "password-ciphertext",
        "client_id": f"client-{account_id}",
        "refresh_token_ciphertext": f"refresh-ciphertext-{account_id}",
    }
    values.update(overrides)
    return FakeStoredAccount(**values)


def _cipher(*account_ids):
    cipher = FakeCredentialCipher()
    for account_id in account_ids:
        cipher.add(f"refresh-ciphertext-{account_id}", f"refresh-{account_id}")
    return cipher


def _login_headers(client):
    response = client.post(
        "/api/login", json={"password": VALID_MAPPING["ACCESS_PASSWORD"]}
    )
    return {
        "Origin": VALID_MAPPING["PUBLIC_ORIGIN"],
        "X-CSRF-Token": response.get_json()["csrfToken"],
    }


def _app(repository, cipher, graph_client):
    return create_app(
        {
            "TESTING": True,
            "AUTH_REPOSITORY": FakeAuthRepository(),
            "AUTH_CLOCK": MutableClock(NOW),
            "ACCOUNT_REPOSITORY": repository,
            "MAIL_REPOSITORY": repository,
            "CREDENTIAL_CIPHER": cipher,
            "MAIL_CLOCK": MutableClock(NOW),
            "GRAPH_CLIENT_FACTORY": graph_client.spawn,
        },
        settings=Settings.from_mapping(VALID_MAPPING),
    )


def test_refresh_claim_commits_and_closes_before_oauth_then_finalizes_with_new_connection():
    claim_connection = RecordingConnection(
        rows=[("client-1", "refresh-ciphertext-1")]
    )
    finalize_connection = RecordingConnection(rows=[(1,)])
    connections = iter((claim_connection, finalize_connection))
    graph_client = FakeGraphClient()

    def refresh_hook(*_):
        assert claim_connection.commits == 1
        assert claim_connection.closed is True
        assert finalize_connection.executed == []

    graph_client.refresh_hook = refresh_hook
    coordinator = TokenCoordinator(
        PostgresMailRepository(lambda: next(connections)),
        lambda: graph_client,
        _cipher(1),
        lambda: NOW,
        lease_factory=lambda: "lease-one",
    )

    assert coordinator.access_token(1) == "access-client-1"
    assert claim_connection.commits == finalize_connection.commits == 1
    assert claim_connection.closed is finalize_connection.closed is True
    assert "refresh_lease_token" in claim_connection.executed[0][0]
    assert "refresh_lease_token = %s" in finalize_connection.executed[0][0]


def test_claim_distinguishes_missing_invalid_and_busy_accounts():
    expires_at = NOW + timedelta(seconds=17)
    missing = PostgresMailRepository(RecordingConnection(rows=[None, None]))
    invalid = PostgresMailRepository(
        RecordingConnection(rows=[None, (False, None)])
    )
    busy = PostgresMailRepository(
        RecordingConnection(rows=[None, (True, expires_at)])
    )

    with pytest.raises(MailAccountNotFound):
        missing.claim_refresh(1, "lease", NOW, timedelta(seconds=30))
    with pytest.raises(TokenInvalid):
        invalid.claim_refresh(1, "lease", NOW, timedelta(seconds=30))
    with pytest.raises(RefreshBusy) as captured:
        busy.claim_refresh(1, "lease", NOW, timedelta(seconds=30))

    assert captured.value.retry_after == 17


def test_old_lease_cannot_finalize_over_a_newer_claim():
    connection = RecordingConnection(rows=[None])

    finalized = PostgresMailRepository(connection).finalize_refresh(
        1, "stale-lease", "new-ciphertext", NOW
    )

    assert finalized is False
    assert connection.executed[0][1][-1] == "stale-lease"


def test_invalid_grant_marks_token_invalid_for_matching_lease():
    repository = FakeMailRepository([_stored()])
    graph_client = FakeGraphClient()
    graph_client.refresh_error = graph.InvalidGrant("sanitized")
    coordinator = TokenCoordinator(
        repository,
        lambda: graph_client,
        _cipher(1),
        lambda: NOW,
        lease_factory=lambda: "lease-one",
    )

    with pytest.raises(graph.InvalidGrant):
        coordinator.access_token(1)

    assert repository.accounts[1].token_valid is False
    assert repository.accounts[1].refresh_lease_token is None


@pytest.mark.parametrize(
    "error",
    [
        graph.UpstreamRateLimited(17),
        graph.UpstreamUnavailable("sanitized"),
        graph.UpstreamProtocolError("sanitized"),
    ],
)
def test_other_refresh_failures_release_lease_without_changing_token(error):
    repository = FakeMailRepository([_stored()])
    graph_client = FakeGraphClient()
    graph_client.refresh_error = error
    coordinator = TokenCoordinator(
        repository, lambda: graph_client, _cipher(1), lambda: NOW
    )

    with pytest.raises(type(error)):
        coordinator.access_token(1)

    assert repository.accounts[1].token_valid is True
    assert repository.accounts[1].refresh_lease_token is None


def test_service_persists_rotation_before_list_and_keeps_it_if_list_fails():
    events = []
    repository = FakeMailRepository([_stored()])
    graph_client = FakeGraphClient()
    graph_client.list_error = graph.UpstreamUnavailable("sanitized")
    original_finalize = repository.finalize_refresh

    def finalize(*args):
        result = original_finalize(*args)
        events.append("persist")
        return result

    repository.finalize_refresh = finalize
    graph_client.list_hook = lambda *_: events.append("list")
    service = MailService(
        repository,
        TokenCoordinator(repository, lambda: graph_client, _cipher(1), lambda: NOW),
        lambda: graph_client,
        lambda: NOW,
    )

    with pytest.raises(graph.UpstreamUnavailable):
        service.list_mail(1, "inbox", 10)

    assert events == ["persist", "list"]
    assert repository.accounts[1].refresh_token_ciphertext == "opaque-ciphertext-1"


def test_same_account_is_immediately_busy_while_different_accounts_run_in_parallel():
    accounts = [_stored(1), _stored(2)]
    repository = FakeMailRepository(accounts)
    cipher = _cipher(1, 2)
    started = Event()
    release = Event()

    def graph_factory():
        client = FakeGraphClient()
        client.refresh_hook = lambda *_: (started.set(), release.wait(timeout=1))
        return client

    coordinator = TokenCoordinator(repository, graph_factory, cipher, lambda: NOW)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(coordinator.access_token, 1)
        assert started.wait(timeout=1)
        second = executor.submit(coordinator.access_token, 1)
        with pytest.raises(RefreshBusy):
            second.result(timeout=0.2)
        release.set()
        assert first.result(timeout=1) == "access-client-1"

    barrier = Barrier(2)
    repository = FakeMailRepository(accounts)

    def parallel_factory():
        client = FakeGraphClient()
        client.refresh_hook = lambda *_: barrier.wait(timeout=1)
        return client

    coordinator = TokenCoordinator(repository, parallel_factory, _cipher(1, 2), lambda: NOW)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(coordinator.access_token, [1, 2]))


def test_mail_mapping_validates_body_and_returns_only_public_fields():
    graph_client = FakeGraphClient(
        [
            {
                "id": "message-1",
                "subject": "Security code",
                "from": {"emailAddress": {"address": "sender@example.test"}},
                "receivedDateTime": "2026-07-13T07:00:00Z",
                "bodyPreview": "Use code 123456",
                "body": {"contentType": "html", "content": "<b>123456</b>"},
                "access_token": "must-not-leak",
            }
        ]
    )
    repository = FakeMailRepository([_stored()])
    service = MailService(
        repository,
        TokenCoordinator(repository, lambda: graph_client, _cipher(1), lambda: NOW),
        lambda: graph_client,
        lambda: NOW,
    )

    result = service.list_mail(1, "inbox", 10)

    assert result == [
        {
            "id": "message-1",
            "subject": "Security code",
            "sender": "sender@example.test",
            "date": "2026-07-13T07:00:00Z",
            "bodyPreview": "Use code 123456",
            "html": "<b>123456</b>",
        }
    ]
    graph_client.messages[0]["body"]["content"] = 123456
    with pytest.raises(graph.UpstreamProtocolError):
        service.list_mail(1, "inbox", 10)


@pytest.mark.parametrize(
    ("subject", "preview", "keyword", "expected"),
    [
        ("Your verification code is A1B2C3", "", None, "A1B2C3"),
        ("Login", "OpenAI code: 654321", "openai", "654321"),
        ("Receipt 654321", "Order shipped", None, None),
        ("Security code", "Code 123", None, None),
    ],
)
def test_verification_code_requires_context_and_keyword_filter(
    subject, preview, keyword, expected
):
    graph_client = FakeGraphClient(
        [
            {
                "id": "m",
                "subject": subject,
                "from": {"emailAddress": {"address": "noreply@openai.com"}},
                "receivedDateTime": "2026-07-13T07:00:00Z",
                "bodyPreview": preview,
                "body": {"contentType": "text", "content": preview},
            }
        ]
    )
    repository = FakeMailRepository([_stored()])
    service = MailService(
        repository,
        TokenCoordinator(repository, lambda: graph_client, _cipher(1), lambda: NOW),
        lambda: graph_client,
        lambda: NOW,
    )

    if expected is None:
        from mail_manager.mail import VerificationCodeNotFound

        with pytest.raises(VerificationCodeNotFound):
            service.verify_code(1, keyword, 10)
    else:
        assert service.verify_code(1, keyword, 10) == expected


@pytest.mark.parametrize(
    ("address", "bound"),
    [
        ("noreply@openai.com", True),
        ("noreply@mail.openai.com", True),
        ("noreply@openai.com.evil.test", False),
        ("noreply@notopenai.com", False),
    ],
)
def test_binding_uses_parsed_sender_domain_not_subject(address, bound):
    graph_client = FakeGraphClient(
        [
            {
                "id": "m",
                "subject": "Official OpenAI account binding",
                "from": {"emailAddress": {"address": address}},
                "receivedDateTime": "2026-07-13T07:00:00Z",
                "bodyPreview": "",
                "body": {"contentType": "text", "content": ""},
            }
        ]
    )
    repository = FakeMailRepository([_stored(bound=True)])
    service = MailService(
        repository,
        TokenCoordinator(repository, lambda: graph_client, _cipher(1), lambda: NOW),
        lambda: graph_client,
        lambda: NOW,
    )

    assert service.check_bound(1) is bound
    assert repository.accounts[1].bound is bound
    assert graph_client.events[-1][-1] == 100


def test_upstream_failure_does_not_clear_existing_bound_state():
    graph_client = FakeGraphClient()
    graph_client.list_error = graph.UpstreamUnavailable("sanitized")
    repository = FakeMailRepository([_stored(bound=True, bound_at=NOW)])
    service = MailService(
        repository,
        TokenCoordinator(repository, lambda: graph_client, _cipher(1), lambda: NOW),
        lambda: graph_client,
        lambda: NOW,
    )

    with pytest.raises(graph.UpstreamUnavailable):
        service.check_bound(1)

    assert repository.accounts[1].bound is True
    assert not any(event[0] == "bound" for event in repository.events)


def test_batch_results_follow_input_order_and_use_at_most_four_workers():
    accounts = [_stored(index) for index in range(1, 7)]
    repository = FakeMailRepository(accounts)
    graph_client = FakeGraphClient()
    counter_lock = Lock()
    active = 0
    maximum_active = 0

    def list_hook(*_):
        nonlocal active, maximum_active
        with counter_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.02)
        with counter_lock:
            active -= 1

    graph_client.list_hook = list_hook
    service = MailService(
        repository,
        TokenCoordinator(
            repository, lambda: graph_client, _cipher(*range(1, 7)), lambda: NOW
        ),
        lambda: graph_client,
        lambda: NOW,
    )

    results = service.check_bound_batch([6, 2, 5, 1, 4, 3])

    assert results == [
        {"id": account_id, "bound": False}
        for account_id in [6, 2, 5, 1, 4, 3]
    ]
    assert maximum_active == 4


def test_random_unbound_uses_five_safe_candidates_and_returns_metadata_only():
    accounts = [_stored(1, bound=True), _stored(2, token_valid=False), _stored(3)]
    repository = FakeMailRepository(accounts)
    graph_client = FakeGraphClient(
        [
            {
                "id": "m",
                "subject": "",
                "from": {"emailAddress": {"address": "hello@openai.com"}},
                "receivedDateTime": "2026-07-13T07:00:00Z",
                "bodyPreview": "",
                "body": {"contentType": "text", "content": ""},
            }
        ]
    )
    service = MailService(
        repository,
        TokenCoordinator(repository, lambda: graph_client, _cipher(3), lambda: NOW),
        lambda: graph_client,
        lambda: NOW,
    )

    assert service.random_unbound() == {"id": 3, "email": "person-3@example.test"}
    assert repository.candidate_limit == 5


@pytest.mark.parametrize(
    ("error", "status", "retry_after"),
    [
        (graph.InvalidGrant("refresh-secret"), 409, None),
        (graph.UpstreamProtocolError("access-secret"), 502, None),
        (graph.UpstreamUnavailable("access-secret"), 503, None),
        (graph.UpstreamRateLimited(19), 429, "19"),
    ],
)
def test_mail_api_maps_sanitized_upstream_failures(error, status, retry_after):
    repository = FakeMailRepository([_stored()])
    graph_client = FakeGraphClient()
    graph_client.refresh_error = error
    client = _app(repository, _cipher(1), graph_client).test_client()
    headers = _login_headers(client)

    response = client.post(
        "/api/accounts/1/mail",
        json={"folder": "inbox", "limit": 10},
        headers=headers,
    )

    assert response.status_code == status
    assert set(response.get_json()) == {"error"}
    assert "secret" not in response.get_data(as_text=True)
    assert response.headers.get("Retry-After") == retry_after


def test_mail_api_accepts_ids_only_and_validates_all_payload_bounds():
    repository = FakeMailRepository([_stored()])
    client = _app(repository, _cipher(1), FakeGraphClient()).test_client()
    headers = _login_headers(client)

    invalid_requests = [
        ("/api/accounts/1/mail", {"folder": "inbox", "limit": 0}),
        ("/api/accounts/1/mail", {"folder": "sent", "limit": 10}),
        ("/api/accounts/1/mail", {"folder": "inbox", "limit": 10, "token": "x"}),
        ("/api/accounts/1/verify-code", {"keyword": "x" * 129, "maxResults": 10}),
        ("/api/accounts/check-bound", {"ids": [1, 1]}),
        ("/api/accounts/check-bound", {"ids": list(range(1, 22))}),
        ("/api/accounts/random-unbound", {"accessToken": "secret"}),
    ]
    for path, payload in invalid_requests:
        response = client.post(path, json=payload, headers=headers)
        assert response.status_code == 400
        assert response.get_json() == {"error": "invalid request"}


def test_mail_api_endpoints_return_public_shapes_and_preserve_batch_order():
    repository = FakeMailRepository([_stored(1), _stored(2)])
    graph_client = FakeGraphClient(
        [
            {
                "id": "m",
                "subject": "Verification code: ABC123",
                "from": {"emailAddress": {"address": "hello@openai.com"}},
                "receivedDateTime": "2026-07-13T07:00:00Z",
                "bodyPreview": "",
                "body": {"contentType": "html", "content": "<b>ABC123</b>"},
            }
        ]
    )
    client = _app(repository, _cipher(1, 2), graph_client).test_client()
    headers = _login_headers(client)

    mail_response = client.post(
        "/api/accounts/1/mail",
        json={"folder": "inbox", "limit": 10},
        headers=headers,
    )
    code_response = client.post(
        "/api/accounts/1/verify-code",
        json={"maxResults": 10},
        headers=headers,
    )
    batch_response = client.post(
        "/api/accounts/check-bound", json={"ids": [2, 1]}, headers=headers
    )

    assert set(mail_response.get_json()[0]) == {
        "id",
        "subject",
        "sender",
        "date",
        "bodyPreview",
        "html",
    }
    assert code_response.get_json() == {"code": "ABC123"}
    assert [item["id"] for item in batch_response.get_json()] == [2, 1]


def test_batch_workers_receive_independent_flask_contexts():
    class ContextCheckingRepository(FakeMailRepository):
        def claim_refresh(self, account_id, lease, now, ttl):
            assert current_app.name == "mail_manager"
            return super().claim_refresh(account_id, lease, now, ttl)

    repository = ContextCheckingRepository([_stored(1), _stored(2)])
    client = _app(repository, _cipher(1, 2), FakeGraphClient()).test_client()
    headers = _login_headers(client)

    response = client.post(
        "/api/accounts/check-bound", json={"ids": [1, 2]}, headers=headers
    )

    assert response.status_code == 200


def test_schema_contains_bounded_refresh_lease_columns_and_index():
    from mail_manager import db

    schema = db.read_schema()

    assert "refresh_lease_token TEXT" in schema
    assert "refresh_lease_expires_at TIMESTAMP WITH TIME ZONE" in schema
    assert "accounts_refresh_lease_expires_at_idx" in schema


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Please use 123456 to continue", "123456"),
        ("expires in 10 minutes:123456", "123456"),
        ("Verification code: ABCDEF", None),
    ],
)
def test_code_extraction_requires_a_digit_and_supports_bounded_context(text, expected):
    from mail_manager.mail import _extract_contextual_code

    assert _extract_contextual_code(text) == expected


def test_missing_accounts_do_not_accumulate_coordinator_locks():
    repository = FakeMailRepository()
    coordinator = TokenCoordinator(
        repository, lambda: FakeGraphClient(), FakeCredentialCipher(), lambda: NOW
    )

    for account_id in range(1, 101):
        with pytest.raises(MailAccountNotFound):
            coordinator.access_token(account_id)

    assert not hasattr(coordinator, "_account_locks")


def test_random_unbound_checks_only_five_candidates_and_twenty_messages_each():
    accounts = [_stored(index) for index in range(1, 7)]
    repository = FakeMailRepository(accounts)
    graph_clients = []

    def graph_factory():
        client = FakeGraphClient()
        graph_clients.append(client)
        return client

    service = MailService(
        repository,
        TokenCoordinator(repository, graph_factory, _cipher(*range(1, 7)), lambda: NOW),
        graph_factory,
        lambda: NOW,
        monotonic=iter([0, 0, 1, 2, 3, 4]).__next__,
    )

    with pytest.raises(NoUnboundAccount):
        service.random_unbound()

    assert repository.candidate_limit == 5
    list_calls = [event for client in graph_clients for event in client.events if event[0] == "list"]
    assert len(list_calls) == 5
    assert all(event[-1] == 20 for event in list_calls)


def test_batch_uses_a_distinct_graph_client_for_every_refresh_and_list_operation():
    repository = FakeMailRepository([_stored(index) for index in range(1, 5)])
    graph_clients = []
    guard = Lock()

    def graph_factory():
        client = FakeGraphClient()
        with guard:
            graph_clients.append(client)
        return client

    service = MailService(
        repository,
        TokenCoordinator(repository, graph_factory, _cipher(*range(1, 5)), lambda: NOW),
        graph_factory,
        lambda: NOW,
    )

    service.check_bound_batch([1, 2, 3, 4])

    assert len(graph_clients) == 8
    assert len({id(client) for client in graph_clients}) == 8


def test_finalize_retries_twice_with_exponential_backoff_then_succeeds():
    repository = FakeMailRepository([_stored()])
    repository.finalize_errors = [
        RefreshPersistenceError("temporary"),
        RefreshPersistenceError("temporary"),
    ]
    delays = []
    coordinator = TokenCoordinator(
        repository,
        FakeGraphClient().spawn,
        _cipher(1),
        lambda: NOW,
        sleep=delays.append,
        lease_factory=lambda: "lease-one",
    )

    assert coordinator.access_token(1) == "access-client-1"
    assert delays == [0.01, 0.02]
    assert repository.accounts[1].refresh_lease_token is None


def test_postgres_finalize_retry_opens_and_closes_a_new_connection_each_time():
    class FailingConnection(RecordingConnection):
        def execute(self, statement, parameters=None):
            super().execute(statement, parameters)
            raise psycopg.OperationalError("database unavailable")

    claim = RecordingConnection(rows=[("client-1", "refresh-ciphertext-1")])
    first_failure = FailingConnection()
    second_failure = FailingConnection()
    success = RecordingConnection(rows=[(1,)])
    opened = [claim, first_failure, second_failure, success]
    source = iter(opened)
    delays = []
    coordinator = TokenCoordinator(
        PostgresMailRepository(lambda: next(source)),
        FakeGraphClient().spawn,
        _cipher(1),
        lambda: NOW,
        sleep=delays.append,
        lease_factory=lambda: "lease-one",
    )

    assert coordinator.access_token(1) == "access-client-1"
    assert delays == [0.01, 0.02]
    assert all(connection.closed for connection in opened)
    assert first_failure.rollbacks == second_failure.rollbacks == 1


def test_finalize_failure_after_three_attempts_returns_unavailable_and_keeps_lease():
    repository = FakeMailRepository([_stored()])
    repository.finalize_errors = [
        RefreshPersistenceError("temporary") for _ in range(3)
    ]
    coordinator = TokenCoordinator(
        repository,
        FakeGraphClient().spawn,
        _cipher(1),
        lambda: NOW,
        sleep=lambda _seconds: None,
        lease_factory=lambda: "lease-one",
    )

    with pytest.raises(graph.UpstreamUnavailable):
        coordinator.access_token(1)

    assert repository.accounts[1].refresh_lease_token == "lease-one"
    assert repository.accounts[1].refresh_token_ciphertext == "refresh-ciphertext-1"


def test_finalize_failure_api_is_503_and_never_returns_access_or_refresh_tokens():
    repository = FakeMailRepository([_stored()])
    repository.finalize_errors = [
        RefreshPersistenceError("database-secret") for _ in range(3)
    ]
    client = _app(repository, _cipher(1), FakeGraphClient()).test_client()
    headers = _login_headers(client)

    response = client.post(
        "/api/accounts/1/mail",
        json={"folder": "inbox", "limit": 10},
        headers=headers,
    )

    assert response.status_code == 503
    body = response.get_data(as_text=True)
    assert "access-client" not in body
    assert "rotated-refresh" not in body
    assert "database-secret" not in body
    assert repository.accounts[1].refresh_lease_token is not None


def test_crashed_lease_can_be_reclaimed_after_expiry_and_old_cas_cannot_overwrite():
    repository = FakeMailRepository([_stored()])

    repository.claim_refresh(1, "old-lease", NOW, timedelta(seconds=30))
    with pytest.raises(RefreshBusy):
        repository.claim_refresh(
            1, "early-lease", NOW + timedelta(seconds=29), timedelta(seconds=30)
        )
    repository.claim_refresh(
        1, "new-lease", NOW + timedelta(seconds=31), timedelta(seconds=30)
    )

    assert repository.finalize_refresh(
        1, "old-lease", "stale-ciphertext", NOW + timedelta(seconds=31)
    ) is False
    assert repository.finalize_refresh(
        1, "new-lease", "fresh-ciphertext", NOW + timedelta(seconds=31)
    ) is True
    assert repository.accounts[1].refresh_token_ciphertext == "fresh-ciphertext"


def test_busy_api_returns_bounded_retry_after_without_calling_graph():
    repository = FakeMailRepository(
        [
            _stored(
                refresh_lease_token="active-lease",
                refresh_lease_expires_at=NOW + timedelta(seconds=30),
            )
        ]
    )
    graph_client = FakeGraphClient()
    client = _app(repository, _cipher(1), graph_client).test_client()
    headers = _login_headers(client)

    response = client.post(
        "/api/accounts/1/mail",
        json={"folder": "inbox", "limit": 10},
        headers=headers,
    )

    assert response.status_code == 409
    assert 1 <= int(response.headers["Retry-After"]) <= 30
    assert graph_client.events == []


def test_random_deadline_stops_before_starting_the_next_candidate():
    repository = FakeMailRepository([_stored(1), _stored(2)])
    graph_clients = []

    def factory():
        client = FakeGraphClient()
        graph_clients.append(client)
        return client

    service = MailService(
        repository,
        TokenCoordinator(repository, factory, _cipher(1, 2), lambda: NOW),
        factory,
        lambda: NOW,
        monotonic=iter([0, 0, 31]).__next__,
    )

    with pytest.raises(graph.UpstreamUnavailable):
        service.random_unbound()

    assert len([client for client in graph_clients if client.events]) == 2


def test_random_candidate_query_is_deterministic_and_bounded():
    connection = RecordingConnection(
        rows=[(1, "one@example.test"), (2, "two@example.test")]
    )

    candidates = PostgresMailRepository(connection).random_unbound_candidates(5)

    assert [candidate.id for candidate in candidates] == [1, 2]
    statement, parameters = connection.executed[0]
    assert "ORDER BY id" in statement
    assert "RANDOM" not in statement
    assert parameters == (5,)
