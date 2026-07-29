-- Cross-replica local-password throttling. Only one-way hashes are retained;
-- submitted identifiers, network addresses and passwords never enter this table.

CREATE TABLE IF NOT EXISTS auth_login_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    identifier_hash varchar(64) NOT NULL,
    requester_hash varchar(64) NOT NULL,
    outcome text NOT NULL DEFAULT 'pending'
        CHECK (
            outcome IN (
                'pending',
                'password_failed',
                'password_verified',
                'succeeded',
                'throttled'
            )
        ),
    throttle_reason text
        CHECK (throttle_reason IS NULL OR throttle_reason IN ('identifier', 'source')),
    completed_at timestamptz,
    identifier_cleared_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (outcome = 'pending' AND completed_at IS NULL)
        OR
        (outcome <> 'pending' AND completed_at IS NOT NULL)
    ),
    CHECK (
        (outcome = 'throttled' AND throttle_reason IS NOT NULL)
        OR
        (outcome <> 'throttled' AND throttle_reason IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS auth_login_attempts_identifier_active_idx
    ON auth_login_attempts (identifier_hash, created_at DESC)
    WHERE outcome IN ('pending', 'password_failed');

CREATE INDEX IF NOT EXISTS auth_login_attempts_requester_active_idx
    ON auth_login_attempts (requester_hash, created_at DESC)
    WHERE outcome IN ('pending', 'password_failed');

CREATE INDEX IF NOT EXISTS auth_login_attempts_throttle_audit_idx
    ON auth_login_attempts (throttle_reason, requester_hash, identifier_hash, created_at DESC)
    WHERE outcome = 'throttled';

CREATE INDEX IF NOT EXISTS auth_login_attempts_created_idx
    ON auth_login_attempts (created_at);
