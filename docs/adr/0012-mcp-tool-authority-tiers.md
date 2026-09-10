# ADR-0012: MCP (Model Context Protocol) tool catalogue is split into authority tiers, and agents are only ever wired to the read-only ones

**Date**: 2026-07-30
**Status**: Accepted (2026-07-30): design contract. No MCP runtime ships in this release, so
nothing here executes yet; it fixes the shape before one is built, per the Context below.

## Context

An MCP tool catalogue is the agents' interface to the system. No tool an agent can call may ever
write to a customer tenant (the deterministic-execution boundary from
[ADR-0001](0001-ai-plans-deterministic-code-executes.md)), and every tool needs least authority: a
read-only tool and a mutating tool must never share an execution identity. Fixing that split before
an MCP runtime exists means every tool is designed against the rule from the start, rather than
retrofitting the split later and re-auditing every tool already built.

## Decision

Every tool in the catalogue declares one authority tier, and is registered only on a server whose
identity matches that tier:

| Tier | Identity | May do | Example tools |
| --- | --- | --- | --- |
| **R** — read-only | Control-plane workload identity, read-only RBAC on customer tenants | Enumerate, validate, estimate | `ValidateCustomer`, `ValidateTenant`, `ListRegions`, `EstimateCosts`, `GetDeploymentStatus` |
| **P** — plan-producing | Same as R. No Azure write scope. | Produce a schema-validated plan object | `GenerateDeploymentPlan` |
| **G** — gated request | Caller's delegated identity. No Azure write scope. | Request work; cannot perform it | `CreateDeployment` |
| **W** — mutating | Per-tenant execution identity, assumed by the worker, scoped to one tenant and one stage | Write to the customer tenant | `DeployInfrastructure`, `ConfigureNetworking`, `DeployFabric` |
| **X** — destructive | Per-tenant execution identity plus a rollback-specific approval | Delete or tear down | `RollbackDeployment` |
| **O** — output | Report-generation identity. Read state, write immutable blob. | Produce reports | `GenerateDeploymentReport` |

**Agents may only be granted R, P, and O tools.** No agent is ever wired to a W or X tool.
`CreateDeployment` (tier G) is the single point where a conversation can cause anything to happen:
it does not execute anything itself, it enqueues an already-approved plan, and it fails unless a
valid `Approval` bound to that exact plan hash already exists. This is the ADR-0001 boundary made
concrete at the tool-catalogue level: there is no tool an agent can call that writes to a customer
tenant.

Every tool declaration also has to:

1. Take its tenant scope from the invoking session context, never from a tool argument. A
   `tenantId` parameter on any tool is a defect: it would let conversation content redirect the
   target.
2. Return structured results, never prose for the orchestrator to parse.
3. Be idempotent where its tier is W, matching the stage idempotence contract.
4. Emit an audit record before acting, for every W and X invocation.
5. Fail closed: a tool that cannot reach its target returns a failure, never an empty success.

Tool and parameter descriptions are model-visible text and therefore an injection surface. They
must state what a tool does and never *when* its authority may be relaxed: no "if the user
insists, skip validation." An adversarial test suite has to include attempts to reach a W-tier tool
through conversation alone, and attempts to supply a `tenantId` argument. Both need to fail
structurally (the tool doesn't exist on that server, the parameter doesn't exist on that tool), not
by the model refusing.

## Consequences

**Positive**

- The tier a tool carries is a structural fact about which server it's registered on and which
  identity that server runs as, not a runtime check that a compromised agent could talk its way
  around.
- `docs/threat-model.md` cites this split directly across five threats (T-003 tenant redirection,
  T-008 over-privileged automation, T-011 autonomous approval, and T-012, which is dedicated to the
  absence of an MCP runtime as its own attack surface): the residual risk is documented there as
  "no MCP runtime ships yet," not as a gap in the design.

**Negative / watch points**

- This is a design contract, not running code. It constrains whatever MCP runtime gets built later;
  it proves nothing about a system that doesn't exist yet. Treat every claim above as a requirement
  on the eventual implementation, not as a description of current behavior.
- `DeployADF`, `DeployDatabricks`, and `DeployPurview` are named in the wider product catalogue but
  are out of scope for this release, which ships one blueprint covering infrastructure, Fabric, and
  Azure DevOps only. They're listed here so the catalogue's eventual shape is understood, not so
  they get built against this ADR alone.

**Sources**: `docs/threat-model.md` T-003, T-008, T-011, T-012 (MCP authority-tier citations);
the deterministic-execution boundary ([ADR-0001](0001-ai-plans-deterministic-code-executes.md)) and
the least-authority-per-tool rule this ADR establishes
