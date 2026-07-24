-- Secure ConnectWise connection metadata and read-only company observations.

ALTER TABLE integration_connections
    ADD COLUMN IF NOT EXISTS credentials_encrypted text,
    ADD COLUMN IF NOT EXISTS credentials_nonce text,
    ADD COLUMN IF NOT EXISTS connection_status text NOT NULL DEFAULT 'not_configured',
    ADD COLUMN IF NOT EXISTS last_test_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_error text,
    ADD COLUMN IF NOT EXISTS revision integer NOT NULL DEFAULT 1;

ALTER TABLE integration_connections
    DROP CONSTRAINT IF EXISTS integration_connections_connection_status_check;
ALTER TABLE integration_connections
    ADD CONSTRAINT integration_connections_connection_status_check
    CHECK (connection_status IN ('not_configured', 'configured', 'verified', 'error'));

CREATE TABLE IF NOT EXISTS provider_company_observations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    integration_connection_id uuid NOT NULL REFERENCES integration_connections(id) ON DELETE CASCADE,
    last_sync_run_id uuid REFERENCES sync_runs(id) ON DELETE SET NULL,
    external_id text NOT NULL,
    identifier text,
    display_name text NOT NULL,
    status_name text,
    type_name text,
    site_name text,
    deleted boolean NOT NULL DEFAULT false,
    provider_updated_at text,
    observed_fields jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload_hash text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (integration_connection_id, external_id)
);

CREATE INDEX IF NOT EXISTS provider_company_observations_connection_active_idx
    ON provider_company_observations(integration_connection_id, active, display_name);
CREATE INDEX IF NOT EXISTS provider_company_observations_identifier_idx
    ON provider_company_observations(integration_connection_id, identifier)
    WHERE identifier IS NOT NULL AND identifier <> '';
