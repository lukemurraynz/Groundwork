// User-assigned managed identities.
//
// Three identities, not one. The secretless-identity rule requires no shared execution context,
// and FR-032 requires that no code path lets one tenant reach another's resources. Collapsing
// these into a single identity would give the model-calling control plane the same Azure
// authority as the tenant-writing orchestrator, which is exactly the separation the
// deterministic-execution boundary (ADR-0001) depends on.
//
// API version pinned to 2023-01-31: the newest the Bicep CLI can type-check. The subscription's
// newest offering is 2025-05-31-PREVIEW, which is excluded anyway — preview API versions require
// architect approval per the team's internal IaC review standards.

@description('Azure region.')
param location string

@description('Deterministic suffix derived from subscription, environment, and location.')
param resourceToken string

@description('Tags applied to every resource.')
param tags object

// Control plane: may call a model, holds read-only access to customer tenants. It must never be
// granted write scope on a customer tenant — that authority belongs to the orchestrator alone.
resource controlPlaneIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-gw-cp-${resourceToken}'
  location: location
  tags: union(tags, { component: 'controlplane' })
}

// Orchestrator: writes to customer tenants under a per-tenant federated credential. Imports no
// model library (enforced by tests/unit/test_import_boundaries.py).
resource orchestratorIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-gw-orch-${resourceToken}'
  location: location
  tags: union(tags, { component: 'orchestrator' })
}

// Cluster identity: AKS control plane only. Separate so cluster operations cannot borrow workload
// authority.
resource clusterIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-gw-aks-${resourceToken}'
  location: location
  tags: union(tags, { component: 'cluster' })
}

output controlPlaneIdentityId string = controlPlaneIdentity.id
output controlPlanePrincipalId string = controlPlaneIdentity.properties.principalId
output controlPlaneClientId string = controlPlaneIdentity.properties.clientId

output orchestratorIdentityId string = orchestratorIdentity.id
output orchestratorPrincipalId string = orchestratorIdentity.properties.principalId
output orchestratorClientId string = orchestratorIdentity.properties.clientId
// The Azure DevOps Project Collection Administrators people-picker resolves a display name far
// more reliably than a bare object ID — found live 2026-09-07 building the onboarding
// instructions this identity's name is used in.
output orchestratorIdentityName string = orchestratorIdentity.name

output clusterIdentityId string = clusterIdentity.id
output clusterPrincipalId string = clusterIdentity.properties.principalId
