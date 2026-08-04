-- Provider-neutral technical inventory and explainable relationship proposals.
-- Inventory is content-addressed and bounded by repository retention. Candidate
-- decisions are deliberately separate from ci_relationships so an integration
-- can never retire or replace a technician-maintained relationship implicitly.

CREATE UNIQUE INDEX IF NOT EXISTS configuration_items_company_id_id_uidx
    ON configuration_items (company_id, id);

CREATE TABLE IF NOT EXISTS ci_inventory_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    ci_id uuid NOT NULL,
    source_mapping_id uuid NOT NULL
        REFERENCES external_object_mappings(id) ON DELETE CASCADE,
    collection_type varchar(80) NOT NULL,
    fingerprint varchar(64) NOT NULL,
    payload jsonb NOT NULL,
    item_count integer NOT NULL DEFAULT 0 CHECK (item_count >= 0),
    completeness text NOT NULL DEFAULT 'unknown'
        CHECK (completeness IN ('unknown', 'partial', 'complete')),
    first_observed_at timestamptz NOT NULL,
    last_observed_at timestamptz NOT NULL,
    superseded_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (company_id, ci_id)
        REFERENCES configuration_items(company_id, id) ON DELETE CASCADE,
    CHECK (btrim(collection_type) <> ''),
    CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CHECK (jsonb_typeof(payload) IN ('array', 'object')),
    CHECK (last_observed_at >= first_observed_at),
    UNIQUE (source_mapping_id, collection_type, fingerprint)
);

CREATE UNIQUE INDEX IF NOT EXISTS ci_inventory_snapshots_current_idx
    ON ci_inventory_snapshots (source_mapping_id, collection_type)
    WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS ci_inventory_snapshots_ci_idx
    ON ci_inventory_snapshots (
        company_id, ci_id, collection_type, last_observed_at DESC
    );

CREATE TABLE IF NOT EXISTS ci_network_interfaces (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    ci_id uuid NOT NULL,
    source_mapping_id uuid NOT NULL
        REFERENCES external_object_mappings(id) ON DELETE CASCADE,
    interface_key varchar(255) NOT NULL,
    name text,
    description text,
    mac_address varchar(64),
    ip_addresses jsonb NOT NULL DEFAULT '[]'::jsonb,
    gateways jsonb NOT NULL DEFAULT '[]'::jsonb,
    dns_servers jsonb NOT NULL DEFAULT '[]'::jsonb,
    dhcp_enabled boolean,
    vlan_id varchar(128),
    operational_state varchar(80),
    speed_mbps bigint CHECK (speed_mbps IS NULL OR speed_mbps >= 0),
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    fingerprint varchar(64) NOT NULL,
    first_observed_at timestamptz NOT NULL,
    last_observed_at timestamptz NOT NULL,
    retired_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (company_id, ci_id)
        REFERENCES configuration_items(company_id, id) ON DELETE CASCADE,
    CHECK (btrim(interface_key) <> ''),
    CHECK (jsonb_typeof(ip_addresses) = 'array'),
    CHECK (jsonb_typeof(gateways) = 'array'),
    CHECK (jsonb_typeof(dns_servers) = 'array'),
    CHECK (jsonb_typeof(attributes) = 'object'),
    CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CHECK (last_observed_at >= first_observed_at),
    UNIQUE (source_mapping_id, interface_key)
);

CREATE INDEX IF NOT EXISTS ci_network_interfaces_active_ci_idx
    ON ci_network_interfaces (company_id, ci_id, name)
    WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS ci_network_interfaces_mac_idx
    ON ci_network_interfaces (company_id, lower(mac_address))
    WHERE retired_at IS NULL AND mac_address IS NOT NULL;

ALTER TABLE ci_relationships
    ADD COLUMN IF NOT EXISTS evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS provenance text NOT NULL DEFAULT 'manual';
ALTER TABLE ci_relationships
    ADD CONSTRAINT ci_relationships_provenance_check
    CHECK (provenance IN ('manual', 'provider'));
ALTER TABLE ci_relationships
    ADD CONSTRAINT ci_relationships_evidence_object_check
    CHECK (jsonb_typeof(evidence) = 'object');

CREATE TABLE IF NOT EXISTS ci_relationship_candidates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    source_mapping_id uuid NOT NULL
        REFERENCES external_object_mappings(id) ON DELETE CASCADE,
    candidate_key varchar(64) NOT NULL,
    from_ci_id uuid,
    to_ci_id uuid,
    from_external_identity jsonb NOT NULL DEFAULT '{}'::jsonb,
    to_external_identity jsonb NOT NULL DEFAULT '{}'::jsonb,
    relationship_type varchar(80) NOT NULL,
    confidence numeric(4,3) NOT NULL
        CHECK (confidence >= 0 AND confidence <= 1),
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'approved', 'rejected', 'ignored')),
    first_observed_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    retired_at timestamptz,
    decided_by uuid REFERENCES users(id) ON DELETE SET NULL,
    decided_at timestamptz,
    decision_notes text,
    approved_relationship_id uuid
        REFERENCES ci_relationships(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (company_id, from_ci_id)
        REFERENCES configuration_items(company_id, id) ON DELETE CASCADE,
    FOREIGN KEY (company_id, to_ci_id)
        REFERENCES configuration_items(company_id, id) ON DELETE CASCADE,
    CHECK (candidate_key ~ '^[0-9a-f]{64}$'),
    CHECK (btrim(relationship_type) <> ''),
    CHECK (jsonb_typeof(from_external_identity) = 'object'),
    CHECK (jsonb_typeof(to_external_identity) = 'object'),
    CHECK (jsonb_typeof(evidence) = 'object'),
    CHECK (
        from_ci_id IS NOT NULL
        OR from_external_identity <> '{}'::jsonb
    ),
    CHECK (
        to_ci_id IS NOT NULL
        OR to_external_identity <> '{}'::jsonb
    ),
    CHECK (from_ci_id IS NULL OR to_ci_id IS NULL OR from_ci_id <> to_ci_id),
    CHECK (last_seen_at >= first_observed_at),
    CHECK (
        (state = 'pending' AND decided_at IS NULL)
        OR
        (state <> 'pending' AND decided_at IS NOT NULL)
    ),
    UNIQUE (source_mapping_id, candidate_key)
);

CREATE INDEX IF NOT EXISTS ci_relationship_candidates_queue_idx
    ON ci_relationship_candidates (
        company_id, state, last_seen_at DESC
    )
    WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS ci_relationship_candidates_from_idx
    ON ci_relationship_candidates (company_id, from_ci_id, state)
    WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS ci_relationship_candidates_to_idx
    ON ci_relationship_candidates (company_id, to_ci_id, state)
    WHERE retired_at IS NULL;
