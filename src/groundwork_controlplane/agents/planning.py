"""The Foundry-hosted planning agent (T045).

PD-001/PD-002 named Microsoft Foundry, not Copilot Studio, as the host, and named the native
Microsoft Agent Framework as the client — that's what runs, via
``agent_framework_foundry.FoundryChatClient``.
``groundwork_controlplane.agents.providers.foundry_openai`` is the one module that knows how the
client is built (see ADR-0014), so a future provider swap touches that module and the
message-construction helpers here, not the rest of this file.

**Cost is never trusted from the model.** ``DeploymentPlan.cost_estimate`` is a required field so
the schema is self-contained, but the authoritative figure is
``groundwork_shared.costing.estimator``'s live Retail-Prices-API computation, not whatever the
model proposed. After schema validation, this module replaces the model's cost estimate with the
real one before returning the plan — the same reasoning as ``tenant_id`` never coming from the
model: a governance-critical number is not something conversation gets to assert.

Split into :class:`PlanningAgent` (takes an already-built client) and :func:`build_planning_agent`
(asks the provider module for the real one) for the same reason
``groundwork_shared.validation.checks.tenant`` splits Azure-calling glue from a decision function:
the real Foundry call is Microsoft's SDK to get right, not ours to re-test with a fake standing in
for the whole service; what belongs to this module and is worth testing directly is what happens
to the response once it arrives.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from agent_framework import Message
from azure.core.credentials_async import AsyncTokenCredential

from groundwork_contracts.blueprint import FabricCapacitySku, PlatformBlueprint
from groundwork_contracts.plan import CostEstimateRef, DeploymentPlan
from groundwork_controlplane.agents.boundary import validate_model_output
from groundwork_controlplane.agents.hardening import PROMPT_HARDENING_PREAMBLE
from groundwork_controlplane.agents.providers.foundry_openai import create_foundry_chat_client
from groundwork_shared.costing.estimator import compose_estimate, fabric_capacity_line
from groundwork_shared.costing.retail_prices import RetailPricesClient

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_PLANNING_INSTRUCTIONS = (_PROMPTS_DIR / "planning_instructions.md").read_text(encoding="utf-8")

# Template text lives in prompts/planning_instructions.md, not here — see that file for the
# actual wording. This module only composes it with the shared hardening preamble and renders
# the per-blueprint placeholders (see build_instructions below).
INSTRUCTIONS_TEMPLATE = PROMPT_HARDENING_PREAMBLE + _PLANNING_INSTRUCTIONS


class PlanGenerationError(Exception):
    """The agent could not produce a plan for reasons outside schema validation.

    Distinct from :class:`~groundwork_contracts.errors.PlanValidationError` — that one means the
    agent responded with something that failed the schema boundary; this one means the agent call
    itself did not complete (empty response, or a non-JSON response).
    """


def _iac_summary(blueprint: PlatformBlueprint) -> str:
    return "\n".join(
        f"- {artefact.module} ({artefact.source}, pinned {artefact.version})"
        for artefact in blueprint.iac_artefacts
    )


def build_instructions(blueprint: PlatformBlueprint) -> str:
    return INSTRUCTIONS_TEMPLATE.format(
        blueprint_id=blueprint.blueprint_id,
        blueprint_version=blueprint.version,
        iac_summary=_iac_summary(blueprint),
        sku_choices=", ".join(sku.value for sku in FabricCapacitySku),
    )


def _extract_text(text: str) -> str:
    if not text or not text.strip():
        raise PlanGenerationError("planning agent returned an empty response")
    return text


def _is_retryable_model_call_error(exc: Exception) -> bool:
    # agent_framework wraps every service-call failure in ChatClientException (`raise ... from
    # ex`), so the transport error that actually matters is one level down, on __cause__ — not on
    # the exception the caller sees directly.
    candidates = (exc, exc.__cause__)
    return any(
        isinstance(candidate, httpx.TransportError)
        or candidate.__class__.__name__ in {"APIConnectionError", "APITimeoutError"}
        for candidate in candidates
        if candidate is not None
    )


class PlanningAgent:
    """Wraps a Foundry-hosted model, constrained to emit only a validated ``DeploymentPlan``.

    Calls the native Agent Framework client's ``get_response(...)`` — see ADR-0014.
    """

    def __init__(
        self,
        *,
        client: Any,  # agent_framework.ChatClientProtocol — avoid import for testability
        model_deployment: str,
        instructions: str,
        retail_prices_client: RetailPricesClient,
        max_attempts: int = 2,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._model = model_deployment
        self._instructions = instructions
        self._retail_prices_client = retail_prices_client
        self._max_attempts = max_attempts
        self._now = now_fn or (lambda: datetime.now(UTC))

    async def generate_plan(self, conversation_summary: str) -> DeploymentPlan:
        """Produce a schema-valid, real-cost-priced plan from ``conversation_summary``."""
        raw_text = await self._request_model_text(conversation_summary)

        if not raw_text.strip():
            raise PlanGenerationError("planning agent returned an empty response")

        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise PlanGenerationError(f"model response was not valid JSON: {exc}") from exc

        plan = validate_model_output(raw)
        return await self._with_real_cost(plan)

    async def _request_model_text(self, conversation_summary: str) -> str:
        """Call the model with a bounded retry budget for transient transport failures only."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.get_response(
                    [
                        Message(role="system", contents=[self._instructions]),
                        Message(role="user", contents=[conversation_summary]),
                    ],
                    options={
                        "model": self._model,
                        "response_format": {"type": "json_object"},
                        "temperature": 0.1,
                        "max_tokens": 2000,
                    },
                )
                return _extract_text(response.text or "")
            except Exception as exc:
                if isinstance(exc, PlanGenerationError):
                    raise
                if not _is_retryable_model_call_error(exc):
                    raise PlanGenerationError(f"model call failed: {exc}") from exc
                if attempt >= self._max_attempts:
                    timestamp = self._now().isoformat()
                    raise PlanGenerationError(
                        "model call failed after "
                        f"{self._max_attempts} attempts at {timestamp}: {exc}"
                    ) from exc
        raise AssertionError("unreachable")

    async def _with_real_cost(self, plan: DeploymentPlan) -> DeploymentPlan:
        """Replace the model's proposed cost with the live-priced figure — see module docstring."""
        fabric_line = await fabric_capacity_line(
            self._retail_prices_client, plan.fabric_capacity_sku, plan.region.value
        )
        real_estimate = compose_estimate(fabric_line, now=plan.cost_estimate.computed_at)
        cost_ref = CostEstimateRef(
            monthly_total=real_estimate.monthly_total,
            uncertainty_lower_pct=real_estimate.uncertainty_lower_pct,
            uncertainty_upper_pct=real_estimate.uncertainty_upper_pct,
            basis=real_estimate.basis,
            computed_at=real_estimate.computed_at,
        )
        return plan.model_copy(update={"cost_estimate": cost_ref})


def build_planning_agent(
    *,
    project_endpoint: str,
    model_deployment: str,
    credential: AsyncTokenCredential,
    blueprint: PlatformBlueprint,
    retail_prices_client: RetailPricesClient,
) -> PlanningAgent:
    """Construct a :class:`PlanningAgent` against a real Foundry project.

    Client construction itself lives in
    :func:`groundwork_controlplane.agents.providers.foundry_openai.create_foundry_chat_client` —
    that is the only place that knows which provider/client library is in use today, and the only
    module that needs to change to switch providers.
    """
    client = create_foundry_chat_client(
        project_endpoint=project_endpoint, credential=credential, model=model_deployment
    )
    return PlanningAgent(
        client=client,
        model_deployment=model_deployment,
        instructions=build_instructions(blueprint),
        retail_prices_client=retail_prices_client,
    )
