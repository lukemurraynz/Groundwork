"""Native Agent Framework client construction for a Foundry project endpoint.

This is the provider seam for :mod:`groundwork_controlplane.agents.planning`: the one place that
knows how to build an authenticated client against Foundry. Swapping providers later means
changing this module, not ``planning.py``.

The RBAC grants this client depends on live at account scope, not project scope — see
``infra/modules/foundry.bicep`` and ADR-0014 for why. ADR-0013 records an earlier, superseded
decision to use a plain OpenAI-compatible client instead of this one.

``FoundryChatClient.get_response`` auto-executes any Python-callable tools passed in
``options["tools"]`` by default. A caller that needs to inspect a pending tool call and dispatch
it to real business-logic functions by name — the voice channel's ``/chat`` multi-tool dispatch in
``api/voice.py`` is the one caller today — builds its own client via this same factory and sets
``client.function_invocation_configuration["enabled"] = False``, which makes ``get_response``
return the model's requested call (name, arguments, call ID) instead of executing it.
"""

from __future__ import annotations

from agent_framework_foundry import FoundryChatClient
from azure.core.credentials_async import AsyncTokenCredential


def create_foundry_chat_client(
    *, project_endpoint: str, credential: AsyncTokenCredential, model: str
) -> FoundryChatClient:
    """Build the native Agent Framework chat client for a Foundry project.

    ``model`` is required here, not just per-call via ``ChatOptions`` — ``FoundryChatClient``
    validates it eagerly at construction and raises if it's missing from both the constructor and
    the ``FOUNDRY_MODEL`` environment variable.

    The returned client's ``.get_response(...)`` is the only method
    :class:`~groundwork_controlplane.agents.planning.PlanningAgent` calls on it.
    """
    return FoundryChatClient(project_endpoint=project_endpoint, credential=credential, model=model)
