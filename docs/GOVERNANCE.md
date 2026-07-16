# Governance, audit and reporting

CMDB Hub separates permanent governance evidence from diagnostic application telemetry.

## Audit ledger

Every governed mutation produces an append-only `audit_events` row in the same PostgreSQL transaction as the data change. Events include:

- UTC occurrence time;
- customer scope, when applicable;
- attributable user or system actor;
- source system, category, action, outcome and severity;
- entity identity and a readable name snapshot;
- sanitised before/after documents and a field-level change list;
- request and correlation identifiers;
- bounded, sanitised request context.

The database rejects updates and deletes against `audit_events`. Portable operational exports deliberately exclude the ledger; PostgreSQL backup and retention controls remain the source of truth for audit recovery.

### Redaction

Audit payloads redact password, token, cookie, secret, credential, private-key, connection-string and embedded-logo values. Values are depth-, count- and length-bounded. Provider adapters must pass only normalised, non-secret metadata into audit context.

### Tenant boundaries

Customer workspaces can query only their customer audit events. MSP roles can query the MSP view, constrained by their effective customer access. Platform administrators can view platform-wide events. The same rules apply to the audit-activity report.

## Request correlation

The API accepts safe `X-Request-ID` and `X-Correlation-ID` headers and generates them when omitted. Both values are returned in response headers and attached to audit events created during the request. Integration workers should send a stable correlation identifier for a complete sync or reconciliation run.

## Audit Center

The Governance > Audit activity screen provides:

- customer-aware chronological activity;
- category, outcome, date and text filters;
- actor, source and entity context;
- field-level before/after inspection;
- correlation data for operational investigation.

Configuration-item detail pages also include an entity-filtered activity timeline.

## Reports Center

The Reports Center uses controlled templates rather than arbitrary database queries. Current templates cover:

- asset register;
- lifecycle and renewal attention;
- ownership gaps;
- business-system register;
- change register;
- audit activity;
- effective access review at platform-admin scope;
- MSP integration health.

Reports are generated from the caller's effective tenant scope. The browser preview is capped at 100 rows while downloads contain the complete result. PDF, filterable XLSX and UTF-8 CSV are supported. Each download creates its own audit event.

## Diagnostic telemetry

Request diagnostics are emitted separately through Python logging with request ID, correlation ID, route, status and duration. Container deployments can forward this stream to OpenTelemetry/Azure Monitor without changing the permanent audit ledger.

## Future controls

Planned extensions include report schedules, Azure Blob artifact retention, access-review certification workflow, configurable retention/archival policy and daily trend snapshots. These should build on the current tenant and audit boundaries rather than introduce separate authorization logic.
