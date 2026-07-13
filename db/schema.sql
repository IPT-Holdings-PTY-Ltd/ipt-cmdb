-- CMDB Hub PostgreSQL schema (PostgreSQL 15+).
-- Run with: python scripts/migrate_postgres.py
-- Canonical CMDB UUIDs never depend on a provider's object ID or name.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS companies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text NOT NULL UNIQUE,
    name text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'prospect')),
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS attributes jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email citext UNIQUE NOT NULL,
    display_name text,
    identity_provider_subject text UNIQUE,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'invited', 'disabled')),
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS attributes jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Local credentials exist only for development/break-glass mode. Production
-- authentication is delegated to Entra Easy Auth and never stores passwords.
CREATE TABLE IF NOT EXISTS local_auth_credentials (
    user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    password_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_company_roles (
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('platform_admin', 'msp_operator', 'customer_admin', 'customer_editor', 'customer_reader')),
    PRIMARY KEY (user_id, company_id, role)
);

-- Platform administrators are deliberately global; all other access is tenant-scoped above.
CREATE TABLE IF NOT EXISTS user_platform_roles (
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('platform_admin')),
    PRIMARY KEY (user_id, role)
);

CREATE TABLE IF NOT EXISTS access_groups (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text NOT NULL UNIQUE,
    name text NOT NULL,
    system boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS access_group_companies (
    access_group_id uuid NOT NULL REFERENCES access_groups(id) ON DELETE CASCADE,
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    PRIMARY KEY (access_group_id, company_id)
);

CREATE TABLE IF NOT EXISTS user_access_groups (
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    access_group_id uuid NOT NULL REFERENCES access_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, access_group_id)
);

CREATE TABLE IF NOT EXISTS integration_connections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text NOT NULL UNIQUE,
    company_id uuid REFERENCES companies(id) ON DELETE CASCADE,
    provider text NOT NULL CHECK (provider IN ('connectwise_manage', 'ncentral', 'passportal', 'future')),
    name text NOT NULL,
    credential_reference text NOT NULL, -- Key Vault secret reference only; never a secret value
    configuration jsonb NOT NULL DEFAULT '{}'::jsonb,
    enabled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (company_id, provider, name)
);
ALTER TABLE integration_connections ADD COLUMN IF NOT EXISTS slug text;
UPDATE integration_connections
SET slug = CASE provider WHEN 'connectwise_manage' THEN 'connectwise' ELSE provider END || '-' || left(id::text, 8)
WHERE slug IS NULL;
ALTER TABLE integration_connections ALTER COLUMN slug SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS integration_connections_slug_idx ON integration_connections(slug);

CREATE TABLE IF NOT EXISTS configuration_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    ci_type text NOT NULL,
    display_name text NOT NULL,
    normalized_name text NOT NULL,
    lifecycle_status text NOT NULL DEFAULT 'in_service' CHECK (lifecycle_status IN ('planned', 'ordered', 'received', 'in_stock', 'in_service', 'maintenance', 'retired', 'disposed')),
    operational_status text NOT NULL DEFAULT 'unknown' CHECK (operational_status IN ('unknown', 'healthy', 'warning', 'critical', 'offline')),
    service_owner_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    technical_owner_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    retired_at timestamptz,
    UNIQUE (company_id, ci_type, normalized_name)
);
CREATE INDEX IF NOT EXISTS configuration_items_company_type_idx ON configuration_items(company_id, ci_type);
CREATE INDEX IF NOT EXISTS configuration_items_attributes_gin_idx ON configuration_items USING gin(attributes);

-- Strong identifiers are shared cautiously: they are scoped to a company and CI type.
CREATE TABLE IF NOT EXISTS ci_identifiers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ci_id uuid NOT NULL REFERENCES configuration_items(id) ON DELETE CASCADE,
    identifier_type text NOT NULL CHECK (identifier_type IN ('serial_number', 'device_uuid', 'bios_uuid', 'mac_address', 'fqdn', 'domain_sid', 'license_key_hash', 'provider_native')),
    identifier_value text NOT NULL,
    verified_at timestamptz,
    source_mapping_id uuid,
    UNIQUE (ci_id, identifier_type, identifier_value)
);
CREATE UNIQUE INDEX IF NOT EXISTS ci_identifier_unique_non_provider
    ON ci_identifiers(identifier_type, identifier_value)
    WHERE identifier_type NOT IN ('provider_native', 'mac_address');

-- Provider object identity. This is the cross-system mapping table: names are descriptive only.
CREATE TABLE IF NOT EXISTS external_object_mappings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    integration_connection_id uuid NOT NULL REFERENCES integration_connections(id) ON DELETE CASCADE,
    external_object_type text NOT NULL,
    external_id text NOT NULL,
    canonical_entity_type text NOT NULL CHECK (canonical_entity_type IN ('company', 'configuration_item', 'user', 'service', 'contract')),
    canonical_entity_id uuid NOT NULL,
    external_name text,
    external_parent_id text,
    external_version text,
    active boolean NOT NULL DEFAULT true,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_synced_at timestamptz,
    UNIQUE (integration_connection_id, external_object_type, external_id)
);
CREATE INDEX IF NOT EXISTS external_mapping_canonical_idx ON external_object_mappings(canonical_entity_type, canonical_entity_id);
CREATE INDEX IF NOT EXISTS external_mapping_name_idx ON external_object_mappings(integration_connection_id, external_object_type, external_name);

