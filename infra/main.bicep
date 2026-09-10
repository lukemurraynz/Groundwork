// Groundwork platform infrastructure.
//
// Deployed by Azure Developer CLI (PD-004). There is no CI/CD pipeline in the release path, so the
// manual controls in docs/release-checklist.md are mandatory, not advisory.
//
// API versions below were verified against the live subscription on 2026-07-30 with:
//   az provider show --namespace <ns> --query "resourceTypes[?resourceType=='<type>'].apiVersions"
// Stable versions only. Preview API versions require architect approval per
// the team's internal IaC review standards.
//
// Two identities, deliberately. The control plane may call a model and holds read-only access to
// customer tenants. The orchestrator writes to customer tenants and never imports a model library.
// Separate identities mean the ADR-0001 authority split is enforced by Azure RBAC, not only by
// code review.

targetScope = 'subscription'

@minLength(1)
@maxLength(24)
@description('Environment name used to derive resource names. Supplied by azd.')
param environmentName string

@description('Azure region. Restricted to regions where AKS, Foundry model deployments, Cosmos DB, and Speech are all available together. The original build of this platform constrained this to Australian regions only, for a customer data-residency requirement — if you need that, narrow the @allowed list back down to the region(s) you require.')
@allowed([
  'australiaeast'
  'australiasoutheast'
  'eastus'
  'eastus2'
  'westus3'
  'swedencentral'
  'francecentral'
  'uksouth'
  'canadacentral'
  'japaneast'
  'southeastasia'
  'germanywestcentral'
])
param location string = 'eastus2'

@description('Object ID of the operator granted data-plane access for break-glass administration. Recorded in the audit trail when used.')
param operatorPrincipalId string = ''

// Capacity profile. This changes *sizing only* — never a security control.
//
// Identical in both profiles: workload identity, OIDC issuer, managed AAD with Azure RBAC, local
// accounts disabled, Entra-only data plane on Cosmos and Storage, no ACR admin user, immutable
// report storage, and the three separate node pools that keep the planning surface isolated from
// executor load (FR-041, FR-045c).
//
// Different in 'dev': fewer and smaller nodes, lower Cosmos throughput, and shorter telemetry
// retention. Zone redundancy is NOT reduced — see the availabilityZones comment below.
//
// The dev profile is genuinely the same platform, smaller. It is not a degraded variant: every
// control, boundary, and identity separation behaves identically, so a change validated in dev is
// validated for production.
@description('Capacity profile. Affects sizing only; every security control is identical in both.')
@allowed([
  'dev'
  'production'
])
param costProfile string = 'dev'
// Monthly spend ceiling; inactive until an operator e-mail is set (see modules/budget.bicep).
param budgetContactEmail string = ''
param budgetAmount int = 200

@description('Recover a soft-deleted Key Vault of the derived name instead of creating a new one. See modules/keyvault.bicep.')
param recoverKeyVault bool = false

var isProduction = costProfile == 'production'

// Pinned rather than floating so an upgrade is a reviewed change rather than drift.
//
// 1.35 is the regional default, verified 2026-07-30 with `az aks get-versions --location
// australiaeast`. 1.32 was the original value and failed preflight: it is past mainstream support
// and available only under Long-Term Support, which this cluster does not enable. Confirm with that
// command before changing this — the supported window moves.
@description('Kubernetes version for the AKS cluster.')
param kubernetesVersion string = '1.35'

// Zone redundancy is required by FR-041a and is kept in BOTH profiles.
//
// An earlier revision made dev single-zone to save money. That was wrong: AKS bills per node
// regardless of which zone it lands in, so restricting zones saves nothing and trades away the
// FR-041a requirement for no benefit. Spreading a small pool across zones is free.
var availabilityZones = ['1', '2', '3']

var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))

// Override for the Foundry account's naming token specifically, independent of every other
// resource's `resourceToken`. A `Microsoft.CognitiveServices` account with
// `allowProjectManagement: true` creates a linked Azure ML workspace behind the scenes; deleting
// and recreating the account under the same name can fail with `Soft-deleted workspace exists`
// while that workspace's own soft-delete retention window is still open. Set `azd env set
// GROUNDWORK_FOUNDRY_ACCOUNT_TOKEN_OVERRIDE <value>` to move the Foundry account to a new name if
// that happens; leave it unset otherwise.
@description('Override for the Foundry account naming token. Leave empty to use the standard resourceToken.')
param foundryAccountTokenOverride string = ''
var foundryResourceToken = empty(foundryAccountTokenOverride) ? resourceToken : foundryAccountTokenOverride

