# Architecture Decision Records: Groundwork Platform

This index covers every ADR for Release 1. Each record is self-contained: a new engineer can
apply the decision without reading the archive files it was distilled from.

Decisions that supersede an earlier one say so explicitly in their Status line and Context section.

| ADR | Title | Date | Status |
| --- | ----- | ---- | ------ |
| [0001](0001-ai-plans-deterministic-code-executes.md) | AI plans, deterministic code executes | 2026-07-30 | Accepted |
| [0002](0002-lighthouse-bootstrap-only-ado-pipeline-execution.md) | Lighthouse bootstrap-only; ongoing provisioning via the customer's own ADO pipeline | 2026-08-24 | Accepted |
| [0003](0003-azd-only-delivery-discipline.md) | azd-only delivery discipline for the Groundwork platform itself (PD-004) | 2026-07-30 | Accepted |
| [0004](0004-conversational-surfaces-web-frontend-voice-live.md) | Conversational surfaces: web frontend and Voice Live now; ACS/PSTN deferred; Teams removed from R1 | 2026-08-26 | Accepted |
| [0005](0005-halt-and-preserve-rollback-501.md) | Halt-and-preserve over auto-rollback; rollback gated but not executed (501) | 2026-08-01 | Accepted |
| [0006](0006-fr040-diagnostics-fabric-capacity-impossible.md) | FR-040 diagnostic settings on Fabric capacity accepted as impossible | 2026-08-26 | Accepted |
| [0007](0007-secretless-identity-ado-wif-mirroring.md) | Secretless identity; ADO WIF issuer/subject mirroring after sprint-253 change | 2026-07-30 | Accepted |
| [0008](0008-residency-boundary-persisted-au-only-transient-exception.md) | Residency boundary: persisted content AU-only; bounded transient offshore-inference exception with consent (PD-005) | 2026-07-30 | Accepted |
| [0009](0009-proposal-rollback-via-customer-pipeline.md) | Rollback executed via the customer's own pipeline (redeploys last-known-good; never deletes) | 2026-08-26 | Accepted |
| [0010](0010-proposal-multi-blueprint-catalogue.md) | Multi-blueprint catalogue (supersedes FR-013a post-R1) | 2026-08-26 | Accepted |
| [0011](0011-voice-alone-authorises-irreversible-actions.md) | Voice alone may authorise an irreversible action; step-up-auth compensating control now on by default (corrected 2026-09-06, see ADR body) | 2026-08-02 | Accepted |
| [0012](0012-mcp-tool-authority-tiers.md) | MCP (Model Context Protocol) tool catalogue split into authority tiers; agents only ever wired to read-only ones | 2026-07-30 | Accepted (design contract) |
| [0013](0013-planning-agent-uses-openai-compatible-client-not-agent-framework.md) | Planning agent calls an OpenAI-compatible client, not `agent_framework.Agent`, pending two upstream fixes | 2026-08-02 | **Superseded by 0014** |
| [0014](0014-native-agent-framework-client-restored.md) | Native Agent Framework client restored; ADR-0013's root cause was wrong (an IaC role-scope gap, not a platform limitation) | 2026-09-06 | Accepted |

> *Note*: ADRs 0001–0008 were recorded retrospectively on 2026-08-26 from the planning archive;
> their dates reflect when each decision was originally made, not when this register was created.
> 0011, 0012, and 0013 were backfilled the same way: 0011 and 0012 when the feature spec they
> were originally recorded against was removed from this public release, 0013 from a live
> deployment check that found the code and its own module docstring disagreed.

## Supersession chain

- **0002** supersedes the multi-tenant Entra app + admin-consent bootstrap model (withdrawn
  2026-08-24, never recorded as a standalone ADR).
- **0004** supersedes the Teams-via-M365-Agents-SDK channel decision, originally recorded in this
  project's internal research notes (2026-07-30), not included in this release.
- **0011** supersedes the prior rule requiring a durable-artefact channel for every irreversible
  action's confirmation; voice is exempted from that requirement specifically.
- **0013** documented a deviation from PD-001's native-Agent-Framework intent, attributed to two
  suspected upstream/platform blockers.
- **0014** supersedes 0013: the real root cause was an IaC role-assignment scope gap in this
  codebase's own `infra/modules/foundry.bicep`, not a platform limitation. The native client is
  restored.

## How to read these records

Each ADR follows MADR-lite format: Title / Date / Status / Context / Decision / Consequences.
The Decision section states the rule plainly enough to apply without reading the archive. The
Consequences section records both what the decision enables and the specific watch points a
future engineer needs to know before changing anything the decision touches.

Source files cited in each record's closing line are the canonical evidence. Do not infer
decisions from narrative history files that may be stale: these records are the live reference.
