-- Audited, reversible integration lifecycle and credential-safe removal.

ALTER TABLE integration_connections
    ADD COLUMN IF NOT EXISTS lifecycle_status text NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS lifecycle_reason text,
    ADD COLUMN IF NOT EXISTS lifecycle_changed_at timestamptz,
    ADD COLUMN IF NOT EXISTS lifecycle_changed_by uuid REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE integration_connections
    DROP CONSTRAINT IF EXISTS integration_connections_lifecycle_status_check;
ALTER TABLE integration_connections
    ADD CONSTRAINT integration_connections_lifecycle_status_check
    CHECK (lifecycle_status IN ('active', 'paused', 'disabled', 'removed'));

CREATE INDEX IF NOT EXISTS integration_connections_lifecycle_idx
    ON integration_connections(lifecycle_status, provider);