var tags = {
  'azd-env-name': environmentName
  application: 'groundwork'
  environment: environmentName
  dataClassification: 'confidential'
}

resource platformResourceGroup 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module identity 'modules/identity.bicep' = {
  name: 'groundwork-identity'
  scope: platformResourceGroup
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
  }
}

module observability 'modules/observability.bicep' = {
  name: 'groundwork-observability'
  scope: platformResourceGroup
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    retentionInDays: isProduction ? 365 : 30
    controlPlanePrincipalId: identity.outputs.controlPlanePrincipalId
    orchestratorPrincipalId: identity.outputs.orchestratorPrincipalId
  }
}

module storage 'modules/storage.bicep' = {
  name: 'groundwork-storage'
  scope: platformResourceGroup
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    controlPlanePrincipalId: identity.outputs.controlPlanePrincipalId
    orchestratorPrincipalId: identity.outputs.orchestratorPrincipalId
    operatorPrincipalId: operatorPrincipalId
  }
}

module cosmos 'modules/cosmos.bicep' = {
  name: 'groundwork-cosmos'
  scope: platformResourceGroup
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    availabilityZones: availabilityZones
    maxThroughput: isProduction ? 4000 : 1000
    controlPlanePrincipalId: identity.outputs.controlPlanePrincipalId
    orchestratorPrincipalId: identity.outputs.orchestratorPrincipalId
    operatorPrincipalId: operatorPrincipalId
  }
}

module keyVault 'modules/keyvault.bicep' = {
  name: 'groundwork-keyvault'
  scope: platformResourceGroup
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    operatorPrincipalId: operatorPrincipalId
    controlPlanePrincipalId: identity.outputs.controlPlanePrincipalId
    logAnalyticsWorkspaceId: observability.outputs.logAnalyticsWorkspaceId
    recoverVault: recoverKeyVault
  }
}

module registry 'modules/registry.bicep' = {
  name: 'groundwork-registry'
  scope: platformResourceGroup
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
  }
}

module aks 'modules/aks.bicep' = {
  name: 'groundwork-aks'
  scope: platformResourceGroup
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    kubernetesVersion: kubernetesVersion
    availabilityZones: availabilityZones
    nodeVmSize: isProduction ? 'Standard_D4s_v5' : 'Standard_D2s_v5'
    systemPoolMinCount: isProduction ? 3 : 1
    systemPoolMaxCount: isProduction ? 3 : 2
    controlPlanePoolMinCount: isProduction ? 2 : 1
    controlPlanePoolMaxCount: isProduction ? 6 : 3
    executorPoolMinCount: isProduction ? 2 : 1
    executorPoolMaxCount: isProduction ? 6 : 3
    logAnalyticsWorkspaceId: observability.outputs.logAnalyticsWorkspaceId
    controlPlaneIdentityId: identity.outputs.controlPlaneIdentityId
    orchestratorIdentityId: identity.outputs.orchestratorIdentityId
    clusterIdentityId: identity.outputs.clusterIdentityId
    operatorPrincipalId: operatorPrincipalId
    containerRegistryName: registry.outputs.registryName
  }
}

module foundry 'modules/foundry.bicep' = {
  name: 'groundwork-foundry'
  scope: platformResourceGroup
  params: {
    location: location
    resourceToken: foundryResourceToken
    tags: tags
    controlPlanePrincipalId: identity.outputs.controlPlanePrincipalId
    operatorPrincipalId: operatorPrincipalId
    modelCapacity: isProduction ? 30 : 10
  }
}

module speech 'modules/speech.bicep' = {
  name: 'groundwork-speech'
  scope: platformResourceGroup
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    controlPlanePrincipalId: identity.outputs.controlPlanePrincipalId
    operatorPrincipalId: operatorPrincipalId
  }
}