ALTER TABLE ci_identifiers
    DROP CONSTRAINT IF EXISTS ci_identifiers_source_mapping_id_fkey;
ALTER TABLE ci_identifiers
    ADD CONSTRAINT ci_identifiers_source_mapping_id_fkey
    FOREIGN KEY (source_mapping_id) REFERENCES external_object_mappings(id) ON DELETE SET NULL;

-- Immutable observations preserve field provenance and make rename/merge decisions explainable.
CREATE TABLE IF NOT EXISTS ci_source_observations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ci_id uuid NOT NULL REFERENCES configuration_items(id) ON DELETE CASCADE,
    mapping_id uuid NOT NULL REFERENCES external_object_mappings(id) ON DELETE CASCADE,
    observed_at timestamptz NOT NULL DEFAULT now(),
    payload_hash text NOT NULL,
    fields jsonb NOT NULL,
    UNIQUE (mapping_id, payload_hash)
);
CREATE INDEX IF NOT EXISTS ci_observations_ci_time_idx ON ci_source_observations(ci_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS ci_field_authority (
    company_id uuid REFERENCES companies(id) ON DELETE CASCADE,
    ci_type text NOT NULL DEFAULT '*',
    field_name text NOT NULL,
    provider text NOT NULL,
    priority smallint NOT NULL DEFAULT 100 CHECK (priority >= 0),
    PRIMARY KEY (company_id, ci_type, field_name, provider)
);

CREATE TABLE IF NOT EXISTS ci_relationships (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    from_ci_id uuid NOT NULL REFERENCES configuration_items(id) ON DELETE CASCADE,
    to_ci_id uuid NOT NULL REFERENCES configuration_items(id) ON DELETE CASCADE,
    relationship_type text NOT NULL CHECK (relationship_type IN ('depends_on', 'connected_to', 'installed_on', 'used_by', 'licensed_to', 'related_to', 'hosts', 'backs_up', 'managed_by')),
    source_mapping_id uuid REFERENCES external_object_mappings(id) ON DELETE SET NULL,
    confidence numeric(4,3) NOT NULL DEFAULT 1 CHECK (confidence >= 0 AND confidence <= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    retired_at timestamptz,
    CHECK (from_ci_id <> to_ci_id),
    UNIQUE (from_ci_id, to_ci_id, relationship_type)
);

ALTER TABLE ci_relationships
    DROP CONSTRAINT IF EXISTS ci_relationships_relationship_type_check;
ALTER TABLE ci_relationships
    ADD CONSTRAINT ci_relationships_relationship_type_check
    CHECK (relationship_type IN ('depends_on', 'connected_to', 'installed_on', 'used_by', 'licensed_to', 'related_to', 'hosts', 'backs_up', 'managed_by'));
CREATE INDEX IF NOT EXISTS ci_relationships_from_idx ON ci_relationships(from_ci_id) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS ci_relationships_to_idx ON ci_relationships(to_ci_id) WHERE retired_at IS NULL;

-- Change packages freeze the CMDB impact seen during approval. Live CI links are
-- retained for navigation, while snapshot JSON keeps the historical record stable.
CREATE TABLE IF NOT EXISTS change_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    change_number text NOT NULL UNIQUE,
    title text NOT NULL,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'impact_review', 'awaiting_approval', 'approved', 'scheduled', 'implementing', 'completed', 'failed', 'backed_out', 'closed')),
    change_type text NOT NULL CHECK (change_type IN ('standard', 'normal', 'emergency')),
    category text NOT NULL,
    priority text NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    risk_level text NOT NULL CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
    risk_source text NOT NULL DEFAULT 'cmdb_suggestion' CHECK (risk_source IN ('cmdb_suggestion', 'technician')),
    outage_expected boolean NOT NULL DEFAULT false,
    -- Change windows are customer-local wall times. Audit timestamps remain UTC.
    planned_start timestamp without time zone,
    planned_end timestamp without time zone,
    reason text NOT NULL,
    business_impact text,
    implementation_plan text NOT NULL,
    validation_plan text NOT NULL,
    rollback_plan text NOT NULL,
    communication_status text NOT NULL DEFAULT 'required' CHECK (communication_status IN ('required', 'not_required', 'completed')),
    communication_plan text,
    assigned_technician text,
    approver text,
    notes text,
    impact_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    risk_assessment jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS change_requests_company_created_idx ON change_requests(company_id, created_at DESC);
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'change_requests' AND column_name = 'planned_start'
          AND data_type = 'timestamp with time zone'
    ) THEN
        ALTER TABLE change_requests
            ALTER COLUMN planned_start TYPE timestamp without time zone USING planned_start AT TIME ZONE 'UTC',
            ALTER COLUMN planned_end TYPE timestamp without time zone USING planned_end AT TIME ZONE 'UTC';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS change_scope_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    change_id uuid NOT NULL REFERENCES change_requests(id) ON DELETE CASCADE,
    ci_id uuid REFERENCES configuration_items(id) ON DELETE SET NULL,
    ci_snapshot jsonb NOT NULL,
    ordinal integer NOT NULL DEFAULT 0,
    UNIQUE (change_id, ci_id)
);
ALTER TABLE change_scope_items ADD COLUMN IF NOT EXISTS ordinal integer NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS change_impact_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    change_id uuid NOT NULL REFERENCES change_requests(id) ON DELETE CASCADE,
    ci_id uuid REFERENCES configuration_items(id) ON DELETE SET NULL,
    impact_role text NOT NULL CHECK (impact_role IN ('scope', 'direct', 'downstream')),
    depth integer NOT NULL CHECK (depth >= 0),
    relationship_path jsonb NOT NULL DEFAULT '[]'::jsonb,
    ci_snapshot jsonb NOT NULL,
    automatically_detected boolean NOT NULL DEFAULT true,
    included boolean NOT NULL DEFAULT true,
    decision_reason text,
    ordinal integer NOT NULL DEFAULT 0,
    UNIQUE (change_id, ci_id)
);
ALTER TABLE change_impact_snapshots ADD COLUMN IF NOT EXISTS ordinal integer NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS change_impact_change_idx ON change_impact_snapshots(change_id, depth);

