from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import os
from threading import Barrier, Event
import uuid

from cryptography.fernet import Fernet
import psycopg
import pytest

from mail_manager import db
from mail_manager.accounts import AccountsService, PostgresAccountRepository
from mail_manager.auth import LoginOutcome, PostgresAuthRepository
from mail_manager.crypto import CredentialCipher
from mail_manager.graph import InvalidGrant
from mail_manager.mail import PostgresMailRepository, RefreshBusy, TokenCoordinator
from mail_manager.models import EncryptedAccount
from mail_manager.rentals import PostgresRentalRepository, hash_access_code


pytestmark = pytest.mark.postgres


@pytest.fixture
def database_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    return url


@pytest.fixture
def namespace(database_url):
    value = f"integration-{uuid.uuid4().hex}"
    yield value
    with psycopg.connect(database_url) as connection:
        account_rows = connection.execute(
            "SELECT id FROM accounts WHERE email_normalized LIKE %s",
            (f"{value}%",),
        ).fetchall()
        account_ids = [row[0] for row in account_rows]
        if account_ids:
            connection.execute(
                "DELETE FROM mailbox_rentals WHERE account_id = ANY(%s)",
                (account_ids,),
            )
        connection.execute(
            "DELETE FROM accounts WHERE email_normalized LIKE %s",
            (f"{value}%",),
        )
        connection.execute(
            "DELETE FROM auth_sessions WHERE session_hash LIKE %s",
            (f"{value}%",),
        )
        connection.execute(
            "DELETE FROM login_failures WHERE client_key LIKE %s",
            (f"{value}%",),
        )


@pytest.fixture(autouse=True)
def initialized_database(database_url):
    with psycopg.connect(database_url) as connection:
        with db.transaction(connection):
            db.execute_schema(connection, db.read_schema())


