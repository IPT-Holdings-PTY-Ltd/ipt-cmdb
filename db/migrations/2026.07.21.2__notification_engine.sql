-- Durable tenant-aware notifications, preferences, evidence and dead-letter delivery.
ALTER TABLE email_outbox DROP CONSTRAINT IF EXISTS email_outbox_status_check;
ALTER TABLE email_outbox ADD CONSTRAINT email_outbox_status_check
    CHECK (status IN ('queued', 'sending', 'accepted', 'failed', 'dead_letter', 'cancelled'));

CREATE TABLE IF NOT EXISTS notification_templates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid REFERENCES companies(id) ON DELETE CASCADE,
    template_key text NOT NULL,
    name text NOT NULL,
    subject_template text NOT NULL,
    html_template text NOT NULL,
    text_template text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS notification_templates_scope_key_idx
    ON notification_templates (COALESCE(company_id, '00000000-0000-0000-0000-000000000000'::uuid), template_key);

CREATE TABLE IF NOT EXISTS notification_rules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid REFERENCES companies(id) ON DELETE CASCADE,
    rule_key text NOT NULL,
    name text NOT NULL,
    event_type text NOT NULL CHECK (event_type IN (
        'asset_renewal', 'asset_eol', 'change_approval', 'missing_owner'
    )),
    enabled boolean NOT NULL DEFAULT true,
    lead_days integer NOT NULL DEFAULT 0 CHECK (lead_days BETWEEN 0 AND 3650),
    cadence text NOT NULL DEFAULT 'daily' CHECK (cadence IN ('immediate', 'daily', 'weekly')),
    recipient_roles jsonb NOT NULL DEFAULT '[]'::jsonb,
    fallback_addresses jsonb NOT NULL DEFAULT '[]'::jsonb,
    template_key text NOT NULL,
    max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 20),
    last_run_at timestamptz,
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS notification_rules_scope_key_idx
    ON notification_rules (COALESCE(company_id, '00000000-0000-0000-0000-000000000000'::uuid), rule_key);
CREATE INDEX IF NOT EXISTS notification_rules_due_idx
    ON notification_rules (enabled, cadence, last_run_at);

CREATE TABLE IF NOT EXISTS notification_preferences (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    contact_id uuid REFERENCES contacts(id) ON DELETE CASCADE,
    user_id uuid REFERENCES users(id) ON DELETE CASCADE,
    email_enabled boolean NOT NULL DEFAULT true,
    event_types jsonb NOT NULL DEFAULT '["*"]'::jsonb,
    digest_mode text NOT NULL DEFAULT 'instant' CHECK (digest_mode IN ('instant', 'daily', 'weekly')),
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((contact_id IS NOT NULL)::integer + (user_id IS NOT NULL)::integer = 1)
);
CREATE UNIQUE INDEX IF NOT EXISTS notification_preferences_contact_idx
    ON notification_preferences (contact_id) WHERE contact_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS notification_preferences_user_idx
    ON notification_preferences (user_id) WHERE user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS notification_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    rule_id uuid NOT NULL REFERENCES notification_rules(id) ON DELETE RESTRICT,
    event_type text NOT NULL,
    entity_type text NOT NULL,
    entity_id uuid NOT NULL,
    entity_name text NOT NULL,
    dedupe_key text NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'queued', 'missing_recipient', 'accepted', 'failed', 'dead_letter', 'cancelled'
    )),
    recipients jsonb NOT NULL DEFAULT '[]'::jsonb,
    missing_roles jsonb NOT NULL DEFAULT '[]'::jsonb,
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    email_outbox_id uuid REFERENCES email_outbox(id) ON DELETE SET NULL,
    scheduled_for timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notification_events_company_created_idx
    ON notification_events (company_id, created_at DESC);
CREATE INDEX IF NOT EXISTS notification_events_status_idx
    ON notification_events (status, scheduled_for);

INSERT INTO notification_templates (
    template_key, name, subject_template, html_template, text_template
)
SELECT seed.* FROM (VALUES
    ('asset_renewal', 'Asset renewal', '{{company_name}}: {{asset_name}} renews in {{days_label}}', '<h2>Renewal attention required</h2><p><strong>{{asset_name}}</strong> ({{asset_type}}) has a renewal date of {{event_date}}.</p><p>Time remaining: {{days_label}}.</p><p>Open the CMDB to review ownership, commercial details and dependencies.</p>', '{{asset_name}} ({{asset_type}}) renews on {{event_date}}. Time remaining: {{days_label}}.'),
    ('asset_eol', 'Asset end of life', '{{company_name}}: {{asset_name}} reaches end of life in {{days_label}}', '<h2>End-of-life attention required</h2><p><strong>{{asset_name}}</strong> ({{asset_type}}) reaches end of life on {{event_date}}.</p><p>Time remaining: {{days_label}}.</p><p>Review replacement, risk acceptance and affected business services in the CMDB.</p>', '{{asset_name}} ({{asset_type}}) reaches end of life on {{event_date}}. Time remaining: {{days_label}}.'),
    ('change_approval', 'Change approval required', 'Approval required: {{change_number}} - {{change_title}}', '<h2>Change approval required</h2><p><strong>{{change_number}} - {{change_title}}</strong> is awaiting approval.</p><p>Risk: {{risk_level}}. Planned start: {{planned_start}}.</p><p>Review the impact, implementation and rollback plans in the CMDB before recording a decision.</p>', '{{change_number}} - {{change_title}} is awaiting approval. Risk: {{risk_level}}. Planned start: {{planned_start}}.'),
    ('missing_owner', 'Missing CI owner', '{{company_name}}: owner missing for {{asset_name}}', '<h2>CMDB ownership gap</h2><p><strong>{{asset_name}}</strong> ({{asset_type}}) has no active structured owner assignment.</p><p>Assign a business, service, technical owner or custodian so operational notifications reach the right people.</p>', '{{asset_name}} ({{asset_type}}) has no active structured owner assignment.')
) AS seed(template_key, name, subject_template, html_template, text_template)
WHERE NOT EXISTS (
    SELECT 1 FROM notification_templates existing
    WHERE existing.company_id IS NULL AND existing.template_key = seed.template_key
);

INSERT INTO notification_rules (
    rule_key, name, event_type, lead_days, cadence, recipient_roles, template_key
)
SELECT seed.rule_key, seed.name, seed.event_type, seed.lead_days, seed.cadence,
       seed.recipient_roles::jsonb, seed.template_key
FROM (VALUES
    ('asset-renewal', 'Asset and subscription renewals', 'asset_renewal', 90, 'daily', '["business_owner","service_owner","technical_owner","custodian"]', 'asset_renewal'),
    ('asset-end-of-life', 'Asset end of life', 'asset_eol', 180, 'daily', '["business_owner","service_owner","technical_owner","custodian"]', 'asset_eol'),
    ('change-approval', 'Change approval required', 'change_approval', 0, 'immediate', '["change_approver","signoff_delegate","business_owner"]', 'change_approval'),
    ('missing-owner', 'Missing CI owner', 'missing_owner', 0, 'weekly', '["support_contact"]', 'missing_owner')
) AS seed(rule_key, name, event_type, lead_days, cadence, recipient_roles, template_key)
WHERE NOT EXISTS (
    SELECT 1 FROM notification_rules existing
    WHERE existing.company_id IS NULL AND existing.rule_key = seed.rule_key
);
