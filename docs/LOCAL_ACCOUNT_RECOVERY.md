# Local account password recovery

IPT CMDB supports self-service password changes and email-based recovery for local
accounts. Microsoft Entra identities continue to manage passwords and recovery in
Entra ID.

## Prerequisites

1. Configure and verify [Microsoft 365 email delivery](MICROSOFT_365_EMAIL.md).
2. Set `PUBLIC_BASE_URL` to the exact externally reachable application origin, for
   example `https://cmdb.example.com`.
3. Use HTTPS for every non-local deployment. Only `localhost`, `127.0.0.1`, and
   `::1` may use HTTP.

`PUBLIC_BASE_URL` is deliberately deployment configuration. The API never builds a
security link from the inbound `Host` header. Azure IaC defaults it to the Container
App HTTPS origin; set the `publicBaseUrl` Bicep parameter when a custom domain is used.

## User flows

- **Forgot password:** the login page sends a single-use link to an eligible local
  account. The response is the same for known and unknown addresses.
- **Change password:** **My security** requires the current password and, when
  enabled, the current TOTP or a recovery code.
- A successful change signs out every browser session. A recovery reset revokes
  personal API tokens by default; the user can opt out. MFA enrollment is preserved.
- A security notice is queued after a successful password change. The reset email
  itself is sent ephemerally so its raw link never enters the durable outbox.

## Security controls

- Recovery tokens use a cryptographically secure random value. Only its SHA-256
  hash is stored.
- Links expire after 30 minutes, are single-use, and invalidate other live reset
  links for the account after completion.
- Requests are limited to three per normalized email address in 15 minutes and 20
  per hashed requester address in one hour. PostgreSQL advisory locks keep those
  limits consistent across replicas.
- The reset page and API responses use `Referrer-Policy: no-referrer` so the token is
  not forwarded to another site.
- Requests and completions are audited without recording the email input, password,
  raw token, or requester IP address.

The API returns the same accepted message even when email is unavailable or an
account does not exist. This avoids account discovery. Administrators should monitor
the email outbox for delivery failures and use the existing administrative reset only
after verifying the user through an approved support process.

## Operational verification

After deployment:

1. Confirm `/api/auth/config` reports `passwordResetAvailable: true`.
2. Request a reset for a dedicated test local account.
3. Open the received link and set a new password.
4. Confirm the old password and an earlier browser session no longer work.
5. Confirm the audit center contains `password_reset_requested`,
   `password_reset_email_accepted`, and `password_reset_completed`, and that the
   non-sensitive password-change notice appears in the email outbox.
