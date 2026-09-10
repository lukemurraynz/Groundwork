"""What-if capture (T074; FR-024): a reviewable preview of resources to be created, changed, and
deleted, retained as a durable deployment artefact — captured once, before a deployment's first
real stage ever attempts a write (wired into ``Sequencer.run`` as a synthetic pseudo-stage; see
that module's own ``_capture_what_if``).

**Deployment Stacks have no native what-if operation** — live-verified 2026-08-02 against
Microsoft Learn's Deployment Stacks REST reference (the full operation list: Create Or Update,
Delete, Export Template, Get, List at each of management-group/resource-group/subscription scope;
no what-if operation anywhere in it). This module instead uses the standard subscription-scope ARM
``Deployments`` what-if operation
(``POST .../subscriptions/{id}/providers/Microsoft.Resources/deployments/{name}/whatIf
?api-version=2025-04-01``, live-verified against the same source) against the *exact* template and
parameters ``stages/infrastructure.py``'s own stack apply uses
(:func:`~groundwork_orchestrator.stages.infrastructure.template_and_parameters`, the one place both
read from) — the underlying ARM deployment engine is the same one a stack's apply runs through,
this just never commits it.

``mode: "Incremental"`` matches what the stack's own ``actionOnUnmanage: detach`` means in
practice: nothing outside the template is ever deleted by this platform, so predicting deletions
under ``"Complete"`` mode would overstate what the real apply will do.

**Coverage, disclosed rather than silently overclaimed** (the same discipline
``validation/checks/policy.py`` already uses for its own scope boundary): this preview covers only
what ``main.bicep``'s single deployment stack creates. It does not cover Fabric capacity, workspace,
or managed private endpoints (``stages/fabric.py`` — no ARM template, Fabric's own REST API), the
Azure DevOps project/pipelines/service connection, or the federated identity credential
(``stages/identity.py`` — a separate ARM ``PUT`` outside this template). FR-024's "reviewable
preview of resources to be created, changed, and deleted" is satisfied for the one stage that
performs bulk, template-driven resource creation; every other stage is an individually small, named,
single-resource write whose own idempotence contract already describes its effect.

**The terminal poll response shape is inferred, not from a worked example.** Microsoft Learn's own
documentation for this operation shows the ``202 Accepted`` response's headers (``Location``,
``Retry-After``) and the eventual ``200 OK`` *result* shape, but no worked example of what a
follow-up ``GET`` on the ``Location`` URL returns while still running versus once complete. This
module follows the same convention every other ARM long-running operation in this codebase relies
on (poll the operation URL; ``202`` means still running, ``200`` means done, the terminal body is
the operation result) — consistent with, though not independently worked-example-verified against,
the async pattern documented at
<https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/async-operations>.
Flagged as a `[VERIFY]` item for the first real run — **resolved live 2026-08-25**: this exact path
executed successfully as deployment ``67f6e287``'s opening pseudo-stage against real ARM (stage
record ``what_if_preview | succeeded``, Cosmos-verified 2026-08-26).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol

import httpx
from azure.core.credentials_async import AsyncTokenCredential

from groundwork_contracts.deployment import Deployment
from groundwork_contracts.plan import DeploymentPlan
from groundwork_orchestrator.stages.infrastructure import template_and_parameters

ARM_SCOPE = "https://management.azure.com/.default"
ARM_ENDPOINT = "https://management.azure.com"
WHAT_IF_API_VERSION = "2025-04-01"


class WhatIfCaptureError(Exception):
    """The what-if operation itself could not be completed — distinct from a real stage failure:
    this happens before any stage of the deployment has run."""


class BlobClientLike(Protocol):
    async def upload_blob(self, data: bytes, **kwargs: Any) -> dict[str, Any]: ...

    async def exists(self, **kwargs: Any) -> bool: ...

    @property
    def url(self) -> str: ...


class ContainerClientLike(Protocol):
    def get_blob_client(self, blob: str) -> BlobClientLike: ...


class WhatIfPreviewStore:
    """Writes one JSON blob per deployment into the ``previews`` immutable container
    (``infra/modules/storage.bicep``, provisioned alongside ``approvals``/``reports`` with the
    same FR-052a-matching retention policy). Same shape as
    ``groundwork_controlplane.approval.artefacts.ApprovalArtefactStore`` — a different container,
    the identical write discipline.
    """

    def __init__(self, container: ContainerClientLike) -> None:
        self._container = container

    async def store(self, *, deployment_id: str, payload: dict[str, Any]) -> str:
        # Same bug, same fix as approval/artefacts.py's own store() (found live 2026-08-24 there
        # first): azure.storage.blob.aio's real upload_blob() returns a plain dict of blob
        # properties, never an object with a .url attribute. The blob's URL is deterministic from
        # the client itself — use that, not the upload result. This was the first what-if capture
        # ever attempted against a real Storage account, which is why nothing caught it sooner.
        #
        # Check-before-write, found live 2026-08-24 immediately after the fix above: the
        # ``previews`` container's time-based immutability policy (same as ``approvals``/
        # ``reports``) means ``overwrite=True`` cannot actually replace a blob that already
        # exists — the fixed code above genuinely wrote the blob on its first (crashing) attempt,
        # and every retry after that failed with ``BlobImmutableDueToPolicy`` trying to write it
        # again. A retry of this pseudo-stage is legitimate (Sequencer's own retry budget covers
        # it) and must be a real no-op against an already-written artefact, the same
        # get-before-put idempotence discipline every other stage in this codebase already uses —
        # not a second write attempt against a container that structurally cannot accept one.
        blob_client = self._container.get_blob_client(f"{deployment_id}.json")
        if await blob_client.exists():
            return blob_client.url
        body = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")
        await blob_client.upload_blob(body, overwrite=True, content_type="application/json")
        return blob_client.url


def build_what_if_preview_store(
    *, storage_account_url: str, credential: AsyncTokenCredential
) -> WhatIfPreviewStore:
    from azure.storage.blob.aio import BlobServiceClient

    service_client = BlobServiceClient(account_url=storage_account_url, credential=credential)
    container_client = service_client.get_container_client("previews")
    return WhatIfPreviewStore(container_client)  # type: ignore[arg-type]


class WhatIfPreviewStoreLike(Protocol):
    async def store(self, *, deployment_id: str, payload: dict[str, Any]) -> str: ...


class WhatIfCapture:
    """Runs the ARM what-if operation for one deployment's infrastructure template and persists
    the result. The one thing ``Sequencer._capture_what_if`` depends on — injectable, the same
    seam every other stage's real-Azure-calling implementation uses."""

    def __init__(
        self,
        *,
        store: WhatIfPreviewStoreLike,
        http_client: httpx.AsyncClient | None = None,
        poll_interval_seconds: float = 5.0,
        max_poll_attempts: int = 60,
    ) -> None:
        self._store = store
        self._client = http_client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = http_client is None
        self._poll_interval_seconds = poll_interval_seconds
        self._max_poll_attempts = max_poll_attempts

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def capture(
        self, *, deployment: Deployment, plan: DeploymentPlan, credential: AsyncTokenCredential
    ) -> str:
        template, parameters, location, _tags = template_and_parameters(plan)
        deployment_name = f"whatif-groundwork-{plan.subscription_id[:8]}"
        headers = await self._headers(credential)
        url = (
            f"{ARM_ENDPOINT}/subscriptions/{plan.subscription_id}/providers/"
            f"Microsoft.Resources/deployments/{deployment_name}/whatIf"
            f"?api-version={WHAT_IF_API_VERSION}"
        )
        body = {
            "location": location,
            "properties": {"mode": "Incremental", "template": template, "parameters": parameters},
        }

        response = await self._client.post(url, headers=headers, json=body)
        response.raise_for_status()
        result = await self._resolve_result(response, headers)

        payload = {
            "deploymentId": deployment.deployment_id,
            "subscriptionId": plan.subscription_id,
            "blueprintId": plan.blueprint_id,
            "capturedResult": result,
        }
        return await self._store.store(deployment_id=deployment.deployment_id, payload=payload)

    async def _resolve_result(
        self, response: httpx.Response, headers: dict[str, str]
    ) -> dict[str, Any]:
        if response.status_code == 200:
            result: dict[str, Any] = response.json()
            return result

        location_url = response.headers.get("Location")
        if not location_url:
            raise WhatIfCaptureError(
                f"what-if operation returned {response.status_code} with no Location header to poll"
            )
        for _ in range(self._max_poll_attempts):
            await asyncio.sleep(self._poll_interval_seconds)
            poll_response = await self._client.get(location_url, headers=headers)
            if poll_response.status_code == 200:
                polled: dict[str, Any] = poll_response.json()
                return polled
            if poll_response.status_code != 202:
                poll_response.raise_for_status()
        raise WhatIfCaptureError(
            f"what-if operation did not complete within the poll budget "
            f"({self._max_poll_attempts} attempts at {self._poll_interval_seconds}s)"
        )

    async def _headers(self, credential: AsyncTokenCredential) -> dict[str, str]:
        token = await credential.get_token(ARM_SCOPE)
        return {"Authorization": f"Bearer {token.token}", "Content-Type": "application/json"}


class WhatIfCaptureLike(Protocol):
    async def capture(
        self, *, deployment: Deployment, plan: DeploymentPlan, credential: AsyncTokenCredential
    ) -> str: ...
