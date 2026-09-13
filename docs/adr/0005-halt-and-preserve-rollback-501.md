# ADR-0005: Halt-and-preserve over auto-rollback; rollback gated but not executed (501)

**Date**: 2026-08-01
**Status**: Accepted (2026-08-01). **Superseded in part by [ADR-0009](0009-proposal-rollback-via-customer-pipeline.md) (2026-08-26, implemented same day): rollback execution is real.**

**[VERIFIED] 2026-09-13: the `501` behaviour this ADR's Decision section describes below is no
longer current.** `api/recovery.py`'s own module docstring and code confirm a fully validated,
approved rollback request now queues and runs a `rollback.yml` pipeline inside the customer's own
Azure DevOps project (redeploying the last known-good configuration; it never fights `denyDelete`
because it redeploys prior state rather than deleting). ADR-0009 refines this ADR rather than
replacing it — every gating rule described below (offered only where the blueprint's
`recovery_path` names it, a distinct and complete rollback approval, bound to the same plan hash,
distinct from the original execution's approval) is unchanged and still enforced exactly as
written. Only the "once all checks pass, the endpoint returns `501`" sentence is now false; treat
every other sentence in the Decision section as still accurate. This correction was made after a
2026-09-13 audit found `customer-journey-map.md` had built a "trust breaker" critical moment and a
top-ranked pain point on the stale `501` premise, discovered only by reading `api/recovery.py`
directly rather than trusting this ADR's own title and body — the same failure mode
`production-validation`'s "false in-use claim" / "claimed-but-unreproducible" taxonomy shapes name.

## Context

When a deployment stage fails permanently, the system must decide what to do with the partially
built infrastructure already created in the customer's subscription. Two options exist: attempt an
automatic teardown (rollback), or halt and leave the resources in place pending a human decision.

`stages/infrastructure.py` sets `denySettings.mode: denyDelete` on its own Deployment Stack,
specifically to protect managed resources from deletion by any principal in the tenant. A rollback
execution path would first need to reconcile with that same protection it relies on elsewhere.
Building per-stage teardown counterparts is substantial, unscoped work with its own per-stage
verification requirements. No task in the original implementation plan (T073, T085) builds rollback
execution; both stop at "offer" and "gate".

Automatic rollback on failure would also remove resources that a human may want to inspect, retry
from, or forward-fix. Tearing down a customer's partially-built Fabric capacity is irreversible
and financially material.

## Decision

On permanent stage failure, the orchestrator halts and preserves the deployment's current state.
It does not tear anything down automatically.

The recovery endpoint (`POST /deployments/{deploymentId}/recovery`) offers `rollback` as a choice
where the blueprint's own `recovery_path` names it. Every check before execution is real and
enforced: the action must be offered for this stage, a distinct rollback approval must be present,
that approval must be complete, it must be bound to the same plan hash, and it must differ from
the approval that authorised the original execution.

Once all checks pass, the endpoint returns `501`. This is the literal HTTP meaning: the server
does not yet support the functionality this request requires. A caller integrating today gets
accurate, forward-compatible error semantics for the parts that are real, and a clear signal for
the one part that is not yet built.

This is a disclosed scope boundary, not a silent stub. `api/recovery.py`'s module docstring
explains it. Do not work around it by marking a deployment `ROLLED_BACK` when nothing was
actually torn down; that status is defined as "actual teardown happened", not merely "rollback
was requested".

`retry` and `forward_fix` are both real, working requeues. They resume from the last checkpoint
without re-running completed stages, identical to how an automatic transient-failure retry works.

## Consequences

**Positive**

- No resources are destroyed without an explicit human decision and a distinct, complete approval.
- The approval gate is already built and tested. When rollback execution is implemented, it slots
  into an already-correct gate with no behaviour change upstream.
- Halted deployments are recoverable: `engine/halt.py`'s `requeue_after_recovery_choice` is the
  single, correct field-clearing path for both automatic and human-chosen requeues.

**Negative / watch points**

- Partially-built infrastructure stays in the customer's subscription until a human chooses a
  path. The customer must be told about the halt and what their options are.
- The `denyDelete` conflict noted above means rollback execution, when built, cannot simply delete
  the deployment stack. It needs its own design to reconcile with that protection.

**Sources**: `engine/halt.py` module docstring; `api/recovery.py` module docstring and rollback
501 rationale; `stages/infrastructure.py` denyDelete comment
