from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
import re
from typing import get_type_hints

import pytest

from mail_manager import create_app
from mail_manager.config import Settings
from mail_manager import db
from mail_manager.models import (
    AccountMetadata,
    MailManagerError,
    MailMessage,
    RevealedCredentials,
)


VALID_MAPPING = {
    "DATABASE_URL": "postgresql://mail:secret@db.example.test/mail_manager",
    "ACCESS_PASSWORD": "correct-horse-battery-staple",
    "SESSION_SECRET": "s" * 32,
    "CREDENTIALS_ENCRYPTION_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    "PUBLIC_ORIGIN": "https://mail.example.test",
}


class FakeConnection:
    def __init__(
        self,
        *,
        execute_error=None,
        commit_error=None,
        rollback_error=None,
        close_error=None,
    ):
        self.execute_error = execute_error
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.close_error = close_error
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def execute(self, statement):
        self.executed.append(statement)
        if self.execute_error is not None:
            raise self.execute_error

    def commit(self):
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_error is not None:
            raise self.rollback_error

    def close(self):
        self.closes += 1
        if self.close_error is not None:
            raise self.close_error


def _make_app():
    return create_app({"TESTING": True}, settings=Settings.from_mapping(VALID_MAPPING))


def _normalized_sql():
    return " ".join(db.read_schema().lower().split())


def test_schema_defines_idempotent_database_lifecycle():
    schema = db.read_schema()
    normalized = _normalized_sql()

    assert "create table if not exists accounts" in normalized
    assert "create table if not exists auth_sessions" in normalized
    assert "create table if not exists login_failures" in normalized
    assert "id bigserial primary key" in normalized
    assert "email text not null" in normalized
    assert "email_normalized text not null unique" in normalized
    assert "password_ciphertext text not null" in normalized
    assert "client_id text not null" in normalized
    assert "refresh_token_ciphertext text not null" in normalized
    assert "last_refresh_time timestamp with time zone" in normalized
    assert "bound boolean not null default false" in normalized
    assert "bound_at timestamp with time zone" in normalized
    assert "token_valid boolean not null default true" in normalized
    assert "session_hash text primary key" in normalized
    assert "csrf_hash char(64) not null" in normalized
    assert "expires_at timestamp with time zone not null" in normalized
    assert "client_key text primary key" in normalized
    assert "failure_count integer not null default 0" in normalized
    assert "check (failure_count >= 0)" in normalized
    assert (
        "create index if not exists login_failures_updated_at_idx "
        "on login_failures (updated_at)"
    ) in normalized

    creates = re.findall(r"create\s+(?:unique\s+)?(?:table|index)\s+", schema, re.I)
    idempotent_creates = re.findall(
        r"create\s+(?:unique\s+)?(?:table|index)\s+if\s+not\s+exists\s+",
        schema,
        re.I,
    )
    assert len(idempotent_creates) == len(creates)


def test_domain_models_are_frozen_value_objects():
    timestamp = datetime(2026, 7, 13, tzinfo=UTC)
    account = AccountMetadata(
        id=1,
        email="person@example.test",
        last_refresh_time=None,
        bound=False,
        bound_at=None,
        token_valid=True,
    )
    credentials = RevealedCredentials(
        email=account.email,
        password="secret",
        client_id="client-id",
        refresh_token="refresh-token",
    )
    message = MailMessage(
        id="message-id",
        subject="Subject",
        sender="sender@example.test",
        received_at=timestamp,
        body_preview="Preview",
    )

    assert credentials.email == account.email
    assert credentials.refresh_token == "refresh-token"
    assert get_type_hints(RevealedCredentials)["refresh_token"] is str
    assert message.received_at == timestamp
    assert issubclass(MailManagerError, Exception)
    with pytest.raises(FrozenInstanceError):
        account.email = "changed@example.test"


def test_connect_database_delegates_to_psycopg(monkeypatch):
    connection = FakeConnection()
    calls = []
    monkeypatch.setattr(db.psycopg, "connect", lambda url: calls.append(url) or connection)

    assert db.connect_database(VALID_MAPPING["DATABASE_URL"]) is connection
    assert calls == [VALID_MAPPING["DATABASE_URL"]]


def test_transaction_commits_on_success():
    connection = FakeConnection()

    with db.transaction(connection) as active_connection:
        assert active_connection is connection

    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_transaction_rolls_back_on_failure():
    connection = FakeConnection()

    with pytest.raises(RuntimeError, match="transaction failed"):
        with db.transaction(connection):
            raise RuntimeError("transaction failed")

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_transaction_preserves_body_error_when_rollback_fails():
    rollback_error = RuntimeError("rollback cleanup failed")
    connection = FakeConnection(rollback_error=rollback_error)
    body_error = ValueError("transaction body failed")

    with pytest.raises(ValueError) as raised:
        with db.transaction(connection):
            raise body_error

    assert raised.value is body_error
    assert any(
        "rollback" in note and "rollback cleanup failed" in note
        for note in raised.value.__notes__
    )


