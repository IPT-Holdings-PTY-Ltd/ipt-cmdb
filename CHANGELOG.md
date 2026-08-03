# Changelog

All notable changes to IPT CMDB are documented here. The project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and intends to use semantic versioning once stable releases begin.

## [Unreleased]

### Added

- Live readiness checks for PostgreSQL connectivity and the complete checksum-protected
  migration history, with bounded connection/statement timeouts and sanitized failures.
- Structured, redacted JSON diagnostics for API requests and worker heartbeats, using
  stable route templates, correlation IDs, status and duration without request secrets.
- Azure Monitor action-group notifications for readiness failures, application 5xx
  responses, stale worker telemetry, PostgreSQL availability and storage pressure.
- Backward-compatible `combined`, `web`, and `worker` process roles, with a reusable
  dedicated worker entry point and `--once` mode for scheduled container jobs.
- Optional Docker Compose worker overlay and zero-idle-cost Azure Container Apps worker
  deployment using the same application image and canonical PostgreSQL repository.
- Durable worker heartbeat, cycle, result and failure telemetry surfaced in the
  integration and notification administration screens.
- Sanitized ConnectWise HTTP/rate-limit telemetry that retains status, quota headers and
  request path only, without provider payloads, query strings or credentials.
- Guided read-only N-central setup using the documented User-API token exchange,
  bounded connection tests, customer organization discovery and explicit tenant mapping.
- Native N-central device-filter, class and status policies with immutable provider IDs,
  reviewed dry-run reconciliation, manual identity linking and lease-safe continuous
  previews.
- Restart-safe asynchronous N-central previews backed by durable `sync_runs`, with
  progress polling, lease heartbeats, stale-run recovery, cooperative cancellation and
  attributable retry lineage.
- Selectable fast, balanced and full N-central enrichment profiles, using bounded
  read-only detail requests without retaining complete provider payloads in run history.
- Provider-neutral reviewed import and linking controls so ConnectWise and N-central
  observations can converge on one canonical CI without provider-side writes.
- Cross-replica local-password reservations with identifier/source cooldowns,
  `Retry-After` guidance and bounded authentication audit evidence.
- Trusted-proxy deployment contracts for Docker and Azure Container Apps.
- Guided Windows and Linux appliance installers with atomic secret generation,
  immutable digest enforcement, Compose preflight, bounded startup waits and live
  plus schema-readiness verification.
- Authoritative runtime release metadata for the application version, source commit
  and deployed image digest in OCI labels and operator-safe health responses.
- Guarded release publishing with explicit database/rollback classification,
  multi-architecture attested images, machine-readable compatibility manifests,
  deterministic self-hosted bundles and stable-channel promotion only after the
  GitHub release succeeds.
- CI coverage that boots a fresh compact appliance from the built image and verifies
  its liveness, canonical PostgreSQL readiness and reported application version.
- Guarded Windows and POSIX update commands for compact appliances and external
  PostgreSQL deployments, with read-only release checks, exact version plus manifest
  checksum approval, verified recovery evidence, one-shot migrations, preserved worker
  state and manifest/schema-history-matched readiness verification.
- Bounded PostgreSQL migration lock and statement timeouts so blocked upgrades fail
  visibly instead of hanging indefinitely.

### Fixed

- Made balanced N-central detail enrichment retain its bounded rotation cursor, so
  larger estates advance beyond the first 25 devices on successive successful previews.
- Prevented partial N-central and GraphQL reads from replacing previously collected
  inventory with authoritative empty snapshots when a source section was not read.
- Separated N-central OS capability properties from genuine Windows Server roles and
  features, and added explainable Hyper-V host classification from host-side virtual
  switch adapter evidence without inventing guest relationships.
- Corrected selected-device N-central capability checks for the documented nested
  `data` response envelope and aligned GraphQL-derived counts with stored collections.
- Made Linux file-backed Compose secrets readable by the non-root CMDB and worker
  processes while retaining private `0700` directories, `0600` environment files and
  read-only `0444` secret files.
- Made the first N-central discovery-policy save install its root connection record
  atomically, preventing a first-use `Integration connection not found` response.
- Forwarded the documented N-central environment contract and compatibility token alias
  through development, production, appliance and split-worker Compose services.
- Removed production demo credentials from the local sign-in screen and made MFA
  challenge consumption single-use under concurrent requests.
- Standardized asset detail/edit requests on the canonical asset resource while keeping
  the legacy v2 read alias hidden for compatibility.
- Made API request headers body-aware so multipart, form, text and binary payloads are
  not mislabeled as JSON, including download requests.
- Replaced brittle test-file cleanup with isolated temporary directories.
- Standardized PostgreSQL row-lock clause ordering and retained the persisted MFA
  attempt count when concurrent requests reach or consume a challenge limit.
- Replaced the legacy one-file PostgreSQL migration helper with the same advisory-locked,
  checksum-protected forward-only migration plan used by the running application.
- Disabled the inherited HTTP health check on dedicated worker containers, which do not
  expose the web service port during Compose update waits.

### Security

- Stopped trusting caller-supplied `X-Forwarded-For` values unless the immediate peer
  is an explicitly configured proxy.
- Added dummy PBKDF2 work for unknown, disabled and non-local identities to reduce
  password-step account enumeration.
- Removed the duplicate raw bearer-token compatibility cache; all local sessions now
  resolve, revoke and expire through the repository's hashed-token store.
- Documented and narrowly suppressed a CodeQL password-taint false positive on the
  deterministic email rate-limit key; local passwords remain protected by the
  salted PBKDF2 password KDF.
- Made Python dependency auditing blocking and added Compose/Bicep deployment validation
  to CI.

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
