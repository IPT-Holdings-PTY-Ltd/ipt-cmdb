-- Guard relationship-candidate decisions against stale reviewer state and retain
-- the number of non-stale provider observations without enabling auto-approval.
ALTER TABLE ci_relationship_candidates
    ADD COLUMN IF NOT EXISTS revision bigint NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS observation_count bigint NOT NULL DEFAULT 1;

ALTER TABLE ci_relationship_candidates
    DROP CONSTRAINT IF EXISTS ci_relationship_candidates_revision_check;
ALTER TABLE ci_relationship_candidates
    ADD CONSTRAINT ci_relationship_candidates_revision_check
    CHECK (revision >= 1);

ALTER TABLE ci_relationship_candidates
    DROP CONSTRAINT IF EXISTS ci_relationship_candidates_observation_count_check;
ALTER TABLE ci_relationship_candidates
    ADD CONSTRAINT ci_relationship_candidates_observation_count_check
    CHECK (observation_count >= 1);
