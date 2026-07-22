from dataclasses import dataclass, replace
from datetime import timedelta
from threading import Lock

from mail_manager.models import AccountMetadata


class MutableClock:
    def __init__(self, current):
        self.current = current

    def __call__(self):
        return self.current

    def advance(self, **kwargs):
        self.current += timedelta(**kwargs)


class FakeAuthRepository:
    def __init__(self):
        self.sessions = {}
        self.failures = {}
        self.failure_updated_at = {}
        self.locked_until_by_client = {}
        self.deleted_sessions = []
        self.decision_hook = None
        self._client_locks = {}
        self._locks_guard = Lock()
        self._state_guard = Lock()

    def _client_lock(self, client_key):
        with self._locks_guard:
            return self._client_locks.setdefault(client_key, Lock())

    def _prune_failures(self, now):
        cutoff = now - timedelta(minutes=15)
        for client_key, updated_at in tuple(self.failure_updated_at.items()):
            locked_until = self.locked_until_by_client.get(client_key)
            if updated_at < cutoff and (locked_until is None or locked_until <= now):
                self.failures.pop(client_key, None)
                self.failure_updated_at.pop(client_key, None)
                self.locked_until_by_client.pop(client_key, None)

    def attempt_login(
        self,
        client_key,
        now,
        password_valid,
        session_hash,
        csrf_hash,
        expires_at,
    ):
        from mail_manager.auth import LoginDecision, LoginOutcome

        with self._client_lock(client_key):
            with self._state_guard:
                self._prune_failures(now)
                locked_until = self.locked_until_by_client.get(client_key)
                if locked_until is not None and locked_until > now:
                    decision = LoginDecision(LoginOutcome.LOCKED, locked_until)
                elif not password_valid:
                    failure_count = self.failures.get(client_key, 0) + 1
                    self.failures[client_key] = failure_count
                    self.failure_updated_at[client_key] = now
                    if failure_count >= 5:
                        locked_until = now + timedelta(minutes=15)
                        self.locked_until_by_client[client_key] = locked_until
                        decision = LoginDecision(LoginOutcome.LOCKED, locked_until)
                    else:
                        self.locked_until_by_client.pop(client_key, None)
                        decision = LoginDecision(LoginOutcome.INVALID)
                else:
                    self.failures.pop(client_key, None)
                    self.failure_updated_at.pop(client_key, None)
                    self.locked_until_by_client.pop(client_key, None)
                    self.sessions[session_hash] = (csrf_hash, expires_at)
                    decision = LoginDecision(LoginOutcome.AUTHENTICATED)

            hook = self.decision_hook
            if hook is not None:
                hook(client_key, decision)
            return decision

    def find_session(self, session_hash, now):
        stored = self.sessions.get(session_hash)
        if stored is None:
            return None
        csrf_hash, expires_at = stored
        if expires_at <= now:
            self.delete_session(session_hash)
            return None

        from mail_manager.auth import AuthSession

        return AuthSession(
            session_hash=session_hash,
            csrf_hash=csrf_hash,
            expires_at=expires_at,
        )

    def delete_session(self, session_hash):
        self.deleted_sessions.append(session_hash)
        self.sessions.pop(session_hash, None)


@dataclass
class FakeStoredAccount:
    id: int
    email: str
    email_normalized: str
    password_ciphertext: str
    client_id: str
    refresh_token_ciphertext: str
    last_refresh_time: object = None
    bound: bool = False
    bound_at: object = None
    token_valid: bool = True
    refresh_lease_token: str | None = None
    refresh_lease_expires_at: object = None


