# Integration design

## Current status

| Provider | Current repository capability | Not yet implemented |
|---|---|---|
| ConnectWise PSA (formerly Manage) | Encrypted or environment-managed setup, connection test, paginated company discovery, explicit customer mapping, saved CI type/status policy, lease-safe continuous previews, governed canonical imports and audited sync evidence | Ticket publishing |
| N-central | Root connection placeholder and adapter boundary | Customer/device/site normalization and scheduled ingestion |
| Passportal | Root connection placeholder and metadata-only policy | Approved owner/folder/asset metadata association |

The application must not ingest Passportal passwords, secure notes, OTP seeds or credential values.

## Administration experience

The root **Administration > Integrations** page is an integration directory rather than a provider-specific setup form:

- **Installed** shows active connections, health, credential source, supported operations and links to provider configuration or the shared reconciliation queue.
- **Available** lists adapters that can be installed and clearly distinguishes roadmap providers whose reviewed adapter is not yet available.
- **Activity** combines sanitized sync, discovery and reconciliation evidence across providers.

Each installed connection owns a dedicated configuration workspace. ConnectWise currently uses
`/admin/integrations/connectwise` for its connection, tests, discovery scope, customer mappings and CI policy. The
guided steps remain available for both first installation and later configuration changes, but they no longer dominate
the directory. New providers should follow the same directory-to-workspace pattern instead of adding setup controls to
the overview.

The UI distinction mirrors the persistence model: a provider adapter is compiled capability metadata, an installed
connection is scoped configuration plus encrypted credentials, a policy controls repeatable discovery, and a sync run
is immutable execution evidence. These records must not be collapsed into one provider-specific object.

## Integration lifecycle

The root integration directory separates provider health from administrative lifecycle:

- **Active** permits tested manual provider calls and scheduled discovery.
- **Paused** is an immediate, reversible master stop. Credentials, schedules, policies and
  mappings remain intact, but workers cannot lease work and API operations cannot contact the
  provider.
- **Disabled** retains the installed integration and all evidence while requiring a platform
  administrator to explicitly re-enable it.
- **Removed** destroys encrypted database credentials and editable connection configuration.
  Canonical CIs, immutable provider mappings, source provenance, sync runs and audit history are
  retained so a later reinstall cannot silently duplicate records.

Every transition requires an operator reason and optimistic revision match and is written to the
audit trail. Before removal the UI reports affected customer mappings, policies, pending reviews,
imported CIs, CI identity mappings and historical runs. CI deletion is deliberately not part of
integration removal because those CIs may be related to business services or corroborated by other
providers.

Container-managed credentials do not bypass the lifecycle switch. Pausing, disabling or removing
an integration blocks use of its environment variables inside IPT CMDB. Removal cannot delete
variables held by Docker, Azure or a secret manager, so deployment operators must remove those
separately when decommissioning the external credential.

## Provider adapter contract

Reviewed built-in adapters register a capability manifest containing provider identity, supported scopes, authentication modes, prerequisites, discovery filters and bounded operations. Operations declare a direction (`input`, `output`, `bidirectional`, `trigger` or `action`), entity type, implementation status, provider-write behaviour and approval requirement.

The setup UI consumes the public manifest rather than duplicating provider capability descriptions. Provider code implements the shared `test_connection`, `discovery_options`, `discover`, `apply_filters` and `preview` boundary. `discovery_options` must be bounded and responsive so menus can populate independently from a full sync. ConnectWise is the reference adapter. New providers should reuse the registry and contract tests rather than add provider branches directly to the web application.

Arbitrary runtime package installation is not supported. Adapters are reviewed, compiled application code so a provider cannot supply executable frontend JavaScript or bypass tenancy, secret handling and audit controls.

## Pipeline contract

All providers should implement the same stages:

```text
collect -> normalize -> identify -> reconcile -> preview -> apply -> audit
```

### Collect

Read provider objects with pagination, rate-limit handling and an incremental cursor where supported. Keep raw payload retention short and encrypted; redact before logging.

### Normalize

