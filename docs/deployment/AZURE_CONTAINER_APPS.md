# Azure Container Apps deployment

The `infra/` Bicep baseline provisions a complete Azure runtime:

- Azure Container Apps environment and application
- Azure Container Registry with managed-identity pull access
- VNet integration and private PostgreSQL Flexible Server networking
- PostgreSQL 16 database with 14-day PITR retention
- Key Vault references through a user-assigned managed identity
- Log Analytics
- startup, liveness, and readiness probes
- optional Microsoft Entra authentication at the Container Apps boundary

The default PostgreSQL SKU and single app replica are economical starting values, not
a universal production sizing recommendation.

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
6. Container Apps logs contain request IDs but no secrets.

## Security and operations notes

- PostgreSQL has no public network access and resolves through a private DNS zone.
- Container Apps reads Key Vault secrets using a user-assigned managed identity.
- Key Vault public access remains enabled in this baseline for Azure control-plane and
  managed-service access. Add an approved private-endpoint/firewall design if required
  by policy; validate Container Apps secret refresh before enforcing it.
- The app starts at one replica. Do not increase it until first-start repository seeding
  is moved to a deployment job or is certified for concurrent cold starts.
- Raise the PostgreSQL SKU, enable zone-redundant HA where supported, and extend backup
  retention according to the service tier and recovery objectives.
- Add a custom domain and managed certificate before production launch.
- Entra users receive MFA through Conditional Access. Application TOTP is for explicitly
  governed local break-glass accounts only.

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
