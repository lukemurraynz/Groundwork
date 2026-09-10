// Immutable storage for deployment reports, approval artefacts, what-if previews, and consent records.
//
// Immutability is what makes FR-052 structurally true rather than merely intended: "return an
// archived report unchanged" is guaranteed by an Azure immutability policy, not by everyone
// remembering not to overwrite the blob.
//
// The 12-month policy period matches FR-052a retention. A lifecycle rule deletes on expiry, so
// retention is enforced by platform policy and cannot be skipped by a cleanup job that stopped
// running.
//
// API version pinned to 2023-05-01: the newest the Bicep CLI can type-check. Newer stable
// versions exist but emit BCP081, losing compile-time property validation.

@description('Azure region.')
param location string

@description('Deterministic suffix.')
param resourceToken string

@description('Tags applied to every resource.')
param tags object

@description('Principal ID of the control-plane identity. Writes approval artefacts.')
param controlPlanePrincipalId string

@description('Principal ID of the orchestrator identity. Writes reports and previews.')
param orchestratorPrincipalId string

@description('Principal ID of the human operator running azd, for local read/write access during development and verification. Empty skips the grant.')
param operatorPrincipalId string = ''

@description('Retention period in days. 365 per FR-052a.')
param retentionDays int = 365

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stgw${resourceToken}'
  location: location
  tags: tags
  sku: {
    // Zone-redundant to match FR-041a. An audit artefact that survives the orchestrator but not a
    // zone outage would not satisfy the audit-by-construction rule.
    name: 'Standard_ZRS'
  }
  kind: 'StorageV2'
  properties: {
    // The secretless-identity rule: Entra-only. No account keys, so there is no key to leak.
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
    encryption: {
      requireInfrastructureEncryption: true
      services: {
        blob: {
          enabled: true
          keyType: 'Account'
        }
      }
      keySource: 'Microsoft.Storage'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
    isVersioningEnabled: true
  }
}

var immutableContainers = [
  'reports'
  'approvals'
  'previews'
  'consent'
]

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [
  for name in immutableContainers: {
    parent: blobService
    name: name
    properties: {
      publicAccess: 'None'
      metadata: {
        dataClassification: 'confidential'
        retentionDays: '${retentionDays}'
      }
    }
  }
]

// Time-based immutability. allowProtectedAppendWrites is false because a report is written once and
// never appended to; permitting appends would let a finished report be extended after the fact,
// which is precisely what immutability is meant to prevent.
resource immutabilityPolicies 'Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies@2023-05-01' = [
  for (name, i) in immutableContainers: {
    name: '${storageAccount.name}/default/${name}/default'
    properties: {
      immutabilityPeriodSinceCreationInDays: retentionDays
      allowProtectedAppendWrites: false
    }
    dependsOn: [containers[i]]
  }
]

// Lifecycle deletion at the end of the immutability period (FR-052a). Without this, blobs would
// become mutable at expiry and then persist indefinitely.
resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'delete-after-retention'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: ['blockBlob']
              prefixMatch: immutableContainers
            }
            actions: {
              baseBlob: {
                delete: {
                  daysAfterCreationGreaterThan: retentionDays
                }
              }
              version: {
                delete: {
                  daysAfterCreationGreaterThan: retentionDays
                }
              }
            }
          }
        }
      ]
    }
  }
}

// Storage Blob Data Contributor.
var blobContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource orchestratorBlobAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, orchestratorPrincipalId, blobContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      blobContributorRoleId
    )
    principalId: orchestratorPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource controlPlaneBlobAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, controlPlanePrincipalId, blobContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      blobContributorRoleId
    )
    principalId: controlPlanePrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Mirrors keyvault.bicep's and aks.bicep's own operatorAccess pattern — granted automatically on
// every `azd provision` so a developer/operator running local verification against real Azure
// services (2026-08-06) never has to hand-run `az role assignment create` again.
resource operatorBlobAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorPrincipalId)) {
  scope: storageAccount
  name: guid(storageAccount.id, operatorPrincipalId, blobContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      blobContributorRoleId
    )
    principalId: operatorPrincipalId
    principalType: 'User'
  }
}

output blobEndpoint string = storageAccount.properties.primaryEndpoints.blob
output accountName string = storageAccount.name
