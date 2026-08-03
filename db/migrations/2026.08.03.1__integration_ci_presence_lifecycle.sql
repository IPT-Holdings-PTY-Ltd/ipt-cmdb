-- Review-gated provider-presence lifecycle for canonical CI mappings.
-- Missing evidence never retires or deletes the canonical configuration item.

CREATE TABLE IF NOT EXISTS integration_ci_presence (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_id uuid NOT NULL REFERENCES integration_ci_policies(id) ON DELETE CASCADE,
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    mapping_id uuid NOT NULL REFERENCES external_object_mappings(id) ON DELETE CASCADE,
    ci_id uuid NOT NULL REFERENCES configuration_items(id) ON DELETE CASCADE,
    external_id text NOT NULL,
    external_name text NOT NULL DEFAULT '',
    provider_parent_id text NOT NULL,
    state text NOT NULL DEFAULT 'observed'
        CHECK (state IN (
            'observed', 'monitoring', 'eligible', 'not_evaluated',
            'retired', 'restore_ready'
        )),
    absence_count integer NOT NULL DEFAULT 0 CHECK (absence_count >= 0),
    required_absences integer NOT NULL DEFAULT 3
        CHECK (required_absences BETWEEN 2 AND 10),
    minimum_missing_hours integer NOT NULL DEFAULT 24
        CHECK (minimum_missing_hours BETWEEN 1 AND 720),
    first_missing_at timestamptz,
    last_missing_at timestamptz,
    candidate_since timestamptz,
    last_observed_at timestamptz,
    last_evaluated_at timestamptz,
    last_evaluated_run_id uuid REFERENCES sync_runs(id) ON DELETE SET NULL,
    scope_mode text NOT NULL DEFAULT 'unfiltered'
        CHECK (scope_mode IN ('unfiltered', 'provider_filtered')),
    provider_read_complete boolean NOT NULL DEFAULT false,
    evaluation_reason text NOT NULL DEFAULT '',
    discovery_scope_fingerprint char(64) NOT NULL,
    policy_decision_fingerprint char(64) NOT NULL,
    connection_revision integer NOT NULL DEFAULT 0 CHECK (connection_revision >= 0),
    policy_revision integer NOT NULL DEFAULT 0 CHECK (policy_revision >= 0),
    snapshot_started_at timestamptz,
    provider_read_completed_at timestamptz,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    reviewed_by uuid REFERENCES users(id) ON DELETE SET NULL,
    reviewed_at timestamptz,
    review_notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (mapping_id),
    UNIQUE (policy_id, external_id)
);

CREATE INDEX IF NOT EXISTS integration_ci_presence_queue_idx
    ON integration_ci_presence(state, updated_at DESC);
CREATE INDEX IF NOT EXISTS integration_ci_presence_company_idx
    ON integration_ci_presence(company_id, state, updated_at DESC);
CREATE INDEX IF NOT EXISTS integration_ci_presence_mapping_idx
    ON integration_ci_presence(mapping_id, updated_at DESC);
