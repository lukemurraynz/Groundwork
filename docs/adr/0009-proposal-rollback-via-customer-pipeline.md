# ADR-0009 (Proposal): Rollback executed via the customer's own pipeline

**Date**: 2026-08-26
**Status**: Accepted (2026-08-26), implemented same day; recovery endpoint triggers customer-side rollback pipeline

## Context

ADR-0005 records halt-and-preserve as the failure posture, with rollback *gated* but not
*executed* (a fully valid rollback request answers `501`). Two forces created that boundary:

1. No stage has a teardown counterpart, and `stages/infrastructure.py` deliberately sets
   `denySettings.mode: denyDelete` so no principal in the tenant can delete managed resources.
   Any teardown design must first reconcile with that protection.
2. FR-006a restricts Groundwork's own credential to a single bootstrap write: direct ARM
   teardown from Groundwork would violate the same constraint the Lighthouse pivot introduced.

## Proposed decision

Rollback becomes **trigger-and-poll of a `rollback.yml` pipeline inside the customer's own Azure
DevOps project**, the identical execution model as every other stage:

- `devops_project` pushes `rollback.yml` alongside `azure-pipelines.yml` at project creation.
- The pipeline re-applies the blueprint template with `applyChanges: false` and the
  last-known-good parameter set captured from the succeeded `infrastructure` run (stack outputs
  are already persisted on the deployment record).
- Because it *redeploys prior state* rather than deleting, it never fights `denyDelete`
  (`denyDelete` blocks deletes, not redeployments of existing resources).
- Groundwork's credential never touches the subscription; the customer's bootstrap identity runs
  the pipeline, exactly as `infrastructure` does today.
- The recovery endpoint keeps its existing approval gate unchanged: an approved, distinct,
  plan-hash-bound rollback approval switches from answering `501` to queueing this pipeline.

## Consequences

- The `501` disclosure in `api/recovery.py` is replaced by real execution; ADR-0005's gate
  guarantees are untouched.
- Rollback granularity equals blueprint granularity (whole stack to last-known-good), not
  per-resource surgery, acceptable for R1-era blueprints, revisited if finer control is needed.
- A customer could hand-edit their pipeline to no-op the rollback; mitigation is the same trust
  model as every other pipeline-side stage (their subscription, their pipeline, our audit trail).

## Supersedes / relates

Refines ADR-0005 (does not supersede its gating rules). Depends on ADR-0002's
pipeline-as-execution-path architecture.
