# Governance, audit and reporting

CMDB Hub separates permanent governance evidence from diagnostic application telemetry.

## Access-control contract

Authentication establishes identity; protected resource routes then apply the required role and, for tenant-bound data, an effective customer-scope check. Browser navigation is a convenience only and is never the authorization boundary.

| Capability | Platform administrator | MSP operator | Customer reader |
| --- | --- | --- | --- |
| Read customer CIs, relationships, contacts, changes and governance data | All customers | Assigned customers | Assigned customer |
| Change customer CIs, relationships, contacts, ownership and change records | All customers | Assigned customers | No |
| Use MSP dashboards, audit, reports, integrations and sync history | Yes | Assigned-customer scope | No |
| Manage customer users, local passwords and personal tokens | Yes | Assigned customers | No |
| Create customers, MSP users or customer groups | Yes | No | No |
| Review RBAC or configure database and recovery | Yes | No | No |

MSP access groups are live policy. Direct customer grants and group-derived grants are kept separate, and the system group `All managed customers` follows the current tenant list. The authorization matrix is exercised against representative read, write, cross-customer and root-only endpoints in `test/test_fastapi_migration.py`; the PostgreSQL contract test verifies the same dynamic group behavior in the production repository.

### Customer access groups

Each manually managed group has a purpose description, accountable MSP owner, explicit customer membership, revision number and assigned-user count. Membership is maintained through searchable available/selected lists rather than an unbounded checkbox wall. Concurrent edits use the revision number to reject stale overwrites.

Before a group is deleted, the API calculates every assigned user and the exact customers for which that user would lose effective access after direct permissions and other groups are considered. The administrator must review that impact and type the group name to confirm deletion. Group assignment references are removed with the group while audit evidence is retained.

The schema supports `manual` and `dynamic` membership modes plus a controlled rule document. Only the system-owned `All managed customers` rule is currently executable. Additional tag- or service-based dynamic rules remain disabled until customer tagging, rule validation and access-review controls are implemented. Nested groups are intentionally unsupported to keep effective access explainable and cycle-free.

### Identity lifecycle

User records are retained as governance identities rather than hard-deleted. Administrators can update names, email addresses, roles and customer scope; disable and re-enable accounts; reset local-only passwords; or archive retired users. Email, role or tenant-scope changes revoke active browser sessions. Password changes, account disablement and archival also revoke active sessions. A signed-in administrator cannot disable or archive their own account, and the final active platform administrator cannot be demoted, disabled or archived.

Microsoft Entra-backed passwords remain owned by Entra ID and cannot be reset by the CMDB. Archived users remain visible in the identity directory and audit evidence, but cannot authenticate.

### Personal API tokens

Personal API access is disabled per user by default. When enabled by an authorised administrator, tokens:

- use separate `cmdb:read` and `cmdb:write` scopes;
- can be narrowed below the user's effective customer scope;
- expire after no more than 365 days;
- are shown once, stored only as SHA-256 hashes and individually revocable;
- are rejected immediately when the owner is disabled, archived or has API access turned off;
- cannot call identity, RBAC, branding, database, recovery or integration-administration endpoints.

Token creation, use metadata and revocation are attributable. Portable backups exclude token hashes, so restored environments require deliberate token re-issuance.

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
