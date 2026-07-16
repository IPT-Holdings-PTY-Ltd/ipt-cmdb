-- Expand the original mutation trail into a queryable, append-only governance ledger.
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS event_category text NOT NULL DEFAULT 'data';
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS actor_type text NOT NULL DEFAULT 'user';
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS actor_label text;
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS source_system text NOT NULL DEFAULT 'web';
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS outcome text NOT NULL DEFAULT 'success';
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS severity text NOT NULL DEFAULT 'informational';
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS request_id text;
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS correlation_id text;
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS entity_name text;
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS changes jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS reason text;
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE audit_events DROP CONSTRAINT IF EXISTS audit_events_outcome_check;
ALTER TABLE audit_events ADD CONSTRAINT audit_events_outcome_check
    CHECK (outcome IN ('success', 'denied', 'failed'));
ALTER TABLE audit_events DROP CONSTRAINT IF EXISTS audit_events_severity_check;
ALTER TABLE audit_events ADD CONSTRAINT audit_events_severity_check
    CHECK (severity IN ('informational', 'warning', 'critical'));

CREATE INDEX IF NOT EXISTS audit_events_company_created_idx
    ON audit_events(company_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_actor_created_idx
    ON audit_events(actor_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_entity_created_idx
    ON audit_events(entity_type, entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_category_created_idx
    ON audit_events(event_category, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_correlation_idx
    ON audit_events(correlation_id) WHERE correlation_id IS NOT NULL;

CREATE OR REPLACE FUNCTION prevent_audit_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events;
CREATE TRIGGER audit_events_append_only
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION prevent_audit_event_mutation();
