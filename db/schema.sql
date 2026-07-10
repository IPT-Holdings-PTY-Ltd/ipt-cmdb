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
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email citext UNIQUE NOT NULL,
    display_name text,
    identity_provider_subject text UNIQUE,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'invited', 'disabled')),
    created_at timestamptz NOT NULL DEFAULT now()
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

CREATE TABLE IF NOT EXISTS integration_connections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
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
    relationship_type text NOT NULL CHECK (relationship_type IN ('depends_on', 'connected_to', 'installed_on', 'used_by', 'licensed_to', 'hosts', 'backs_up', 'managed_by')),
    source_mapping_id uuid REFERENCES external_object_mappings(id) ON DELETE SET NULL,
    confidence numeric(4,3) NOT NULL DEFAULT 1 CHECK (confidence >= 0 AND confidence <= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    retired_at timestamptz,
    CHECK (from_ci_id <> to_ci_id),
    UNIQUE (from_ci_id, to_ci_id, relationship_type)
);
CREATE INDEX IF NOT EXISTS ci_relationships_from_idx ON ci_relationships(from_ci_id) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS ci_relationships_to_idx ON ci_relationships(to_ci_id) WHERE retired_at IS NULL;

CREATE TABLE IF NOT EXISTS sync_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    integration_connection_id uuid NOT NULL REFERENCES integration_connections(id) ON DELETE CASCADE,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'review_required')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    discovered_count integer NOT NULL DEFAULT 0,
    created_count integer NOT NULL DEFAULT 0,
    updated_count integer NOT NULL DEFAULT 0,
    review_count integer NOT NULL DEFAULT 0,
    error_summary text
);

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

-- Transitional application state store. It keeps the current demo API operational
-- while endpoint-by-endpoint repositories move to the canonical tables above.
-- This is API-owned state; it is never accessed by the frontend.
CREATE TABLE IF NOT EXISTS application_state (
    state_key text PRIMARY KEY,
    state jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
