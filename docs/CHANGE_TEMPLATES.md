# Change-template governance

Change templates reduce repetitive typing without turning a previous change into an unsafe copy-and-paste source. They contain reusable technical procedure text and prompts; each change still calculates its own CMDB impact and requires a technician to review the result.

## Scope and permissions

- **Global standards** are available in every customer workspace and can only be managed by a platform administrator.
- **Customer procedures** are available only inside one customer workspace and can be managed by an MSP user with management access to that customer.
- Customer readers can use published templates only when their role permits change creation. Draft and retired content never appears in the technician picker.
- A template cannot change the selected customer, CIs, schedule, calculated impact or risk, assigned technician, owner identities, sign-off delegates or approval evidence.

The management screen is available from **MSP workspace → Operations → Change templates**.

## Lifecycle and versions

Every template starts at version 1. Any edit, publication or retirement creates the next immutable version under optimistic concurrency:

| State | Technician use | Intended purpose |
|---|---|---|
| Draft | Hidden | Authoring and internal review |
| Published | Available | Approved operational use |
| Retired | Hidden | Preserve history without new use |

Existing changes pin the template identity, exact version, content snapshot and validated input values. Editing or retiring a template does not alter an existing change or its PDF. Deletion is intentionally replaced by retirement.

Assign a procedure owner and review date for each maintained template. The root library can search by name, stable key, description or tags and filter by customer and lifecycle.

## Authoring

The procedure controls these defaults:

- change type, category, priority and expected outage;
- expected duration as guidance;
- title, reason, business impact, implementation, validation, rollback and communication text;
- suggested approver responsibility as guidance.

Use tokens such as `{{target_version}}` in text. Add a parameter with the same stable key so the wizard can prompt the technician. Supported input types are text, multiline text, number, select and boolean. Select inputs require an explicit option list.

Three built-in context tokens need no parameter:

- `{{company_name}}`
- `{{asset_name}}`
- `{{business_system_name}}`

The `scope_primary_name` parameter source auto-fills a prompt from the first selected CI. The technician can review every generated field and adapt it to the specific change before saving.

## Seeded standards

New installations receive six published, MSP-wide starting procedures:

1. Windows server patching
2. Network firmware upgrade
3. Certificate renewal
4. Database maintenance
5. Firewall rule change
6. Emergency service restoration

These are practical baselines, not automatic approval. Duplicate a standard when a customer or internal policy requires different steps, then publish the scoped copy after review.

## Evidence, backup and integration

Template creation and every new version are audited. A saved change records template provenance and parameter values with its normal immutable CMDB impact snapshot. PostgreSQL portable export includes template identities and all versions; database-native backup remains the production recovery mechanism.

Stable template keys and immutable version numbers are suitable for future ConnectWise change-type mapping or workflow selection. External integrations should resolve a published template by ID/key, supply valid typed parameters, and submit the exact version they previewed.
