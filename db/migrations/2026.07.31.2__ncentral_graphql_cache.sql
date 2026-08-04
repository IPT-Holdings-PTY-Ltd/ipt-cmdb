-- Bounded, tenant-scoped cache state for optional provider enrichments.
--
-- The tables intentionally retain normalized summaries rather than provider
-- response envelopes.  N-central server and device identities are stored in
-- separate columns because device identifiers are only unique within one
-- N-central server.

CREATE TABLE IF NOT EXISTS integration_capability_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    integration_connection_id uuid NOT NULL
        REFERENCES integration_connections(id) ON DELETE CASCADE,
    capability_key varchar(120) NOT NULL,
    status varchar(32) NOT NULL
        CHECK (status IN ('supported', 'unsupported', 'unavailable', 'error')),
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    schema_fingerprint varchar(64) NOT NULL,
    error_category varchar(80),
    checked_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (btrim(capability_key) <> ''),
    CHECK (jsonb_typeof(summary) = 'object'),
    CHECK (octet_length(summary::text) <= 65536),
    CHECK (schema_fingerprint ~ '^[0-9a-f]{64}$'),
    CHECK (expires_at >= checked_at),
    UNIQUE (integration_connection_id, capability_key)
);

CREATE INDEX IF NOT EXISTS integration_capability_snapshots_expiry_idx
    ON integration_capability_snapshots (integration_connection_id, expires_at);

CREATE TABLE IF NOT EXISTS integration_enrichment_previews (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    integration_connection_id uuid NOT NULL
        REFERENCES integration_connections(id) ON DELETE CASCADE,
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    policy_id uuid REFERENCES integration_ci_policies(id) ON DELETE SET NULL,
    sync_run_id uuid REFERENCES sync_runs(id) ON DELETE SET NULL,
    source_namespace varchar(80) NOT NULL,
    source_server_id varchar(255) NOT NULL,
    source_device_id varchar(255) NOT NULL,
    provider_parent_id varchar(255) NOT NULL,
    canonical_ci_id uuid,
    source_mapping_id uuid
        REFERENCES external_object_mappings(id) ON DELETE SET NULL,
    status varchar(32) NOT NULL
        CHECK (status IN ('ready', 'partial', 'unavailable', 'error')),
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_fingerprint varchar(64) NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (company_id, canonical_ci_id)
        REFERENCES configuration_items(company_id, id) ON DELETE CASCADE,
    CHECK (btrim(source_namespace) <> ''),
    CHECK (btrim(source_server_id) <> ''),
    CHECK (btrim(source_device_id) <> ''),
    CHECK (btrim(provider_parent_id) <> ''),
    CHECK (jsonb_typeof(summary) = 'object'),
    CHECK (octet_length(summary::text) <= 262144),
    CHECK (source_fingerprint ~ '^[0-9a-f]{64}$'),
    CHECK (expires_at >= observed_at),
    UNIQUE (
        integration_connection_id, company_id, source_namespace,
        source_server_id, source_device_id
    )
);

CREATE INDEX IF NOT EXISTS integration_enrichment_previews_scope_expiry_idx
    ON integration_enrichment_previews (
        integration_connection_id, company_id, provider_parent_id, expires_at
    );
CREATE INDEX IF NOT EXISTS integration_enrichment_previews_expiry_idx
    ON integration_enrichment_previews (expires_at, updated_at);
