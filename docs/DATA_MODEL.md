# Data model and relationship semantics

## Tenant boundary

`companies.id` is the application tenant identifier. Customer-scoped tables reference it directly or inherit scope through their canonical parent. API access is always evaluated against the current user's allowed companies.

## Canonical configuration items

`configuration_items` stores the stable CI identity, type, lifecycle status, source summary and structured attributes. Provider object IDs do not become the primary key.

Related tables provide:

- `ci_identifiers`: serial numbers, UUIDs, SIDs and other matching evidence;
- `external_object_mappings`: durable provider-to-canonical links;
- `ci_source_observations`: time-stamped provider evidence and payload hashes;
- `ci_field_authority`: source precedence for canonical fields;
- `ci_relationships`: directed topology edges and impact policy;
- `reconciliation_candidates`: ambiguous identity decisions requiring review.

## CI layers

The visual model uses seven layers:

1. Physical / cloud foundation
2. Network and security
3. Storage and backup
4. Virtualization
5. Compute and operating systems
6. Applications and data
7. Business systems

An explicit `displayLayer` wins. When it is missing, the frontend and dashboard infer a safe default from CI type and role.

## Business systems

A business system is a first-class CI that describes the service recognised by business users. It can carry:

- business and service owners;
- sign-off delegate and requirement;
- department and user population;
- RTO and RPO;
- support hours and data classification;
- customer-facing status and business description.

`depends_on` relationships connect the business system to applications, databases and infrastructure. The UI traverses the same graph backwards to show the supporting full stack.

## Relationship types

| Type | Typical meaning |
|---|---|
| `depends_on` | A requires B to deliver its function |
| `installed_on` | Software runs on a host/VM |
| `hosts` | A platform hosts a workload |
| `stored_on` | Workload or datastore uses storage |
| `provided_by` | A logical service is provided by a platform |
| `protected_by` | A workload is protected by cluster/backup/redundancy evidence |
| `connected_to` | Network or physical connection |
| `managed_by` | A CI is managed by a platform or service |
| `backs_up` | Backup service protects a target; normally informational for outage propagation |
| `member_of` | Membership/grouping relation |
| `licensed_to`, `used_by`, `related_to` | Association according to the recorded context |

Dependency cycles and duplicate symmetric relationships are rejected by the API.

## Impact policies

| Policy | Behaviour |
|---|---|
| `required` | Failure propagates outage impact |
| `degraded` | Failure propagates degraded service |
| `redundant` | Alternate capacity may protect the dependent service |
| `informational` | Display only; excluded from impact traversal |

Impact direction is normalized by relationship type. For example, `A depends_on B` means a failure of B can affect A even though the stored edge is written from A to B.

## Change snapshots

`change_requests` holds the change record. Scope, calculated impact, revisions and external links are normalized into related tables. Impact snapshots intentionally duplicate selected CI attributes so a historical change document does not mutate when live inventory changes.

## Integration and audit records

`integration_connections` stores provider type, scope, public configuration and installation-bound encrypted credential material. API responses, audit values and portable exports remove that material. `provider_company_observations` retains the least-data ConnectWise discovery snapshot, while `external_object_mappings` holds the durable provider-company-to-canonical-customer decision. `sync_runs` records execution outcomes and `audit_events` captures security- and data-relevant mutations. Passportal credential content does not belong in any of these records.
