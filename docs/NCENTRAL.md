# N-central customer and device inventory

IPT CMDB's N-central connector is deliberately read-only on the N-central side. It exchanges a permanent N-central User-API token for a short-lived access token, discovers customer organization units and devices, and sends selected device changes through the same reviewed canonical reconciliation pipeline used by other providers. It does not call N-central create, update or delete endpoints.

The optional N-able platform GraphQL path is an additive enrichment source. REST remains
available for N-central-version-specific device, service and lifecycle reads; GraphQL can
add joined hardware, operating-system, network, health, reboot, Azure VM, patch and
vulnerability-management evidence. The connector never sends GraphQL mutations.

Use the API explorer bundled with your own N-central server at
`https://<your-n-central-server>/api-explorer/index.html#/`. API availability can vary
between N-central releases, so confirm the connector requirements against the explorer
and documentation shipped with the server you are integrating before production rollout.

## Before you start

1. Create a dedicated N-central user for IPT CMDB.
2. Assign only the roles and access groups needed to see the customers and devices that should enter the CMDB.
3. Disable MFA/2FA for this API user where required by N-central's User-API token flow. Do not reuse a human administrator identity.
4. Generate an **N-central User-API token**.
5. Record the HTTPS N-central server root, for example `https://ncentral.example.com`.

For GraphQL enrichment, separately generate an **N-able platform API token** in N-able
Login/API Token Management. This is not the N-central User-API token. Give the creating
identity access only to the customers that should be visible to IPT CMDB; the token
inherits that identity's scope.

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
6. can run a value-free **Inventory capability check** against one mapped customer
   before deeper reads are enabled;
7. saves native filter ID, class/status filters, canonical type mappings, exclusions,
   enrichment profile and a manual or continuous-preview schedule;
8. queues a durable preview of create, update, identity-link, unchanged and conflict
   decisions before a platform administrator selects imports.

The optional GraphQL panel stores its token separately, tests the platform endpoint, and
requires a mapped organization whose GraphQL type is exactly `Customer`. A matching
Partner or Service Organization label is never accepted as a tenant boundary. After the
connection test, the searchable Customer catalogue contains up to 500 token-visible
Customer records so a typical MSP is not limited to the provider's first page. After the
Customer scope is saved, **Detect server identity** performs a bounded, read-only
identity check and compares GraphQL device IDs with exact REST device IDs from the mapped
N-central customer. Only one positively corroborated server identity can be preselected;
the administrator must still review and save it explicitly. Detection requires a stored
GraphQL credential but can run while GraphQL enrichment is disabled.

Asset enrichment is deliberately refresh-driven. **Refresh cache** performs the bounded
provider read and caches only records that report the saved
`ncentralDevice.server.id`, a `deviceId`, and the saved Customer scope. Preview reads that
tenant-scoped cache (and labels expired evidence stale); normal REST reconciliation uses
only unexpired rows whose `deviceId` exactly matches a REST device. It never makes an
implicit GraphQL call. Patch-installation evidence remains a separate live, read-only
query and is not merged into base inventory.

### Container, Docker secret or Azure

Set the server and exactly one permanent-token source:

```text
NCENTRAL_BASE_URL=https://ncentral.example.com
NCENTRAL_USER_API_TOKEN=<permanent User-API token>
NCENTRAL_PAGE_SIZE=250
NCENTRAL_GRAPHQL_ENABLED=false
NCENTRAL_GRAPHQL_ENDPOINT=https://api.n-able.com/graphql
NCENTRAL_GRAPHQL_API_TOKEN=<N-able platform API token>
NCENTRAL_GRAPHQL_PAGE_SIZE=100
NCENTRAL_GRAPHQL_SERVER_ID=<optional advanced fallback; exact ncentralDevice.server.id>
```

For a Docker/Kubernetes secret mount:

```text
NCENTRAL_BASE_URL=https://ncentral.example.com
NCENTRAL_USER_API_TOKEN_FILE=/run/secrets/ncentral_user_api_token
NCENTRAL_PAGE_SIZE=250
NCENTRAL_GRAPHQL_ENABLED=false
NCENTRAL_GRAPHQL_ENDPOINT=https://api.n-able.com/graphql
NCENTRAL_GRAPHQL_API_TOKEN_FILE=/run/secrets/ncentral_graphql_api_token
NCENTRAL_GRAPHQL_PAGE_SIZE=100
NCENTRAL_GRAPHQL_SERVER_ID=<optional advanced fallback; exact ncentralDevice.server.id>
```

