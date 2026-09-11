"""Per-tenant offshore-inference consent capture (T099; FR-053d).

Writes a consent artefact to the immutable ``consent`` blob container — same Protocol/split pattern
as ``approval/artefacts.py``, so ``OffshoreInferenceConsentStore.record`` is unit-testable with no
live storage account behind it. FR-053d requires a durable, immutable artefact recording exactly
who consented, when, and against which disclosure version — this module is the write path into
that container.

The ``consent`` container is deliberately separate from ``approvals``: consent and approval are
different concepts, recorded at different times, for different purposes.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Final, Protocol

from groundwork_contracts.tenant import OffshoreInferenceConsent

CURRENT_DISCLOSURE_VERSION: Final = "1.0.0"

# The one canonical disclosure text (FR-053d) — shown verbatim on every surface that records
# consent (voice narration, chat, and the REST disclosure endpoint), matching the pattern
# ``groundwork_controlplane/costing/licensing.py`` uses for the Power BI viewer-licensing
# disclosure. Consent is only meaningful against the text the customer was actually shown
# (``groundwork_contracts/tenant.py``'s own warning), so the text and the version it describes
# must stay in the same module and change together.
OFFSHORE_INFERENCE_DISCLOSURE: Final = (
    "Groundwork's voice channel uses Azure AI Voice Live for real-time speech. "
    "To transcribe and understand what is said, a brief, transient portion of the call "
    "is processed at an inference endpoint that may be located outside your configured "
    "data-residency region (australia). "
    "Raw audio is never stored or retained by Groundwork. "
    "Recorded consent means you accept this transient offshore processing for the purpose "
    "of the conversation. "
    "Revoking this consent disables the voice channel but does not affect running deployments."
)


class BlobClientLike(Protocol):
    # Matches the real azure.storage.blob.aio return shape (a plain dict of blob properties, not
    # an object with .url) — see engine/preview.py's own WhatIfPreviewStore.store() for the real
    # bug this codebase hit when a sibling module's Protocol claimed otherwise. This module's own
    # call site already never reads .url off the upload result, so this was a type-annotation
    # inaccuracy, not a runtime bug — fixed for consistency.
    async def upload_blob(self, data: bytes, **kwargs: Any) -> dict[str, Any]: ...

    @property
    def url(self) -> str: ...


class ContainerClientLike(Protocol):
    def get_blob_client(
        self,
        blob: str,
        snapshot: str | None = None,
        *,
        version_id: str | None = None,
    ) -> BlobClientLike: ...


class _AzureBlobClientAdapter:
    def __init__(self, blob_client: Any) -> None:
        self._blob_client = blob_client

    async def upload_blob(self, data: bytes, **kwargs: Any) -> dict[str, Any]:
        return await self._blob_client.upload_blob(data, **kwargs)

    @property
    def url(self) -> str:
        return self._blob_client.url


class _AzureContainerClientAdapter:
    def __init__(self, container_client: Any) -> None:
        self._container_client = container_client

    def get_blob_client(
        self,
        blob: str,
        snapshot: str | None = None,
        *,
        version_id: str | None = None,
    ) -> BlobClientLike:
        return _AzureBlobClientAdapter(
            self._container_client.get_blob_client(blob, snapshot=snapshot, version_id=version_id)
        )


class OffshoreInferenceConsentStore:
    """Writes one JSON blob per consent into the ``consent`` container, overwrite=False — once
    recorded, consent cannot be replaced (the container's own time-based immutability policy
    enforces this, matching ``approvals`` and ``reports``)."""

    def __init__(self, container: ContainerClientLike) -> None:
        self._container = container

    def blob_url_for(self, consent_id: str) -> str:
        """Deterministic URL before write — the same pattern ``ApprovalArtefactStore.blob_url_for``
        and ``ReportArchiveStore.blob_url_for`` already use, so a caller can build a validated
        ``OffshoreInferenceConsent`` with its real ``artefact_uri`` set before the upload runs."""
        return self._container.get_blob_client(f"{consent_id}.json").url

    async def record(
        self,
        *,
        consent_id: str,
        consenting_identity_object_id: str,
        consenting_identity_display_name: str,
        disclosure_version: str,
        now: datetime,
    ) -> OffshoreInferenceConsent:
        artefact_uri = self.blob_url_for(consent_id)
        consent = OffshoreInferenceConsent(
            consenting_identity_object_id=consenting_identity_object_id,
            consenting_identity_display_name=consenting_identity_display_name,
            consented_at=now,
            artefact_uri=artefact_uri,
            disclosure_version=disclosure_version,
        )

        blob_client = self._container.get_blob_client(f"{consent_id}.json")
        body = json.dumps(consent.model_dump(mode="json"), sort_keys=True, indent=2).encode("utf-8")
        await blob_client.upload_blob(body, overwrite=False, content_type="application/json")
        return consent


def build_consent_store(
    *, storage_account_url: str, credential: Any
) -> OffshoreInferenceConsentStore:
    """Construct a real :class:`OffshoreInferenceConsentStore` against the ``consent`` container.

    The only place ``BlobServiceClient`` is constructed — everything else in this package works
    against the protocols above, so nothing downstream needs to know blob storage is involved.
    """
    from azure.storage.blob.aio import BlobServiceClient

    service_client = BlobServiceClient(account_url=storage_account_url, credential=credential)
    container_client = service_client.get_container_client("consent")
    return OffshoreInferenceConsentStore(_AzureContainerClientAdapter(container_client))
