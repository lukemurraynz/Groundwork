# ADR-0010 (Proposal): Multi-blueprint catalogue activation path

**Date**: 2026-08-26
**Status**: Accepted (2026-08-26), implemented; supersedes FR-013a for post-R1 releases

## Context

FR-013a scoped Release 1 to exactly one approved blueprint, and `api/main.py` enforces that
structurally: startup raises `BlueprintLoadError` if the catalogue contains more than one entry.
The loader itself (`groundwork_shared.config.blueprints.load_catalogue`) already reads every
directory under the blueprints root, so the constraint lives in one assertion, but activating a
second blueprint touches more than that assertion:

- `app.state.blueprint` (singular) is threaded into planning, validation, sequencer recovery
  options, and the status monitor.
- `CreatePlanRequest` already carries `blueprintId`/`blueprintVersion` and validates against the
  catalogue, so the request half of selection exists.
- A drafted second blueprint exists at `docs/blueprint-dev-sandbox-draft/` (deliberately NOT
  wired into `infra/blueprints/`) to prove the declarative-extension shape FR-013b promises.

## Proposed decision (post-R1)

1. Replace `app.state.blueprint` with per-id resolution from `app.state.blueprints`; keep the
   startup assertion as "at least one, each valid" instead of "exactly one".
2. Thread `blueprint_id` through readiness evaluation, sequencer construction, and report
   building wherever the singular assumption currently reaches.
3. Activate `dev-sandbox` as the first second entry: smaller SKU (F8), reduced resource set
   (no Key Vault private endpoint requirement), non-production environment tag.
4. Keep FR-013a satisfied historically: this ADR supersedes it for post-R1 releases only, with
   product-owner sign-off recorded here at activation time.

## Consequences

- Approval thresholds may want per-blueprint defaults (F8 vs F2 economics differ); fold into the
  threshold-policy settings rather than hardcoding.
- The traceability matrix in the archived task board maps several FRs to the single-blueprint
  simplification; revisit T049/T050/T073 citations during implementation.
