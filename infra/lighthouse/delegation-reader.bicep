// Azure Lighthouse delegation — READ-ONLY variant for the readiness-report offering
// (the project's offer-readiness review (not included in this release)).
//
// **Deployed by the CUSTOMER, into the CUSTOMER's own subscription** — same self-service pattern
// as delegation.bicep, but grants exactly ONE role: Reader. This is the trust-light first step of
// the product ladder: Groundwork can run its nine landing-zone readiness checks and produce a
// validated, costed assessment with ZERO write capability anywhere in the subscription.
//
// Upgrade path: when the customer is ready for provisioning, they deploy the full
// `delegation.bicep` (Contributor + UAA-restricted-to-Contributor per ADR-0002). Lighthouse treats
// a changed authorization set on the same mspOfferName as an update to the existing registration,
// not a new offer — so this template's offer name is deliberately distinct from the bootstrap one
// to keep the two tiers independently lifecycle-able.
//
// Reader (acdd72a7-3385-48ef-bd42-f606fba81ae7) covers every read the nine readiness checks make:
// ARM resource reads, policy/quota/role-assignment reads, private-DNS and VNet inspection. The
// Fabric SPN check authenticates directly against api.fabric.microsoft.com and does not consume
// this delegation.
//
// API version matches delegation.bicep (2020-02-01-preview; see docs/adr/0002-lighthouse-bootstrap-only-ado-pipeline-execution.md).

targetScope = 'subscription'

@description('Display name for this read-only delegation, shown to the customer in the Azure portal (Service providers blade).')
param mspOfferName string = 'Groundwork Platform readiness assessment (read-only)'

@description('Explains what this delegation is for.')
param mspOfferDescription string = 'Groundwork Platform: READ-ONLY access to assess your subscription against eight landing-zone design areas and produce a costed readiness report. No write capability is granted.'

@description('Groundwork\'s own Entra tenant ID — the managing tenant this subscription is delegated to.')
param managedByTenantId string

@description('Object ID of the Groundwork identity (in Groundwork\'s own tenant) that receives Reader on this subscription.')
param principalId string

@description('Display name for principalId.')
param principalIdDisplayName string = 'Groundwork Platform assessment identity'

var readerRoleDefinitionId = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

var authorizations = [
  {
    principalId: principalId
    principalIdDisplayName: principalIdDisplayName
    roleDefinitionId: readerRoleDefinitionId
  }
]

var registrationName = guid(mspOfferName)
var registrationAssignmentName = guid(mspOfferName)

resource registrationDefinition 'Microsoft.ManagedServices/registrationDefinitions@2020-02-01-preview' = {
  name: registrationName
  properties: {
    registrationDefinitionName: mspOfferName
    description: mspOfferDescription
    managedByTenantId: managedByTenantId
    authorizations: authorizations
  }
}

resource registrationAssignment 'Microsoft.ManagedServices/registrationAssignments@2020-02-01-preview' = {
  name: registrationAssignmentName
  properties: {
    registrationDefinitionId: registrationDefinition.id
  }
}

output registrationDefinitionId string = registrationDefinition.id
output registrationAssignmentId string = registrationAssignment.id
