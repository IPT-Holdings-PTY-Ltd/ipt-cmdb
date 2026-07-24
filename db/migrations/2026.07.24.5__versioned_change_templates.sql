-- Versioned MSP/customer change procedures and immutable change provenance.
CREATE TABLE IF NOT EXISTS change_templates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid REFERENCES companies(id) ON DELETE CASCADE,
    template_key citext NOT NULL,
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    tags jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL DEFAULT 'draft' CHECK (
        status IN ('draft', 'published', 'retired')
    ),
    system boolean NOT NULL DEFAULT false,
    current_version integer NOT NULL DEFAULT 1 CHECK (current_version > 0),
    owner_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    review_due_date date,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS change_templates_global_key_idx
    ON change_templates(lower(template_key::text)) WHERE company_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS change_templates_company_key_idx
    ON change_templates(company_id, lower(template_key::text)) WHERE company_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS change_templates_scope_status_idx
    ON change_templates(company_id, status, name);

CREATE TABLE IF NOT EXISTS change_template_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id uuid NOT NULL REFERENCES change_templates(id) ON DELETE CASCADE,
    version integer NOT NULL CHECK (version > 0),
    content jsonb NOT NULL,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (template_id, version)
);

ALTER TABLE change_requests
    ADD COLUMN IF NOT EXISTS template_id uuid REFERENCES change_templates(id) ON DELETE SET NULL;
ALTER TABLE change_requests
    ADD COLUMN IF NOT EXISTS template_version integer CHECK (template_version > 0);
ALTER TABLE change_requests
    ADD COLUMN IF NOT EXISTS template_snapshot jsonb;
ALTER TABLE change_requests
    ADD COLUMN IF NOT EXISTS template_parameters jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS change_requests_template_idx
    ON change_requests(template_id, template_version)
    WHERE template_id IS NOT NULL;
