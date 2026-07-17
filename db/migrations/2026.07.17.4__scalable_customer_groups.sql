ALTER TABLE access_groups
    ADD COLUMN IF NOT EXISTS description text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS membership_mode text NOT NULL DEFAULT 'manual'
        CHECK (membership_mode IN ('manual', 'dynamic')),
    ADD COLUMN IF NOT EXISTS membership_rules jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS owner_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS revision integer NOT NULL DEFAULT 1 CHECK (revision > 0);

UPDATE access_groups
SET membership_mode = 'dynamic',
    membership_rules = '{"rule":"all_managed_customers"}'::jsonb
WHERE system = true;

CREATE INDEX IF NOT EXISTS access_groups_owner_idx
    ON access_groups (owner_user_id)
    WHERE owner_user_id IS NOT NULL;
