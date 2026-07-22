from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from flask import request
from werkzeug.test import EnvironBuilder

from mail_manager import create_app, db
from mail_manager.accounts import (
    AccountsService,
    ExportLimitError,
    PostgresAccountRepository,
)
from mail_manager.config import Settings
from tests.fakes import (
    FakeAccountRepository,
    FakeAuthRepository,
    FakeCredentialCipher,
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


@pytest.fixture
def account_app():
    cipher = FakeCredentialCipher()
    cipher.add("password-one-ciphertext", "password-one")
    cipher.add("token-one-ciphertext", "token-one")
    cipher.add("password-two-ciphertext", "password-two")
    cipher.add("token-two-ciphertext", "token-two")
    repository = FakeAccountRepository(
        [
            FakeStoredAccount(
                id=1,
                email="first@example.test",
                email_normalized="first@example.test",
                password_ciphertext="password-one-ciphertext",
                client_id="client-one",
                refresh_token_ciphertext="token-one-ciphertext",
                last_refresh_time=NOW - timedelta(hours=1),
                bound=True,
                bound_at=NOW - timedelta(days=1),
                token_valid=True,
            ),
            FakeStoredAccount(
                id=2,
                email="second@example.test",
                email_normalized="second@example.test",
                password_ciphertext="password-two-ciphertext",
                client_id="client-two",
                refresh_token_ciphertext="token-two-ciphertext",
                token_valid=False,
            ),
        ]
    )
    app = create_app(
        {
            "TESTING": True,
            "AUTH_REPOSITORY": FakeAuthRepository(),
            "AUTH_CLOCK": MutableClock(NOW),
            "ACCOUNT_REPOSITORY": repository,
            "CREDENTIAL_CIPHER": cipher,
            "ACCOUNTS_CLOCK": MutableClock(NOW),
        },
        settings=Settings.from_mapping(VALID_MAPPING),
    )
    return app, repository, cipher


def _login_headers(client):
    response = client.post(
        "/api/login", json={"password": VALID_MAPPING["ACCESS_PASSWORD"]}
    )
    csrf_token = response.get_json()["csrfToken"]
    return {
        "Origin": VALID_MAPPING["PUBLIC_ORIGIN"],
        "X-CSRF-Token": csrf_token,
    }


def _account(email="new@example.test", **overrides):
    account = {
        "email": email,
        "password": "mailbox-password",
        "clientId": "client-id",
        "refreshToken": "refresh-token",
    }
    account.update(overrides)
    return account


def test_authenticated_account_list_returns_metadata_only(account_app):
    app, _, _ = account_app
    client = app.test_client()
    _login_headers(client)

    response = client.get("/api/accounts")

    assert response.status_code == 200
    assert response.get_json() == [
        {
            "id": 1,
            "email": "first@example.test",
            "lastRefreshTime": "2026-07-13T07:00:00Z",
            "bound": True,
            "boundAt": "2026-07-12T08:00:00Z",
            "tokenValid": True,
            "rentable": True,
            "rented": False,
        },
        {
            "id": 2,
            "email": "second@example.test",
            "lastRefreshTime": None,
            "bound": False,
            "boundAt": None,
            "tokenValid": False,
            "rentable": True,
            "rented": False,
        },
    ]
    rendered = response.get_data(as_text=True)
    for secret in ("password", "client-one", "client-two", "token-one", "token-two"):
        assert secret not in rendered


def test_import_normalizes_email_encrypts_secrets_and_stores_only_ciphertext(
    account_app,
):
    app, repository, cipher = account_app
    client = app.test_client()
    headers = _login_headers(client)

    response = client.post(
        "/api/accounts/import",
        json={"accounts": [_account("  New.Person@Example.Test  ")]},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {"imported": 1}
    stored = repository.accounts[3]
    assert stored.email == "New.Person@Example.Test"
    assert stored.email_normalized == "new.person@example.test"
    assert stored.password_ciphertext != "mailbox-password"
    assert stored.refresh_token_ciphertext != "refresh-token"
    assert cipher.encrypt_calls == ["mailbox-password", "refresh-token"]
    assert "mailbox-password" not in repr(repository.accounts)
    assert "refresh-token" not in repr(repository.accounts)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"accounts": []},
        {"accounts": "not-a-list"},
        {"accounts": ["not-an-object"]},
        {"accounts": [_account(email=" ")]},
        {"accounts": [_account(email=1)]},
        {"accounts": [_account(email="e" * 321)]},
        {"accounts": [_account(password=1)]},
        {"accounts": [_account(password="p" * 1025)]},
        {"accounts": [_account(clientId=" ")]},
        {"accounts": [_account(clientId="c" * 257)]},
        {"accounts": [_account(refreshToken=" ")]},
        {"accounts": [_account(refreshToken="t" * 8193)]},
    ],
)
def test_import_rejects_invalid_shapes_and_field_limits(account_app, payload):
    app, repository, cipher = account_app
    client = app.test_client()
    headers = _login_headers(client)

    response = client.post("/api/accounts/import", json=payload, headers=headers)

    assert response.status_code == 400
    assert repository.import_calls == []
    assert cipher.encrypt_calls == []


