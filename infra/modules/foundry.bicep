// Microsoft Foundry — the planning agent's host (T045; docs/product-specification.md PD-001:
// Foundry, not Copilot Studio; native Microsoft Agent Framework, Foundry-hosted).
//
// GA resource model verified live 2026-07-31 against learn.microsoft.com/rest/api/cost-management
// (Microsoft.CognitiveServices/accounts template reference): the pre-GA hub/project/AI-Services
// five-resource model is gone. Current shape is two resources — an account (kind: AIServices,
// allowProjectManagement: true) and a child project — plus a model deployment under the account.
//
// API version 2025-12-01: the newest this Bicep CLI (0.43.8) can fully type-check for all three
// resource types (account, project, deployment) with zero BCP081/BCP037 diagnostics. 2026-05-01 and
// newer have no type definitions yet (BCP081); 2024-10-01 and older reject allowProjectManagement
// (BCP037 — the property does not exist pre-GA). Verified by compiling a minimal test file across
// the version range rather than trusting the docs' version list alone.
//
// Only the control plane calls this — the orchestrator never imports a model client (the
// deterministic-execution boundary, enforced mechanically by tests/unit/test_import_boundaries.py),
// so only the control-plane identity is granted access here.

@description('Azure region.')
param location string

@description('Deterministic suffix.')
param resourceToken string

@description('Tags applied to every resource.')
param tags object

@description('Principal ID of the control-plane workload identity, the only caller of this agent.')
param controlPlanePrincipalId string

@description('Principal ID of the human operator running azd, for local plan-generation verification during development. Empty skips the grant.')
param operatorPrincipalId string = ''

// GlobalStandard capacity is billed per 1,000 tokens/minute provisioned, not per request — kept
// small deliberately per the project's single-engagement scope decision (not included in this release) (minimum viable for one customer
// engagement, scale-out configured but not built out).
@description('GPT-4o GlobalStandard capacity, in units of 1,000 tokens/minute.')
@minValue(1)
param modelCapacity int = 10

var accountName = 'foundry-gw-${resourceToken}'
var projectName = 'groundwork-planning'
var modelDeploymentName = 'gpt-5-1'

resource account 'Microsoft.CognitiveServices/accounts@2025-12-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  // No identity on the account itself: nothing here calls out to a BYO storage/networking
  // resource on the account's own behalf. The control-plane workload identity that *calls* this
  // project is granted access below, via role assignments — that is the only identity relationship
  // this module needs.
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    // The secretless-identity rule: no API keys. The control plane authenticates with its own
    // workload identity; there is no local-auth credential for anything to leak.
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-12-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-12-01' = {
  parent: account
  name: modelDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      // gpt-4o, not gpt-5.1: this account was first built against gpt-4o, which was the current
      // generation at design time. By 2026-07-31, live `az cognitiveservices model list --location
      // australiaeast` preflight checks reject every gpt-4o version (2024-05-13, 2024-08-06,
      // 2024-11-20) with `ServiceModelDeprecating: ... is in deprecating state and cannot be used
      // for new deployments` — the model list command listing a version does not mean ARM will
      // accept a new deployment of it; two live `azd provision` failures against this exact Bicep
      // confirmed that gap before this fix. gpt-5.1 is the model the same live query reports as
      // `lifecycleStatus: GenerallyAvailable` with `inference` deprecation not until 2027-05-15 and
      // GlobalStandard SKU support, `chatCompletion: true` — i.e. the mainline chat-completion
      // replacement, not a specialised variant (codex, mini, nano, pro, or the realtime family
      // the research notes § V-003 already cover for Voice Live).
      name: 'gpt-5.1'
      version: '2025-11-13'
    }
  }
}

// Foundry Agent Consumer — least-privilege role for a principal that only interacts with agents
// (including calling the Responses API) without creating or modifying them. Granted at account
// scope: project scope alone is not sufficient for Responses-API calls against this account. See
// ADR-0014 for the full rationale.
var foundryAgentConsumerRoleId = 'eed3b665-ab3a-47b6-8f48-c9382fb1dad6'

resource controlPlaneAgentAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, controlPlanePrincipalId, foundryAgentConsumerRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      foundryAgentConsumerRoleId
    )
    principalId: controlPlanePrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Cognitive Services OpenAI User — required for model inference calls (chat completions and
// responses) at the account scope. Foundry Agent Consumer alone does not grant inference
// permission. This is the same role the Azure Portal assigns when you use its "Add role
// assignment" blade for "Cognitive Services OpenAI User" on an AI Services account.
var cognitiveServicesOpenAIUserId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource controlPlaneInferenceAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, controlPlanePrincipalId, cognitiveServicesOpenAIUserId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesOpenAIUserId
    )
    principalId: controlPlanePrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Mirrors keyvault.bicep's and aks.bicep's own operatorAccess pattern — granted automatically on
// every `azd provision` so a developer/operator running local plan-generation verification against
// real Foundry (2026-08-06) never has to hand-run role assignments by hand. Same grants the
// control plane's own identity needs above, at the same scopes, for the same documented reasons.
resource operatorAgentAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorPrincipalId)) {
  scope: account
  name: guid(account.id, operatorPrincipalId, foundryAgentConsumerRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      foundryAgentConsumerRoleId
    )
    principalId: operatorPrincipalId
    principalType: 'User'
  }
}

resource operatorInferenceAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorPrincipalId)) {
  scope: account
  name: guid(account.id, operatorPrincipalId, cognitiveServicesOpenAIUserId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesOpenAIUserId
    )
    principalId: operatorPrincipalId
    principalType: 'User'
  }
}

// Voice Live (the /ws/voice realtime surface) needs both roles below in addition to the
// inference and agent-consumer roles above — neither of those alone covers the
// /voice-live/realtime operation.
var cognitiveServicesUserId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource controlPlaneVoiceLiveAccountAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, controlPlanePrincipalId, cognitiveServicesUserId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesUserId
    )
    principalId: controlPlanePrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource controlPlaneVoiceLiveFoundryUserAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, controlPlanePrincipalId, foundryUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      foundryUserRoleId
    )
    principalId: controlPlanePrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Operator equivalents of the Voice Live grants above, so a developer running local
// plan-generation verification has the same access as the control-plane identity.
resource operatorVoiceLiveAccountAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorPrincipalId)) {
  scope: account
  name: guid(account.id, operatorPrincipalId, cognitiveServicesUserId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesUserId
    )
    principalId: operatorPrincipalId
    principalType: 'User'
  }
}

resource operatorVoiceLiveFoundryUserAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorPrincipalId)) {
  scope: account
  name: guid(account.id, operatorPrincipalId, foundryUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      foundryUserRoleId
    )
    principalId: operatorPrincipalId
    principalType: 'User'
  }
}

output accountName string = account.name
output resourceId string = account.id
output projectName string = project.name
output modelDeploymentName string = modelDeployment.name
output projectEndpoint string = 'https://${account.properties.customSubDomainName}.services.ai.azure.com/api/projects/${project.name}'