`NCENTRAL_API_TOKEN` remains a compatibility alias for older deployments. New deployments should use `NCENTRAL_USER_API_TOKEN` or `NCENTRAL_USER_API_TOKEN_FILE`. Do not configure both a direct token and a token file. The supported page size is 25 to 1,000.

Likewise, configure only one of `NCENTRAL_GRAPHQL_API_TOKEN` and
`NCENTRAL_GRAPHQL_API_TOKEN_FILE`. Keep `NCENTRAL_GRAPHQL_ENABLED=false` until the
wizard's read-only test and Customer-scoped server detection have been reviewed. Enable
enrichment only when the saved identity and scope are ready for a cache refresh. The
GraphQL endpoint is pinned to `https://api.n-able.com/graphql`; arbitrary hosts and
redirects are rejected so the bearer token cannot be forwarded elsewhere.

`NCENTRAL_GRAPHQL_PAGE_SIZE` is bounded to 1-100. Normally the wizard detects the server
identity after a GraphQL Customer is selected. `NCENTRAL_GRAPHQL_SERVER_ID` is the
advanced deployment fallback for supplying that exact identity to environment-managed
installations; an asset is linked only when both this value and GraphQL
`ncentralDevice.deviceId` match the REST source.

The supplied Compose files forward the same variable names to combined, web and worker
processes. A `_FILE` value must be a container-visible path; mount that read-only secret
at the same path in both services when using the split-worker overlay. Compose does not
mount a host file merely because its path is present in an environment variable.

Environment or secret-file settings take precedence over database settings and appear read-only in the wizard. In Azure Container Apps, use a Key Vault-backed secret reference. The integration lifecycle switch still blocks all provider requests when the connection is paused, disabled or removed.

## N-central server identity detection

`ncentralDevice.server.id` is the immutable N-able GraphQL identifier for the N-central
server that owns a reported `ncentralDevice`. It is an opaque provider value. It is not
the N-central URL or hostname, a REST or GraphQL Customer ID, a Site ID, the GraphQL asset
`id` or `guid`, or the N-central device ID.

One REST connector is anchored to one N-central server root, but an N-able platform token
can expose assets from more than one N-central server. The wizard therefore does not use
the first observed server, a display name, or the apparent URL. Its detection flow is:

1. save the exact GraphQL `Customer` organization scope for the explicitly mapped REST
   customer;
2. run **Detect server identity**, which makes a bounded GraphQL identity read inside
   that saved scope and a read-only REST device read for the mapped N-central customer;
3. group the returned GraphQL devices by `ncentralDevice.server.id` and count exact
   `ncentralDevice.deviceId` to REST device-ID matches;
4. preselect an identity only when the evidence resolves to one positively corroborated
   server; and
5. review the match count and explicitly select **Save server identity** before cache or
   enrichment controls become available, then enable GraphQL enrichment when ready to
   refresh the scoped cache.

Detection never stores the recommendation automatically. A bounded result is labelled so
the operator can review whether the sample is representative. If no device ID
corroborates the server, more than one server remains possible, or the provider omits
either half of the identity, the application does not guess. GraphQL cache publication
and REST enrichment remain locked until an administrator supplies and saves one exact
server identity and enables enrichment; the ordinary REST preview and import path
continues to work.

The platform-administrator-only endpoint is
`POST /api/integrations/ncentral/graphql/server-candidates`. Its sample limit is 1-500
(100 by default). The static `source_server_detection` operation requests only the saved
Customer ID and `ncentralDevice.deviceId + server.id`; it rechecks every returned Customer
ID before using the evidence. Raw GraphQL and REST device IDs remain process-local. The
browser receives only each server ID's GraphQL-device count, exact REST-match count,
confidence, truncation status and optional recommendation. Provider, configuration,
mapping or scope errors return no recommendation and do not change configuration.

For an ambiguous multi-server result, compare the displayed exact REST match counts and
select only a server whose evidence belongs to this REST connection. If the evidence is
still inconclusive, use another mapped Customer with overlapping REST and GraphQL devices
or stop and verify the provider identities outside IPT CMDB. **Advanced: enter server ID
manually** bypasses automatic corroboration and should be used only with the exact opaque
`ncentralDevice.server.id` returned by N-able GraphQL.

