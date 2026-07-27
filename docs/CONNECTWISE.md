# ConnectWise PSA company discovery

IPT CMDB's ConnectWise provider is deliberately read-only on the ConnectWise side. It proves the connection, discovers and maps companies, and supports reviewed configuration-item reconciliation into the canonical CMDB. It does not create or modify ConnectWise records.

ConnectWise PSA is the current name for the product formerly known as ConnectWise Manage. Use the ConnectWise Developer Network and your regional API hostname when creating the API member and keys.

## Before you start

1. Create a dedicated ConnectWise API member for IPT CMDB.
2. Grant only the permissions required to read companies. Do not grant service-ticket write access for this release.
3. Create the public/private API-key pair and obtain a ConnectWise client ID.
4. Confirm the regional REST root, for example `https://api-<region>.myconnectwise.net/v4_6_release/apis/3.0`.
5. Keep the private key in your secret manager. It is shown by ConnectWise only at creation time.

## Choose one configuration method

### Root administration screen

Open **MSP workspace > Administration > Integrations**. Install ConnectWise from **Available**, or select **Change configuration** on the installed ConnectWise card. Enter the REST root, ConnectWise company ID, client ID and API keys in the dedicated configuration workspace. The public and private keys are write-only in the UI and are stored as an AES-GCM encrypted bundle bound to this installation's `MFA_ENCRYPTION_KEY`.

The installed card also exposes **Manage lifecycle**. Pausing is the recommended emergency or
maintenance stop because it immediately prevents both manual and scheduled ConnectWise requests
without changing mappings or schedules. Disabling is intended for a longer administrative
shutdown. Removal erases encrypted connection credentials and editable configuration after showing
the retained-data impact and requiring the exact integration name. Imported CIs, company and CI
identity mappings, review evidence, sync runs and audit records are preserved.

Keep `MFA_ENCRYPTION_KEY` stable across upgrades and replicas. Losing or rotating it without a controlled migration makes the stored provider credentials unreadable.

### Container or Azure environment

Inject the complete set below through your platform's secret/configuration facility:

```text
CW_BASE_URL=https://api-<region>.myconnectwise.net/v4_6_release/apis/3.0
CW_COMPANY_ID=<ConnectWise company identifier>
CW_PUBLIC_KEY=<dedicated API member public key>
CW_PRIVATE_KEY=<dedicated API member private key>
CW_CLIENT_ID=<ConnectWise client ID>
CW_PAGE_SIZE=100
```

If any required `CW_*` value is present, all required values must be present. Environment settings take precedence over database settings and appear read-only in the administration screen. For Azure Container Apps, reference Key Vault secrets from the container environment rather than placing secrets in source-controlled parameters.

Environment credentials remain outside the application database. IPT CMDB's lifecycle state is
therefore the master kill switch: a paused, disabled or removed connection cannot use `CW_*`
values. Removing the integration does not remove container or Azure settings; remove those through
the deployment platform when permanently revoking the API member. An environment-managed removed
installation can be restored from the lifecycle dialog after the variables have been reviewed.

## Guided setup

The ConnectWise wizard follows six stages:

1. **Provider** shows prerequisites and the adapter's declared input, future input and disabled output capabilities.
2. **Connection** stores the API-member settings or explains the environment-managed connection.
3. **Tests** progressively verifies configuration, authentication and company-read permission. No provider write is attempted.
4. **Filters & preview** automatically loads company statuses, types and company territories into the menus, then stores an audited policy for those values, deleted records and explicit provider-ID exclusions. ConnectWise site records are deliberately not used as territories. A preview reads and filters a bounded sample without persisting observations or mappings.
5. **Customer mapping** persists included observations and lets an administrator explicitly map each immutable provider ID to one CMDB customer. If the customer does not exist yet, **Create customer** opens a reviewed form that creates the isolated CMDB customer and immediately records the explicit provider-ID mapping; it never writes the customer back to ConnectWise.
6. **CI reconciliation** loads the mapped company's ConnectWise type and status catalogue, stores immutable-ID filters, maps provider configuration-type IDs to canonical CMDB types and previews create, update, identity-link, unchanged and conflict decisions.

