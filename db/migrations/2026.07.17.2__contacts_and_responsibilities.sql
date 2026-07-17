-- Tenant contacts, portal identity linkage and effective-dated CI responsibility.
CREATE TABLE IF NOT EXISTS contacts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    linked_user_id uuid UNIQUE REFERENCES users(id) ON DELETE SET NULL,
    display_name text NOT NULL,
    normalized_name text NOT NULL,
    first_name text,
    last_name text,
    primary_email citext,
    phone text,
    mobile text,
    job_title text,
    department text,
    location text,
    timezone text,
    manager_contact_id uuid REFERENCES contacts(id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'active',
    source text NOT NULL DEFAULT 'manual',
    sync_status text NOT NULL DEFAULT 'not_synced',
    last_seen_at timestamptz,
    last_synced_at timestamptz,
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT contacts_status_check CHECK (status IN ('active', 'on_leave', 'left_company', 'inactive')),
    CONSTRAINT contacts_sync_status_check CHECK (sync_status IN ('not_synced', 'current', 'stale', 'conflict', 'error'))
);

CREATE UNIQUE INDEX IF NOT EXISTS contacts_company_email_idx
    ON contacts(company_id, primary_email) WHERE primary_email IS NOT NULL;
CREATE INDEX IF NOT EXISTS contacts_company_status_name_idx
    ON contacts(company_id, status, normalized_name);
CREATE INDEX IF NOT EXISTS contacts_linked_user_idx
    ON contacts(linked_user_id) WHERE linked_user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS contact_responsibilities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    ci_id uuid NOT NULL REFERENCES configuration_items(id) ON DELETE CASCADE,
    contact_id uuid NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
    responsibility_role text NOT NULL,
    is_primary boolean NOT NULL DEFAULT true,
    effective_from timestamptz NOT NULL DEFAULT now(),
    effective_until timestamptz,
    escalation_order integer NOT NULL DEFAULT 1,
    notes text,
    source text NOT NULL DEFAULT 'manual',
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    ended_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT contact_responsibilities_role_check CHECK (
        responsibility_role IN (
            'business_owner', 'service_owner', 'technical_owner', 'custodian',
            'change_approver', 'signoff_delegate', 'support_contact'
        )
    ),
    CONSTRAINT contact_responsibilities_dates_check CHECK (
        effective_until IS NULL OR effective_until >= effective_from
    ),
    CONSTRAINT contact_responsibilities_escalation_check CHECK (escalation_order >= 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS contact_responsibilities_active_member_idx
    ON contact_responsibilities(ci_id, responsibility_role, contact_id)
    WHERE effective_until IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS contact_responsibilities_active_primary_idx
    ON contact_responsibilities(ci_id, responsibility_role)
    WHERE effective_until IS NULL AND is_primary;
CREATE INDEX IF NOT EXISTS contact_responsibilities_contact_idx
    ON contact_responsibilities(contact_id, effective_until, effective_from DESC);
CREATE INDEX IF NOT EXISTS contact_responsibilities_ci_idx
    ON contact_responsibilities(ci_id, effective_until, responsibility_role);

-- Contacts use the same external identity map as CIs and users. Provider IDs,
-- never names, remain the authoritative cross-system identity.
ALTER TABLE external_object_mappings
    DROP CONSTRAINT IF EXISTS external_object_mappings_canonical_entity_type_check;
ALTER TABLE external_object_mappings
    ADD CONSTRAINT external_object_mappings_canonical_entity_type_check
    CHECK (canonical_entity_type IN ('company', 'configuration_item', 'contact', 'user', 'service', 'contract'));

-- Safely convert existing free-text owners into customer-scoped legacy contacts.
WITH owner_values AS (
    SELECT ci.company_id, trim(owner.value) AS display_name
    FROM configuration_items ci
    CROSS JOIN LATERAL (
        VALUES
            (ci.attributes #>> '{metadata,businessOwner}'),
            (ci.attributes #>> '{metadata,serviceOwner}'),
            (ci.attributes #>> '{metadata,technicalOwner}'),
            (ci.attributes #>> '{metadata,custodian}'),
            (ci.attributes #>> '{metadata,signoffDelegate}')
    ) owner(value)
    WHERE trim(COALESCE(owner.value, '')) <> ''
), distinct_owners AS (
    SELECT DISTINCT company_id, display_name, lower(display_name) AS normalized_name
    FROM owner_values
)
INSERT INTO contacts (
    id, company_id, display_name, normalized_name, status, source,
    sync_status, attributes, created_at, updated_at
)
SELECT gen_random_uuid(), owner.company_id, owner.display_name, owner.normalized_name,
       'active', 'legacy_import', 'not_synced',
       jsonb_build_object('legacyOwnerName', owner.display_name), now(), now()
FROM distinct_owners owner
WHERE NOT EXISTS (
    SELECT 1 FROM contacts contact
    WHERE contact.company_id = owner.company_id
      AND contact.normalized_name = owner.normalized_name
);

WITH assignments AS (
    SELECT ci.id AS ci_id, ci.company_id, role.role_name,
           trim(role.owner_name) AS owner_name
    FROM configuration_items ci
    CROSS JOIN LATERAL (
        VALUES
            ('business_owner', ci.attributes #>> '{metadata,businessOwner}'),
            ('service_owner', ci.attributes #>> '{metadata,serviceOwner}'),
            ('technical_owner', ci.attributes #>> '{metadata,technicalOwner}'),
            ('custodian', ci.attributes #>> '{metadata,custodian}'),
            ('signoff_delegate', ci.attributes #>> '{metadata,signoffDelegate}')
    ) role(role_name, owner_name)
    WHERE trim(COALESCE(role.owner_name, '')) <> ''
)
INSERT INTO contact_responsibilities (
    id, company_id, ci_id, contact_id, responsibility_role,
    is_primary, effective_from, escalation_order, source, created_at
)
SELECT gen_random_uuid(), assignment.company_id, assignment.ci_id, contact.id,
       assignment.role_name, true, now(), 1, 'legacy_import', now()
FROM assignments assignment
JOIN LATERAL (
    SELECT id FROM contacts
    WHERE company_id = assignment.company_id
      AND normalized_name = lower(assignment.owner_name)
    ORDER BY created_at, id
    LIMIT 1
) contact ON true
ON CONFLICT DO NOTHING;