For an environment-managed connection, place that reviewed value in
`NCENTRAL_GRAPHQL_SERVER_ID` and restart the application. Environment configuration takes
precedence and cannot be changed in the wizard. Supplying an ID manually or through the
environment does not weaken later linking: every cached asset must still have the exact
pair `(saved server ID, GraphQL ncentralDevice.deviceId)`, and no cached evidence can
enrich a CI unless the device ID exactly matches the REST source. Missing, mismatched,
duplicate or cross-customer identities remain unmatched; names, serial numbers and MAC
addresses are never substitutes for this crosswalk.

## Customer boundaries

N-central organization IDs are provider-native identities. Exact customer names and pre-existing external IDs are suggestions only; an administrator must map each organization to one CMDB customer. A provider-side rename updates the observed label without moving the tenant boundary.

GraphQL returns Partner, Service Organization, Customer and Site organization objects.
Even when several levels share the same display name, enrichment accepts only the exact
mapped `Customer` object. Every asset page is requested with that immutable Customer ID;
an asset whose returned customer ID escapes the requested scope fails the preview closed.

The wizard can create a new isolated CMDB customer and immediately record the explicit organization-ID mapping. It never creates an organization in N-central.

## Device filters and reconciliation

The selected N-central device filter ID is sent to `GET /api/org-units/{orgUnitId}/devices`, so broad estates are reduced by N-central before the CMDB applies saved class, status and explicit device-ID filters. Filter and type IDs—not display labels—are the durable policy values.

The normalized device record retains the provider device ID, name, device class,
license mode, OS label, customer/site references, provider check-in time, and bounded
inventory evidence. Logged-in users, service accounts, executable paths, remote-control
URIs, tokens, passwords and software licence keys are deliberately discarded.

Choose an enrichment profile according to the review:

| Profile | Provider detail reads | Recommended use |
|---|---|---|
| **Fast** | No per-device asset reads | Quick name/type/status reconciliation across a large estate |
| **Balanced** | `/assets` for up to 25 matching devices; the saved cursor advances after each successful preview | Normal continuous inventory that rotates through larger estates without a full read every run |
| **Full** | `/assets`, lifecycle, service-monitor and maintenance-window reads for up to 250 devices, plus bounded customer active issues | Controlled deep inventory and operational-state review |

Every profile uses no more than four concurrent per-device read-only workers, preserves
result order and obeys the saved provider/customer filters. Optional full-profile
endpoints are skipped safely when the N-central version or API role does not expose
them. A failure to enrich one device does not discard the rest of the preview. The
full profile is intentionally more expensive; keep **Balanced** as the normal default.
The balanced cursor is aggregate run metadata only. It does not retain provider records,
and a failed or cancelled run does not advance the last successfully published window.

The capability check uses a maximum of three devices and reports only endpoint access,
field names, JSON types and counts. It never returns provider values. Use it to verify
whether the dedicated API user can read:

- device details and asset inventory;
- lifecycle planning information;
- service-monitor status and maintenance windows;
- customer active issues; and
- custom-property response shape.

Custom-property names and values are not imported. These fields are user-defined and
can contain credentials or other secrets.

## Technical inventory and provenance

An approved import or explicit immutable-ID link stores sanitized technical collections
separately from the canonical CI:

- hardware, BIOS, operating system, processors and memory modules;
- every reported network interface, including bounded IP, MAC, gateway, DNS and VLAN
  observations;
- physical disks and logical volumes;
- bounded installed-software summaries and explicit server roles/features when those
  named sections are supplied;
- allow-listed N-central OS capabilities (such as PowerShell version) kept separate
  from Windows Server roles and features;
- monitoring state, active-issue summaries and maintenance windows;
- lifecycle dates; and
- virtualization classification and explicit host/guest/cluster/storage IDs when the
  provider supplies them.

Collections are content-addressed and retain a bounded history. Repeated check-in
timestamps do not create a new inventory version, stale observations cannot roll back a
newer snapshot, and a partial preview cannot erase a collection it did not read. An
absent provider section means **not reported** and retains the prior snapshot; a present
empty section is an authoritative empty observation.
newer snapshot, and disappeared NICs are retired rather than erased. The asset screen
shows current data, collection freshness, coverage and source evidence in separate
technical tabs.

## Relationship discovery and guarded automation

VM and Hyper-V host classification can be suggested from explicit virtualization
inventory, manufacturer/model evidence and installed Hyper-V features. Classification
does not prove topology. A virtualization relationship candidate is emitted only when
N-central supplies immutable device IDs for both endpoints, for example a guest device
ID and its host device ID. The evidence records the versioned
`ncentral_explicit_virtualization_identity` detector so the decision remains explainable
after provider labels change.

