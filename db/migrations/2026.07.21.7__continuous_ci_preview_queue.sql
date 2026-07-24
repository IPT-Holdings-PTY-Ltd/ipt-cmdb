-- Durable, lease-safe continuous CI discovery and its review queue.

ALTER TABLE integration_ci_policies
    ADD COLUMN IF NOT EXISTS next_run_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_run_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_success_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_error text,
    ADD COLUMN IF NOT EXISTS consecutive_failures integer NOT NULL DEFAULT 0
        CHECK (consecutive_failures >= 0),
    ADD COLUMN IF NOT EXISTS lease_owner text,
    ADD COLUMN IF NOT EXISTS lease_until timestamptz;

UPDATE integration_ci_policies
SET next_run_at = now()
WHERE enabled = true
  AND sync_mode = 'continuous_preview'
  AND next_run_at IS NULL;

DROP INDEX IF EXISTS integration_ci_policies_due_idx;
CREATE INDEX integration_ci_policies_due_idx
    ON integration_ci_policies(next_run_at, lease_until)
    WHERE enabled = true AND sync_mode = 'continuous_preview';

CREATE TABLE IF NOT EXISTS integration_ci_review_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_id uuid NOT NULL REFERENCES integration_ci_policies(id) ON DELETE CASCADE,
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    last_sync_run_id uuid REFERENCES sync_runs(id) ON DELETE SET NULL,
    external_id text NOT NULL,
    external_name text NOT NULL,
    decision text NOT NULL
        CHECK (decision IN ('create', 'update', 'link', 'conflict')),
    candidate_ci_id uuid REFERENCES configuration_items(id) ON DELETE SET NULL,
    reason text NOT NULL DEFAULT '',
    provider_record jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    content_hash char(64) NOT NULL,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'dismissed', 'resolved')),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    reviewed_by uuid REFERENCES users(id) ON DELETE SET NULL,
    reviewed_at timestamptz,
    review_notes text,
    UNIQUE (policy_id, external_id)
);

CREATE INDEX IF NOT EXISTS integration_ci_review_queue_idx
    ON integration_ci_review_items(state, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS integration_ci_review_company_idx
    ON integration_ci_review_items(company_id, state, last_seen_at DESC);