module email 'modules/email.bicep' = {
  name: 'groundwork-email'
  scope: platformResourceGroup
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    orchestratorPrincipalId: identity.outputs.orchestratorPrincipalId
  }
}

// azd reads these to populate the service environment. Note there is no connection string or key
// among them: every consumer authenticates with workload identity (FR-044, secretless identity).
// Explicit rather than relying on azd's implicit AZURE_TENANT_ID — the k8s manifests read this by
// name, and an explicit output is a documented contract instead of an assumption about azd behaviour.
output AZURE_TENANT_ID string = subscription().tenantId
output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = platformResourceGroup.name
output AZURE_AKS_CLUSTER_NAME string = aks.outputs.clusterName
// azd resolves the image push target from these two outputs.
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.loginServer
output AZURE_CONTAINER_REGISTRY_NAME string = registry.outputs.registryName
output GROUNDWORK_COSMOS_ENDPOINT string = cosmos.outputs.endpoint
output GROUNDWORK_STORAGE_ACCOUNT_URL string = storage.outputs.blobEndpoint
output GROUNDWORK_KEY_VAULT_URI string = keyVault.outputs.vaultUri
output GROUNDWORK_STORAGE_REGION string = location
output APPLICATIONINSIGHTS_CONNECTION_STRING string = observability.outputs.appInsightsConnectionString
output GROUNDWORK_FOUNDRY_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output GROUNDWORK_FOUNDRY_MODEL_DEPLOYMENT string = foundry.outputs.modelDeploymentName
output GROUNDWORK_CONTROLPLANE_CLIENT_ID string = identity.outputs.controlPlaneClientId
output GROUNDWORK_ORCHESTRATOR_CLIENT_ID string = identity.outputs.orchestratorClientId
// T036/identity.py's readiness check needs the *object id* of the identity that will execute a
// deployment (CL-004: one workload identity per component, not per customer tenant this release —
// see groundwork_shared/identity/credentials.py's module docstring). The client id above is what
// the orchestrator pod's own federated credential uses to authenticate; this is the same
// identity's object id, which is what ARM role-assignment queries filter on.
output GROUNDWORK_ORCHESTRATOR_PRINCIPAL_ID string = identity.outputs.orchestratorPrincipalId
// api/tenants.py's grant_customer_ado_org_access calls Azure DevOps as the control plane's own
// identity, not the orchestrator's — a brand-new Azure DevOps organization has never heard of
// this identity either, and needs its own object id to build correct "entitle this identity too"
// instructions instead of a misleading, unresolvable 401 (found live 2026-09-07).
output GROUNDWORK_CONTROLPLANE_PRINCIPAL_ID string = identity.outputs.controlPlanePrincipalId
// The Azure DevOps onboarding instructions (api/lighthouse_onboarding.py) tell a customer to add
// the orchestrator's identity to Project Collection Administrators "by name" — the ADO
// people-picker resolves this far more reliably than the bare object id above.
output GROUNDWORK_ORCHESTRATOR_DISPLAY_NAME string = identity.outputs.orchestratorIdentityName
// ACS Email: the orchestrator sends deployment-outcome notifications to the address recorded on
// each CustomerTenant.notification_email (FR-051). Both outputs are required — absent either, the
// worker skips notification rather than failing startup (OrchestratorSettings.acs_email_endpoint
// and acs_email_sender_address are each Optional).
output GROUNDWORK_ACS_EMAIL_ENDPOINT string = email.outputs.communicationServiceEndpoint
output GROUNDWORK_ACS_EMAIL_SENDER_ADDRESS string = email.outputs.senderAddress
output GROUNDWORK_VOICE_LIVE_ENDPOINT string = speech.outputs.endpoint

// Monthly spend ceiling for the whole platform subscription (inactive until budgetContactEmail is set).
resource monthlyBudget 'Microsoft.Consumption/budgets@2019-11-01' = if (!empty(budgetContactEmail)) {
  name: 'groundwork-monthly'
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: '2026-09-01'
    }
    notifications: {
      enabled: true
      operator: 'GreaterThanOrEqual'
      threshold: 100
      contactEmails: [budgetContactEmail]
      thresholdType: 'Actual'
    }
  }
}
// budget activation re-run marker
