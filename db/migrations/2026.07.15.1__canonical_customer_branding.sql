-- Move the final customer presentation settings into canonical PostgreSQL.
CREATE TABLE IF NOT EXISTS company_branding (
    company_id uuid PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    display_name text NOT NULL,
    logo_text varchar(3) NOT NULL,
    accent_color varchar(16) NOT NULL DEFAULT '#50d5b9',
    secondary_color varchar(16) NOT NULL DEFAULT '#7997ff',
    logo_data_url text,
    logo_file_name text,
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Preserve the old document only as a one-time import source. Runtime writes
-- never target this table after this migration.
DO $$
BEGIN
    IF to_regclass('public.application_state') IS NOT NULL
       AND to_regclass('public.legacy_application_state') IS NULL THEN
        ALTER TABLE application_state RENAME TO legacy_application_state;
    END IF;
END $$;
