# ADR-0014: Native Agent Framework client restored

**Date**: 2026-09-06
**Status**: Accepted

## Context

ADR-0013 adopted a plain OpenAI-compatible client against `/openai/v1/chat/completions` as a
workaround, after `agent_framework_foundry.FoundryChatClient` returned an empty `403 Forbidden`
against Groundwork's Foundry project on the `/openai/v1/responses` route the native client uses.

Further investigation traced the `403` to the Foundry account's RBAC configuration rather than a
client or platform defect: `infra/modules/foundry.bicep` granted the agent-interaction role
(`Foundry Agent Consumer`) at Foundry **project** scope only. Project scope is what Microsoft's
own documentation uses as its least-privilege example for this role, but on this tenant it does
not grant `/responses` access; the same role also granted at **account** scope is what clears it.
Once corrected, the native client works, and testing confirmed the project-scope grant was
redundant once account scope was in place, so it was removed rather than kept alongside it.

## Decision

`infra/modules/foundry.bicep` grants `Foundry Agent Consumer` and `Cognitive Services OpenAI User`
at account scope, to both the control-plane and operator identities.
`groundwork_controlplane.agents.providers.foundry_openai.create_foundry_chat_client` builds a real
`agent_framework_foundry.FoundryChatClient`, and `PlanningAgent` calls its native
`.get_response(...)` method (an `agent_framework.Message` sequence in, a `ChatResponse` with
`.text` out) in place of the OpenAI `.chat.completions.create(...)` shape ADR-0013 introduced.

`PlanGenerationError`'s JSON parsing and `groundwork_controlplane.agents.boundary.validate_model_output`'s
schema check are unchanged: the ADR-0001 "one gate" requirement never depended on
which client produced the raw text. The framework's own structured-output parsing
(`ChatResponse.value`) remains unused by design, for the same reason.

## Consequences

**Positive**

- PD-001/PD-002's native-Agent-Framework intent is now what runs, not aspirational.
- `agent-framework-core`/`agent-framework-foundry` are genuinely used again;
  `tests/architecture/test_declared_dependencies_are_used.py` (added under ADR-0013 specifically
  to catch this class of drift) passes with an empty `KNOWN_UNUSED_DEPENDENCIES`.

**Negative / watch points**

- Microsoft's own `rbac-foundry` documentation assigns `Foundry Agent Consumer` at project scope
  as its documented least-privilege pattern. This tenant needed account scope as well. Treat that
  as tenant-specific unless re-confirmed elsewhere: don't assume every Foundry project needs the
  same grant.
- `infra/main.bicep`'s `foundryAccountTokenOverride` parameter exists because recreating a Foundry
  account under its original deterministic name can collide with that account's own linked Azure
  ML workspace soft-delete state. It defaults to the standard `resourceToken` and only needs
  setting if that happens.

**Sources**: `infra/modules/foundry.bicep`;
`src/groundwork_controlplane/agents/providers/foundry_openai.py`;
`src/groundwork_controlplane/agents/planning.py`;
[Microsoft Foundry RBAC documentation](https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry);
ADR-0013 (superseded)