def test_transaction_preserves_commit_error_when_rollback_fails():
    commit_error = RuntimeError("commit failed")
    rollback_error = RuntimeError("rollback cleanup failed")
    connection = FakeConnection(
        commit_error=commit_error,
        rollback_error=rollback_error,
    )

    with pytest.raises(RuntimeError) as raised:
        with db.transaction(connection):
            pass

    assert raised.value is commit_error
    assert any(
        "rollback" in note and "rollback cleanup failed" in note
        for note in raised.value.__notes__
    )


def test_execute_schema_executes_statement():
    connection = FakeConnection()

    db.execute_schema(connection, "SELECT 'schema';")

    assert connection.executed == ["SELECT 'schema';"]
    assert connection.commits == 0
    assert connection.rollbacks == 0


def test_schema_transaction_rolls_back_execution_failure():
    connection = FakeConnection(execute_error=RuntimeError("execute failed"))

    with pytest.raises(RuntimeError, match="execute failed"):
        with db.transaction(connection):
            db.execute_schema(connection, "invalid schema")

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_get_db_reuses_connection_and_teardown_closes(monkeypatch):
    app = _make_app()
    connection = FakeConnection()
    calls = []
    monkeypatch.setattr(
        db,
        "connect_database",
        lambda url: calls.append(url) or connection,
    )

    with app.app_context():
        assert db.get_db() is connection
        assert db.get_db() is connection

    assert calls == [VALID_MAPPING["DATABASE_URL"]]
    assert connection.closes == 1


def test_teardown_rolls_back_and_closes_after_exception(monkeypatch):
    app = _make_app()
    connection = FakeConnection()
    monkeypatch.setattr(db, "connect_database", lambda _url: connection)

    with pytest.raises(RuntimeError, match="request failed"):
        with app.app_context():
            db.get_db()
            raise RuntimeError("request failed")

    assert connection.rollbacks == 1
    assert connection.closes == 1


def test_close_db_preserves_request_error_and_notes_cleanup_failures(monkeypatch):
    app = _make_app()
    connection = FakeConnection(
        rollback_error=RuntimeError("rollback cleanup failed"),
        close_error=RuntimeError("close cleanup failed"),
    )
    request_error = ValueError("request failed")
    monkeypatch.setattr(db, "connect_database", lambda _url: connection)

    with app.app_context():
        db.get_db()
        db.close_db(request_error)

    assert connection.rollbacks == 1
    assert connection.closes == 1
    assert any(
        "rollback" in note and "rollback cleanup failed" in note
        for note in request_error.__notes__
    )
    assert any(
        "close" in note and "close cleanup failed" in note
        for note in request_error.__notes__
    )


def test_close_db_propagates_close_failure_without_request_error(monkeypatch):
    app = _make_app()
    close_error = RuntimeError("close cleanup failed")
    connection = FakeConnection(close_error=close_error)
    monkeypatch.setattr(db, "connect_database", lambda _url: connection)

    with app.app_context():
        db.get_db()
        with pytest.raises(RuntimeError) as raised:
            db.close_db()

    assert raised.value is close_error
    assert connection.closes == 1


def test_init_db_cli_executes_packaged_schema_and_closes(monkeypatch):
    app = _make_app()
    connection = FakeConnection()
    calls = []
    monkeypatch.setattr(db, "connect_database", lambda url: connection)
    monkeypatch.setattr(db, "read_schema", lambda: "schema statement")
    monkeypatch.setattr(
        db,
        "execute_schema",
        lambda active, schema: calls.append((active, schema)),
    )

    result = app.test_cli_runner().invoke(args=["init-db"])

    assert result.exit_code == 0
    assert calls == [(connection, "schema statement")]
    assert connection.closes == 1


def test_init_db_cli_closes_connection_after_failure(monkeypatch):
    app = _make_app()
    connection = FakeConnection()
    monkeypatch.setattr(db, "connect_database", lambda _url: connection)
    monkeypatch.setattr(db, "read_schema", lambda: "schema statement")

    def fail_execute(_connection, _schema):
        raise RuntimeError("schema failed")

    monkeypatch.setattr(db, "execute_schema", fail_execute)

    result = app.test_cli_runner().invoke(args=["init-db"])

    assert result.exit_code != 0
    assert connection.rollbacks == 1
    assert connection.closes == 1
