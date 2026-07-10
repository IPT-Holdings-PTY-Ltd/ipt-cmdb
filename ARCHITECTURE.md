# CMDB Hub architecture

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

The current browser demo can still run from JSON without Docker. When `DATABASE_URL` is set, the API runs against PostgreSQL and writes its current API state to `application_state`; the browser still has no database access. This is a deliberately temporary compatibility store while each endpoint moves to the canonical tables. Start PostgreSQL with `docker compose up -d postgres`, install `requirements.txt`, set `DATABASE_URL=postgresql://cmdb:cmdb@localhost:5432/cmdb`, then run `python scripts/migrate_postgres.py`. To retain an existing prototype dataset, run `python scripts/import_json_state.py` once before starting the PostgreSQL-backed API.

The next API migration replaces the JSON repository endpoint-by-endpoint behind a repository interface; it must not expose PostgreSQL to the frontend or use `external_id` fields as canonical keys.
