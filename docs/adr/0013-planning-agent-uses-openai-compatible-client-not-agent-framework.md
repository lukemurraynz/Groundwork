# ADR-0013: The planning agent calls an OpenAI-compatible client, not `agent_framework.Agent`

**Date**: 2026-08-02
**Status**: Superseded by [ADR-0014](0014-native-agent-framework-client-restored.md) (2026-09-06).
Kept as the historical record of this decision; see ADR-0014 for the actual resolution.

## Context

PD-001 named the native Microsoft Agent Framework as the client for the Foundry-hosted planning
agent, instead of a direct OpenAI or Azure OpenAI SDK call. Wiring the real Foundry call surfaced
a problem: `agent_framework_foundry.FoundryChatClient` returned an empty `403 Forbidden` (no
error body) on every call to `/openai/v1/responses`, the route the framework's client uses. This
reproduced consistently across every identity and role combination tested, and did not respond to
RBAC changes at the time, which pointed toward a platform-level limitation on that API surface
rather than a permission gap in this project's own configuration.

## Decision

The planning agent uses a plain OpenAI-compatible async client
(`groundwork_controlplane.agents.providers.foundry_openai.create_foundry_chat_client`) against the
Foundry project's `/openai/v1/chat/completions` route instead, authenticated with a token provider
scoped to `https://ai.azure.com/.default`. `PlanningAgent` is written against that narrow shape (an
object exposing `.chat.completions.create(...)`), not against `agent_framework.Agent`.

Client construction is isolated in its own module
(`groundwork_controlplane/agents/providers/foundry_openai.py`) specifically so restoring the
native client later is a one-file change, not a rewrite of `PlanningAgent` or anything that calls
`build_planning_agent`.

The `agent-framework-core`/`agent-framework-foundry` dependencies stay in `pyproject.toml`:
removing them would be a separate decision (drop native-framework support entirely) that nobody
made. They are unused by the code that runs while this decision stands.

## Consequences

**Positive**

- The gap between stated intent (PD-001, the dependency list) and actual behavior is written down
  in one place, instead of discoverable only by reading `planning.py` line by line.
- The provider seam keeps the eventual fix scoped: one module, one factory function, no change to
  the schema boundary, the retry logic, or anything downstream.

**Negative / watch points**

- This is a workaround, not a resolution.
- Because `PlanningAgent` never sees `agent_framework.AgentResponse`, none of the framework's own
  structured-output parsing (`response.value`) is exercised anywhere in this codebase. That was
  never trusted for the schema boundary anyway (the ADR-0001 boundary requires
  `boundary.validate_model_output` to be the one gate), so this loses nothing safety-relevant.
- `agent-framework-core`/`agent-framework-foundry` sit in the dependency tree unused while this
  decision stands.

**Sources**: `src/groundwork_controlplane/agents/providers/foundry_openai.py`;
`docs/product-specification.md` PD-001
