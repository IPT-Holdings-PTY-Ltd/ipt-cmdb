ALTER TABLE email_connections
    ADD COLUMN IF NOT EXISTS service_principal_object_id text;