Translate provider-specific fields into canonical customer, CI, identifier, ownership and lifecycle observations. Preserve the original provider object ID and observation timestamp.

Provider category mappings are policy, not identity. ConnectWise configuration-type mappings are stored per mapped customer using the immutable provider type ID and a canonical CMDB type label. Unmapped categories remain visible in preview evidence and can optionally be blocked from import.

### Identify

Prefer an existing external mapping. Otherwise use one unique strong identifier. A mutable name alone must not automatically merge records.

### Reconcile

Ambiguous matches become review candidates with evidence, confidence and conflicting fields. The operator must be able to select an existing CI, create a new one or ignore the observation.

The ConnectWise company workbench implements the first customer-mapping boundary. Exact names are suggestions only; an administrator must explicitly map them. The root Operations > Reconciliation workbench implements the corresponding durable CI decision queue and customer-specific field-authority policy. Recording either decision does not write to a provider.

### Preview and apply

Show created, updated, unchanged, blocked and conflicted counts before writes. Applying the same provider page twice must be idempotent.

Canonical apply remains a separate administrator decision. The workbench permits bounded bulk import only for non-conflicting ConnectWise observations from one saved customer policy. The provider is re-read before apply, immutable provider mappings are persisted, field authority is enforced again, and decision notes are attached to the sync evidence. There is no ConnectWise write in this path.

### Audit

Store the sync run, mapping decision, affected canonical IDs, field-authority decisions and sanitized failure details.

## Customer mapping

An MSP-level provider tenant often has a different customer identifier from IPT CMDB. Store explicit mappings between provider company IDs and canonical `companies.id`. Name matching can suggest a mapping but must not silently become the durable key.

When the provider company name changes, the mapping survives and the configured field-authority rule decides whether the canonical display name changes.

## Field authority example

| Field | Suggested authority |
|---|---|
| Device last seen and operational state | N-central |
| ConnectWise configuration reference | ConnectWise Manage |
| Customer/service ownership | ConnectWise Manage or an explicit CMDB override |
| Hardware serial/device UUID | Observed from the most reliable device source |
| Business owner, RTO and RPO | CMDB-managed business-system record |
| Passportal association | Passportal metadata only |

Manual overrides should remain explicit and must not be overwritten by a lower-priority provider.

Authority is evaluated at the most-specific matching CI type, falling back to `*`. Lower priority numbers win; equal priorities are an intentional tie. When no rule exists, the compatibility policy permits the incoming value. The supplied **Balanced MSP** and **CMDB protected** baselines create individually audited rules rather than hidden defaults.

## ConnectWise security

- Create a dedicated API member with the minimum read permissions required for company and configuration discovery.
- Configure the connection in the root Integrations screen, where API keys are encrypted with the installation key, or inject the complete `CW_*` environment set from a container secret store. Environment configuration takes precedence and is read-only in the UI.
- Use the regional API hostname, not the ConnectWise browser URL.
- Company discovery issues only `GET` requests, retains a small allow-list of customer metadata and never stores provider response bodies in error messages.
- Do not enable service-ticket creation until the read-only mapping path is proven.
- Future ticket publishing must use an idempotency/external-reference strategy and explicit technician confirmation.

See the [ConnectWise company discovery runbook](CONNECTWISE.md) for setup, permissions, mapping and troubleshooting.

## Worker behaviour

Only one active run should process a connection/cursor at a time. Workers should use bounded retries, persist checkpoints after durable writes, distinguish provider rejection from temporary failure and avoid logging full provider responses.

## Inputs, outputs and workflows

An integration is not synonymous with a sync. Provider manifests may expose scheduled or webhook inputs, controlled outputs, triggers and bounded actions. These layers remain separate:

```text
connector -> canonical event/observation -> workflow -> approval -> action executor -> audit
```

Provider writes are disabled by default and must be enabled per operation. Future output execution requires a dry-run payload, tenant validation, explicit approval where declared, an idempotency key, bounded retries and immutable result evidence. Generic outbound webhooks can later hand advanced orchestration to n8n, Power Automate or Azure Logic Apps without turning IPT CMDB into an unrestricted workflow-code host.
