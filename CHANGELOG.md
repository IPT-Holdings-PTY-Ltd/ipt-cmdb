# Changelog

All notable changes to IPT CMDB are documented here. The project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and intends to use semantic versioning once stable releases begin.

## [Unreleased]

### Added

- Backward-compatible `combined`, `web`, and `worker` process roles, with a reusable
  dedicated worker entry point and `--once` mode for scheduled container jobs.
- Optional Docker Compose worker overlay and zero-idle-cost Azure Container Apps worker
  deployment using the same application image and canonical PostgreSQL repository.
- Durable worker heartbeat, cycle, result and failure telemetry surfaced in the
  integration and notification administration screens.
- Sanitized ConnectWise HTTP/rate-limit telemetry that retains status, quota headers and
  request path only, without provider payloads, query strings or credentials.

## [0.3.0] - 2026-07-28

### Added

- Root-managed ConnectWise PSA setup with AES-GCM encrypted API keys or environment-owned configuration, a least-data connection test and bounded paginated company discovery.
- Review-gated ConnectWise-to-CMDB customer mapping with exact-match suggestions, immutable provider IDs, retained mapping history and attributable audit evidence.
- Capability-driven provider registry with typed input/output/action declarations and ConnectWise as the reference adapter; future provider writes are visible but disabled.
- Guided ConnectWise setup with progressive tests, audited discovery filters, read-only dry-run counts and explicit customer mapping.
- Automatically populated ConnectWise filter menus with bounded one-page sampling, explicit empty/loading states and responsive dry-run previews.
- Per-customer ConnectWise CI policies with immutable type/status IDs, audited revisions, explicit exclusions and saved continuous-preview intervals.
- Cross-provider canonical identity linking with reviewed same-customer manual links, provider-ID mappings and tenant/type-scoped strong identifiers.
- Lease-safe continuous ConnectWise CI previews with per-customer schedules, durable review observations, audited dismissals, evidence-change reopening and zero automatic imports.
- Lease-aware **Sync now** execution, customer-aware run history and bounded sync-history filters for ConnectWise operations.
- Bounded exponential retry scheduling for failed continuous previews, with rate-limited Microsoft 365 failure alerts and recovery notifications.
- Provider-neutral reconciliation workbench with exact paging/search, customer/provider/decision filters, side-by-side evidence, explicit identity linking and bounded bulk decisions.
- Reversible provider-object ignores keyed to immutable external IDs, with bulk policy safeguards, mandatory reasons, an ignored-items workbench and audited restore.
- Enforced customer field authority during ConnectWise preview and canonical import, including protected-field evidence, source provenance and audited baseline presets.
- Change approval batches derived from structured business-system ownership, with Microsoft 365 invitations, expiring single-use public review links, multi-approver completion, decline handling and immutable decision evidence.
- Technician-facing approval delivery/status controls and a branded, login-free external sign-off page scoped to each approver's affected business systems.
- Asset-linked change history with tenant-safe reverse lookup, named affected-CI summaries and scope/direct/downstream impact context on both change and asset views.
- Governed change reassignment and unassignment with customer-scoped technician selection, optimistic concurrency, immutable assignment history, audit evidence, optional Microsoft 365 notification and assignment-aware register filters.
- Governed global and customer change-template library with six seeded procedures, typed technician prompts, ownership, review dates, draft/publish/retire lifecycle and immutable versions.
- Template-first change authoring with safe token rendering, pinned procedure provenance and parameter evidence while retaining CMDB-derived scope, impact, risk, identities and approvals.
- Guided post-change review and closure with template-governed validation tests, evidence capture, outcome and service-state rules, risk-based PIR and stakeholder acceptance gates, accountable follow-up actions, immutable audit evidence and expanded change PDFs.
- Governance navigation with MSP- and customer-scoped Audit Center and Reports Center.
- Append-only audit v2 schema with actor attribution, source, outcome, severity, sanitised field changes and request correlation.
- Authentication, authorization-denial, portable-export, recovery and report-download audit coverage.
- Configuration-item activity timelines.
- Asset, lifecycle, ownership, business-system, change, audit, access-review and integration-health report templates.
- Branded PDF, filterable XLSX and UTF-8 CSV report downloads.
- Governed user lifecycle, session-revoking password changes and restricted personal API tokens.
- Scalable customer-group directory with searchable membership assignment, accountable ownership, optimistic revisions and delete-impact review.
- RFC 6238 TOTP for local accounts with AES-GCM encrypted seeds, QR enrollment, replay protection, one-use recovery codes and governed reset.
- Two-stage password/MFA login challenges and browser sessions persisted as hashes in PostgreSQL for container replicas.
- Self-service **My security** controls plus per-user and environment-level MFA enforcement policy.
- Platform-admin-only MFA reset with local administrator step-up verification, typed target confirmation, optional ticket evidence, discoverable disabled states, detailed audit metadata and immediate target-session revocation.
- Replaced root user-management customer checkboxes with a scalable, search-driven scope selector, collapsed selections, filtered bulk management, group/direct/effective counts and assignment-source preview.
- Blank-database and forward-upgrade coverage through schema `2026.07.24.6`.
- Guarded semantic-version release automation that reuses CI, publishes attested multi-architecture GHCR images and generates categorized GitHub releases.