Network inventory can produce a lower-confidence, review-only `provided_by` suggestion
when a device's configured DNS or DHCP server IP address resolves to exactly one mapped
CI in the same customer. Duplicate or ambiguous address ownership suppresses the hint.
This detector is deliberately ineligible for automatic approval: an address is useful
operational evidence, but it is not immutable identity.

The connector never infers a relationship from a name, site, subnet, default gateway or
proximity in an address range. It also cannot currently derive physical switch-port
topology from ordinary N-central interface inventory. `connected_to` switch and network
edges require explicit LLDP/CDP neighbour data or another provider source that identifies
both endpoints and the connection.

Provider relationship suggestions enter a tenant-scoped pending review queue on the
relationship screen. The proposed-relationships overlay can display these candidates on
the map, but proposed edges are visually distinct and excluded from impact calculation
until they become canonical. A customer-authorized administrator can approve, reject or
ignore each item with notes. Approval atomically creates a relationship with provider
provenance, source mapping, confidence and bounded evidence. An unchanged repeated
observation increases its observation count; materially revised evidence receives a new
fingerprint and revision so an earlier reject or ignore cannot silently decide different
evidence.

The saved device policy defaults to `review`, which means every suggestion needs an
administrator decision. An administrator may explicitly choose `auto_explicit` and an
allow-list of relationship types. In the current N-central wizard the automatic type
allow-list is limited to `hosts`, `member_of` and `stored_on`. A candidate is eligible
only when all of these controls pass:

- both endpoints resolve through stable provider mappings in the same customer;
- its type is selected in the saved allow-list;
- its detector is approved for immutable provider identity (currently explicit
  virtualization identity only);
- confidence meets the saved threshold (0.98 by default);
- the unchanged evidence has been observed enough times (two by default); and
- the latest observation is within the freshness window (72 hours by default).

Failing any gate leaves the candidate in review and records the reason; it does not
weaken the policy or guess. Auto-approval is disabled unless both `auto_explicit` and at
least one relationship type are selected. Refreshing or retiring a candidate never
deletes a canonical relationship. A manual relationship remains authoritative and is
never replaced, downgraded or deleted by provider discovery or automation.

Identity precedence is:

1. an existing N-central device-ID mapping;
2. one unique strong identifier such as serial number or MAC address;
3. explicit administrator linking;
4. otherwise a new record or conflict.

Names never create an automatic identity link. This allows N-central and ConnectWise identities to converge on one canonical CI without relying on mutable labels. An explicit link consumes the current durable, pending review observation and validates its customer, provider parent and immutable external ID. It does not repeat N-central discovery, and the UI resolves the linked row locally instead of immediately launching another preview.

GraphQL identities are stored as additional source evidence. The safe crosswalk to REST
is the exact pair `(ncentralDevice.server.id, ncentralDevice.deviceId)`; GraphQL asset
`id` and `guid` remain separate identities. A GraphQL asset ID, hostname, serial number,
MAC address or display name is never assumed to equal the REST device ID.

## GraphQL enrichment and performance

The connector uses a static, reviewed query catalogue rather than accepting arbitrary
GraphQL text from the browser. Cursor pagination, page limits, response-size limits and
timeouts are enforced in the backend. Both `data` and top-level `errors` are checked so
partial GraphQL responses cannot silently publish incomplete evidence.

Normal device enrichment retrieves only allow-listed identity, OS, system, chassis,
BIOS, CPU, memory, disk, network, agent, reboot, vulnerability-status and Azure VM fields.
Patch installations/compliance and detailed vulnerability detections are separate
capabilities because they
can be materially slower than normal asset inventory. Their results are cached with a
short expiry and must not block customer mapping, device linking or ordinary preview.

GraphQL exposes Azure VM identity, but does not currently provide a general Hyper-V or
VMware host-to-guest edge. Manufacturer/chassis/BIOS evidence may classify a CI as a
virtual machine; it cannot create a canonical `hosts` relationship without an immutable
provider relationship identity.
REST or GraphQL network inventory can classify a physical server as a probable Hyper-V
host when it reports the host-side `Hyper-V Virtual Ethernet Adapter`/`vEthernet ...
Virtual Switch` signature. That evidence is labelled and confidence-scored; it never
invents guest identities or a host-to-guest edge.

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