def test_import_rejects_more_than_configured_limit(account_app):
    app, repository, _ = account_app
    app.config["IMPORT_LIMIT"] = 2
    client = app.test_client()
    headers = _login_headers(client)

    response = client.post(
        "/api/accounts/import",
        json={
            "accounts": [
                _account("one@example.test"),
                _account("two@example.test"),
                _account("three@example.test"),
            ]
        },
        headers=headers,
    )

    assert response.status_code == 413
    assert repository.import_calls == []


def test_import_crypto_failure_never_starts_repository_write(account_app):
    app, repository, cipher = account_app
    cipher.fail_encrypt_on = "failing-secret"
    client = app.test_client()
    headers = _login_headers(client)

    response = client.post(
        "/api/accounts/import",
        json={
            "accounts": [
                _account("one@example.test"),
                _account("two@example.test", password="failing-secret"),
            ]
        },
        headers=headers,
    )

    assert response.status_code == 500
    assert repository.import_calls == []
    assert "failing-secret" not in response.get_data(as_text=True)


def test_import_repository_failure_rolls_back_whole_batch(account_app):
    app, repository, _ = account_app
    repository.fail_import_at = 1
    original = repository.accounts.copy()
    client = app.test_client()
    headers = _login_headers(client)

    response = client.post(
        "/api/accounts/import",
        json={
            "accounts": [
                _account("one@example.test"),
                _account("two-new@example.test"),
            ]
        },
        headers=headers,
    )

    assert response.status_code == 500
    assert repository.accounts == original
    assert "mailbox-password" not in response.get_data(as_text=True)


def test_duplicate_import_restores_token_valid_and_preserves_binding(account_app):
    app, repository, _ = account_app
    original_bound_at = repository.accounts[1].bound_at
    repository.accounts[1].token_valid = False
    client = app.test_client()
    headers = _login_headers(client)

    response = client.post(
        "/api/accounts/import",
        json={
            "accounts": [
                _account(
                    " FIRST@example.test ",
                    password="replacement-password",
                    clientId="replacement-client",
                    refreshToken="replacement-token",
                )
            ]
        },
        headers=headers,
    )

    assert response.status_code == 200
    stored = repository.accounts[1]
    assert stored.bound is True
    assert stored.bound_at == original_bound_at
    assert stored.token_valid is True
    assert stored.client_id == "replacement-client"


