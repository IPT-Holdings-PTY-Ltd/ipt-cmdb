-- Secure, expiring and auditable external sign-off for change requests.
CREATE TABLE IF NOT EXISTS change_approval_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    change_id uuid NOT NULL REFERENCES change_requests(id) ON DELETE CASCADE,
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    batch_id uuid NOT NULL,
    change_revision integer NOT NULL CHECK (change_revision > 0),
    approver_contact_id uuid REFERENCES contacts(id) ON DELETE SET NULL,
    approver_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    approver_name text NOT NULL,
    approver_email citext NOT NULL,
    responsibility_role text NOT NULL,
    scope jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'approved', 'declined', 'expired', 'revoked')
    ),
    token_hash char(64) NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    delivery_status text NOT NULL DEFAULT 'pending' CHECK (
        delivery_status IN ('pending', 'accepted', 'failed')
    ),
    provider_request_id text,
    last_error text,
    decision_comments text,
    decided_at timestamptz,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS change_approval_requests_change_idx
    ON change_approval_requests(change_id, created_at DESC);
CREATE INDEX IF NOT EXISTS change_approval_requests_batch_status_idx
    ON change_approval_requests(batch_id, status);
CREATE INDEX IF NOT EXISTS change_approval_requests_pending_expiry_idx
    ON change_approval_requests(expires_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS change_approval_requests_email_idx
    ON change_approval_requests(lower(approver_email::text), created_at DESC);