CREATE TABLE IF NOT EXISTS change_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    change_id uuid NOT NULL REFERENCES change_requests(id) ON DELETE CASCADE,
    revision integer NOT NULL,
    document jsonb NOT NULL,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (change_id, revision)
);

-- A provider-neutral external envelope prevents the PDF workflow from being
-- coupled to ConnectWise. A future publisher can populate this idempotently.
CREATE TABLE IF NOT EXISTS change_external_links (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    change_id uuid NOT NULL REFERENCES change_requests(id) ON DELETE CASCADE,
    provider text NOT NULL,
    external_type text NOT NULL DEFAULT 'ticket',
    external_id text,
    external_url text,
    sync_status text NOT NULL DEFAULT 'not_published' CHECK (sync_status IN ('not_published', 'queued', 'published', 'failed')),
    idempotency_key text NOT NULL UNIQUE,
    last_attempt_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (change_id, provider, external_type)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    integration_connection_id uuid NOT NULL REFERENCES integration_connections(id) ON DELETE CASCADE,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'blocked', 'failed', 'review_required')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    discovered_count integer NOT NULL DEFAULT 0,
    created_count integer NOT NULL DEFAULT 0,
    updated_count integer NOT NULL DEFAULT 0,
    review_count integer NOT NULL DEFAULT 0,
    error_summary text,
    message text,
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb
);
ALTER TABLE sync_runs DROP CONSTRAINT IF EXISTS sync_runs_status_check;
ALTER TABLE sync_runs ADD CONSTRAINT sync_runs_status_check
    CHECK (status IN ('queued', 'running', 'succeeded', 'blocked', 'failed', 'review_required'));
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS message text;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS attributes jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS reconciliation_candidates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sync_run_id uuid NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    mapping_id uuid REFERENCES external_object_mappings(id) ON DELETE CASCADE,
    candidate_ci_id uuid REFERENCES configuration_items(id) ON DELETE CASCADE,
    reason text NOT NULL,
    confidence numeric(4,3) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'approved', 'rejected')),
    created_at timestamptz NOT NULL DEFAULT now(),
    reviewed_by uuid REFERENCES users(id) ON DELETE SET NULL,
    reviewed_at timestamptz
);

CREATE TABLE IF NOT EXISTS audit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid REFERENCES companies(id) ON DELETE SET NULL,
    actor_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    entity_type text NOT NULL,
    entity_id uuid,
    action text NOT NULL,
    before_value jsonb,
    after_value jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- MSP presentation settings are application-owned configuration. Small logo
-- images are kept as a constrained data URL so backups remain self-contained;
-- larger document assets can move to Azure Blob Storage without changing the
-- public branding contract.
CREATE TABLE IF NOT EXISTS msp_branding (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    display_name text NOT NULL DEFAULT 'CMDB Hub',
    logo_text varchar(3) NOT NULL DEFAULT 'C',
    accent_color varchar(16) NOT NULL DEFAULT '#50d5b9',
    secondary_color varchar(16) NOT NULL DEFAULT '#7997ff',
    logo_data_url text,
    logo_file_name text,
    support_email text,
    support_url text,
    support_phone text,
    welcome_message text,
    report_footer text,
    confidentiality_label text,
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Transitional application state store. It keeps the current demo API operational
-- while endpoint-by-endpoint repositories move to the canonical tables above.
-- This is API-owned state; it is never accessed by the frontend.
CREATE TABLE IF NOT EXISTS application_state (
    state_key text PRIMARY KEY,
    state jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    description text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations (version, description)
VALUES ('2026.07.13.1', 'Canonical CMDB, integration, change control and MSP branding baseline')
ON CONFLICT (version) DO NOTHING;
