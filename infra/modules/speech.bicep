// Dedicated Speech-kind resource for Voice Live realtime (/ws/voice).
//
// Live investigation on 2026-08-21 (the project's internal implementation notes (not included in this release)) found that a standard
// `AIServices` account — including foundry.bicep's own account — rejects
// `/voice-live/realtime` with `PermissionDenied: Principal does not have access to
// API/Operation` for every principal, even Foundry Account Owner. A dedicated `SpeechServices`
// account granted `Cognitive Services Speech User` (the only one of 929 scanned built-in roles
// carrying the `SpeechServices/voicelive/realtime/*` data action) connected immediately for a
// user identity in that investigation. This module codifies that finding instead of leaving it
// a manual, undocumented step — the previous environment's speech resource and its grant were
// both created by hand and were lost with that environment's resource group.
//
// Resolved live 2026-08-25: the first voice-triggered end-to-end deployment ran through this
// resource with the control-plane workload identity (deployment 67f6e287, voice.html → plan →
// approval → execution), so SP access to Voice Live realtime is proven in production — the
// suspected SP-specific rollout gating never materialised.

@description('Azure region.')
param location string

@description('Deterministic suffix.')
param resourceToken string

@description('Tags applied to every resource.')
param tags object

@description('Principal ID of the control-plane workload identity, the only caller of Voice Live.')
param controlPlanePrincipalId string

@description('Principal ID of the human operator running azd, for local voice verification during development. Empty skips the grant.')
param operatorPrincipalId string = ''

var accountName = 'speech-gw-${resourceToken}'

resource account 'Microsoft.CognitiveServices/accounts@2025-12-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'SpeechServices'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: accountName
    // The secretless-identity rule: no API keys. Voice Live authenticates via the control plane's
    // own workload identity token, never a local-auth key.
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

// Cognitive Services Speech User — verified live 2026-08-21, see module docstring above.
var cognitiveServicesSpeechUserRoleId = 'f2dc8367-1007-4938-bd23-fe263f013447'

resource controlPlaneSpeechAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, controlPlanePrincipalId, cognitiveServicesSpeechUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesSpeechUserRoleId
    )
    principalId: controlPlanePrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource operatorSpeechAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorPrincipalId)) {
  scope: account
  name: guid(account.id, operatorPrincipalId, cognitiveServicesSpeechUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesSpeechUserRoleId
    )
    principalId: operatorPrincipalId
    principalType: 'User'
  }
}

output accountName string = account.name
output endpoint string = 'https://${account.properties.customSubDomainName}.cognitiveservices.azure.com'
