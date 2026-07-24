-- Structured post-change validation, PIR evidence and follow-up actions.
ALTER TABLE change_requests
    ADD COLUMN IF NOT EXISTS closure_assessment jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE change_requests DROP CONSTRAINT IF EXISTS change_requests_outcome_check;
ALTER TABLE change_requests
    ADD CONSTRAINT change_requests_outcome_check CHECK (
        outcome IN (
            'pending', 'successful', 'successful_with_issues',
            'partially_implemented', 'failed', 'backed_out', 'cancelled'
        )
    );