Status and type choices plus previews use one read-only page of up to 1,000 company records so the wizard remains responsive. Territory choices are collected from the territory references across all accessible company pages using a territory-only field projection, so values are not omitted merely because no company in the preview sample uses them. The UI reports when the preview sample is truncated. Full review-gated discovery automatically uses 1,000-record pages, requests only the company fields required for matching, and retains a hard 100-page safety limit. A ConnectWise tenant with roughly 7,200 companies therefore needs about eight company requests rather than more than seventy 100-record requests. Stored observations contain only provider ID, identifier, name, status, type, company territory, deleted flag and provider update timestamp. Exact-name or known-external-ID matches remain suggestions and are never applied automatically.

Mappings use the immutable ConnectWise company ID. A later company-name change therefore updates the observed label without losing the canonical customer association. Unmapping deactivates the mapping and preserves audit history.

## Configuration-item reconciliation

After a ConnectWise company is explicitly mapped, the final wizard step can read that company's `/company/configurations` collection. The request is constrained by the immutable numeric company ID; vendor notes, questions and other unreviewed provider payloads are discarded by the adapter. The normalized record retains operational identity and inventory fields such as configuration ID, name, type, status, serial number, model, tag, device identifier, IP/MAC addressing, operating-system summary, site and provider update version.

Each mapped customer has an independently revisioned CI policy. Type and status selections are stored using ConnectWise IDs while the current names are display labels only, so a provider-side rename does not silently widen or narrow the sync scope. The policy also supports explicit configuration-ID exclusions and either manual reviewed import or a saved continuous-preview interval. Continuous-preview policy never enables automatic canonical imports or ConnectWise writes.

### Configuration type mapping

The CI policy contains a customer-specific mapping from the immutable ConnectWise configuration-type ID to a canonical CMDB type such as **Server**, **Virtual machine**, **Network device** or **Software**. The provider type name remains visible as evidence, but an ordinary rename in ConnectWise does not break the mapping.

**Auto-map exact names** safely fills only provider names that exactly match the canonical catalogue. Administrators must review the remaining rows. When **Block unmapped configuration types from import** is enabled, an included CI whose provider type has no mapping becomes a conflict and cannot be imported. With blocking disabled, the provider type name is retained for backwards compatibility and the preview reports the unmapped count explicitly.

### Continuous preview and review queue

The final wizard step also exposes a durable customer-scoped review queue. Every manual or scheduled preview refreshes current `create`, `update`, `link` and `conflict` observations while summary evidence remains in sync history. Unchanged observations disappear from the current queue.

Administrators have two deliberately different noise controls:

- **Dismiss** records a decision against the current observation. It remains dismissed only while its normalized provider evidence is unchanged and reopens automatically when that evidence changes.
- **Ignore configuration** creates an audited, reversible suppression keyed to the immutable ConnectWise configuration ID. The ID is added to the customer CI policy, so future manual and continuous previews exclude it even if its name or attributes change. The Ignored tab records the reason, actor and time and provides a governed restore action. Ignore and restore never delete a ConnectWise record, canonical CI or identity mapping.

Bulk ignore is limited to records from one customer integration policy. Restoring an ID immediately returns its last known observation to review and schedules an enabled continuous-preview policy to revalidate it; it does not import the configuration.

To enable scheduled previews in a container deployment:

```text
INTEGRATION_WORKER_ENABLED=true
INTEGRATION_WORKER_INTERVAL_SECONDS=60
NOTIFICATION_WORKER_ENABLED=true
NOTIFICATION_WORKER_INTERVAL_SECONDS=60
INTEGRATION_ALERT_RECIPIENTS=integration-ops@example.com
```

`INTEGRATION_ALERT_RECIPIENTS` accepts a comma- or semicolon-separated list. When it is blank, active platform-administrator email addresses are used. Alerts require an enabled Microsoft 365 email connection in `configured` or `verified` state plus the notification worker. A failed policy queues an alert on failure 1, 2, 4, 8 and so on, which keeps an extended outage visible without sending on every retry. A successful unattended run after failures queues one recovery message.

The polling interval is bounded between 15 and 3,600 seconds. Each customer policy retains its own 15-minute to seven-day preview interval. PostgreSQL leases use `FOR UPDATE SKIP LOCKED`, so multiple application replicas can safely have the worker enabled without executing the same due policy concurrently. A failed call releases the lease, records sanitized sync evidence and uses bounded exponential retry delays of 15, 30, 60 minutes and so on up to 24 hours. A successful run returns to the customer policy's normal interval; no canonical import occurs.

