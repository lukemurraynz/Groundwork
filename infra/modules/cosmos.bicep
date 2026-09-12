// Cosmos DB for NoSQL — deployment state, plans, approvals, audit, conversations.
//
// Every tenant-scoped container partitions on /tenantId. That is the structural half of FR-032:
// tenant isolation is a property of the data layer, not a convention the query code is trusted to
// follow.
//
// Zone-redundant in a single Australian region. CL-008 settled that 99.9% applies to the request
// surface, and in-flight deployments are protected by durable resumability rather than uptime, so
// multi-region is out of scope (FR-041a).
//
// API version pinned to 2024-11-15: the newest the Bicep CLI can type-check.

@description('Azure region.')
param location string

@description('Deterministic suffix.')
param resourceToken string

@description('Tags applied to every resource.')
param tags object

@description('Availability zones for zone redundancy (FR-041a).')
param availabilityZones array

// 1000 is the Cosmos autoscale floor. Autoscale bills a minimum of 10% of the maximum, so the
// choice here sets the idle cost: 1000 max bills at 100 RU/s idle, 4000 max bills at 400 RU/s.
@description('Maximum autoscale throughput (RU/s). 1000 is the platform minimum for autoscale.')
@minValue(1000)
param maxThroughput int = 4000

@description('Principal ID of the control-plane identity.')
param controlPlanePrincipalId string

@description('Principal ID of the orchestrator identity.')
param orchestratorPrincipalId string

@description('Principal ID of the human operator running azd, for local read/write access during development and verification. Empty skips the grant.')
param operatorPrincipalId string = ''

var databaseName = 'groundwork'

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: 'cosmos-groundwork-${resourceToken}'
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    // The secretless-identity rule: no connection strings anywhere. Data-plane access is
    // Entra-only, so a leaked key cannot exist because no key is ever valid.
    disableLocalAuth: true
    disableKeyBasedMetadataWriteAccess: true
    minimalTlsVersion: 'Tls12'
    enableAutomaticFailover: false
    enableMultipleWriteLocations: false
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: !empty(availabilityZones)
      }
    ]
    consistencyPolicy: {
      // Session consistency is insufficient here. A deployment resuming on a different replica
      // (FR-035) must observe its own last checkpoint, and bounded staleness makes that guarantee
      // explicit rather than dependent on session-token propagation.
      defaultConsistencyLevel: 'BoundedStaleness'
      maxStalenessPrefix: 100
      maxIntervalInSeconds: 5
    }
    backupPolicy: {
      type: 'Continuous'
      continuousModeProperties: {
        // 7-day continuous backup. Supports the FR-046 disaster recovery objective without
        // retaining Confidential conversation content beyond the FR-053a period.
        tier: 'Continuous7Days'
      }
    }
    publicNetworkAccess: 'Enabled'
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: account
  name: databaseName
  properties: {
    resource: {
      id: databaseName
    }
    options: {
      autoscaleSettings: {
        maxThroughput: maxThroughput
      }
    }
  }
}

// Container definitions. TTL of -1 means "TTL enabled, no default expiry"; a positive value is an
// enforced default expiry in seconds.
//
// FR-052a and FR-053a require 12-month retention on audit, reports, and conversations, enforced by
// platform policy rather than an application cleanup job — a job that stops running fails silently,
// a container TTL does not.
var twelveMonthsInSeconds = 31536000

var containers = [
  {
    name: 'tenants'
    partitionKey: '/tenantId'
    defaultTtl: -1
  }
  {
    name: 'plans'
    partitionKey: '/tenantId'
    defaultTtl: -1
  }
  {
    name: 'validations'
    partitionKey: '/tenantId'
    defaultTtl: -1
  }
  {
    name: 'approvals'
    partitionKey: '/tenantId'
    defaultTtl: -1
  }
  {
    name: 'deployments'
    partitionKey: '/tenantId'
    defaultTtl: -1
  }
  {
    name: 'stage_records'
    partitionKey: '/tenantId'
    defaultTtl: -1
  }
  {
    name: 'audit'
    partitionKey: '/tenantId'
    defaultTtl: twelveMonthsInSeconds
  }
  {
    name: 'reports'
    partitionKey: '/tenantId'
    defaultTtl: twelveMonthsInSeconds
  }
  {
    name: 'drift_summaries'
    partitionKey: '/tenantId'
    defaultTtl: twelveMonthsInSeconds
  }
  {
    name: 'conversations'
    partitionKey: '/tenantId'
    defaultTtl: twelveMonthsInSeconds
  }
  {
    // Subscription serialisation lease (FR-045b). Keyed on subscription rather than tenant because
    // the invariant is one executing deployment per *subscription*. A short TTL means a worker that
    // dies without releasing its lease cannot deadlock the subscription forever.
    name: 'subscription_leases'
    partitionKey: '/subscriptionId'
    defaultTtl: 3600
  }
]

resource containerResources 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = [
  for container in containers: {
    parent: database
    name: container.name
    properties: {
      resource: {
        id: container.name
        partitionKey: {
          paths: [container.partitionKey]
          kind: 'Hash'
          version: 2
        }
        defaultTtl: container.defaultTtl
        indexingPolicy: {
          indexingMode: 'consistent'
          automatic: true
          includedPaths: [
            {
              path: '/*'
            }
          ]
          excludedPaths: [
            {
              path: '/"_etag"/?'
            }
            {
              // Transcript bodies are Confidential and never queried by content. Excluding them
              // from the index avoids indexing conversation text at all.
              path: '/transcript/*'
            }
          ]
        }
      }
    }
  }
]

// Built-in Cosmos DB Data Contributor. Assigned at account scope to both workloads; per-tenant
// isolation is enforced by partition-key-scoped access in the repository layer plus the federated
// credential model, not by separate role assignments per tenant.
var dataContributorRoleId = '00000000-0000-0000-0000-000000000002'

resource controlPlaneDataAccess 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: account
  name: guid(account.id, controlPlanePrincipalId, dataContributorRoleId)
  properties: {
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/${dataContributorRoleId}'
    principalId: controlPlanePrincipalId
    scope: account.id
  }
}

resource orchestratorDataAccess 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: account
  name: guid(account.id, orchestratorPrincipalId, dataContributorRoleId)
  properties: {
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/${dataContributorRoleId}'
    principalId: orchestratorPrincipalId
    scope: account.id
  }
}

// Mirrors keyvault.bicep's and aks.bicep's own operatorAccess pattern — granted automatically on
// every `azd provision` so a developer/operator running local verification against real Azure
// services (2026-08-06) never has to hand-run `az cosmosdb sql role assignment create` again.
resource operatorDataAccess 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = if (!empty(operatorPrincipalId)) {
  parent: account
  name: guid(account.id, operatorPrincipalId, dataContributorRoleId)
  properties: {
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/${dataContributorRoleId}'
    principalId: operatorPrincipalId
    scope: account.id
  }
}

output endpoint string = account.properties.documentEndpoint
output accountName string = account.name
output databaseName string = databaseName
output resourceId string = account.id
