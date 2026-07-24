-- Asset detail pages resolve related changes through the immutable impact
-- snapshot instead of duplicating change references in CI metadata.
CREATE INDEX IF NOT EXISTS change_impact_ci_change_idx
    ON change_impact_snapshots(ci_id, change_id)
    WHERE included = true;
