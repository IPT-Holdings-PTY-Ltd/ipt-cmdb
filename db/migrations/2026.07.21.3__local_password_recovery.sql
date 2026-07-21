-- Single-use, hashed recovery tokens for local accounts. Raw tokens never enter PostgreSQL.
CREATE TABLE IF NOT EXISTS auth_password_resets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid REFERENCES users(id) ON DELETE CASCADE,
    token_hash varchar(64) NOT NULL UNIQUE,
    identifier_hash varchar(64) NOT NULL,
    requester_hash varchar(64) NOT NULL,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    invalidated_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (consumed_at IS NULL OR invalidated_at IS NULL)
);

CREATE INDEX IF NOT EXISTS auth_password_resets_identifier_rate_idx
    ON auth_password_resets (identifier_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS auth_password_resets_requester_rate_idx
    ON auth_password_resets (requester_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS auth_password_resets_created_idx
    ON auth_password_resets (created_at);
CREATE INDEX IF NOT EXISTS auth_password_resets_live_user_idx
    ON auth_password_resets (user_id, expires_at)
    WHERE consumed_at IS NULL AND invalidated_at IS NULL;