## Missing-device lifecycle

IPT CMDB treats a missing N-central device as evidence about its immutable source
mapping, not as permission to delete or retire the canonical CI. Only a successful,
complete and unfiltered device snapshot can prove absence. By default, a mapping becomes
eligible for review after both **three consecutive complete snapshots** have missed it
and **24 hours** have elapsed since the first qualifying absence. Administrators can
adjust these safeguards between 2-10 snapshots and 1-720 hours in the saved policy.

Native N-central filtering makes the snapshot unsuitable for absence decisions, even
though returned devices can still be reconciled normally. Incomplete, cancelled,
failed, lease-lost or stale policy/connection runs do not advance the absence counter.
Local type, status and import-exclusion rules likewise do not turn a returned provider
identity into a missing device.

The **Missing devices** queue uses these states:

| State | Operator meaning |
|---|---|
| **Observed** | N-central returned the device and its source mapping is active. |
| **Monitoring** | A complete unfiltered snapshot missed the device, but the count or elapsed-time safeguard has not been met. |
| **Eligible** | Both safeguards have been met; an administrator may review the source link for retirement. Nothing is changed automatically. |
| **Not evaluated** | Filtering, an incomplete read or another safety gate means the run cannot prove absence. |
| **Retired** | An administrator explicitly deactivated the N-central source mapping. |
| **Restore ready** | N-central has observed the previously retired immutable identity again; explicit restoration is available. |

Retire and restore are platform-administrator decisions. Each action requires notes and
the current item revision, so stale screens cannot overwrite newer evidence. Retirement
deactivates only the N-central source link: it does not delete or retire the canonical
CI, and it preserves manually maintained relationships. While the provider mapping is
inactive it cannot be used for import updates, identity matching or new provider-derived
relationship edges.

Action evidence also expires. The valid window is the greater of 24 hours or three
saved policy intervals, capped at 168 hours. After that window, retirement and
restoration are blocked until a new complete preview supplies fresh evidence.

Reappearance never silently reactivates a retired mapping. It moves the item to
**Restore ready**; an administrator must inspect the fresh evidence and explicitly
restore it, again with notes and the current revision. The mapping then returns to
**Observed** and can participate in reviewed imports and relationship discovery.

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
- GraphQL provider requests are allow-listed `query` operations only; mutations and
  browser-supplied query documents are rejected by design.
- Permanent and temporary tokens are excluded from public APIs, audit values, telemetry and provider error messages.
- Provider response bodies and complete device payloads are not retained in run
  summaries or exposed on failed requests.
- Technical inventory keeps only normalized allow-listed values; custom-property values
  and credential-like fields are never stored.
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
| Server identity is not detected | Save an exact GraphQL `Customer` scope, verify that the mapped REST customer and GraphQL Customer share at least one device ID, and rerun detection; REST remains available |
| Several N-central server identities are shown | Review exact REST match counts; do not choose by name or order. Use another mapped Customer when the bounded evidence is inconclusive |
| Detection is marked bounded | Treat the counts as a sample. Use a mapped Customer with a clear overlapping device set before saving the identity |
| Environment-managed server identity cannot be saved | Copy the reviewed exact value to `NCENTRAL_GRAPHQL_SERVER_ID`, restart the application, and verify the reported effective configuration |
| GraphQL inventory is visible but nothing is cached or merged | Confirm the saved server ID matches `ncentralDevice.server.id`; enrichment also requires an exact REST-matching `ncentralDevice.deviceId`, and unmatched evidence fails closed |
| Many devices lack technical evidence | Run the capability check, verify `/assets` access, then select **Full** for a controlled review |
| Full preview is slow | Use **Balanced** for routine operation; Full deliberately adds lifecycle, monitoring and maintenance reads |
| No host-to-VM edge is proposed | Confirm N-central supplied immutable host/guest device IDs; names and shared networks are intentionally insufficient |
| Stored token cannot be decrypted | Restore the stable `MFA_ENCRYPTION_KEY` used when it was saved |
| Manual preview remains queued | In a split deployment, start a dedicated worker; in combined mode, inspect worker health and sync history |
| Scheduled preview never runs | Enable `INTEGRATION_WORKER_ENABLED` and the customer policy, then inspect worker health and sync history |
| Cancellation is not immediate | An in-flight provider request completes before the cooperative cancellation boundary is reached |

N-central and N-able are trademarks of their respective owners. IPT CMDB uses the documented API but is not endorsed or certified by N-able.