class FakeAccountRepository:
    def __init__(self, accounts=()):
        self.accounts = {account.id: account for account in accounts}
        self.import_calls = []
        self.fail_import_at = None
        self.export_events = []

    def list_accounts(self):
        return [
            AccountMetadata(
                id=account.id,
                email=account.email,
                last_refresh_time=account.last_refresh_time,
                bound=account.bound,
                bound_at=account.bound_at,
                token_valid=account.token_valid,
            )
            for account in sorted(self.accounts.values(), key=lambda item: item.id)
        ]

    def upsert_accounts(self, accounts):
        snapshot = self.accounts.copy()
        incoming = list(accounts)
        self.import_calls.append(incoming)
        try:
            for index, account in enumerate(incoming):
                if self.fail_import_at == index:
                    raise RuntimeError("database import failed")
                existing = next(
                    (
                        stored
                        for stored in self.accounts.values()
                        if stored.email_normalized == account.email_normalized
                    ),
                    None,
                )
                if existing is None:
                    account_id = max(self.accounts, default=0) + 1
                    self.accounts[account_id] = FakeStoredAccount(
                        id=account_id,
                        email=account.email,
                        email_normalized=account.email_normalized,
                        password_ciphertext=account.password_ciphertext,
                        client_id=account.client_id,
                        refresh_token_ciphertext=account.refresh_token_ciphertext,
                    )
                    continue
                self.accounts[existing.id] = replace(
                    existing,
                    email=account.email,
                    password_ciphertext=account.password_ciphertext,
                    client_id=account.client_id,
                    refresh_token_ciphertext=account.refresh_token_ciphertext,
                    token_valid=True,
                )
        except BaseException:
            self.accounts = snapshot
            raise
        return len(incoming)

    def get_credentials(self, account_id):
        return self.accounts.get(account_id)

    def iter_encrypted_credentials(self, limit, batch_size=100):
        self.export_events.append(("preflight", limit, batch_size))
        if len(self.accounts) > limit:
            from mail_manager.accounts import ExportLimitError

            raise ExportLimitError

        def generate():
            for account in sorted(self.accounts.values(), key=lambda item: item.id):
                self.export_events.append(("yield", account.id))
                yield account

        return generate()

    def update_flags(self, account_id, changes, now):
        account = self.accounts.get(account_id)
        if account is None:
            return None
        bound = changes.get("bound", account.bound)
        token_valid = changes.get("token_valid", account.token_valid)
        updated = replace(
            account,
            bound=bound,
            bound_at=now if bound else None,
            token_valid=token_valid,
        )
        self.accounts[account_id] = updated
        return self.list_accounts()[
            [item.id for item in self.list_accounts()].index(account_id)
        ]

    def delete_account(self, account_id):
        return self.accounts.pop(account_id, None) is not None

    def delete_accounts(self, account_ids):
        if any(account_id not in self.accounts for account_id in account_ids):
            return False
        for account_id in account_ids:
            del self.accounts[account_id]
        return True


class FakeCredentialCipher:
    def __init__(self):
        self.encrypt_calls = []
        self.decrypt_calls = []
        self._plaintext_by_ciphertext = {}
        self.fail_encrypt_on = None

    def add(self, ciphertext, plaintext):
        self._plaintext_by_ciphertext[ciphertext] = plaintext

    def encrypt(self, plaintext):
        self.encrypt_calls.append(plaintext)
        if plaintext == self.fail_encrypt_on:
            raise RuntimeError("encryption failed")
        ciphertext = f"opaque-ciphertext-{len(self.encrypt_calls)}"
        self._plaintext_by_ciphertext[ciphertext] = plaintext
        return ciphertext

    def decrypt(self, ciphertext):
        self.decrypt_calls.append(ciphertext)
        return self._plaintext_by_ciphertext[ciphertext]


class ScriptedResult:
    def __init__(self, connection):
        self.connection = connection

    def fetchone(self):
        return self.connection.rows.pop(0) if self.connection.rows else None

    def fetchall(self):
        rows = self.connection.rows
        self.connection.rows = []
        return rows


class RecordingConnection:
    def __init__(self, rows=(), fail_at=None, cursor_rows=()):
        self.rows = list(rows)
        self.cursor_rows = list(cursor_rows)
        self.executed = []
        self.cursors = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.closed = False
        self.fail_at = fail_at
        self.cursor_fetch_hook = None

    def execute(self, statement, parameters=None):
        if self.closed:
            raise RuntimeError("connection is closed")
        self.executed.append((statement, parameters))
        if self.fail_at == len(self.executed) - 1:
            raise RuntimeError("database execution failed")
        return ScriptedResult(self)

    def cursor(self, name=None):
        if self.closed:
            raise RuntimeError("connection is closed")
        cursor = RecordingServerCursor(self, name)
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1
        self.closed = True


class RecordingServerCursor:
    def __init__(self, connection, name):
        self.connection = connection
        self.name = name
        self.executed = []
        self.fetchmany_calls = []
        self.fetchone_calls = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, _error_type, _error, _traceback):
        self.closed = True

    def execute(self, statement, parameters=None):
        if self.connection.closed:
            raise RuntimeError("connection is closed")
        self.executed.append((statement, parameters))

    def fetchmany(self, size):
        if self.connection.closed:
            raise RuntimeError("connection is closed")
        if self.connection.cursor_fetch_hook is not None:
            self.connection.cursor_fetch_hook()
        self.fetchmany_calls.append(size)
        rows = self.connection.cursor_rows[:size]
        del self.connection.cursor_rows[:size]
        return rows

    def fetchone(self):
        if self.connection.closed:
            raise RuntimeError("connection is closed")
        if self.connection.cursor_fetch_hook is not None:
            self.connection.cursor_fetch_hook()
        self.fetchone_calls += 1
        if not self.connection.cursor_rows:
            return None
        return self.connection.cursor_rows.pop(0)


