# ADR-0003: azd-only delivery discipline for the Groundwork platform itself (PD-004)

**Date**: 2026-07-30
**Status**: Accepted (2026-07-30)

## Context

Groundwork runs on AKS. Without a fixed deployment discipline, the cluster's running state can
diverge from the repository, making drift undetectable and rollback unreliable. The product also
creates Azure DevOps pipelines inside customer tenants as a delivered capability (FR-038). Those
two activities must stay completely separate: the same tool cannot be used for both, or the
boundary between Groundwork's own infrastructure and a customer's becomes ambiguous.

A CI/CD pipeline (GitHub Actions, Azure DevOps) would provide automated quality gates, but no
budget or approvals decision exists for one. The chosen substitute is the Azure Developer CLI
(`azd`) with mandatory manual controls that enforce the same discipline a pipeline gate would.

## Decision

`azd` is the only sanctioned mechanism for provisioning and deploying the Groundwork platform.

**What this means in practice:**

- Run `azd provision --preview` before any provision, retain the output, and read it in full. The
  reviewable-preview requirement applies to Groundwork's own infrastructure, not only to customer
  tenants.
- Deploy services with `azd deploy <service> --no-prompt`. Never `kubectl apply` or
  `kubectl set image` directly against the deployed cluster. Direct `kubectl` writes make running
  state diverge from the repository and render drift undetectable on the next deploy.
- The deployed commit must be tagged before deploying. The full test suite must pass and its
  result must be recorded. Deploys must come from a clean working tree. The deployer identity and
  the tag must both be recorded in the audit log.
- The `azd` deploy pipeline never enters a customer tenant. Customer-facing pipelines never enter
  Groundwork's release path. These are separate tracks, and confusing them is a defect.

**Accepted risk (recorded at PD-004):** a local CLI deploy has no automated quality gate. The
manual controls above substitute for absent pipeline gates. They are mandatory, not advisory. Not
running `azd provision --preview` before provisioning is the same class of violation as skipping
a pipeline gate.

## Consequences

**Positive**

- Cluster state is always reproducible from the repository. A failed deploy can be retried from
  the same commit with the same outcome.
- The separation between Groundwork's own delivery and customer-tenant provisioning is structural,
  not conventional.

**Negative / watch points**

- Manual controls depend on discipline. The `preprovision` hook enforces the region check and
  resource-provider registration; it does not enforce the tag or test-suite requirements, which
  remain operator responsibility.
- Rolling updates kill in-flight sequencer runs (observed live 2026-08-25). Deploy when no
  deployments are executing, or expect orphaned `executing` records requiring manual requeue via
  `engine/halt.py`'s `requeue_after_recovery_choice`.

**Sources**: `docs/product-specification.md` PD-004; `docs/release-checklist.md`; the project's internal handoff notes (not included in this release) §6
