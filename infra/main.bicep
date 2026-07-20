targetScope = 'resourceGroup'

@description('Short environment name such as dev, test, or prod.')
param environmentName string

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('PostgreSQL administrator name used only by the application deployment.')
param postgresAdministratorLogin string = 'cmdbadmin'

@secure()
@description('URL-safe PostgreSQL administrator password. Avoid URL-reserved characters.')
param postgresAdministratorPassword string

@secure()
@description('Stable URL-safe base64 encoding of 32 random bytes for local MFA seed encryption.')
param mfaEncryptionKey string

@description('Email address mapped to the first CMDB platform administrator.')
param bootstrapAdminEmail string

@secure()
@description('Random initial local password for the first administrator, even when Entra is used.')
param bootstrapAdminPassword string

@description('Enable the Azure Container Apps Microsoft Entra authentication boundary.')
param enableEntraAuth bool = true

@description('Client ID of the single-tenant Entra application registration.')
param entraClientId string = ''

@secure()
@description('Client secret of the Entra application registration.')
param entraClientSecret string = ''

@description('Initial image used during provisioning. azd deploy replaces this with the built image.')
param bootstrapImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('PostgreSQL SKU. Increase this for production workload and HA requirements.')
param postgresSkuName string = 'Standard_B1ms'

@description('PostgreSQL SKU tier.')
@allowed([
  'Burstable'
  'GeneralPurpose'
  'MemoryOptimized'
])
param postgresSkuTier string = 'Burstable'

var suffix = uniqueString(subscription().id, resourceGroup().id, environmentName)
var compactEnvironmentName = replace(replace(toLower(environmentName), '-', ''), '_', '')
var commonTags = {
  'azd-env-name': environmentName
  application: 'ipt-cmdb'
  environment: environmentName
}
var appName = 'cmdb-${environmentName}-${suffix}'
var postgresName = 'cmdb-pg-${environmentName}-${suffix}'
var databaseUrl = 'postgresql://${postgresAdministratorLogin}:${postgresAdministratorPassword}@${postgresName}.postgres.database.azure.com:5432/cmdb?sslmode=require'
var keyVaultSecrets = concat([
  {
    name: 'database-url'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/database-url'
    identity: appIdentity.id
  }
  {
    name: 'mfa-encryption-key'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/mfa-encryption-key'
    identity: appIdentity.id
  }
  {
    name: 'bootstrap-admin-password'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/bootstrap-admin-password'
    identity: appIdentity.id
  }
], enableEntraAuth ? [
  {
    name: 'entra-client-secret'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/entra-client-secret'
    identity: appIdentity.id
  }
] : [])

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: 'cmdb-log-${environmentName}-${suffix}'
  location: location
  tags: commonTags
  properties: {
    retentionInDays: 30
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'cmdb-vnet-${environmentName}-${suffix}'
  location: location
  tags: commonTags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.42.0.0/16'
      ]
    }
    subnets: [
      {
        name: 'container-apps'
        properties: {
          addressPrefix: '10.42.0.0/23'
          delegations: [
            {
              name: 'Microsoft.App.environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'postgresql'
        properties: {
          addressPrefix: '10.42.4.0/24'
          delegations: [
            {
              name: 'Microsoft.DBforPostgreSQL.flexibleServers'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
    ]
  }
}

resource containerAppsSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: 'container-apps'
}

resource postgresSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: 'postgresql'
}

resource postgresPrivateDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: '${postgresName}.private.postgres.database.azure.com'
  location: 'global'
  tags: commonTags
}

resource postgresDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: postgresPrivateDns
  name: 'cmdb-vnet-link'
  location: 'global'
  tags: commonTags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresName
  location: location
  tags: commonTags
  sku: {
    name: postgresSkuName
    tier: postgresSkuTier
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdministratorLogin
    administratorLoginPassword: postgresAdministratorPassword
    availabilityZone: '1'
    backup: {
      backupRetentionDays: 14
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      delegatedSubnetResourceId: postgresSubnet.id
      privateDnsZoneArmResourceId: postgresPrivateDns.id
      publicNetworkAccess: 'Disabled'
    }
    storage: {
      storageSizeGB: 32
      autoGrow: 'Enabled'
    }
  }
  dependsOn: [
    postgresDnsLink
  ]
}

resource cmdbDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: 'cmdb'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: take('cmdb${compactEnvironmentName}${suffix}', 24)
  location: location
  tags: commonTags
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
  }
}

