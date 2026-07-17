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

Local password resets are administrative actions and revoke the user's active sessions. Entra-backed passwords must be changed through Entra ID. Disabled and archived CMDB users are denied both browser-session and personal-token authentication.

Local accounts support RFC 6238 TOTP. TOTP seeds are encrypted with AES-256-GCM and bound to the user identifier; `MFA_ENCRYPTION_KEY` must be a stable 32-byte key supplied through Key Vault or the container secret store. Do not rotate or remove it without a controlled seed re-encryption plan. Seeds, provisioning URIs, authenticator codes and recovery codes must never appear in logs, audit payloads, portable exports or support tickets.

Browser sessions and password-verified MFA challenges are stored in PostgreSQL using SHA-256 token hashes. Challenges expire after five minutes and five attempts. Accepted TOTP time steps are persisted to prevent replay. Recovery codes are high-entropy, one-use values stored only as salted PBKDF2 hashes. Administrative MFA reset is restricted to platform administrators, requires explicit target-email confirmation, and requires local administrators to repeat their password plus an enrolled MFA factor. The reset reason, optional ticket reference, verification method and session revocation are audited without retaining credentials or codes.

### Personal API tokens

Personal tokens are intended for bounded CMDB automation, not interactive administration. Keep API access disabled unless required, grant the minimum read/write and customer scope, use short expiries and revoke unused tokens. Raw token values are returned once; only SHA-256 hashes are persisted. Personal tokens cannot access platform-administration APIs and are deliberately omitted from portable backup files.

### Secrets

- Keep `DATABASE_URL` and provider credentials in Key Vault or an equivalent secret store.
- Keep `MFA_ENCRYPTION_KEY` in Key Vault and use the same value across every application replica.
- Never expose secrets through frontend configuration, logs, API responses or portable exports.
- Use dedicated, least-privilege provider identities.
- Rotate any secret that is accidentally committed or shown in an issue; deleting it from the latest commit is not sufficient.

### Passportal

IPT CMDB is not a password vault. Passwords, secure notes, OTP seeds, private keys and credential values must never be collected or stored. Only explicitly approved ownership/folder/customer/asset metadata may be associated.

### Integration writes

External writes are disabled by default. A future write path must be scoped, idempotent, previewed, explicitly confirmed and audited. ConnectWise ticket publishing must not be coupled to merely viewing or saving a CMDB change draft.

### Backups and exports

Portable exports contain operational and user-related data. Encrypt them, restrict access and define retention. They deliberately exclude password hashes, MFA seeds, recovery codes, browser sessions, login challenges and personal-token hashes. They do not replace PostgreSQL backup and point-in-time recovery.

## Supported versions

Until the first stable release, security fixes are applied to the latest `main` branch and newest tagged pre-release only.
