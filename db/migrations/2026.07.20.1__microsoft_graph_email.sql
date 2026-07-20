CREATE TABLE IF NOT EXISTS email_connections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope text NOT NULL DEFAULT 'msp' CHECK (scope = 'msp'),
    provider text NOT NULL DEFAULT 'microsoft_graph' CHECK (provider = 'microsoft_graph'),
    enabled boolean NOT NULL DEFAULT false,
    auth_mode text NOT NULL DEFAULT 'managed_identity'
        CHECK (auth_mode IN ('managed_identity', 'client_secret', 'certificate')),
    tenant_id text,
    client_id text,
    managed_identity_client_id text,
    sender_address text,
    sender_name text,
    reply_to text,
    graph_base_url text NOT NULL DEFAULT 'https://graph.microsoft.com/v1.0',
    client_secret_encrypted text,
    client_secret_nonce text,
    status text NOT NULL DEFAULT 'not_configured'
        CHECK (status IN ('not_configured', 'configured', 'verified', 'error', 'disabled')),
    last_test_at timestamptz,
    last_error text,
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scope)
);

CREATE TABLE IF NOT EXISTS email_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid REFERENCES companies(id) ON DELETE SET NULL,
    connection_id uuid NOT NULL REFERENCES email_connections(id),
    idempotency_key text NOT NULL UNIQUE,
    to_addresses jsonb NOT NULL DEFAULT '[]'::jsonb,
    cc_addresses jsonb NOT NULL DEFAULT '[]'::jsonb,
    bcc_addresses jsonb NOT NULL DEFAULT '[]'::jsonb,
    subject text NOT NULL,
    body_html text,
    body_text text,
    template_key text NOT NULL DEFAULT 'manual',
    template_version integer NOT NULL DEFAULT 1 CHECK (template_version > 0),
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'sending', 'accepted', 'failed', 'cancelled')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    next_attempt_at timestamptz,
    accepted_at timestamptz,
    last_error text,
    provider_request_id text,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS email_outbox_delivery_idx
    ON email_outbox (status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS email_outbox_company_idx
    ON email_outbox (company_id, created_at DESC);
