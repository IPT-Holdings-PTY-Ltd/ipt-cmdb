-- Durable, provider-neutral exclusions for noisy external configuration records.

CREATE TABLE IF NOT EXISTS integration_object_suppressions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_id uuid NOT NULL REFERENCES integration_ci_policies(id) ON DELETE CASCADE,
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    external_object_type text NOT NULL DEFAULT 'configuration',
    external_id text NOT NULL,
    external_name text NOT NULL DEFAULT '',
    provider_record jsonb NOT NULL DEFAULT '{}'::jsonb,
    reason text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    ignored_by uuid REFERENCES users(id) ON DELETE SET NULL,
    ignored_at timestamptz NOT NULL DEFAULT now(),
    restored_by uuid REFERENCES users(id) ON DELETE SET NULL,
    restored_at timestamptz,
    restore_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (policy_id, external_object_type, external_id)
);

CREATE INDEX IF NOT EXISTS integration_object_suppressions_active_idx
    ON integration_object_suppressions(company_id, active, updated_at DESC);

CREATE INDEX IF NOT EXISTS integration_object_suppressions_policy_idx
    ON integration_object_suppressions(policy_id, active);
