// Deterministic, known-in-advance public IP for the AKS cluster's outbound traffic.
//
// A tiny standalone module rather than a resource inline in main.bicep, only because Bicep
// requires resource-group-scoped resources to go through a module from a subscription-scoped
// file (BCP139) — main.bicep's targetScope is 'subscription'.
//
// Exists specifically so registry.bicep's IP firewall can allowlist a fixed address for kubelet
// image pulls: AKS's own default managed outbound IP isn't knowable at template-author time.
// Declared independently of both aks.bicep and registry.bicep to avoid a circular dependency —
// aks.bicep already needs registry.bicep's name (for the kubelet AcrPull role assignment), so
// registry.bicep cannot also depend on aks.bicep's output for this IP.

@description('Azure region.')
param location string

@description('Tags applied to the resource.')
param tags object

@description('Deterministic suffix.')
param resourceToken string

// Static allocation across all three zones to match the FR-041a zone redundancy the rest of the
// cluster already carries.
@description('Availability zones for the IP.')
param availabilityZones array

resource outboundPublicIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: 'pip-aks-outbound-${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  zones: availabilityZones
  properties: {
    publicIPAllocationMethod: 'Static'
  }
}

output resourceId string = outboundPublicIp.id
output ipAddress string = outboundPublicIp.properties.ipAddress
