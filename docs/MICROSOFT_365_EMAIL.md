# Microsoft 365 email delivery

IPT CMDB sends platform mail through Microsoft Graph. Feature code writes a
provider-neutral message to the durable PostgreSQL outbox first; the delivery
adapter then submits it with Graph application permissions. The initial release
sends explicit administrator test messages. Renewal reminders and change-control
notifications can use the same outbox in later releases.

## Security model

- Only a `platform_admin` in the MSP/root workspace can view or change email settings.
- Client secrets are encrypted with the installation's `MFA_ENCRYPTION_KEY` and are
  write-only in the API and UI.
- Certificate private keys stay in the container secret store or Azure Key Vault;
  they are never uploaded through the browser or written to PostgreSQL.
- Portable exports omit email credentials, email bodies and the outbox.
- Use Exchange Online Application RBAC to grant `Application Mail.Send` only for
  the configured sender mailbox. Do not also grant an unscoped Entra `Mail.Send`
  application permission, because permission grants are additive.
- Graph `202 Accepted` means Microsoft accepted the request for processing. It is
  not proof of final mailbox delivery.

## Recommended deployment choices

| Environment | Authentication | Why |
|---|---|---|
| Azure Container Apps | System- or user-assigned managed identity | No application secret or private-key file in the container |
| Customer Docker instance | App registration with certificate | Portable and stronger than a long-lived client secret |
| Development / initial evaluation | App registration with client secret | Fastest setup; use a short expiry and rotate it |

## Microsoft 365 administrator setup

1. Create or identify the Entra enterprise application or Azure managed identity.
2. Record its application/client ID and enterprise application object ID. These are
   different identifiers.
3. In IPT CMDB, open **MSP / Root level > Email delivery**, choose the authentication
   method, set the sender mailbox, and save.
4. Download the generated **Exchange RBAC script**.
5. Review the script, replace `<ENTERPRISE_APP_OBJECT_ID>`, and run it in Exchange
   Online PowerShell as an Exchange administrator.
6. Allow directory/Exchange changes time to propagate, then send a test email from
   IPT CMDB. The outbox records acceptance or a sanitized failure.

The generated script creates an Exchange recipient management scope for exactly
one mailbox, registers the service principal in Exchange, grants `Application
Mail.Send` for that scope, and runs `Test-ServicePrincipalAuthorization`.

## Azure managed identity

Enable a managed identity on the Container App and select **Azure managed identity**
in the UI. Leave the managed-identity client ID blank for the system-assigned
identity, or enter the client ID for a user-assigned identity. Use the identity's
application/client ID and object ID when applying the Exchange RBAC assignment.

No client secret is stored. Azure Identity obtains the Graph token from the managed
identity endpoint available to the running revision.

## Docker certificate credentials

Create an Entra application registration, upload the public certificate, and make
the private certificate available to only the container runtime. Configure:

```text
EMAIL_CERTIFICATE_PATH=/run/secrets/cmdb-email-certificate.pfx
EMAIL_CERTIFICATE_PASSWORD_FILE=/run/secrets/cmdb-email-certificate-password
```

Mount both files read-only. The path is host/container configuration, not an
editable browser field. In the Email delivery screen choose **Application
certificate**, then enter the tenant ID, application client ID and sender mailbox.

For a PEM certificate, the file must include the RSA private key expected by Azure
Identity. Protect certificate files with restrictive host ACLs and rotate before
expiry.

## Client-secret credentials

Choose **Application client secret**, enter the tenant ID, client ID and secret, and
save. The plaintext secret is immediately encrypted and is never returned. Leaving
the secret field blank on later saves retains the existing ciphertext.

Use a short secret lifetime. The platform stores only the sanitized outcome of
authentication or Graph failures in the outbox and audit ledger.

## Delivery and retry behavior

Each send has an idempotency key, status, attempt count, provider request ID and
timestamps. Explicit retries are allowed only for queued or failed messages and stop
at the message retry limit. The Graph adapter observes `Retry-After` for HTTP 429 and
uses a bounded retry before recording a failure.

Scheduled background delivery, dead-letter handling and templated change/renewal
notifications are intentionally separated from this initial control-plane feature.