**Sync now** runs the saved filter and mapping policy immediately while taking the same exclusive lease. It is therefore safe to use during normal scheduler operation and returns a conflict if another replica is already running that policy. The wizard's sync-history table shows the customer, trigger, status and discovered/reviewed counts for recent ConnectWise runs.

For a small MSP or dedicated customer instance, enabling the worker in the single application container is the simplest supported topology. At larger scale the same execution function can move to a dedicated worker service while sharing PostgreSQL, policy leases and the review queue.

The preview never changes either system. It classifies each record as:

- **New** — no provider mapping, strong-identifier match or possible name duplicate exists.
- **Changed** — an immutable provider mapping or unique strong identifier resolves one CI and provider-managed values differ.
- **Identity link** — a unique strong identifier resolves an unchanged CI that still needs the provider-ID mapping.
- **Unchanged** — the existing immutable mapping resolves a CI with the same managed values.
- **Conflict** — strong identifiers disagree, a mapping target is unavailable or a mutable-name duplicate requires human review.

Only new, changed and identity-link rows can be selected. Import re-reads ConnectWise and recalculates every decision before applying the selected IDs, so a stale preview cannot silently overwrite newer evidence. Successful imports create or update canonical CIs, store a `provider_native` identifier, upsert the external-object mapping, append a content-addressed source observation and emit audit plus sync-run evidence. Conflicts remain excluded for the governed Data quality workflow. ConnectWise is read-only throughout.

For duplicate candidates, **Link existing** lets a platform administrator explicitly attach the immutable ConnectWise configuration ID to an existing same-customer canonical CI. Multiple provider mappings may point to that one CI, which allows later N-central and Passportal identities to converge on the same object even when names or mutable attributes differ. Existing mappings win, unique strong identifiers can suggest a link, and names never create an automatic identity match.

## Security and audit behaviour

- Discovery performs `GET` requests only; no ConnectWise write endpoint is implemented.
- Dry-run preview records attributable execution evidence but does not retain provider observations or change mappings.
- The adapter manifest declares available company and configuration-item inputs plus a future change-ticket output. Provider output remains disabled.
- API keys are never returned by the API, included in audit before/after values or written to provider error messages.
- Provider response bodies are not retained on failed requests.
- Each connection test, discovery run, mapping and unmapping action is attributable in the audit ledger.
- Continuous previews are opt-in at both deployment and policy level; they populate review evidence only.
- Manual **Sync now** and scheduled work share one exclusive policy lease.
- Failures use bounded exponential backoff and rate-limited email alerts; recovery is also reported.
- Review dismissals require notes and are reopened by changed provider evidence.
- Durable ignores require notes, use immutable provider IDs and remain excluded until restored.
- MSP operators can test and discover. Only platform administrators can change credentials or mappings.
- The production PostgreSQL repository is canonical. Local JSON state remains a development-only mode.

## Troubleshooting

| Symptom | Check |
|---|---|
| `HTTP 401` | ConnectWise company ID, public/private key pair, API-member status and client ID |
| `HTTP 403` | API-member company read permission and security role |
| Environment configuration is incomplete | Supply every required `CW_*` value or remove all of them and use the UI |
| Stored credentials cannot be decrypted | Restore the same `MFA_ENCRYPTION_KEY` used when the keys were saved |
| Discovery reaches the safety limit | Reduce the visible company scope or raise `CW_PAGE_SIZE`; the hard page limit intentionally cannot exceed 100 |
| A suggested company is wrong | Ignore the suggestion and explicitly choose the correct CMDB customer |
| An enabled policy never runs | Set `INTEGRATION_WORKER_ENABLED=true`, restart the container and check the worker/status chips in CI reconciliation |
| Failure alerts are not ready | Enable and verify Microsoft 365 email, enable `NOTIFICATION_WORKER_ENABLED`, and configure `INTEGRATION_ALERT_RECIPIENTS` or active platform-administrator email addresses |
| Sync now says the policy is already running | Wait for the active lease/run to finish, refresh the status and retry; do not bypass the lease |
| Review item returns after dismissal | Its normalized provider evidence changed, so the platform deliberately reopened the decision |

ConnectWise is a trademark of ConnectWise, LLC. IPT CMDB uses the ConnectWise API but is not endorsed or certified by ConnectWise.
