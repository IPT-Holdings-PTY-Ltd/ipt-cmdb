# Azure Container Apps deployment

The `infra/` Bicep baseline provisions a complete Azure runtime:

- Azure Container Apps environment and application
- Azure Container Registry with managed-identity pull access
- VNet integration and private PostgreSQL Flexible Server networking
- PostgreSQL 16 database with 14-day PITR retention
- Key Vault references through a user-assigned managed identity
- Log Analytics with structured application logs
- Azure Monitor action group and readiness, 5xx, worker, PostgreSQL availability, and
  PostgreSQL storage alerts
- startup, liveness, and readiness probes
- a non-ingress worker Container App that scales to zero until dedicated mode is selected
- optional Microsoft Entra authentication at the Container Apps boundary

The default PostgreSQL SKU and single app replica are economical starting values, not
a universal production sizing recommendation.

Operational alerts are enabled by default and use `bootstrapAdminEmail` as their
receiver. Set the optional `operationalAlertEmail` Bicep parameter to a monitored
operations mailbox. Use `enableOperationalAlerts=false` only for a deliberate
non-production deployment. `postgresStorageAlertPercent` defaults to 80 and accepts
values from 50 through 95.

## Prerequisites

- Azure subscription and permission to create resources and role assignments
- Azure Developer CLI (`azd`) and Azure CLI (`az`)
- A single-tenant Entra app registration with a client secret
- A region that supports Container Apps VNet integration and PostgreSQL Flexible Server

## 1. Create the Entra app registration

Create a Web application registration in the same tenant. A redirect URI can be added
after the first provision when the generated FQDN is known:

```text
https://<container-app-fqdn>/.auth/login/aad/callback
```

Record its application/client ID and create a client secret. Do not place the secret
in source control. The Bicep deployment stores it in Key Vault and configures the
Container Apps authentication boundary.

## 2. Initialize the azd environment

```powershell
azd auth login
azd env new prod
azd env set AZURE_LOCATION southafricanorth
```

Generate a URL-safe PostgreSQL password and MFA encryption key:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set the values in the local azd environment. They are secure deployment inputs; avoid
shell history on shared administration machines:

```powershell
azd env set CMDB_POSTGRES_ADMIN_PASSWORD '<generated-password>'
azd env set CMDB_MFA_ENCRYPTION_KEY '<generated-key>'
azd env set CMDB_BOOTSTRAP_ADMIN_EMAIL '<first-platform-admin-email>'
azd env set CMDB_BOOTSTRAP_ADMIN_PASSWORD '<generated-bootstrap-password>'
azd env set CMDB_ENTRA_CLIENT_ID '<application-client-id>'
azd env set CMDB_ENTRA_CLIENT_SECRET '<application-client-secret>'
azd env set CMDB_WORKER_DEPLOYMENT_MODE embedded
azd env set CMDB_ENABLE_NOTIFICATION_WORKER false
azd env set CMDB_ENABLE_INTEGRATION_WORKER false
```

## 3. Provision and deploy

```powershell
azd up
```

`azd` provisions `infra/main.bicep`, builds the Dockerfile remotely in ACR, and deploys
the built image to the tagged `api` Container App. If your organisation separates
infrastructure and application duties, use `azd provision` and `azd deploy api` as two
approved stages.

After the first provision, retrieve the FQDN and add the exact redirect URI to the Entra
app registration:

```powershell
azd env get-values
```

Run `azd deploy api` after changing application code. Run `azd provision` after reviewing
infrastructure changes.

The IaC sets `PUBLIC_BASE_URL` to the generated Container App HTTPS origin so local
break-glass accounts can receive safe recovery links. When a custom domain becomes the
canonical entry point, supply that exact HTTPS origin through the optional
`publicBaseUrl` Bicep parameter and reprovision.

The Bicep baseline sets `FORWARDED_ALLOW_IPS` to its dedicated Container Apps
infrastructure subnet (`10.42.0.0/23`). Uvicorn therefore evaluates Azure's
right-most appended client address without trusting arbitrary direct callers. If the
network ranges in `infra/main.bicep` are changed, update `forwardedAllowIps` to the
new smallest infrastructure-proxy range in the same reviewed deployment. Never set it
to `*` or an all-address CIDR.

Scheduled ConnectWise and N-central previews and integration alert delivery are opt-in
Bicep parameters. After Microsoft 365 email is verified in the root workspace, set
`enableIntegrationWorker=true` and `enableNotificationWorker=true`. Optionally set
`integrationAlertRecipients` to a comma-separated operations mailbox list; when blank,
active platform-administrator email addresses receive alerts. The Container App receives
the same `INTEGRATION_*` and `NOTIFICATION_*` settings used by Docker deployments.

