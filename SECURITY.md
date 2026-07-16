# Security policy

## Project maturity

IPT CMDB is a pre-release project under active development. Local demo credentials and demo data are provided for evaluation and must not be exposed to the internet.

Production deployment requires an external identity boundary, PostgreSQL, HTTPS, protected secrets, network restrictions, tested recovery and removal or rotation of all development identities.

## Reporting a vulnerability

Do not open a public GitHub issue for a suspected vulnerability, exposed credential or customer-data problem. Contact the repository owner privately through the organisation's established security channel.

Include:

- a concise description and potential impact;
- affected version, tag or commit;
- safe reproduction steps or proof of concept;
- whether customer data or credentials may be exposed;
- suggested mitigation when known.

Do not include real passwords, API keys, Easy Auth principal payloads, database strings, customer exports or Passportal content in the report.

## Security boundaries

### Tenant isolation

Every customer-scoped API operation must enforce the current user's allowed company set. Root-only functions require an MSP or platform role. Hiding a menu item is never sufficient authorization.

### Authentication

Local authentication is for development. Production should use Microsoft Entra ID through a trusted gateway such as Azure Container Apps/App Service authentication. The gateway must remove spoofed inbound identity headers before injecting verified values.

`ALLOW_LOCAL_BREAK_GLASS`, `ALLOW_UI_DATABASE_CONFIG` and `ALLOW_LOCAL_DEVELOPMENT` should remain `false` in production.

### Secrets

- Keep `DATABASE_URL` and provider credentials in Key Vault or an equivalent secret store.
- Never expose secrets through frontend configuration, logs, API responses or portable exports.
- Use dedicated, least-privilege provider identities.
- Rotate any secret that is accidentally committed or shown in an issue; deleting it from the latest commit is not sufficient.

### Passportal

IPT CMDB is not a password vault. Passwords, secure notes, OTP seeds, private keys and credential values must never be collected or stored. Only explicitly approved ownership/folder/customer/asset metadata may be associated.

### Integration writes

External writes are disabled by default. A future write path must be scoped, idempotent, previewed, explicitly confirmed and audited. ConnectWise ticket publishing must not be coupled to merely viewing or saving a CMDB change draft.

### Backups and exports

Portable exports contain operational and user-related data. Encrypt them, restrict access and define retention. They do not replace PostgreSQL backup and point-in-time recovery.

## Supported versions

Until the first stable release, security fixes are applied to the latest `main` branch and newest tagged pre-release only.
