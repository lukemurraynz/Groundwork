# ADR-0007: Secretless identity; ADO WIF issuer/subject mirroring after sprint-253 change

**Date**: 2026-07-30
**Status**: Accepted (2026-07-30; implementation detail updated 2026-08-25)

## Context

The product asks customers to grant a third-party system authority inside their Azure tenant.
That trust is only justifiable when no long-lived credential exists that could be leaked, stolen,
or rotated by an engineer with source access.

A concrete production defect (sprint-253 change, found live 2026-08-25) sharpened the rule
beyond the general secretless principle. Azure DevOps changed the default issuer for new
`WorkloadIdentityFederation` service connections from the legacy vstoken issuer
(`https://vstoken.dev.azure.com/{orgGuid}`, subject `sc://{org}/{project}/{connection}`) to the
Entra issuer (`https://login.microsoftonline.com/{tenant}/v2.0`, subject
`/eid1/c/pub/t/./a/./sc/{orgGuid}/{endpointGuid}`). The new subject embeds the server-assigned
endpoint GUID, which is unknowable before the service connection exists.

The prior approach (predicting the subject at bootstrap time and creating a federated credential
with a fixed expected string) broke silently on all new service connections created after the
sprint-253 cutover, producing `AADSTS700211` at pipeline login time.

## Decision

**No client secrets anywhere.** Entra ID, workload identity federation, and managed identity
are the only authentication mechanisms. Client secrets are not a starting point and not a
fallback. This applies to:

- AKS workload identity for all Groundwork service pods
- The bootstrap managed identity created in each customer subscription (federated via ADO WIF,
  not a secret)
- The Lighthouse-delegated control-plane identity (managed identity, no secret)
- No exceptions stored in Kubernetes, CI/CD variables, source, logs, traces, or fixtures

**ADO WIF issuer/subject mirroring (sprint-253 fix):** After creating or updating an ADO
`WorkloadIdentityFederation` service connection, `devops_project` reads back the endpoint
document returned by ADO and extracts the effective `workloadIdentityFederationIssuer` and
`workloadIdentityFederationSubject` from `authorization.parameters`. It then creates or updates
a federated identity credential (FIC) named `fc-sc-{endpointId}` on the bootstrap managed
identity with exactly those server-assigned values.

The FIC name `fc-sc-{endpointId}` is deterministic from the endpoint ID, making the
create-or-update idempotent. The issuer and subject are always read from the live ADO response,
never predicted. This works for both the Entra issuer (post-sprint-253) and the legacy vstoken
issuer (pre-sprint-253 or existing connections), since `federation_parameters()` in
`devops_pipelines.py` reads whichever ADO actually provides.

Additionally: newly created ADO service connections and pipeline environments park runs at
`Checkpoint.Authorization` until explicitly approved. `devops_project` PATCHes
`pipelinepermissions` for the endpoint and environment after creation to pre-approve them, so no
manual click is needed in the ADO web UI before the first pipeline run.

## Consequences

**Positive**

- No credential in the system has a fixed lifetime that must be rotated. Revocation is structural
  (remove the FIC or the Lighthouse registration assignment), not key deletion.
- The sprint-253 production failure is not repeatable: the FIC is always mirrored from the live
  ADO-assigned values, so future ADO issuer changes require no code change.
- Pipeline runs do not park at `Checkpoint.Authorization` on first use.

**Negative / watch points**

- The `azure-mgmt-msi` SDK dependency is required for FIC create/update. It was added in
  2026-08-25 (`v2024_11_30` submodule import required; top-level models union causes pyright
  noise). Ensure it stays in the `orchestrator` extras group in `pyproject.toml`.
- Deleting and recreating a service connection changes its endpoint ID, which changes the FIC
  name. The old `fc-sc-{oldEndpointId}` FIC is orphaned (harmless but untidy). The
  idempotence logic in `ensure_service_connection` updates rather than recreates where possible.

**Sources**: the secretless-identity requirement this ADR implements; `AGENT_HANDOFF.md` §4 bug #2 and §9-10b;
`stages/devops_pipelines.py` `federation_parameters()` and `ensure_service_connection()`
