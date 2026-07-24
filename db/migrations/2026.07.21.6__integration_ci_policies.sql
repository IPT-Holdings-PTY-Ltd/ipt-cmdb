-- Provider-neutral CI discovery policies and tenant-scoped strong identifiers.

CREATE TABLE IF NOT EXISTS integration_ci_policies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    integration_connection_id uuid NOT NULL REFERENCES integration_connections(id) ON DELETE CASCADE,
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    external_parent_id text NOT NULL,
    external_object_type text NOT NULL DEFAULT 'configuration',
    filter_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
    sync_mode text NOT NULL DEFAULT 'manual'
        CHECK (sync_mode IN ('manual', 'continuous_preview')),
    interval_minutes integer NOT NULL DEFAULT 360
        CHECK (interval_minutes BETWEEN 15 AND 10080),
    enabled boolean NOT NULL DEFAULT false,
    revision integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (integration_connection_id, company_id, external_parent_id, external_object_type)
);

CREATE INDEX IF NOT EXISTS integration_ci_policies_due_idx
    ON integration_ci_policies(enabled, sync_mode, updated_at)
    WHERE enabled = true;

ALTER TABLE ci_identifiers
    ADD COLUMN IF NOT EXISTS company_id uuid REFERENCES companies(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS ci_type text;

UPDATE ci_identifiers identifier
SET company_id = ci.company_id,
    ci_type = ci.ci_type
FROM configuration_items ci
WHERE ci.id = identifier.ci_id
  AND (identifier.company_id IS NULL OR identifier.ci_type IS NULL);

ALTER TABLE ci_identifiers
    ALTER COLUMN company_id SET NOT NULL,
    ALTER COLUMN ci_type SET NOT NULL;

DROP INDEX IF EXISTS ci_identifier_unique_non_provider;
CREATE UNIQUE INDEX IF NOT EXISTS ci_identifier_unique_non_provider
    ON ci_identifiers(company_id, ci_type, identifier_type, lower(identifier_value))
    WHERE identifier_type NOT IN ('provider_native', 'mac_address');