def test_schema_can_be_applied_twice(database_url):
    with psycopg.connect(database_url) as connection:
        with db.transaction(connection):
            db.execute_schema(connection, db.read_schema())
            db.execute_schema(connection, db.read_schema())

        tables = connection.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = current_schema()
              AND tablename = ANY(%s)
            """,
            ((
                "accounts",
                "auth_sessions",
                "login_failures",
                "mailbox_rentals",
                "pickup_sessions",
                "pickup_failures",
                "rental_events",
            ),),
        ).fetchall()

    assert {row[0] for row in tables} == {
        "accounts",
        "auth_sessions",
        "login_failures",
        "mailbox_rentals",
        "pickup_sessions",
        "pickup_failures",
        "rental_events",
    }


def test_account_upsert_round_trips_encrypted_credentials(database_url, namespace):
    cipher = CredentialCipher(Fernet.generate_key().decode("ascii"))
    with psycopg.connect(database_url) as connection:
        repository = PostgresAccountRepository(connection)
        service = AccountsService(repository, cipher)
        email = f"{namespace}@example.test"

        assert service.import_accounts(
            {
                "accounts": [
                    {
                        "email": email,
                        "password": "first-password",
                        "clientId": "first-client",
                        "refreshToken": "first-refresh-token",
                    }
                ]
            },
            10,
        ) == 1
        account_id, password_ciphertext, refresh_ciphertext = connection.execute(
            """
            SELECT id, password_ciphertext, refresh_token_ciphertext
            FROM accounts
            WHERE email_normalized = %s
            """,
            (email,),
        ).fetchone()
        connection.execute(
            "UPDATE accounts SET bound = TRUE, token_valid = FALSE WHERE id = %s",
            (account_id,),
        )
        connection.commit()

        assert "first-password" not in password_ciphertext
        assert "first-refresh-token" not in refresh_ciphertext

        assert service.import_accounts(
            {
                "accounts": [
                    {
                        "email": email.upper(),
                        "password": "replacement-password",
                        "clientId": "replacement-client",
                        "refreshToken": "replacement-refresh-token",
                    }
                ]
            },
            10,
        ) == 1

        revealed = service.reveal(account_id)
        stored = repository.list_accounts()

    assert revealed.email == email.upper()
    assert revealed.password == "replacement-password"
    assert revealed.client_id == "replacement-client"
    assert revealed.refresh_token == "replacement-refresh-token"
    assert len(stored) >= 1
    account = next(item for item in stored if item.id == account_id)
    assert account.bound is True
    assert account.token_valid is True


def test_session_creation_lookup_and_revocation(database_url, namespace):
    now = datetime.now(UTC)
    session_hash = f"{namespace}-session"
    csrf_hash = "a" * 64
    with psycopg.connect(database_url) as connection:
        repository = PostgresAuthRepository(connection)

        decision = repository.attempt_login(
            f"{namespace}-client",
            now,
            True,
            session_hash,
            csrf_hash,
            now + timedelta(hours=1),
        )
        session = repository.find_session(session_hash, now)
        repository.delete_session(session_hash)

        assert repository.find_session(session_hash, now) is None

    assert decision.outcome is LoginOutcome.AUTHENTICATED
    assert session is not None
    assert session.csrf_hash == csrf_hash


def test_fifth_login_failure_locks_client_in_one_repository(database_url, namespace):
    now = datetime.now(UTC)
    client_key = f"{namespace}-client"
    with psycopg.connect(database_url) as connection:
        repository = PostgresAuthRepository(connection)
        decisions = [
            repository.attempt_login(
                client_key,
                now + timedelta(seconds=attempt),
                False,
                f"{namespace}-unused-session-{attempt}",
                "b" * 64,
                now + timedelta(hours=1),
            )
            for attempt in range(5)
        ]
        failure_count, locked_until = connection.execute(
            """
            SELECT failure_count, locked_until
            FROM login_failures
            WHERE client_key = %s
            """,
            (client_key,),
        ).fetchone()

    assert [decision.outcome for decision in decisions[:4]] == [
        LoginOutcome.INVALID
    ] * 4
    assert decisions[4].outcome is LoginOutcome.LOCKED
    assert failure_count == 5
    assert locked_until is not None


def _create_mail_account(database_url, namespace, suffix, cipher):
    email = f"{namespace}-{suffix}@example.test"
    with psycopg.connect(database_url) as connection:
        service = AccountsService(PostgresAccountRepository(connection), cipher)
        assert service.import_accounts(
            {
                "accounts": [
                    {
                        "email": email,
                        "password": f"password-{suffix}",
                        "clientId": f"client-{suffix}",
                        "refreshToken": f"refresh-{suffix}",
                    }
                ]
            },
            1,
        ) == 1
        return connection.execute(
            "SELECT id FROM accounts WHERE email_normalized = %s",
            (email,),
        ).fetchone()[0]


def test_token_coordinator_lease_blocks_only_the_same_account(
    database_url, namespace
):
    cipher = CredentialCipher(Fernet.generate_key().decode("ascii"))
    first_id = _create_mail_account(database_url, namespace, "first", cipher)
    second_id = _create_mail_account(database_url, namespace, "second", cipher)
    entered = Event()
    release = Event()

    class BlockingGraph:
        def refresh(self, client_id, refresh_token):
            entered.set()
            assert release.wait(timeout=5)
            return f"access-{client_id}", f"rotated-{refresh_token}"

    repository = PostgresMailRepository(lambda: psycopg.connect(database_url))
    coordinator = TokenCoordinator(repository, BlockingGraph, cipher)
    with ThreadPoolExecutor(max_workers=2) as executor:
        active = executor.submit(coordinator.access_token, first_id)
        assert entered.wait(timeout=5)
        with pytest.raises(RefreshBusy):
            coordinator.access_token(first_id)
        release.set()
        assert active.result(timeout=5) == "access-client-first"

    barrier = Barrier(2)

    class ParallelGraph:
        def refresh(self, client_id, refresh_token):
            barrier.wait(timeout=5)
            return f"access-{client_id}", f"rotated-again-{refresh_token}"

    coordinator = TokenCoordinator(repository, ParallelGraph, cipher)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(coordinator.access_token, (first_id, second_id))
        )

    assert results == ["access-client-first", "access-client-second"]


def test_invalid_grant_marks_token_invalid_and_releases_lease(
    database_url, namespace
):
    cipher = CredentialCipher(Fernet.generate_key().decode("ascii"))
    account_id = _create_mail_account(database_url, namespace, "invalid", cipher)

    class InvalidGrantGraph:
        def refresh(self, _client_id, _refresh_token):
            raise InvalidGrant("refresh grant is invalid")

    repository = PostgresMailRepository(lambda: psycopg.connect(database_url))
    coordinator = TokenCoordinator(repository, InvalidGrantGraph, cipher)

    with pytest.raises(InvalidGrant):
        coordinator.access_token(account_id)

    with psycopg.connect(database_url) as connection:
        token_valid, lease, expires_at = connection.execute(
            """
            SELECT token_valid, refresh_lease_token, refresh_lease_expires_at
            FROM accounts
            WHERE id = %s
            """,
            (account_id,),
        ).fetchone()

    assert token_valid is False
    assert lease is None
    assert expires_at is None


def test_rental_repository_prevents_overlapping_active_rentals(
    database_url, namespace
):
    cipher = CredentialCipher(Fernet.generate_key().decode("ascii"))
    account_id = _create_mail_account(database_url, namespace, "rental", cipher)
    now = datetime.now(UTC)
    repository = PostgresRentalRepository(
        lambda: psycopg.connect(database_url)
    )

    first = repository.create_rental(
        account_id,
        hash_access_code(f"{namespace}-first"),
        now,
        now + timedelta(hours=1),
    )

    from mail_manager.rentals import NoRentableAccount

    with pytest.raises(NoRentableAccount):
        repository.create_rental(
            account_id,
            hash_access_code(f"{namespace}-second"),
            now,
            now + timedelta(hours=1),
        )

    assert first.account_id == account_id


def test_expired_rental_releases_account_for_reuse(database_url, namespace):
    cipher = CredentialCipher(Fernet.generate_key().decode("ascii"))
    account_id = _create_mail_account(database_url, namespace, "reuse", cipher)
    now = datetime.now(UTC)
    repository = PostgresRentalRepository(
        lambda: psycopg.connect(database_url)
    )
    repository.create_rental(
        account_id,
        hash_access_code(f"{namespace}-expired"),
        now - timedelta(hours=2),
        now - timedelta(hours=1),
    )

    replacement = repository.create_rental(
        account_id,
        hash_access_code(f"{namespace}-replacement"),
        now,
        now + timedelta(hours=1),
    )

    assert replacement.account_id == account_id
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            """
            SELECT ended_at FROM mailbox_rentals
            WHERE account_id = %s ORDER BY id
            """,
            (account_id,),
        ).fetchall()
    assert rows[0][0] is not None
    assert rows[1][0] is None


def test_batch_repository_rolls_back_every_account_on_database_error(
    database_url, namespace
):
    email = f"{namespace}-valid@example.test"
    valid = EncryptedAccount(
        email=email,
        email_normalized=email,
        password_ciphertext="ciphertext",
        client_id="client",
        refresh_token_ciphertext="refresh-ciphertext",
    )
    invalid = EncryptedAccount(
        email=None,
        email_normalized=f"{namespace}-invalid@example.test",
        password_ciphertext="ciphertext",
        client_id="client",
        refresh_token_ciphertext="refresh-ciphertext",
    )

    with psycopg.connect(database_url) as connection:
        repository = PostgresAccountRepository(connection)
        with pytest.raises(psycopg.errors.NotNullViolation):
            repository.upsert_accounts((valid, invalid))

        count = connection.execute(
            "SELECT COUNT(*) FROM accounts WHERE email_normalized LIKE %s",
            (f"{namespace}%",),
        ).fetchone()[0]

    assert count == 0
