-- Durable, restart-safe integration preview runs and cooperative cancellation.

ALTER TABLE sync_runs
    ADD COLUMN IF NOT EXISTS company_id uuid REFERENCES companies(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS policy_id uuid
        REFERENCES integration_ci_policies(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS requested_by uuid REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS requested_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS available_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0
        CHECK (attempt_count >= 0),
    ADD COLUMN IF NOT EXISTS max_attempts integer NOT NULL DEFAULT 3
        CHECK (max_attempts BETWEEN 1 AND 10),
    ADD COLUMN IF NOT EXISTS lease_owner text,
    ADD COLUMN IF NOT EXISTS lease_until timestamptz,
    ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz,
    ADD COLUMN IF NOT EXISTS cancel_requested_at timestamptz,
    ADD COLUMN IF NOT EXISTS cancelled_at timestamptz,
    ADD COLUMN IF NOT EXISTS retry_of_id uuid REFERENCES sync_runs(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS dedupe_key text,
    ADD COLUMN IF NOT EXISTS progress jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE sync_runs
    ALTER COLUMN started_at DROP NOT NULL;

UPDATE sync_runs
SET requested_at = COALESCE(requested_at, started_at, now()),
    available_at = COALESCE(available_at, started_at, now()),
    updated_at = COALESCE(updated_at, finished_at, started_at, now());

UPDATE sync_runs run
SET company_id = company.id
FROM companies company
WHERE run.company_id IS NULL
  AND run.attributes ->> 'companyId' = company.slug;

UPDATE sync_runs run
SET policy_id = policy.id
FROM integration_ci_policies policy
WHERE run.policy_id IS NULL
  AND run.attributes ->> 'policyId' = policy.id::text;

ALTER TABLE sync_runs DROP CONSTRAINT IF EXISTS sync_runs_status_check;
ALTER TABLE sync_runs ADD CONSTRAINT sync_runs_status_check
    CHECK (
        status IN (
            'queued', 'running', 'succeeded', 'blocked', 'failed',
            'review_required', 'cancelled'
        )
    );

CREATE UNIQUE INDEX IF NOT EXISTS sync_runs_active_policy_idx
    ON sync_runs(policy_id)
    WHERE policy_id IS NOT NULL AND status IN ('queued', 'running');

CREATE UNIQUE INDEX IF NOT EXISTS sync_runs_active_dedupe_idx
    ON sync_runs(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS sync_runs_claim_idx
    ON sync_runs(status, available_at, lease_until, requested_at);

CREATE INDEX IF NOT EXISTS sync_runs_policy_status_idx
    ON sync_runs(policy_id, status, requested_at DESC)
    WHERE policy_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS sync_runs_company_requested_idx
    ON sync_runs(company_id, requested_at DESC);
