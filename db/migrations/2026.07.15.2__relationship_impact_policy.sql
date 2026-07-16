-- Record how a supporting CI affects a dependent CI. This makes impact
-- analysis useful for resilient and informational relationships without
-- changing the stored relationship direction.
ALTER TABLE ci_relationships
    ADD COLUMN IF NOT EXISTS impact_policy text NOT NULL DEFAULT 'required';

ALTER TABLE ci_relationships
    DROP CONSTRAINT IF EXISTS ci_relationships_impact_policy_check;
ALTER TABLE ci_relationships
    ADD CONSTRAINT ci_relationships_impact_policy_check
    CHECK (impact_policy IN ('required', 'degraded', 'redundant', 'informational'));
