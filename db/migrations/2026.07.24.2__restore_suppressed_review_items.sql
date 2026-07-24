-- Restored suppressions should immediately return their last known observation to review.

UPDATE integration_ci_review_items review
SET state = 'pending',
    reviewed_by = NULL,
    reviewed_at = NULL,
    review_notes = NULL
FROM integration_object_suppressions suppression
WHERE suppression.policy_id = review.policy_id
  AND suppression.external_id = review.external_id
  AND suppression.external_object_type = 'configuration'
  AND suppression.active = false
  AND review.state = 'resolved';
