# Microsoft 365 email delivery

IPT CMDB sends platform mail through Microsoft Graph. Feature code writes a
provider-neutral message to the durable PostgreSQL outbox first; the delivery
adapter then submits it with Graph application permissions. Administrator tests,
lifecycle reminders, ownership gaps, change-approval notifications, and local
account recovery share the same durable delivery path.

## Security model

- Only a `platform_admin` in the MSP/root workspace can view or change email settings.
- Client secrets are encrypted with the installation's `MFA_ENCRYPTION_KEY` and are
  write-only in the API and UI.
- Certificate private keys stay in the container secret store or Azure Key Vault;
  they are never uploaded through the browser or written to PostgreSQL.
- Portable exports omit email credentials, email bodies and the outbox.
- Use Exchange Online Application RBAC to grant `Application Mail.Send` only for
  the configured sender mailbox. Do not also grant an unscoped Entra `Mail.Send`
  application permission. The Exchange role assignment is sufficient for Graph
  `sendMail`, and permission grants are additive.
- Graph `202 Accepted` means Microsoft accepted the request for processing. It is
  not proof of final mailbox delivery.

## Recommended deployment choices

| Environment | Authentication | Why |
|---|---|---|
| Azure Container Apps | System- or user-assigned managed identity | No application secret or private-key file in the container |
| Customer Docker instance | App registration with certificate | Portable and stronger than a long-lived client secret |
| Development / initial evaluation | App registration with client secret | Fastest setup; use a short expiry and rotate it |

## Guided Microsoft 365 setup

Open **MSP workspace > Platform > Email delivery**. The six-step wizard guides a
platform administrator through the supported setup without asking the CMDB for
broad Microsoft 365 administrative permissions:

1. Choose Azure managed identity, an application certificate, or a client secret.
2. Choose an existing sender mailbox or ask the setup package to create a dedicated
   shared mailbox.
3. Enter the Entra tenant ID, application/client ID, and **enterprise application
   object ID**. For the Exchange values, open **Entra ID > Enterprise applications**,
   search using the Application ID, and copy that result's Application ID and Object
   ID. Do not use the Object ID from **App registrations**; that is a different
   directory object and Exchange rejects it.
4. Review and save the runtime configuration. Client secrets are write-only and
   certificate private keys stay outside the browser.
5. Download the tailored PowerShell package and run it with PowerShell 7.2 or later
   as an Exchange administrator.
6. Confirm that the mailbox is in scope, then send an explicit verification email.

The generated script is safe to re-run. It creates or reuses the mailbox, updates
the Exchange recipient management scope for exactly that mailbox, creates or reuses
the Exchange service principal and `Application Mail.Send` role assignment, runs
`Test-ServicePrincipalAuthorization`, and writes a non-sensitive
`ipt-cmdb-email-setup-result.json` result file. The script never includes client
secrets, private keys, or certificate passwords.

On Windows, review and unblock only the downloaded file. Do not change the
machine-wide execution policy to `Unrestricted`:

```powershell
cd $HOME\Downloads
Get-Content .\setup-ipt-cmdb-email.ps1
Unblock-File -LiteralPath .\setup-ipt-cmdb-email.ps1
.\setup-ipt-cmdb-email.ps1
```

The script allows for normal Exchange mailbox and Entra service-principal
replication delays. If it ultimately reports `AADServicePrincipalNotFound`, return
to the wizard's identity step and correct the Object ID from **Enterprise
applications**. The script stops at that failure and does not attempt the role
assignment with a null application reference.

The setup script finishing with `GrantedPermissions = Mail.Send` and
`InScope = True` is the expected authorization confirmation. It does not mean
`Mail.Send` should also appear under the app registration's **API permissions**
blade.

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
at the message retry limit. The Graph adapter observes `Retry-After` for HTTP 429.
The notification worker adds exponential retry scheduling, safe multi-replica
claims, stale-claim recovery and a dead-letter state. See [Notification rules and
delivery](NOTIFICATIONS.md).

Local password recovery is shown on the login page only after this connection is
enabled and has a sender mailbox. Set the trusted `PUBLIC_BASE_URL` as described in
[Local account password recovery](LOCAL_ACCOUNT_RECOVERY.md); do not expose recovery
until a test message has succeeded. Reset-link emails bypass the durable outbox so
the raw recovery token is never stored; accepted or failed delivery is recorded as a
sanitized audit event instead.

## Container connectivity troubleshooting

The application container needs outbound HTTPS to `login.microsoftonline.com`
and `graph.microsoft.com`. On Docker Desktop, keep **Settings > Resources >
Network > DNS resolution behavior** on **Auto**, or filter IPv6 records when the
Docker network is IPv4-only. The development Compose network is dual stack so
Microsoft endpoints that resolve to IPv6 remain reachable.

Verify identity discovery from the running application container:

```powershell
docker compose exec -T cmdb python -c "import urllib.request; print(urllib.request.urlopen('https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration', timeout=10).status)"
```

An HTTP `200` confirms that Microsoft identity discovery is reachable. Corporate
proxy deployments must configure Docker or container HTTPS proxy settings too.
