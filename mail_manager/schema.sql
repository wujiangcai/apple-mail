CREATE TABLE IF NOT EXISTS accounts (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL,
    email_normalized TEXT NOT NULL UNIQUE,
    password_ciphertext TEXT NOT NULL,
    client_id TEXT NOT NULL,
    refresh_token_ciphertext TEXT NOT NULL,
    refresh_lease_token TEXT,
    refresh_lease_expires_at TIMESTAMP WITH TIME ZONE,
    last_refresh_time TIMESTAMP WITH TIME ZONE,
    bound BOOLEAN NOT NULL DEFAULT FALSE,
    bound_at TIMESTAMP WITH TIME ZONE,
    token_valid BOOLEAN NOT NULL DEFAULT TRUE,
    rentable BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (length(trim(email)) > 0),
    CHECK (length(trim(email_normalized)) > 0),
    CHECK (length(trim(client_id)) > 0)
);

CREATE INDEX IF NOT EXISTS accounts_status_idx
    ON accounts (bound, token_valid);

ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS refresh_lease_token TEXT;

ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS refresh_lease_expires_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS rentable BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS accounts_refresh_lease_expires_at_idx
    ON accounts (refresh_lease_expires_at)
    WHERE refresh_lease_token IS NOT NULL;

CREATE INDEX IF NOT EXISTS accounts_token_maintenance_idx
    ON accounts (token_valid, last_refresh_time, id);

CREATE TABLE IF NOT EXISTS token_maintenance_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    status TEXT NOT NULL,
    scanned_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    invalid_count INTEGER NOT NULL DEFAULT 0,
    busy_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    CHECK (status IN ('running', 'completed', 'failed')),
    CHECK (scanned_count >= 0 AND success_count >= 0),
    CHECK (invalid_count >= 0 AND busy_count >= 0 AND failed_count >= 0)
);

CREATE INDEX IF NOT EXISTS token_maintenance_runs_started_at_idx
    ON token_maintenance_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS auth_sessions (
    session_hash TEXT PRIMARY KEY,
    csrf_hash CHAR(64) NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (length(trim(session_hash)) > 0),
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS auth_sessions_expires_at_idx
    ON auth_sessions (expires_at);

CREATE TABLE IF NOT EXISTS login_failures (
    client_key TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (length(trim(client_key)) > 0),
    CHECK (failure_count >= 0)
);

CREATE INDEX IF NOT EXISTS login_failures_locked_until_idx
    ON login_failures (locked_until)
    WHERE locked_until IS NOT NULL;

CREATE INDEX IF NOT EXISTS login_failures_updated_at_idx
    ON login_failures (updated_at);

CREATE TABLE IF NOT EXISTS mailbox_rentals (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    access_code_hash CHAR(64) NOT NULL UNIQUE,
    access_code_ciphertext TEXT,
    code_rotated_at TIMESTAMP WITH TIME ZONE,
    starts_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE,
    ended_at TIMESTAMP WITH TIME ZONE,
    revoked_at TIMESTAMP WITH TIME ZONE,
    last_access_at TIMESTAMP WITH TIME ZONE,
    access_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT mailbox_rentals_expiry_check
        CHECK (expires_at IS NULL OR expires_at > starts_at),
    CHECK (access_count >= 0),
    CHECK (revoked_at IS NULL OR ended_at IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS mailbox_rentals_active_account_idx
    ON mailbox_rentals (account_id)
    WHERE ended_at IS NULL;

CREATE INDEX IF NOT EXISTS mailbox_rentals_expires_at_idx
    ON mailbox_rentals (expires_at)
    WHERE expires_at IS NOT NULL AND ended_at IS NULL;

ALTER TABLE mailbox_rentals
    ADD COLUMN IF NOT EXISTS access_code_ciphertext TEXT;

ALTER TABLE mailbox_rentals
    ADD COLUMN IF NOT EXISTS code_rotated_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE mailbox_rentals
    ALTER COLUMN expires_at DROP NOT NULL;

ALTER TABLE mailbox_rentals
    DROP CONSTRAINT IF EXISTS mailbox_rentals_check;

ALTER TABLE mailbox_rentals
    DROP CONSTRAINT IF EXISTS mailbox_rentals_expiry_check;

ALTER TABLE mailbox_rentals
    ADD CONSTRAINT mailbox_rentals_expiry_check
    CHECK (expires_at IS NULL OR expires_at > starts_at) NOT VALID;

ALTER TABLE mailbox_rentals
    VALIDATE CONSTRAINT mailbox_rentals_expiry_check;

CREATE TABLE IF NOT EXISTS pickup_sessions (
    session_hash CHAR(64) PRIMARY KEY,
    rental_id BIGINT NOT NULL REFERENCES mailbox_rentals(id) ON DELETE CASCADE,
    csrf_hash CHAR(64) NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS pickup_sessions_rental_id_idx
    ON pickup_sessions (rental_id);

CREATE INDEX IF NOT EXISTS pickup_sessions_expires_at_idx
    ON pickup_sessions (expires_at);

CREATE TABLE IF NOT EXISTS pickup_failures (
    client_key TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (length(trim(client_key)) > 0),
    CHECK (failure_count >= 0)
);

CREATE INDEX IF NOT EXISTS pickup_failures_updated_at_idx
    ON pickup_failures (updated_at);

CREATE TABLE IF NOT EXISTS rental_events (
    id BIGSERIAL PRIMARY KEY,
    rental_id BIGINT NOT NULL REFERENCES mailbox_rentals(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    client_key TEXT,
    detail TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (length(trim(event_type)) > 0)
);

CREATE INDEX IF NOT EXISTS rental_events_rental_created_idx
    ON rental_events (rental_id, created_at DESC);