### Changed

- Migrated the frontend shell, authentication boundary, navigation, notifications and asset CRUD from React Admin to application-owned MUI components on React Router 8.3.
- Raised the supported frontend runtime to Node.js 22.22.2 or newer in local development, CI and container builds.
- Hardened the React 19 migration with recoverable lazy routes, Router-native deep links, semantic navigation, Vitest regression coverage and React hooks/accessibility linting in CI.

### Security

- Removed the React Router 7 and `react-router-dom` dependency tree affected by GHSA-qwww-vcr4-c8h2.
- Added a blocking npm advisory audit to CI so high-severity frontend dependency findings cannot be merged unnoticed.

## [0.2.0] - 2026-07-16

### Added

- React Admin and FastAPI web-platform architecture with the retired legacy workspace removed.
- PostgreSQL canonical repositories, forward-only checksum migrations and blank/upgrade verification scripts.
- Microsoft Entra ID Easy Auth-compatible container authentication with configurable headers and strict local break-glass controls.
- Business-system catalogue with business owners, sign-off, RTO/RPO and full-stack filtering.
- Layered relationship perspectives for business impact, applications, virtualization, storage, network and full-stack topology.
- Virtualization, cluster, host, datastore, storage and HA-aware impact modelling.
- Relationship-aware asset inventory with layer/application/attention filters, ownership, freshness and CSV export.
- MSP operational dashboard and customer business-system/action dashboard.
- Guided change-control workflow with saved impact snapshots and branded PDF generation.
- MSP branding for screens and reports, customer branding storage and audited configuration.
- PostgreSQL setup, portable export/import preview and recovery guidance.
- Root customer, group, user, RBAC, integration and database administration screens.

### Changed

- PostgreSQL now fails closed instead of silently falling back to local JSON state.
- Provider and canonical identities are separated through stable mappings and reconciliation evidence.
- Relationship edges include explicit impact policies.
- Integration controls are root-scoped while retaining a future customer-scoped connection model.

### Security

- Server-enforced tenant and role checks across the typed API.
- Local credentials stored as salted PBKDF2-SHA256 hashes.
- Passportal restricted to metadata-only integration design.
- External provider writes remain disabled by default.

## [0.1.0] - 2026-07-10

### Added

- Initial multi-tenant MSP/customer workspace.
- ITIL-aligned CI metadata, lifecycle ownership, renewal and end-of-life tracking.
- Interactive relationship topology and change-impact foundation.
- Docker image, PostgreSQL schema and initial Azure service descriptor.