resource databaseUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'database-url'
  properties: {
    value: databaseUrl
  }
}

resource mfaKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'mfa-encryption-key'
  properties: {
    value: mfaEncryptionKey
  }
}

resource bootstrapAdminPasswordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'bootstrap-admin-password'
  properties: {
    value: bootstrapAdminPassword
  }
}

resource entraSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (enableEntraAuth) {
  parent: keyVault
  name: 'entra-client-secret'
  properties: {
    value: entraClientSecret
  }
}

resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'cmdb-app-${environmentName}-${suffix}'
  location: location
  tags: commonTags
}

resource keyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, appIdentity.id, 'Key Vault Secrets User')
  scope: keyVault
  properties: {
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: take('cmdb${compactEnvironmentName}${suffix}', 50)
  location: location
  tags: commonTags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, appIdentity.id, 'AcrPull')
  scope: registry
  properties: {
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cmdb-env-${environmentName}-${suffix}'
  location: location
  tags: commonTags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    vnetConfiguration: {
      infrastructureSubnetId: containerAppsSubnet.id
      internal: false
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2025-01-01' = {
  name: appName
  location: location
  tags: union(commonTags, {
    'azd-service-name': 'api'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 3000
        transport: 'auto'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: appIdentity.id
        }
      ]
      secrets: keyVaultSecrets
    }
    template: {
      containers: [
        {
          name: 'api'
          image: bootstrapImage
          env: [
            {
              name: 'PORT'
              value: '3000'
            }
            {
              name: 'DATABASE_URL'
              secretRef: 'database-url'
            }
            {
              name: 'DATABASE_SEED_MODE'
              value: 'empty'
            }
            {
              name: 'BOOTSTRAP_ADMIN_EMAIL'
              value: bootstrapAdminEmail
            }
            {
              name: 'BOOTSTRAP_ADMIN_PASSWORD_FILE'
              value: ''
            }
            {
              name: 'BOOTSTRAP_ADMIN_PASSWORD'
              secretRef: 'bootstrap-admin-password'
            }
            {
              name: 'AUTH_MODE'
              value: enableEntraAuth ? 'easy_auth' : 'local'
            }
            {
              name: 'MFA_ENCRYPTION_KEY'
              secretRef: 'mfa-encryption-key'
            }
            {
              name: 'LOCAL_MFA_POLICY'
              value: 'admins'
            }
            {
              name: 'ALLOW_LOCAL_BREAK_GLASS'
              value: 'false'
            }
            {
              name: 'ALLOW_UI_DATABASE_CONFIG'
              value: 'false'
            }
            {
              name: 'ALLOW_LOCAL_DEVELOPMENT'
              value: 'false'
            }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/api/live'
                port: 3000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 3
              periodSeconds: 5
              failureThreshold: 18
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/api/live'
                port: 3000
                scheme: 'HTTP'
              }
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/api/ready'
                port: 3000
                scheme: 'HTTP'
              }
              periodSeconds: 10
              failureThreshold: 6
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
  dependsOn: [
    acrPull
    cmdbDatabase
    databaseUrlSecret
    mfaKeySecret
    bootstrapAdminPasswordSecret
    keyVaultSecretsUser
  ]
}

resource authConfig 'Microsoft.App/containerApps/authConfigs@2025-01-01' = if (enableEntraAuth) {
  parent: containerApp
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
      excludedPaths: [
        '/api/live'
        '/api/ready'
        '/api/health'
        '/api/v2/health'
      ]
    }
    httpSettings: {
      requireHttps: true
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraClientId
          clientSecretSettingName: 'entra-client-secret'
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            entraClientId
            'api://${entraClientId}'
          ]
        }
      }
    }
  }
  dependsOn: [
    entraSecret
  ]
}

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.properties.loginServer
output AZURE_CONTAINER_APP_NAME string = containerApp.name
output AZURE_CONTAINER_APP_FQDN string = containerApp.properties.configuration.ingress.fqdn
output AZURE_KEY_VAULT_NAME string = keyVault.name
output AZURE_POSTGRESQL_SERVER_NAME string = postgres.name