class FakeGraphClient:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.events = []
        self.refresh_error = None
        self.list_error = None
        self.refresh_hook = None
        self.list_hook = None

    def spawn(self):
        client = FakeGraphClient(self.messages)
        client.refresh_error = self.refresh_error
        client.list_error = self.list_error
        client.refresh_hook = self.refresh_hook
        client.list_hook = self.list_hook
        return client

    def refresh(self, client_id, refresh_token):
        self.events.append(("refresh", client_id, refresh_token))
        if self.refresh_hook is not None:
            self.refresh_hook(client_id, refresh_token)
        if self.refresh_error is not None:
            raise self.refresh_error
        return f"access-{client_id}", f"rotated-{refresh_token}"

    def list_messages(self, access_token, folder, limit):
        self.events.append(("list", access_token, folder, limit))
        if self.list_hook is not None:
            self.list_hook(access_token, folder, limit)
        if self.list_error is not None:
            raise self.list_error
        return self.messages[:limit]


class FakeMailRepository:
    def __init__(self, accounts=()):
        self.accounts = {account.id: account for account in accounts}
        self.events = []
        self.candidate_limit = None
        self._guard = Lock()
        self.finalize_errors = []

    def claim_refresh(self, account_id, lease, now, ttl):
        from mail_manager.mail import (
            MailAccountNotFound,
            RefreshBusy,
            TokenInvalid,
        )
        from mail_manager.models import RefreshClaim

        with self._guard:
            account = self.accounts.get(account_id)
            if account is None:
                raise MailAccountNotFound
            if not account.token_valid:
                raise TokenInvalid
            if (
                account.refresh_lease_token is not None
                and account.refresh_lease_expires_at > now
            ):
                retry_after = max(
                    1,
                    min(30, int((account.refresh_lease_expires_at - now).total_seconds())),
                )
                raise RefreshBusy(retry_after)
            self.accounts[account_id] = replace(
                account,
                refresh_lease_token=lease,
                refresh_lease_expires_at=now + ttl,
            )
            self.events.append(("claim", account_id, lease))
            return RefreshClaim(
                client_id=account.client_id,
                refresh_token_ciphertext=account.refresh_token_ciphertext,
            )

    def finalize_refresh(self, account_id, lease, ciphertext, now):
        with self._guard:
            if self.finalize_errors:
                raise self.finalize_errors.pop(0)
            account = self.accounts.get(account_id)
            if account is None or account.refresh_lease_token != lease:
                return False
            self.accounts[account_id] = replace(
                account,
                refresh_token_ciphertext=ciphertext,
                token_valid=True,
                last_refresh_time=now,
                refresh_lease_token=None,
                refresh_lease_expires_at=None,
            )
            self.events.append(("persist", account_id, ciphertext))
            return True

    def invalidate_refresh(self, account_id, lease, now):
        with self._guard:
            account = self.accounts.get(account_id)
            if account is None or account.refresh_lease_token != lease:
                return False
            self.accounts[account_id] = replace(
                account,
                token_valid=False,
                refresh_lease_token=None,
                refresh_lease_expires_at=None,
            )
            self.events.append(("invalid", account_id))
            return True

    def release_refresh(self, account_id, lease):
        with self._guard:
            account = self.accounts.get(account_id)
            if account is None or account.refresh_lease_token != lease:
                return False
            self.accounts[account_id] = replace(
                account,
                refresh_lease_token=None,
                refresh_lease_expires_at=None,
            )
            self.events.append(("release", account_id))
            return True

    def set_bound(self, account_id, bound, now):
        account = self.accounts.get(account_id)
        if account is None:
            return False
        self.accounts[account_id] = replace(
            account,
            bound=bound,
            bound_at=now if bound else None,
        )
        self.events.append(("bound", account_id, bound))
        return True

    def random_unbound_candidates(self, limit):
        from mail_manager.models import AccountSummary

        self.candidate_limit = limit
        return [
            AccountSummary(id=account.id, email=account.email)
            for account in sorted(self.accounts.values(), key=lambda item: item.id)
            if not account.bound and account.token_valid
        ][:limit]