def test_reveal_is_post_only_and_disables_caching(account_app):
    app, _, cipher = account_app
    client = app.test_client()
    headers = _login_headers(client)

    get_response = client.get("/api/accounts/1/reveal")
    response = client.post("/api/accounts/1/reveal", headers=headers)

    assert get_response.status_code == 405
    assert response.status_code == 200
    assert response.get_json() == {
        "email": "first@example.test",
        "password": "password-one",
        "clientId": "client-one",
        "refreshToken": "token-one",
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert cipher.decrypt_calls == ["password-one-ciphertext", "token-one-ciphertext"]


def test_reveal_unknown_account_returns_404(account_app):
    app, _, _ = account_app
    client = app.test_client()
    headers = _login_headers(client)

    response = client.post("/api/accounts/999/reveal", headers=headers)

    assert response.status_code == 404


def test_export_is_lazy_streamed_ndjson_and_decrypts_one_record_at_a_time(
    account_app,
):
    app, repository, cipher = account_app
    client = app.test_client()
    headers = _login_headers(client)

    response = client.post("/api/accounts/export", headers=headers)

    assert response.status_code == 200
    assert response.is_streamed is True
    assert response.mimetype == "application/x-ndjson"
    assert response.mimetype_params["charset"].lower() == "utf-8"
    assert response.headers["Content-Disposition"] == (
        "attachment; filename=accounts.jsonl"
    )
    assert response.headers["Cache-Control"] == "no-store"
    assert repository.export_events == [("preflight", 10000, 100)]
    assert cipher.decrypt_calls == []

    chunks = iter(response.response)
    assert next(chunks) == b""
    first = json.loads(next(chunks))
    assert first == {
        "email": "first@example.test",
        "password": "password-one",
        "clientId": "client-one",
        "refreshToken": "token-one",
    }
    assert repository.export_events == [
        ("preflight", 10000, 100),
        ("yield", 1),
    ]
    assert cipher.decrypt_calls == ["password-one-ciphertext", "token-one-ciphertext"]

    second = json.loads(next(chunks))
    assert second["email"] == "second@example.test"
    assert repository.export_events[-1] == ("yield", 2)
    assert cipher.decrypt_calls[-2:] == [
        "password-two-ciphertext",
        "token-two-ciphertext",
    ]
    with pytest.raises(StopIteration):
        next(chunks)


def test_export_ndjson_round_trips_delimiters_newlines_unicode_and_formulas(
    account_app,
):
    app, repository, cipher = account_app
    dangerous = {
        "email": "=email\r\n----雪@example.test",
        "password": "+password\n----\rsecret",
        "clientId": "-client\r----\nvalue",
        "refreshToken": "@token\r\n----雪",
    }
    repository.accounts[1].email = dangerous["email"]
    repository.accounts[1].client_id = dangerous["clientId"]
    del repository.accounts[2]
    cipher.add("password-one-ciphertext", dangerous["password"])
    cipher.add("token-one-ciphertext", dangerous["refreshToken"])
    client = app.test_client()
    headers = _login_headers(client)

    response = client.post("/api/accounts/export", headers=headers)
    records = [
        json.loads(line) for line in response.get_data(as_text=True).splitlines()
    ]

    assert records == [dangerous]


def test_export_over_limit_returns_413_without_starting_decryption(account_app):
    app, repository, cipher = account_app
    app.config["EXPORT_LIMIT"] = 1
    client = app.test_client()
    headers = _login_headers(client)

    response = client.post("/api/accounts/export", headers=headers)

    assert response.status_code == 413
    assert repository.export_events == [("preflight", 1, 100)]
    assert cipher.decrypt_calls == []


def _open_wsgi_export(monkeypatch):
    cipher = FakeCredentialCipher()
    cipher.add("password-one", "decrypted-password-one")
    cipher.add("token-one", "decrypted-token-one")
    cipher.add("password-two", "decrypted-password-two")
    cipher.add("token-two", "decrypted-token-two")
    regular_connection = RecordingConnection()
    export_connection = RecordingConnection(
        cursor_rows=[
            ("one@example.test", "password-one", "client-one", "token-one", 2),
            ("two@example.test", "password-two", "client-two", "token-two", 2),
        ]
    )
    connections = iter((regular_connection, export_connection))
    monkeypatch.setattr(db, "connect_database", lambda _url: next(connections))
    app = create_app(
        {
            "TESTING": True,
            "AUTH_REPOSITORY": FakeAuthRepository(),
            "AUTH_CLOCK": MutableClock(NOW),
            "CREDENTIAL_CIPHER": cipher,
        },
        settings=Settings.from_mapping(VALID_MAPPING),
    )

    @app.before_request
    def open_regular_request_connection():
        if request.path == "/api/accounts/export":
            db.get_db()

    client = app.test_client()
    headers = _login_headers(client)
    cookie = client.get_cookie("mail_session")
    builder = EnvironBuilder(
        path="/api/accounts/export",
        method="POST",
        headers={**headers, "Cookie": f"mail_session={cookie.value}"},
    )
    environ = builder.get_environ()
    response_state = {}

    def start_response(status, response_headers, _exc_info=None):
        response_state["status"] = status
        response_state["headers"] = response_headers

    iterable = app.wsgi_app(environ, start_response)
    builder.close()
    return iterable, response_state, regular_connection, export_connection


@pytest.mark.parametrize("consume_all", [True, False])
def test_wsgi_export_owns_connection_past_request_teardown(monkeypatch, consume_all):
    iterable, response_state, regular, export = _open_wsgi_export(monkeypatch)

    assert response_state["status"].startswith("200")
    assert regular.closed is True
    assert export.closed is False
    if consume_all:
        records = [
            json.loads(line) for line in b"".join(iterable).decode("utf-8").splitlines()
        ]
        assert [record["email"] for record in records] == [
            "one@example.test",
            "two@example.test",
        ]
    else:
        iterable.close()

    assert export.closed is True


def test_patch_allows_only_boolean_bound(account_app):
    app, repository, _ = account_app
    client = app.test_client()
    headers = _login_headers(client)

    response = client.patch("/api/accounts/2", json={"bound": True}, headers=headers)

    assert response.status_code == 200
    assert response.get_json()["bound"] is True
    assert response.get_json()["boundAt"] == "2026-07-13T08:00:00Z"
    assert repository.accounts[2].bound is True


def test_service_normalizes_naive_bound_clock_to_utc():
    repository = FakeAccountRepository(
        [
            FakeStoredAccount(
                id=1,
                email="person@example.test",
                email_normalized="person@example.test",
                password_ciphertext="password-ciphertext",
                client_id="client-id",
                refresh_token_ciphertext="token-ciphertext",
            )
        ]
    )
    naive_now = datetime(2026, 7, 13, 8, 0)
    service = AccountsService(repository, FakeCredentialCipher(), lambda: naive_now)

    updated = service.update_bound(1, {"bound": True})

    assert updated.bound_at == NOW
    assert repository.accounts[1].bound_at.tzinfo is UTC


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"bound": "yes"},
        {"tokenValid": True},
        {"bound": True, "tokenValid": True},
        {"bound": True, "unexpected": False},
    ],
)
def test_patch_rejects_client_controlled_token_state_and_unknown_fields(
    account_app, payload
):
    app, repository, _ = account_app
    client = app.test_client()
    headers = _login_headers(client)
    original = repository.accounts[1]

    response = client.patch("/api/accounts/1", json=payload, headers=headers)

    assert response.status_code == 400
    assert repository.accounts[1] == original


