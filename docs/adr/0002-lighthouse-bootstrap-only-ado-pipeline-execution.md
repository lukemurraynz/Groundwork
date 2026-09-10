# ADR-0002: Lighthouse bootstrap-only; ongoing provisioning via the customer's own ADO pipeline

**Date**: 2026-08-24
**Status**: Accepted (2026-08-24)
**Supersedes**: the multi-tenant Entra app + admin-consent bootstrap model (withdrawn 2026-08-24)

## Context

Groundwork must write Azure resources into a customer's subscription without holding long-lived
credentials inside the platform. The original design registered a multi-tenant Entra app and
walked the customer through an admin-consent flow. That model was replaced in the 2026-08-24
clarification session after it proved unable to satisfy the secretless-identity requirement (see
[ADR-0007](0007-secretless-identity-ado-wif-mirroring.md)) at scale, and because it would leave
Groundwork's identity permanently active inside every customer subscription.

The replacement is Azure Lighthouse for the one-off trust handshake, and the customer's own Azure
DevOps pipeline for every subsequent provisioning write.

A structural caveat applies to single-tenant dev/test environments: Lighthouse fundamentally
cannot delegate a subscription back to its own home tenant. When the customer subscription and
Groundwork's tenant share the same Entra tenant (as in this project's own dev environment),
`az deployment sub create` with `delegation.bicep` fails with
`InvalidRegistrationDefinitionCreateRequest`. The workaround is granting the same two roles
directly via `az rest` role-assignment PUTs. This does not exercise the real cross-tenant code
path and is a dev/test-only substitute, not an alternative production model.

## Decision

**Onboarding (once per subscription, human-in-the-loop):** The customer's admin deploys
`infra/lighthouse/delegation.bicep` into their own subscription. This grants Groundwork's
control-plane identity exactly two built-in roles: `Contributor`, and `User Access Administrator`
restricted via `delegatedRoleDefinitionIds` to grant only `Contributor`. No custom roles.
Lighthouse's `authorizations` schema accepts only built-in RBAC role IDs.

**Bootstrap (once per subscription, `POST /{tenant}/subscriptions/{sub}/bootstrap-identity` in
`api/tenants.py`):** Using the Lighthouse-delegated access, the control plane creates exactly one
user-assigned managed identity in the customer subscription, a federated credential trusting the
Azure DevOps workload-identity-federation service connection, and grants that identity Contributor
on the subscription via the delegated UAA role. This is the only direct ARM write Groundwork ever
makes into a customer subscription.

**Ongoing provisioning (every deployment):** The `devops_project` stage creates the ADO project,
pushes `infra/blueprints/<blueprint_id>/` as pipeline source, creates the pipeline, and creates
the ADO service connection using the bootstrap identity's client ID. Subsequent stages
(`infrastructure`, `fabric`, `monitoring`) trigger and poll that pipeline. Every real
provisioning write executes through the customer's own pipeline, authenticated as the bootstrap
identity via its federated credential. Groundwork's direct Azure access is not used again after
bootstrap.

**ADO org access (FR-038b)** is a separate, human-assisted handshake: the orchestrator's workload
identity must be a member of the customer's ADO org before `devops_project` can run. This
currently requires two manual steps by someone with ADO org-owner rights (license/membership POST,
then PCA group membership via the ADO web UI). No automation exists for this today.

## Consequences

**Positive**

- Groundwork never holds a credential that can provision arbitrary resources. The bootstrap
  identity is scoped to `Contributor` on one subscription, granted by the customer's own admin.
- The customer can revoke Lighthouse at any time by deleting the registration assignment. After
  bootstrap, revoking it does not affect ongoing deployments (which run as the bootstrap identity,
  not as Groundwork's delegation).
- Activity Log entries in the customer's own subscription are attributable to specific Groundwork
  principals, satisfying FR-047 without extra plumbing.

**Negative / watch points**

- Single-tenant dev/test workaround (direct role assignment) does not exercise Lighthouse. Tests
  against this environment prove the bootstrap and pipeline stages work, but not the delegation
  mechanism itself.
- ADO org access has no automated onboarding path today. A new customer engagement requires manual
  operator steps before any deployment can run.
- `Microsoft.ManagedServices` resource provider must be registered in the customer's subscription
  before deploying `delegation.bicep`. This is a manual, subscription-owner-only prerequisite
  that belongs in the onboarding instructions sent alongside the template.

**Sources**: `AGENT_HANDOFF.md` §3 (supersedes note); `infra/lighthouse/delegation.bicep`;
`src/groundwork_orchestrator/stages/pipeline_execution.py`; `api/tenants.py` bootstrap route
