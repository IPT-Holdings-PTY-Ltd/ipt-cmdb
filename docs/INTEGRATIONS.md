# Integration design

## Current status

| Provider | Current repository capability | Not yet implemented |
|---|---|---|
| ConnectWise Manage | Root connection definition, environment configuration and safe connectivity/sync-run boundary | Reviewed company/configuration ingestion and ticket publishing |
| N-central | Root connection placeholder and adapter boundary | Customer/device/site normalization and scheduled ingestion |
| Passportal | Root connection placeholder and metadata-only policy | Approved owner/folder/asset metadata association |

The application must not ingest Passportal passwords, secure notes, OTP seeds or credential values.

## Pipeline contract

All providers should implement the same stages:

```text
collect -> normalize -> identify -> reconcile -> preview -> apply -> audit
```

### Collect

Read provider objects with pagination, rate-limit handling and an incremental cursor where supported. Keep raw payload retention short and encrypted; redact before logging.

### Normalize

Translate provider-specific fields into canonical customer, CI, identifier, ownership and lifecycle observations. Preserve the original provider object ID and observation timestamp.

### Identify

Prefer an existing external mapping. Otherwise use one unique strong identifier. A mutable name alone must not automatically merge records.

### Reconcile

Ambiguous matches become review candidates with evidence, confidence and conflicting fields. The operator must be able to select an existing CI, create a new one or ignore the observation.

### Preview and apply

Show created, updated, unchanged, blocked and conflicted counts before writes. Applying the same provider page twice must be idempotent.

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

## ConnectWise security

- Create a dedicated API member with the minimum read permissions required for company and configuration discovery.
- Keep private/public keys and client ID in Key Vault-backed environment settings.
- Use the regional API hostname, not the ConnectWise browser URL.
- Do not enable service-ticket creation until the read-only mapping path is proven.
- Future ticket publishing must use an idempotency/external-reference strategy and explicit technician confirmation.

## Worker behaviour

Only one active run should process a connection/cursor at a time. Workers should use bounded retries, persist checkpoints after durable writes, distinguish provider rejection from temporary failure and avoid logging full provider responses.
