# Changelog

All notable changes to IPT CMDB are documented here. The project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and intends to use semantic versioning once stable releases begin.

## [Unreleased]

### Added

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
- Blank-database and forward-upgrade coverage for schema `2026.07.17.5`.

### Planned

- Read-only ConnectWise customer and configuration ingestion with mapping review.
- Reconciliation inbox and field-authority controls.
- Scheduled integration worker execution and richer diagnostics.

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
