-- Immutable technician assignment identity and reassignment evidence.
ALTER TABLE change_requests
    ADD COLUMN IF NOT EXISTS assigned_user_id uuid REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE change_requests
    ADD COLUMN IF NOT EXISTS assignment_history jsonb NOT NULL DEFAULT '[]'::jsonb;

UPDATE change_requests change
SET assigned_user_id = matched_user.id
FROM users matched_user
WHERE change.assigned_user_id IS NULL
  AND change.assigned_technician IS NOT NULL
  AND lower(change.assigned_technician) = lower(matched_user.email::text);

UPDATE change_requests
SET assignment_history = jsonb_build_array(jsonb_build_object(
    'id', gen_random_uuid()::text,
    'previousUserId', NULL,
    'previousDisplayName', '',
    'assignedUserId', assigned_user_id,
    'assignedDisplayName', assigned_technician,
    'reason', 'Assignment history initialized during upgrade',
    'actorId', NULL,
    'actorEmail', '',
    'createdAt', created_at
))
WHERE assignment_history = '[]'::jsonb
  AND COALESCE(assigned_technician, '') <> '';

CREATE INDEX IF NOT EXISTS change_requests_assigned_user_idx
    ON change_requests(company_id, assigned_user_id, status)
    WHERE status NOT IN ('cancelled', 'closed');
