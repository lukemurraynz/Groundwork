# ADR-0008: Residency boundary: persisted content AU-only; bounded transient offshore-inference exception with consent (PD-005)

**Date**: 2026-07-30
**Status**: Accepted (2026-07-30)

## Context

Voice Live in `australiaeast` is only available as a Global standard deployment. A Global standard
deployment does not guarantee that inference processing stays within the resource's geography. This
conflicts with FR-053b's original requirement that all conversation content be processed only in
Australian regions.

The product's primary entry point is voice. Removing voice from Release 1 entirely to preserve a
strict residency guarantee would eliminate the signature experience for every customer, including
those whose contracts permit offshore inference processing.

The platform also handles data that falls under Australian data-sovereignty requirements. Any
residency rule must be enforceable in code, because no Azure control enforces where a Global
standard deployment runs its inference.

## Decision

Persisted content stays in Australian Azure regions without exception (FR-053b, narrowed in
wording to "persisted").

Transient in-call audio inference is a strictly bounded exception (FR-053c): it may leave the
geography, but only during a live voice session. It cannot be widened to transcripts, plans,
reports, audit records, or any other stored artefact. An intermediate inference result that
gets persisted anywhere breaches FR-053b regardless of the FR-053c exception.

This exception applies per tenant, not per release (FR-053d). A tenant whose contract forbids
offshore processing of in-call audio can decline consent. That tenant retains full capability
over the chat interface and the REST API (FR-004d). Their calls are never silently routed through
a consented path.

Concretely:

- `contracts/` tenant validators enforce `approved_regions` on every sealed plan. A region
  outside the Australian set is rejected at schema time (confirmed in `test_plan_boundary.py`).
- `api/voice.py`'s `_seal_plan_for_caller` is the single choke point for all three voice flows.
  It enforces `approved_regions` and checks consent state before any voice session proceeds.
- No transcript, plan, or report is written outside Australian storage. Audit records go to
  Australian Cosmos and blob containers.
- Consent is durable and per-tenant, stored in the `consent` immutable blob container with the
  same 12-month retention policy as approvals.
- SC-020 (narrowed) asserts no persisted content outside Australian regions. SC-020a asserts
  consent coverage for the FR-053c exception.

The boundary is enforced by code, not infrastructure. A future change that persists an
intermediate inference artefact would breach FR-053b without tripping any Azure-level control.
SC-020's test must assert on actual stored artefacts and their regions, not on configuration.

## Consequences

**Positive**

- Customers whose contracts require strict data residency keep full functionality by declining
  voice consent. The capability loss is explicit and known at onboarding, not discovered later.
- Every persisted artefact has a deterministic, auditable storage location in an Australian region.

**Negative / watch points**

- The FR-053c exception is enforced only by application code. Any future artefact that captures
  an intermediate inference result (e.g. a streaming transcript buffer that gets flushed to
  storage) requires an explicit residency review before being added.
- Caller-identity verification for the voice channel is a separate, still-open gap (see
  [ADR-0011](0011-voice-alone-authorises-irreversible-actions.md)). Consent given by a verified
  tenant admin may still be activated by an unverified caller. Do not conflate the residency
  decision with the authentication gap.

**Sources**: `docs/product-specification.md` PD-005; `src/groundwork_contracts/tenant.py` validators
(`AUSTRALIAN_REGIONS`); the project's internal research notes (not included in this release),
CL-009 resolution and FR-053c/d; V-003 residency finding
