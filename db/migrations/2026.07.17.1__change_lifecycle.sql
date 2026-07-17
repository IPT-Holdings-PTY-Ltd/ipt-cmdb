-- Controlled change lifecycle, execution outcome and approval evidence.
ALTER TABLE change_requests DROP CONSTRAINT IF EXISTS change_requests_status_check;
ALTER TABLE change_requests
    ADD CONSTRAINT change_requests_status_check CHECK (
        status IN (
            'draft', 'impact_review', 'awaiting_approval', 'approved', 'declined',
            'scheduled', 'implementing', 'completed', 'failed', 'backed_out',
            'post_implementation_review', 'cancelled', 'closed'
        )
    );

ALTER TABLE change_requests ADD COLUMN IF NOT EXISTS actual_start timestamptz;
ALTER TABLE change_requests ADD COLUMN IF NOT EXISTS actual_end timestamptz;
ALTER TABLE change_requests ADD COLUMN IF NOT EXISTS actual_outage_minutes integer NOT NULL DEFAULT 0;
ALTER TABLE change_requests ADD COLUMN IF NOT EXISTS outcome text NOT NULL DEFAULT 'pending';
ALTER TABLE change_requests ADD COLUMN IF NOT EXISTS failure_reason text;
ALTER TABLE change_requests ADD COLUMN IF NOT EXISTS validation_result text;
ALTER TABLE change_requests ADD COLUMN IF NOT EXISTS rollback_executed boolean NOT NULL DEFAULT false;
ALTER TABLE change_requests ADD COLUMN IF NOT EXISTS rollback_result text;
ALTER TABLE change_requests ADD COLUMN IF NOT EXISTS closure_notes text;
ALTER TABLE change_requests ADD COLUMN IF NOT EXISTS approvals jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE change_requests ADD COLUMN IF NOT EXISTS status_history jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE change_requests DROP CONSTRAINT IF EXISTS change_requests_actual_outage_minutes_check;
ALTER TABLE change_requests
    ADD CONSTRAINT change_requests_actual_outage_minutes_check CHECK (actual_outage_minutes >= 0);
ALTER TABLE change_requests DROP CONSTRAINT IF EXISTS change_requests_outcome_check;
ALTER TABLE change_requests
    ADD CONSTRAINT change_requests_outcome_check CHECK (
        outcome IN ('pending', 'successful', 'failed', 'backed_out', 'cancelled')
    );

UPDATE change_requests
SET status_history = jsonb_build_array(jsonb_build_object(
    'id', gen_random_uuid()::text,
    'fromStatus', NULL,
    'toStatus', status,
    'reason', 'Lifecycle history initialized during upgrade',
    'actorId', NULL,
    'actorEmail', '',
    'createdAt', created_at
))
WHERE status_history = '[]'::jsonb;
