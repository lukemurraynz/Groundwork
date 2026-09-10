// Key Vault's private DNS zone, deployed at resource-group scope. Split out of main.bicep because
// main.bicep's targetScope is 'subscription' — a resource-group-scoped resource cannot be declared
// directly in a subscription-scoped file, only reached via a module (BCP139).

@description('Tags applied to the zone and its VNet link.')
param tags object

@description('Deterministic suffix, used in the VNet link name.')
param resourceToken string

@description('Resource ID of the VNet to link this zone to.')
param vnetResourceId string

resource keyVaultPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.vaultcore.azure.net'
  location: 'global'
  tags: tags
}

resource keyVaultPrivateDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: keyVaultPrivateDnsZone
  name: 'link-${resourceToken}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetResourceId
    }
  }
}

output zoneResourceId string = keyVaultPrivateDnsZone.id
