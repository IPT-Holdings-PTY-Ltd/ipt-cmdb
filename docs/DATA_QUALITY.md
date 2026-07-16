# Data quality and reconciliation

The Governance > Data quality screen is available at MSP and customer scope. The API enforces the same company boundary as assets and relationships; changing a URL cannot broaden access.

## Quality score

The score is a prioritization aid rather than a compliance certification. It evaluates active canonical CIs for accountable ownership, business-system recovery metadata, relationship coverage, integration freshness, operational status, stable provider identity and duplicate signals.

Findings include evidence, a recommended correction and a severity. The MSP view ranks customers by score and high-priority count.

## Exceptions

An MSP operator or platform administrator may suppress a finding only by recording a reason and optional expiry. The CI is not modified. Exceptions are tenant-scoped and create attributable audit events.

## Reconciliation decisions

Ambiguous observations are reviewed as use an existing CI, create a new CI, or ignore the source record. This release records and audits the decision. It intentionally does not execute external writes; future workers will consume approved decisions idempotently.

## Source authority

Authority rules are customer-specific and select which provider wins for a canonical field. A lower priority number is more authoritative. Typical policies make N-central authoritative for device health, ConnectWise authoritative for configuration references, and CMDB-managed records authoritative for business ownership and recovery objectives.

## API surface

- `GET /api/data-quality`
- `POST /api/data-quality/exceptions`
- `DELETE /api/data-quality/exceptions/{id}`
- `GET /api/reconciliation-candidates`
- `PATCH /api/reconciliation-candidates/{id}`
- `GET|PUT|DELETE /api/field-authority`
