// Dev/sandbox Fabric platform — reduced baseline infrastructure, promoted from ADR-0010's draft.
//
// AVM versions intentionally align with standard-production-fabric's pinned versions where the same
// modules are used.

targetScope = 'subscription'

@description('Azure region for every resource this blueprint creates.')
param location string

@description('Deterministic resource group name.')
param resourceGroupName string

@description('Tags applied to every resource.')
param tags object

@description('Deterministic suffix for globally-unique resource names.')
param resourceToken string

@description('Sandbox VNet address space. Kept aligned with the shared readiness network check.')
param vnetAddressSpace string = '10.42.0.0/16'

resource rg 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

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
        name: 'snet-private-endpoints'
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

output resourceGroupName string = rg.name
output vnetResourceId string = vnet.outputs.resourceId
output privateEndpointSubnetResourceId string = vnet.outputs.subnetResourceIds[0]
output logAnalyticsWorkspaceResourceId string = logAnalytics.outputs.resourceId
output applicationInsightsConnectionString string = appInsights.outputs.connectionString
output managedIdentityResourceId string = identity.outputs.resourceId
output managedIdentityPrincipalId string = identity.outputs.principalId
output managedIdentityClientId string = identity.outputs.clientId