The default `workerDeploymentMode=embedded` retains one API replica with optional
in-process jobs. After the environment is initialized and `/api/ready` succeeds, set
`workerDeploymentMode=dedicated`, reprovision, and deploy both azd services:

```powershell
azd env set CMDB_WORKER_DEPLOYMENT_MODE dedicated
azd env set CMDB_ENABLE_NOTIFICATION_WORKER true
azd env set CMDB_ENABLE_INTEGRATION_WORKER true
azd provision
azd deploy api
azd deploy worker
```

Dedicated mode changes the API process role to `web` and keeps exactly one non-ingress
worker replica. Both services use the same image source, managed identity, Key Vault
references and PostgreSQL database. See the [worker runbook](WORKERS.md) for the
one-shot Container Apps Job option and monitoring behaviour.

## 4. Verify

The auth boundary intentionally excludes only health endpoints. Test them first:

```powershell
$fqdn = azd env get-value AZURE_CONTAINER_APP_FQDN
Invoke-RestMethod "https://$fqdn/api/live"
Invoke-RestMethod "https://$fqdn/api/ready"
```

Then verify:

1. the root URL redirects to Microsoft sign-in;
2. an enabled mapped CMDB user can sign in;
3. an unmapped Entra user receives no CMDB access;
4. tenant switching respects customer/group assignments;
5. an asset read and branded PDF report succeed;
6. Container Apps logs contain JSON request IDs and route templates but no secrets;
7. a forged left-most `X-Forwarded-For` value does not change the resolved client
   shown in audit evidence;
8. the Azure Monitor action group has a confirmed email receiver and each alert rule
   is enabled.

The direct Log Analytics connection used by this template writes console output to
`ContainerAppConsoleLogs_CL`. A quick structured-log check is:

```powershell
$workspace = az monitor log-analytics workspace show `
  --resource-group <resource-group> `
  --workspace-name <workspace-name> `
  --query customerId -o tsv
az monitor log-analytics query `
  --workspace $workspace `
  --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s startswith 'cmdb-' | extend payload=parse_json(Log_s) | where isnotempty(payload.event) | project TimeGenerated, ContainerAppName_s, payload | take 20"
```

Azure Monitor delivery is independent of the CMDB Microsoft Graph email setup. Test the
action group from Azure Monitor after deployment and route it to an address monitored
outside the CMDB application.

## Security and operations notes

- PostgreSQL has no public network access and resolves through a private DNS zone.
- Container Apps reads Key Vault secrets using a user-assigned managed identity.
- Key Vault public access remains enabled in this baseline for Azure control-plane and
  managed-service access. Add an approved private-endpoint/firewall design if required
  by policy; validate Container Apps secret refresh before enforcing it.
- The app starts at one replica. Do not increase it until first-start repository seeding
  is moved to a deployment job or is certified for concurrent cold starts.
- Keep the worker at zero replicas until the first web startup completes on a blank
  database. Dedicated mode is a post-bootstrap topology change.
- Raise the PostgreSQL SKU, enable zone-redundant HA where supported, and extend backup
  retention according to the service tier and recovery objectives.
- Add a custom domain and managed certificate before production launch.
- Entra users receive MFA through Conditional Access. Application TOTP is for explicitly
  governed local break-glass accounts only.
- PostgreSQL coordinates local-password identifier and source limits across replicas;
  an external WAF/ingress rate limit remains recommended for volumetric attacks.
- `LOG_FORMAT=json` and `LOG_LEVEL=INFO` are set on web and worker containers. Uvicorn's
  duplicate access log is disabled in the image; request middleware is the canonical
  request diagnostic stream.

## IaC validation without deployment

```powershell
az bicep build --file infra/main.bicep
az deployment group validate `
  --resource-group <existing-validation-rg> `
  --template-file infra/main.bicep `
  --parameters environmentName=validation `
               postgresAdministratorPassword='<temporary-url-safe-password>' `
               mfaEncryptionKey='<temporary-32-byte-base64-key>' `
               bootstrapAdminEmail='<first-platform-admin-email>' `
               bootstrapAdminPassword='<temporary-bootstrap-password>' `
               entraClientId='<client-id>' `
               entraClientSecret='<client-secret>'
```

Validation still requires provider registration and permission to inspect the target
resource group. It does not create the resources.

## Official references

- [Azure Developer CLI `azure.yaml` schema](https://learn.microsoft.com/azure/developer/azure-developer-cli/azd-schema)
- [Container Apps Key Vault secret references](https://learn.microsoft.com/azure/container-apps/manage-secrets)
- [Container Apps authentication with Microsoft Entra ID](https://learn.microsoft.com/azure/container-apps/authentication-entra)
- [Container Apps health probes](https://learn.microsoft.com/azure/container-apps/health-probes)
- [PostgreSQL Flexible Server private networking](https://learn.microsoft.com/azure/postgresql/network/concepts-networking-private)
