-- First-class virtualization topology. Relationship rows remain provider-neutral;
-- VMware, Hyper-V and cloud adapters map their native identifiers separately.
ALTER TABLE ci_relationships
    DROP CONSTRAINT IF EXISTS ci_relationships_relationship_type_check;

ALTER TABLE ci_relationships
    ADD CONSTRAINT ci_relationships_relationship_type_check
    CHECK (relationship_type IN (
        'depends_on', 'connected_to', 'installed_on', 'used_by', 'licensed_to',
        'related_to', 'hosts', 'backs_up', 'managed_by', 'member_of',
        'stored_on', 'provided_by', 'protected_by'
    ));
