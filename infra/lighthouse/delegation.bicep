// Azure Lighthouse delegation (FR-006; Clarifications, 2026-08-24 — replaces the previously
// planned multi-tenant Entra app + workload-identity-federation bootstrap model).
//
// **Deployed by the CUSTOMER, into the CUSTOMER's own subscription** — never by Groundwork. The
// customer's admin runs this once per subscription they want to onboard (`az deployment sub create
// --location <region> --template-file delegation.bicep --parameters managedByTenantId=<groundwork
// tenant id> principalId=<groundwork identity object id>`), the same self-service pattern every
// Azure Lighthouse onboarding uses. Requires a role with `Microsoft.Authorization/roleAssignments/
// {write,delete,read}` (e.g. Owner) on the target subscription — [VERIFIED],
// learn.microsoft.com/azure/lighthouse/concepts/architecture, retrieved 2026-08-24 (see
// docs/adr/0002-lighthouse-bootstrap-only-ado-pipeline-execution.md).
//
// **Two roles, never more, never Owner, never a custom role.** Lighthouse's own `authorizations`
// schema only accepts built-in RBAC roles — confirmed against the official worked example and a
// practitioner source independently (see docs/adr/0002-lighthouse-bootstrap-only-ado-pipeline-execution.md):
//   - Contributor: lets System's bootstrap-identity route (FR-006a) create the one user-assigned
//     managed identity + federated credential this subscription will ever get from System directly.
//   - User Access Administrator, restricted via `delegatedRoleDefinitionIds` to grant *only*
//     Contributor: without this, System could create the bootstrap identity but could never grant
//     it any RBAC role, since `Microsoft.Authorization/roleAssignments/write` is blocked under a
//     bare Contributor delegation. This is the one and only thing the delegated UAA authorization
//     is used for — see `api/tenants.py`'s `bootstrap_identity` route.
//
// After bootstrap, System's own direct access to this subscription is never used again — every
// real provisioning write executes through the customer's Azure DevOps pipeline (FR-038a), using
// the bootstrap identity's own federated credential, not this delegation.
//
// API version 2020-02-01-preview is the current version in Microsoft's own worked example; no
// newer stable version was documented as of retrieval — see docs/adr/0002-lighthouse-bootstrap-only-ado-pipeline-execution.md.

targetScope = 'subscription'

@description('Display name for this delegation, shown to the customer in the Azure portal (Service providers blade).')
param mspOfferName string = 'Groundwork Platform bootstrap access'

@description('Explains what this delegation is for, shown to the customer alongside mspOfferName.')
param mspOfferDescription string = 'Groundwork Platform: bootstrap-only access to create one deployment identity per subscription. All ongoing provisioning runs through your own Azure DevOps pipeline, not this delegation.'

@description('Groundwork\'s own Entra tenant ID — the managing tenant this subscription is delegated to.')
param managedByTenantId string

@description('Object ID of the Groundwork identity (in Groundwork\'s own tenant) that receives Contributor + restricted User Access Administrator on this subscription.')
param principalId string

@description('Display name for principalId, shown to the customer alongside the delegation.')
param principalIdDisplayName string = 'Groundwork Platform bootstrap identity'

// Built-in RBAC role definition GUIDs — fixed, well-known Azure identifiers, not configuration.
// Must stay byte-identical to CONTRIBUTOR_ROLE_DEFINITION_ID in
// src/groundwork_controlplane/api/tenants.py — that route grants exactly this role to the
// bootstrap identity it creates, and the delegated User Access Administrator authorization below
// exists solely to make that one grant possible.
var contributorRoleDefinitionId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'
var userAccessAdministratorRoleDefinitionId = '18d7d88d-d35e-4fb5-a5c3-7773c20a72d9'

var authorizations = [
  {
    principalId: principalId
    principalIdDisplayName: principalIdDisplayName
    roleDefinitionId: contributorRoleDefinitionId
  }
  {
    principalId: principalId
    principalIdDisplayName: principalIdDisplayName
    roleDefinitionId: userAccessAdministratorRoleDefinitionId
    delegatedRoleDefinitionIds: [
      contributorRoleDefinitionId
    ]
  }
]

// Deterministic from mspOfferName, matching Microsoft's own worked example — do not change
// mspOfferName after first deployment, or Lighthouse treats this as a new, separate offer instead
// of updating the existing one (see docs/adr/0002-lighthouse-bootstrap-only-ado-pipeline-execution.md).
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
