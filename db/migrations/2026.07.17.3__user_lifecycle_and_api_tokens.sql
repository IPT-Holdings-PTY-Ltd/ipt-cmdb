ALTER TABLE users
    ADD COLUMN IF NOT EXISTS api_access_enabled boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS last_login_at timestamptz,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS archived_at timestamptz;

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_status_check;
ALTER TABLE users
    ADD CONSTRAINT users_status_check
    CHECK (status IN ('active', 'invited', 'disabled', 'archived'));

CREATE TABLE IF NOT EXISTS user_api_tokens (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name text NOT NULL,
    token_prefix varchar(24) NOT NULL,
    token_hash varchar(64) NOT NULL UNIQUE,
    scopes text[] NOT NULL DEFAULT ARRAY['cmdb:read']::text[],
    company_ids text[] NOT NULL DEFAULT '{}'::text[],
    expires_at timestamptz NOT NULL,
    last_used_at timestamptz,
    revoked_at timestamptz,
    created_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    revoked_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (cardinality(scopes) > 0),
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS user_api_tokens_user_idx
    ON user_api_tokens (user_id, revoked_at, expires_at);
CREATE INDEX IF NOT EXISTS user_api_tokens_prefix_idx
    ON user_api_tokens (token_prefix);