def test_patch_and_delete_unknown_account_return_404(account_app):
    app, _, _ = account_app
    client = app.test_client()
    headers = _login_headers(client)

    patched = client.patch("/api/accounts/999", json={"bound": True}, headers=headers)
    deleted = client.delete("/api/accounts/999", headers=headers)

    assert patched.status_code == 404
    assert deleted.status_code == 404


def test_delete_single_account(account_app):
    app, repository, _ = account_app
    client = app.test_client()
    headers = _login_headers(client)

    response = client.delete("/api/accounts/1", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"deleted": True}
    assert 1 not in repository.accounts


@pytest.mark.parametrize(
    "payload, status",
    [
        (None, 400),
        ({}, 400),
        ({"ids": []}, 400),
        ({"ids": [1, 1]}, 400),
        ({"ids": [True]}, 400),
        ({"ids": [0]}, 400),
        ({"ids": list(range(1, 1002))}, 413),
    ],
)
def test_batch_delete_validates_unique_positive_integer_ids(
    account_app, payload, status
):
    app, repository, _ = account_app
    client = app.test_client()
    headers = _login_headers(client)
    original = repository.accounts.copy()

    response = client.post("/api/accounts/delete-batch", json=payload, headers=headers)

    assert response.status_code == status
    assert repository.accounts == original


