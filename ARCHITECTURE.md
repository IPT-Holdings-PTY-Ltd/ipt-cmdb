# CMDB Hub architecture

## Incremental web-platform migration

The public container runs FastAPI and serves a compiled React/TypeScript application built with React Admin, Material UI and React Flow. The retired browser UI, its static bundle and the internal compatibility HTTP server have been removed. Every active browser endpoint is now handled directly by a typed FastAPI route.

New API work belongs in typed FastAPI routes with Pydantic request/response models and PostgreSQL repositories. The remaining transition boundary is storage-only: `app.py` contains domain helpers and the temporary state repository, but no HTTP handler. Customers, users, access groups, configuration items, relationships, change packages, integration connections and sync history are read and written through `PostgresCmdbRepository` when PostgreSQL is available. Their canonical UUID records and write audit events live in normalized tables; the state document is refreshed as a portable fallback/backup mirror.

Azure Container Apps Easy Auth is the production identity boundary. With `AUTH_MODE=easy_auth`, FastAPI reads the platform-injected Entra principal and maps the email claim to an assigned CMDB user. Tenant and record authorization remains enforced by the Python API. Local-development and break-glass credentials are stored separately as salted PBKDF2-SHA256 hashes; plaintext passwords are never persisted.

The browser is a separate, static frontend. It calls an authenticated API and never receives a database connection string or integration credential. The API owns authorization, canonical CMDB writes and audit events. Integration workers run separately from the web process; they obtain provider credentials from Key Vault, write normalised observations, and enqueue review work where identity is uncertain.

```text
Static SPA (Azure Static Web Apps or Container Apps)
          │ HTTPS / OIDC
          ▼
CMDB API (Container Apps) ──────► PostgreSQL (private endpoint)
          │                              ▲
          ├──► Service Bus / outbox ──────┤
          ▼                              │
Sync workers / Container Apps Jobs ──────┘
          │
          └──► ConnectWise, N-central, Passportal (read-only credentials in Key Vault)
```

## Identity and rename rules

`configuration_items.id` is the canonical UUID used by the UI, relationships, ownership and audit history. It never changes when a provider renames an item. `external_object_mappings` binds that canonical ID to an integration connection, object type and provider-specific ID. This makes the same device linkable to a ConnectWise configuration ID, an N-central device ID and a Passportal metadata object.

Workers reconcile in this order:

1. Existing `(connection, external object type, external ID)` mapping.
2. A single verified strong identifier (serial, BIOS/device UUID, SID or hashed licence key).
3. A pending review candidate; mutable names are useful evidence, never automatic identity.
4. New canonical CI only when no safe match exists.

Integration connections are scope-aware: a connection with no `company_id` is an MSP/root tool (such as a ConnectWise or N-central tenant connection); a future connection with a `company_id` is customer-scoped. Credentials remain a Key Vault reference in either case. The portal currently exposes only MSP tools in the Root level workspace, leaving customer-scoped integration controls for a later, explicitly permissioned screen.

Each source observation is retained with a timestamp and payload hash. `ci_field_authority` decides which provider may update a given field. For example, N-central can be authoritative for device name/last seen, ConnectWise for customer/service ownership, and Passportal only for approved metadata links. A lower-priority provider rename is stored as an observation rather than silently replacing the canonical name.

## Local development

The browser demo can still run from JSON without Docker. When `DATABASE_URL` is set, startup takes an advisory migration lock, applies the idempotent schema before querying any table, records the schema version, then seeds an uninitialised database according to `DATABASE_SEED_MODE`. All tenant, identity, CMDB, change-control, integration operational records and the MSP brand profile then use canonical tables. Change scope, frozen impact paths, immutable revisions and external publishing state are stored separately from live CI records. Integration rows store only environment or Key Vault credential references—never secret values—and allow either MSP-wide or future customer-scoped connections. Small PNG/JPEG MSP logos are stored as constrained data URLs so portable exports stay self-contained; this contract can later be backed by Azure Blob Storage. The API keeps a synchronized mirror for portable export and fallback during the remaining customer-theme migration. The browser never has database access.

The application initialises schemas and operational seed data; it does not create PostgreSQL servers or databases. Docker Compose owns local provisioning, while Azure infrastructure-as-code owns production provisioning. `DATABASE_URL` supplied by the environment is treated as managed configuration and cannot be overwritten in the browser unless the explicit local-only `ALLOW_UI_DATABASE_CONFIG` override is enabled.

Portable export format version 2 includes an integrity checksum and a restore preview. It intentionally excludes PostgreSQL roles/grants, canonical audit history, raw source observations and transaction history. Production recovery uses Azure PostgreSQL point-in-time restore or independently scheduled `pg_dump`/`pg_restore`.

The final application-state migration moves separately permissioned customer theme overrides into normalized tables. It must not expose PostgreSQL to the frontend or use provider `external_id` values as canonical keys.
