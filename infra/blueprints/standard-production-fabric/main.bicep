// Standard Production Fabric platform — the customer-tenant infrastructure this blueprint deploys
// (T078; FR-036, FR-039). Composes exactly the six AVM modules blueprint.yaml's own iacArtefacts
// declares, at the same pinned versions — the manifest and this file must never disagree about
// what gets deployed, which is why every module reference below is copied from blueprint.yaml
// rather than re-decided here.
//
// This is customer-tenant infrastructure, not Groundwork's own hosting infrastructure — do not
// confuse this with infra/main.bicep (which provisions AKS/Cosmos/Foundry for Groundwork itself
// and never runs in a customer subscription). The orchestrator's infrastructure stage (T077)
// deploys this file into the target subscription via an Azure deployment stack, under the
// deployment identity's stage-scoped Contributor grant (blueprint.yaml's own requiredPermissions).
//
// AVM module versions verified live 2026-08-01 (`az bicep build` against a minimal per-module test
// file, the same compile-test-loop approach used for the Foundry API version this session already
// verified) — all six resolve and type-check at the versions blueprint.yaml pins.
//
// Fabric capacity itself is deliberately not provisioned here: it has no AVM module in
// blueprint.yaml's iacArtefacts and is instead the Fabric stage's own concern (T081), which uses
// the Fabric-specific REST API this generic AVM composition has no reason to know about.

targetScope = 'subscription'

@description('Azure region for every resource this blueprint creates.')
param location string

@description('Deterministic resource group name — computed by the orchestrator the same way naming.py\'s readiness check computes it (rg-groundwork-{subscriptionId[:8]}), so the two can never disagree about what name is in play.')
param resourceGroupName string

@description('Tags applied to every resource.')
param tags object

@description('Deterministic suffix for globally-unique resource names (Key Vault, workspace).')
param resourceToken string

@description('Fixed platform VNet address space — must match validation/checks/network.py\'s DEFAULT_VNET_ADDRESS_SPACE exactly, or the readiness check and this deployment silently disagree about what was reserved.')
param vnetAddressSpace string = '10.42.0.0/16'

resource rg 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// A single subnet sized for private endpoints. Splitting further (e.g. a dedicated Fabric managed
// private endpoint subnet) is deferred to the Fabric stage (T081) if Fabric's own networking
// requirements need it — this module provisions the platform VNet's baseline, not every consumer's
// eventual subnet.
var privateEndpointSubnetName = 'snet-private-endpoints'

module vnet 'br/public:avm/res/network/virtual-network:0.5.1' = {
  name: 'vnet-${resourceToken}'
  scope: resourceGroup(rg.name)
  params: {
    name: 'vnet-gw-${resourceToken}'
    location: location
    tags: tags
    addressPrefixes: [vnetAddressSpace]
    subnets: [
      {
        name: privateEndpointSubnetName
        addressPrefix: cidrSubnet(vnetAddressSpace, 24, 0)
        privateEndpointNetworkPolicies: 'Disabled'
      }
    ]
  }
}

module logAnalytics 'br/public:avm/res/operational-insights/workspace:0.7.0' = {
  name: 'law-${resourceToken}'
  scope: resourceGroup(rg.name)
  params: {
    name: 'law-gw-${resourceToken}'
    location: location
    tags: tags
  }
}

module appInsights 'br/public:avm/res/insights/component:0.4.1' = {
  name: 'ai-${resourceToken}'
  scope: resourceGroup(rg.name)
  params: {
    name: 'ai-gw-${resourceToken}'
    location: location
    tags: tags
    workspaceResourceId: logAnalytics.outputs.resourceId
  }
}

module identity 'br/public:avm/res/managed-identity/user-assigned-identity:0.4.0' = {
  name: 'uami-${resourceToken}'
  scope: resourceGroup(rg.name)
  params: {
    name: 'uami-gw-${resourceToken}'
    location: location
    tags: tags
  }
}

// FR-036: private networking, satisfied at the floor per blueprint.yaml's own verified comment.
// privatelink.vaultcore.azure.net is Microsoft's fixed, documented private-link DNS zone name for
// Key Vault — a stable public contract, not a version-sensitive fact this session's compile-test
// loop applies to (that loop verified the AVM *module* surface, which is what actually changes
// between versions). Split into private-dns.bicep because a resource-group-scoped resource cannot
// be declared directly in this subscription-scoped file (BCP139) — only reached via a module.
module keyVaultDns 'private-dns.bicep' = {
  name: 'kvdns-${resourceToken}'
  scope: resourceGroup(rg.name)
  params: {
    tags: tags
    resourceToken: resourceToken
    vnetResourceId: vnet.outputs.resourceId
  }
}

module keyVault 'br/public:avm/res/key-vault/vault:0.9.0' = {
  name: 'kv-${resourceToken}'
  scope: resourceGroup(rg.name)
  params: {
    name: 'kv-gw-${resourceToken}'
    location: location
    tags: tags
    sku: 'standard'
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    // FR-036: no public path. The only access is through the private endpoint below.
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
    }
    diagnosticSettings: [
      {
        workspaceResourceId: logAnalytics.outputs.resourceId
      }
    ]
    privateEndpoints: [
      {
        subnetResourceId: vnet.outputs.subnetResourceIds[0]
        service: 'vault'
        privateDnsZoneGroup: {
          privateDnsZoneGroupConfigs: [
            {
              privateDnsZoneResourceId: keyVaultDns.outputs.zoneResourceId
            }
          ]
        }
      }
    ]
  }
}

output resourceGroupName string = rg.name
output vnetResourceId string = vnet.outputs.resourceId
output privateEndpointSubnetResourceId string = vnet.outputs.subnetResourceIds[0]
output keyVaultResourceId string = keyVault.outputs.resourceId
output keyVaultUri string = keyVault.outputs.uri
output logAnalyticsWorkspaceResourceId string = logAnalytics.outputs.resourceId
output applicationInsightsConnectionString string = appInsights.outputs.connectionString
output managedIdentityResourceId string = identity.outputs.resourceId
output managedIdentityPrincipalId string = identity.outputs.principalId
output managedIdentityClientId string = identity.outputs.clientId
