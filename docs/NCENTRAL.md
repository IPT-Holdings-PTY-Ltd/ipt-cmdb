# N-central customer and device inventory

IPT CMDB's N-central connector is deliberately read-only on the N-central side. It exchanges a permanent N-central User-API token for a short-lived access token, discovers customer organization units and devices, and sends selected device changes through the same reviewed canonical reconciliation pipeline used by other providers. It does not call N-central create, update or delete endpoints.

The implementation follows the current [N-central API explorer](https://console.n-sure.co.za/api-explorer/index.html#/). Confirm the requirements against the API documentation shipped with your own N-central version before production rollout.

## Before you start

1. Create a dedicated N-central user for IPT CMDB.
2. Assign only the roles and access groups needed to see the customers and devices that should enter the CMDB.
3. Disable MFA/2FA for this API user where required by N-central's User-API token flow. Do not reuse a human administrator identity.
4. Generate an **N-central User-API token**.
5. Record the HTTPS N-central server root, for example `https://ncentral.example.com`.

The permanent token is used only for `POST /api/auth/authenticate`. The returned access token is retained in process memory until shortly before expiry and is never written to PostgreSQL, logs, audit records or browser storage. The refresh token is not retained.

## Choose one configuration method

### Root administration wizard

Open **MSP workspace > Administration > Integrations**, install **N-central**, and select **Change configuration**. The User-API token is write-only and stored as an AES-GCM encrypted bundle bound to the installation's stable `MFA_ENCRYPTION_KEY`.

The wizard:

1. explains the provider boundary and prerequisites;
2. stores the server root and permanent token;
3. progressively tests token exchange, access-token validation and a one-record organization read;
4. discovers only `CUSTOMER` organization units for explicit mapping;
5. loads N-central device filters and the accessible device class/status catalogue for a mapped customer;
6. saves native filter ID, class/status filters, canonical type mappings, exclusions,
   enrichment profile and a manual or continuous-preview schedule;
7. queues a durable preview of create, update, identity-link, unchanged and conflict
   decisions before a platform administrator selects imports.

### Container, Docker secret or Azure

Set the server and exactly one permanent-token source:

```text
NCENTRAL_BASE_URL=https://ncentral.example.com
NCENTRAL_USER_API_TOKEN=<permanent User-API token>
NCENTRAL_PAGE_SIZE=250
```

For a Docker/Kubernetes secret mount:

```text
NCENTRAL_BASE_URL=https://ncentral.example.com
NCENTRAL_USER_API_TOKEN_FILE=/run/secrets/ncentral_user_api_token
NCENTRAL_PAGE_SIZE=250
```

`NCENTRAL_API_TOKEN` remains a compatibility alias for older deployments. New deployments should use `NCENTRAL_USER_API_TOKEN` or `NCENTRAL_USER_API_TOKEN_FILE`. Do not configure both a direct token and a token file. The supported page size is 25 to 1,000.

The supplied Compose files forward the same variable names to combined, web and worker
processes. A `_FILE` value must be a container-visible path; mount that read-only secret
at the same path in both services when using the split-worker overlay. Compose does not
mount a host file merely because its path is present in an environment variable.

Environment or secret-file settings take precedence over database settings and appear read-only in the wizard. In Azure Container Apps, use a Key Vault-backed secret reference. The integration lifecycle switch still blocks all provider requests when the connection is paused, disabled or removed.

## Customer boundaries

N-central organization IDs are provider-native identities. Exact customer names and pre-existing external IDs are suggestions only; an administrator must map each organization to one CMDB customer. A provider-side rename updates the observed label without moving the tenant boundary.

The wizard can create a new isolated CMDB customer and immediately record the explicit organization-ID mapping. It never creates an organization in N-central.

## Device filters and reconciliation

The selected N-central device filter ID is sent to `GET /api/org-units/{orgUnitId}/devices`, so broad estates are reduced by N-central before the CMDB applies saved class, status and explicit device-ID filters. Filter and type IDs—not display labels—are the durable policy values.

The normalized device record retains the provider device ID, name, device class,
license mode, OS label, customer/site references, last user/check-in information, and
bounded inventory evidence. Choose an enrichment profile according to the review:

| Profile | Provider detail reads | Recommended use |
|---|---:|---|
| **Fast** | None | Quick name/type/status reconciliation across a large estate |
| **Balanced** | First 25 matching devices | Normal preview with representative serial, model, manufacturer, IP and MAC evidence |
| **Full** | Up to 250 matching devices | Controlled duplicate/link review where stronger hardware evidence is needed |

`GET /api/devices/{deviceId}/assets` is one request per enriched device. Every profile
uses no more than four concurrent read-only requests, preserves result order and obeys
the saved provider/customer filters. The full profile is still bounded; it does not
create unbounded detail traffic for a large estate.

Identity precedence is:

1. an existing N-central device-ID mapping;
2. one unique strong identifier such as serial number or MAC address;
3. explicit administrator linking;
4. otherwise a new record or conflict.

Names never create an automatic identity link. This allows N-central and ConnectWise identities to converge on one canonical CI without relying on mutable labels. An explicit link consumes the current durable, pending review observation and validates its customer, provider parent and immutable external ID. It does not repeat N-central discovery, and the UI resolves the linked row locally instead of immediately launching another preview.

Import re-reads N-central and recalculates the decision before applying selected IDs. Only non-conflicting create, update and identity-link rows are selectable. Successful import changes IPT CMDB and its audit/mapping evidence only.

## Durable previews

**Preview devices** and **Sync saved policy now** enqueue a durable `sync_runs` record
and return immediately. The integration screen restores the latest run after a reload
and polls its phase, count and message until it succeeds, fails or is cancelled.

A worker claims each run with an expiring PostgreSQL lease and renews its heartbeat
during discovery, enrichment and reconciliation. An interrupted run is therefore
restart-safe: another worker can reclaim it after the lease expires. The following
controls remain explicit:

- **Cancel** requests cooperative cancellation. Pagination, enrichment scheduling and
  reconciliation stop at a safe boundary; an already-running HTTPS request is allowed
  to finish.
- **Retry** creates a new attributable run linked to the earlier attempt. It does not
  rewrite the original evidence.
- only a sanitized count/decision/error summary is retained with the run; complete
  N-central responses and device payloads are not stored there;
- sanitized provider observations that require review remain in the reconciliation
  queue until they are imported, linked, ignored or otherwise resolved.

## Worker and continuous-preview configuration

The durable manual-run consumer is always present in `combined` and dedicated `worker`
processes. The polling interval defaults to two seconds:

```text
INTEGRATION_WORKER_INTERVAL_SECONDS=2
```

`INTEGRATION_WORKER_ENABLED` controls only scheduled continuous-preview policies. Set
it after at least one saved policy has been reviewed:

```text
INTEGRATION_WORKER_ENABLED=true
```

Each customer policy has its own 15-minute to seven-day interval. Scheduled runs
refresh the durable review queue, use bounded exponential retry after failures and
never import canonical CIs automatically. Manual and scheduled runs share the same
policy lease and are safe across replicas.

A split deployment with `CMDB_PROCESS_ROLE=web` must run the dedicated worker service;
otherwise manual runs remain queued. A periodic `--once` job is suitable for scheduled
batch operation, but is not a substitute for a continuously available worker when
operators expect prompt previews, progress, cancellation and retry.

## Security and operational behaviour

- Provider requests are `POST /api/auth/authenticate` plus read-only `GET` operations.
- Permanent and temporary tokens are excluded from public APIs, audit values, telemetry and provider error messages.
- Provider response bodies and complete device payloads are not retained in run
  summaries or exposed on failed requests.
- HTTP 429 responses record only sanitized status, retry delay and request path.
- MSP operators may test and preview. Only platform administrators may store credentials, map customers, change policies, link identities or import devices.
- Pausing or disabling stops provider access but retains configuration and evidence. Removal erases stored credentials and editable connection configuration while retaining canonical CIs, mappings, reviews, sync runs and audit history.

## Troubleshooting

| Symptom | Check |
|---|---|
| `HTTP 401` during authentication | User-API token validity, correct server root and API-user status |
| `HTTP 401` after authentication | Access-token exchange response and N-central version/API compatibility |
| `HTTP 403` | User role and access-group scope for the requested customer/devices |
| No customer organizations | The API user can see `CUSTOMER` organization units, not only sites |
| Device filter returns no rows | Test the same filter and customer in N-central; saved filter IDs remain exact |
| Many devices lack serial/MAC evidence | Select **Full** for a controlled review or link important duplicates explicitly; every profile remains bounded |
| Stored token cannot be decrypted | Restore the stable `MFA_ENCRYPTION_KEY` used when it was saved |
| Manual preview remains queued | In a split deployment, start a dedicated worker; in combined mode, inspect worker health and sync history |
| Scheduled preview never runs | Enable `INTEGRATION_WORKER_ENABLED` and the customer policy, then inspect worker health and sync history |
| Cancellation is not immediate | An in-flight provider request completes before the cooperative cancellation boundary is reached |

N-central and N-able are trademarks of their respective owners. IPT CMDB uses the documented API but is not endorsed or certified by N-able.