def test_batch_delete_is_atomic_and_unknown_ids_return_404(account_app):
    app, repository, _ = account_app
    client = app.test_client()
    headers = _login_headers(client)

    failed = client.post(
        "/api/accounts/delete-batch", json={"ids": [1, 999]}, headers=headers
    )
    succeeded = client.post(
        "/api/accounts/delete-batch", json={"ids": [1, 2]}, headers=headers
    )

    assert failed.status_code == 404
    assert succeeded.status_code == 200
    assert succeeded.get_json() == {"deleted": 2}
    assert repository.accounts == {}


def test_postgres_list_maps_rows_and_uses_no_secret_columns():
    connection = RecordingConnection(
        rows=[(1, "person@example.test", NOW, True, NOW, False)]
    )
    repository = PostgresAccountRepository(connection)

    accounts = repository.list_accounts()

    assert accounts[0].last_refresh_time.tzinfo is UTC
    assert accounts[0].bound_at.tzinfo is UTC
    statement, parameters = connection.executed[0]
    normalized = " ".join(statement.lower().split())
    assert "password_ciphertext" not in normalized
    assert "refresh_token_ciphertext" not in normalized
    assert "client_id" not in normalized
    assert parameters is None
    assert connection.commits == 1


def test_postgres_import_is_one_parameterized_transaction():
    connection = RecordingConnection()
    repository = PostgresAccountRepository(connection)
    accounts = [
        SimpleNamespace(
            email="one@example.test",
            email_normalized="one@example.test",
            password_ciphertext="password-ciphertext-one",
            client_id="client-one",
            refresh_token_ciphertext="token-ciphertext-one",
        ),
        SimpleNamespace(
            email="two@example.test",
            email_normalized="two@example.test",
            password_ciphertext="password-ciphertext-two",
            client_id="client-two",
            refresh_token_ciphertext="token-ciphertext-two",
        ),
    ]

    imported = repository.upsert_accounts(accounts)

    assert imported == 2
    assert len(connection.executed) == 2
    assert connection.commits == 1
    assert connection.rollbacks == 0
    for statement, parameters in connection.executed:
        normalized = " ".join(statement.lower().split())
        assert "on conflict (email_normalized) do update" in normalized
        assert "token_valid = true" in normalized
        assert "bound =" not in normalized
        assert "password-ciphertext" not in statement
        assert "token-ciphertext" not in statement
        assert len(parameters) == 5


def test_postgres_import_rolls_back_all_rows_on_second_failure():
    connection = RecordingConnection(fail_at=1)
    repository = PostgresAccountRepository(connection)
    account = SimpleNamespace(
        email="person@example.test",
        email_normalized="person@example.test",
        password_ciphertext="password-ciphertext",
        client_id="client-id",
        refresh_token_ciphertext="token-ciphertext",
    )

    with pytest.raises(RuntimeError, match="database execution failed"):
        repository.upsert_accounts([account, account])

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_postgres_export_preflights_and_streams_in_one_named_cursor_snapshot():
    encrypted_rows = [
        ("one@example.test", "password-one", "client-one", "token-one", 2),
        ("two@example.test", "password-two", "client-two", "token-two", 2),
    ]
    connection = RecordingConnection(cursor_rows=encrypted_rows)
    repository = PostgresAccountRepository(export_connection_source=connection)

    records = repository.iter_encrypted_credentials(limit=10, batch_size=1)
    assert len(connection.cursors) == 1
    cursor = connection.cursors[0]
    assert cursor.fetchone_calls == 1
    statement, parameters = cursor.executed[0]
    normalized = " ".join(statement.lower().split())
    assert "count(*) over() as total_count" in normalized
    assert "limit %s" in normalized
    assert parameters == (11,)
    assert connection.executed == []

    first = next(records)
    assert first.email == "one@example.test"
    assert cursor.name is not None
    assert cursor.fetchmany_calls == []
    assert "password-one" not in cursor.executed[0][0]

    assert [record.email for record in records] == ["two@example.test"]
    assert cursor.fetchmany_calls == [1, 1]
    assert cursor.closed is True
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closes == 1


