// Key Vault.
//
// Worth being explicit about what this is *not* for. The secretless-identity rule prohibits
// long-lived client secrets, and every workload authenticates with workload identity, so this vault holds no
// application credentials. It exists for customer-supplied certificate material and for the
// federated-credential metadata the orchestrator needs — and it is configured so that adding a
// long-lived secret later is a visible, audited act rather than a quiet convenience.
//
// API version pinned to 2023-07-01: the newest the Bicep CLI can type-check.

@description('Azure region.')
param location string

@description('Deterministic suffix.')
param resourceToken string

@description('Tags applied to every resource.')
param tags object

@description('Object ID of the break-glass operator. Empty means no standing human access, which is the preferred posture.')
param operatorPrincipalId string = ''

@description('Principal ID of the control-plane workload identity, granted read access to secrets.')
param controlPlanePrincipalId string

@description('Log Analytics workspace for diagnostics.')
param logAnalyticsWorkspaceId string

// Recovery, not a bypass. Purge protection (below) means a deleted vault cannot be purged before
// its 90-day retention expires — that is the control working as intended, not a defect. But the
// vault name is derived deterministically from subscription + environment + location, so
// re-provisioning the same environment after a deletion collides with the soft-deleted vault
// unless the deployment explicitly recovers it. `createMode: 'recover'` is the ARM-native path for
// exactly this: it restores the original vault (including any secrets) rather than purging
// protection to make room for a new one.
//
// Set this true only when you know a soft-deleted vault with this exact name already exists
// (`az keyvault list-deleted`). Recovery fails loudly if there is nothing to recover, so leaving
// it false is always safe for a genuinely new environment.
@description('Recover a soft-deleted vault of this name instead of creating a new one.')
param recoverVault bool = false

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-gw-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    createMode: recoverVault ? 'recover' : 'default'
    // RBAC rather than access policies: access policies cannot express least privilege at the
    // granularity the secretless-identity rule requires, and are not auditable in the same way.
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    // Purge protection prevents an attacker — or an accident — destroying key material and its
    // recovery path together.
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

// Diagnostics are mandatory here, not optional. Key Vault access is exactly the audit trail an
// incident investigation needs, and it must exist before the incident.
resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: keyVault
  name: 'kv-diagnostics'
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'AuditEvent'
        enabled: true
      }
      {
        category: 'AzurePolicyEvaluationDetails'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

// Key Vault Secrets User — read-only. Deliberately not Secrets Officer: neither break-glass access
// nor the control plane's own runtime need write access to secret material.
var secretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource operatorAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorPrincipalId)) {
  scope: keyVault
  name: guid(keyVault.id, operatorPrincipalId, secretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      secretsUserRoleId
    )
    principalId: operatorPrincipalId
    principalType: 'User'
  }
}

// The control plane's own readiness check reads this vault (api/main.py's _check_key_vault) to
// prove real connectivity, per FR-042 — a health check that cannot actually reach the dependency it
// claims to check is worse than no check at all. This grant was missing entirely until a live pod
// surfaced it as a 403 on every readiness probe; enableRbacAuthorization above means there is no
// access-policy fallback that could have masked the gap.
resource controlPlaneAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, controlPlanePrincipalId, secretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      secretsUserRoleId
    )
    principalId: controlPlanePrincipalId
    principalType: 'ServicePrincipal'
  }
}

output vaultUri string = keyVault.properties.vaultUri
output vaultName string = keyVault.name
output resourceId string = keyVault.id
