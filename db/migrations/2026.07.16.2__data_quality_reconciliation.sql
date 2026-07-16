-- Actionable data-quality exceptions and richer reconciliation review evidence.

CREATE TABLE IF NOT EXISTS data_quality_exceptions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    rule_key text NOT NULL,
    entity_type text NOT NULL DEFAULT 'configuration_item',
    entity_id uuid NOT NULL,
    reason text NOT NULL,
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'resolved')),
    expires_at timestamptz,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_by uuid REFERENCES users(id) ON DELETE SET NULL,
    resolved_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS data_quality_exceptions_active_idx
    ON data_quality_exceptions(company_id, rule_key, entity_type, entity_id)
    WHERE state = 'active';
CREATE INDEX IF NOT EXISTS data_quality_exceptions_company_idx
    ON data_quality_exceptions(company_id, state, created_at DESC);

ALTER TABLE reconciliation_candidates ADD COLUMN IF NOT EXISTS company_id uuid REFERENCES companies(id) ON DELETE CASCADE;
ALTER TABLE reconciliation_candidates ADD COLUMN IF NOT EXISTS external_record jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE reconciliation_candidates ADD COLUMN IF NOT EXISTS conflict_details jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE reconciliation_candidates ADD COLUMN IF NOT EXISTS decision text;
ALTER TABLE reconciliation_candidates ADD COLUMN IF NOT EXISTS decision_notes text;
ALTER TABLE reconciliation_candidates ADD COLUMN IF NOT EXISTS target_ci_id uuid REFERENCES configuration_items(id) ON DELETE SET NULL;

ALTER TABLE reconciliation_candidates DROP CONSTRAINT IF EXISTS reconciliation_candidates_decision_check;
ALTER TABLE reconciliation_candidates ADD CONSTRAINT reconciliation_candidates_decision_check
    CHECK (decision IS NULL OR decision IN ('use_existing', 'create_new', 'ignore'));

UPDATE reconciliation_candidates rc
SET company_id = ci.company_id
FROM configuration_items ci
WHERE rc.company_id IS NULL AND rc.candidate_ci_id = ci.id;

CREATE INDEX IF NOT EXISTS reconciliation_candidates_company_state_idx
    ON reconciliation_candidates(company_id, state, created_at DESC);