def test_postgres_export_limit_failure_rolls_back_before_returning_stream():
    connection = RecordingConnection(
        cursor_rows=[("one@example.test", "password-one", "client-one", "token-one", 2)]
    )
    repository = PostgresAccountRepository(export_connection_source=connection)

    with pytest.raises(ExportLimitError):
        repository.iter_encrypted_credentials(limit=1)

    cursor = connection.cursors[0]
    assert cursor.executed[0][1] == (2,)
    assert cursor.closed is True
    assert cursor.fetchmany_calls == []
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closes == 1


def test_postgres_export_client_disconnect_closes_cursor_and_transaction():
    connection = RecordingConnection(
        cursor_rows=[
            ("one@example.test", "password-one", "client-one", "token-one", 2),
            ("two@example.test", "password-two", "client-two", "token-two", 2),
        ]
    )
    records = PostgresAccountRepository(
        export_connection_source=connection
    ).iter_encrypted_credentials(limit=10)

    assert next(records).email == "one@example.test"
    records.close()

    assert connection.cursors[0].closed is True
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closes == 1


def test_service_export_close_before_first_record_releases_preflight_cursor():
    connection = RecordingConnection(
        cursor_rows=[("one@example.test", "password-one", "client-one", "token-one", 1)]
    )
    repository = PostgresAccountRepository(export_connection_source=connection)
    records = AccountsService(repository, FakeCredentialCipher()).export(limit=10)

    records.close()

    assert connection.cursors[0].closed is True
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closes == 1


def test_recording_cursor_rejects_fetch_after_connection_close():
    connection = RecordingConnection(cursor_rows=[("row",)])
    cursor = connection.cursor(name="export")

    connection.close()

    with pytest.raises(RuntimeError, match="connection is closed"):
        cursor.fetchmany(1)


def test_postgres_credentials_patch_and_deletes_are_parameterized():
    credentials_connection = RecordingConnection(
        rows=[
            (
                "person@example.test",
                "password-ciphertext",
                "client-id",
                "token-ciphertext",
            )
        ]
    )
    credentials_repository = PostgresAccountRepository(credentials_connection)
    credentials = credentials_repository.get_credentials(7)

    assert credentials.password_ciphertext == "password-ciphertext"
    assert credentials.refresh_token_ciphertext == "token-ciphertext"
    assert credentials_connection.executed[0][1] == (7,)
    assert "7" not in credentials_connection.executed[0][0]

    update_connection = RecordingConnection(
        rows=[(7, "person@example.test", None, True, NOW, True)]
    )
    updated = PostgresAccountRepository(update_connection).update_flags(
        7, {"bound": True}, NOW
    )
    assert updated.bound is True
    assert update_connection.executed[0][1] == (True, NOW, 7)

    delete_connection = RecordingConnection(rows=[(7,)])
    assert PostgresAccountRepository(delete_connection).delete_account(7) is True
    assert delete_connection.executed[0][1] == (7,)

    batch_connection = RecordingConnection(rows=[(7,), (8,)])
    assert PostgresAccountRepository(batch_connection).delete_accounts([7, 8]) is True
    assert batch_connection.executed[0][1] == ([7, 8],)


def test_postgres_update_rejects_dynamic_columns_outside_fixed_allowlist():
    connection = RecordingConnection()
    repository = PostgresAccountRepository(connection)

    with pytest.raises(ValueError, match="unsupported account field"):
        repository.update_flags(1, {"bound = TRUE; DROP TABLE accounts": True}, NOW)

    assert connection.executed == []
