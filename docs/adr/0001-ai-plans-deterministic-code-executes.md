# ADR-0001: AI plans, deterministic code executes

**Date**: 2026-07-30
**Status**: Accepted (2026-07-30)

## Context

The Groundwork Platform executes infrastructure deployments inside customer Azure tenants. The
system uses AI agents (Microsoft Foundry) to reason about what a customer wants and to produce a
deployment plan. A separate orchestrator then carries out that plan.

Without a hard boundary between these two layers, model output could become an executed command,
a Bicep template body, or an ARM API call shape. That would make the system non-auditable and
would give a prompt-injection attack a direct path into a customer's tenant.

A live defect validates that this boundary is load-bearing: on 2026-08-26, the schema validation
layer intermittently rejected nested-dict structures in `PlanResource.properties`. That rejection
is correct behaviour, not a bug. Any attempt to widen the schema to accept nested objects would
re-open the injection surface the boundary exists to close.

## Decision

Model output MUST cross into orchestration as a schema-validated `DeploymentPlan` object or the
request fails. There is no partial acceptance, no defaulting of missing fields, no retry against
a loosened schema, and no coercion of type errors.

Concretely:

- `agents/boundary.py`'s `validate_model_output` is the single, unavoidable gate. Every piece of
  raw model output passes through it before anything downstream sees a `DeploymentPlan`.
- `DeploymentPlan` uses `extra="forbid"`. Unknown fields, executable-content patterns in property
  values, nested objects in properties, and control-plane-owned fields (`tenant_id`, `plan_hash`,
  `approval_status`) are all schema rejections, not runtime checks.
- A model MUST NOT hold credentials, call Azure APIs directly, or invoke an MCP tool that mutates
  customer state.
- Every executable capability MUST be reachable identically without AI, via the REST API or CLI.
  If a deployment can only be triggered by conversation, the seam is in the wrong place.

Validation failure fails the request. The caller gets every failure in the response, not just the
first.

## Consequences

**Positive**

- Every deployment that executes was produced by a known, version-pinned deterministic path. The
  audit trail can be traced without reconstructing model reasoning.
- Template-expression injection (`[reference(...)]`), shell interpolation (`$(whoami)`),
  PowerShell backtick expansion, and pipe-to-execute patterns are all caught at the schema layer
  before reaching the orchestrator.
- Each stage is independently testable and replayable without an AI session.

**Negative / watch points**

- The schema is a commitment. A model that produces a structurally valid plan containing a
  surprising value (an unexpected region, an oversized SKU) is not caught here; that is the
  validation layer's job.
- Nested-dict rejection (2026-08-26 intermittent production finding) is correct. Do not widen
  `PlanResource.properties` to accept `dict[str, Any]` to silence a failing test without
  understanding what object the model produced.

**Sources**: the deterministic-execution boundary this ADR establishes; `agents/boundary.py`; `tests/contract/test_plan_boundary.py`
