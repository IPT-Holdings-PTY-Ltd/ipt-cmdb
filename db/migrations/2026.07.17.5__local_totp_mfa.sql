ALTER TABLE local_auth_credentials
    ADD COLUMN IF NOT EXISTS mfa_required boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS user_mfa_credentials (
    user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    method text NOT NULL DEFAULT 'totp' CHECK (method = 'totp'),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'enabled')),
    encrypted_secret text NOT NULL,
    secret_nonce text NOT NULL,
    key_version integer NOT NULL DEFAULT 1 CHECK (key_version > 0),
    last_accepted_counter bigint,
    enabled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_mfa_recovery_codes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash text NOT NULL,
    used_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS user_mfa_recovery_codes_user_idx
    ON user_mfa_recovery_codes (user_id, used_at);

CREATE TABLE IF NOT EXISTS auth_login_challenges (
    token_hash varchar(64) PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose text NOT NULL CHECK (purpose IN ('verify', 'enroll')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS auth_login_challenges_expiry_idx
    ON auth_login_challenges (expires_at) WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash varchar(64) PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);

CREATE INDEX IF NOT EXISTS user_sessions_user_idx
    ON user_sessions (user_id, revoked_at, expires_at);
