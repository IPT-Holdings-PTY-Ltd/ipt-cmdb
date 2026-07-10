# Security policy

## Supported use

This project is an early foundation. It is suitable for local development and controlled evaluation. Do not expose the demo login, local JSON store, or development credentials to the internet.

For production, use Microsoft Entra ID/OIDC, Azure Database for PostgreSQL, Key Vault-backed secrets, encrypted backups, HTTPS, least-privilege provider API members, and network restrictions.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or exposed credential. Contact the repository owner privately with:

- a concise description and impact;
- reproduction steps or proof of concept;
- affected version/commit; and
- any suggested mitigation.

Do not include customer data, passwords, private keys, or backups in the report.

## Sensitive data boundaries

- Application backup exports include operational and user data and must be protected.
- The browser must never receive database or provider secrets.
- Passportal passwords, secure notes, and credential values are out of scope for CMDB synchronisation.
