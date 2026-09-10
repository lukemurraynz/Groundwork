// ACS Email Communication Service for deployment-outcome notifications (FR-051).
//
// Provisions:
//   - Microsoft.Communication/emailServices — the parent resource
//   - Microsoft.Communication/emailServices/domains — the Azure-managed sender domain
//     (azurecomm.net). No custom-domain DNS verification is required for the AzureManaged domain;
//     it is provisioned and ready immediately. A customer-visible From address requires
//     custom-domain verification performed outside azd — see the ACS Email documentation.
//   - Microsoft.Communication/communicationServices — the endpoint the SDK authenticates against
//     and that links the email domain for sending.
//   - Role assignment: Contributor on the Communication Service, for the orchestrator managed
//     identity, so the worker sends email without a key (the secretless-identity rule: Entra-only
//     access, no long-lived credentials).
//
// API versions pinned to stable versions verified 2026-08-15 with:
//   az provider show --namespace Microsoft.Communication \
//     --query "resourceTypes[?resourceType=='emailServices'].apiVersions"
// Stable: 2023-04-01.

@description('Azure region.')
param location string

@description('Deterministic suffix.')
param resourceToken string

@description('Tags applied to every resource.')
param tags object

@description('Principal ID of the orchestrator identity. Granted Contributor on the Communication Service so it can send email without a key.')
param orchestratorPrincipalId string

// Contributor built-in role (b24988ac-6180-42a0-ab88-20f7382dd24c). Used here because no
// narrower data-plane built-in role is available for ACS Email send access with managed identity
// in the 2023-04-01 API surface. If a minimum-privilege ACS Email Sender role becomes available,
// replace this with that role definition ID.
var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'

resource emailService 'Microsoft.Communication/emailServices@2023-04-01' = {
  name: 'acs-email-gw-${resourceToken}'
  // Email services must be deployed to 'global'; a per-region deployment is not supported.
  // Data residency is controlled by the dataLocation property.
  location: 'global'
  tags: tags
  properties: {
    dataLocation: 'Australia'
  }
}

// Azure-managed sender domain — no DNS verification required.
resource senderDomain 'Microsoft.Communication/emailServices/domains@2023-04-01' = {
  parent: emailService
  name: 'AzureManagedDomain'
  location: 'global'
  tags: tags
  properties: {
    domainManagement: 'AzureManaged'
  }
}

// Communication Service — the endpoint the SDK calls; links the sender domain.
resource communicationService 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: 'acs-gw-${resourceToken}'
  location: 'global'
  tags: tags
  properties: {
    dataLocation: 'Australia'
    linkedDomains: [
      senderDomain.id
    ]
  }
}

// Orchestrator identity → Contributor on the Communication Service.
resource orchestratorContributorRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(communicationService.id, orchestratorPrincipalId, contributorRoleId)
  scope: communicationService
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
    principalId: orchestratorPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// The endpoint the worker SDK authenticates against (workload identity, no key).
output communicationServiceEndpoint string = 'https://${communicationService.name}.communication.azure.com'

// The From address for deployment-outcome notifications.
// Uses the Azure-managed domain initially; replace with a verified custom-domain address
// for production customer-visible branding.
output senderAddress string = 'DoNotReply@${senderDomain.properties.mailFromSenderDomain}'
