"""Approval evidence, written to immutable blob storage (T067).

``infra/modules/storage.bicep`` provisions the ``approvals`` container with a time-based
immutability policy — FR-052's "an approval, once recorded, cannot be altered" is an Azure
guarantee here, not an application-level promise that a bug could quietly break. This module is
just the write path into that container: real ``azure.storage.blob.aio`` calls, split into a thin
protocol and a real client the same way every other Azure integration this session touches is
split, so the write path is unit-testable without a live storage account.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from azure.core.credentials_async import AsyncTokenCredential


class BlobClientLike(Protocol):
    async def upload_blob(self, data: bytes, **kwargs: Any) -> dict[str, Any]: ...

    @property
    def url(self) -> str: ...


class ContainerClientLike(Protocol):
    def get_blob_client(self, blob: str) -> BlobClientLike: ...


class ApprovalArtefactStore:
    """Writes one JSON blob per approval into the ``approvals`` container.

    One blob per approval id, not per plan — a plan that needs a second approval gets its evidence
    updated in place (the same document, re-uploaded) rather than a second, orphaned blob, since
    :class:`~groundwork_contracts.approval.Approval` is itself one record whose ``second_approval``
    field is filled in later, not two records.
    """

    def __init__(self, container: ContainerClientLike) -> None:
        self._container = container

    def blob_url_for(self, approval_id: str) -> str:
        """The URL ``store`` will upload to, without performing any I/O.

        Constructing a blob client and reading its ``url`` is a pure client-side computation (the
        URL is deterministic from account + container + blob name) — no network call happens
        here. Lets a caller build a schema-validated record with its real ``artefact_uri`` set
        *before* the upload runs, so a record that fails its own validation never triggers a
        wasted Azure write.
        """
        return self._container.get_blob_client(f"{approval_id}.json").url

    async def store(self, *, approval_id: str, payload: dict[str, Any]) -> str:
        """Upload ``payload`` as ``{approval_id}.json`` and return its blob URL.

        ``overwrite=True`` is what lets a second-approver update replace the same blob — the
        immutability *policy* on the container is what actually prevents alteration once the
        configured retention period is in force; this call is not itself the enforcement point.
        """
        # Found live 2026-08-24: azure.storage.blob.aio's real `upload_blob` returns a plain
        # dict of blob properties (etag, last_modified, ...), never an object with a `.url`
        # attribute — invisible to every prior test because they all faked the WRONG return
        # shape, and this was the first approval ever recorded against a real Storage account.
        # The blob's URL is deterministic from the client itself, exactly as `blob_url_for`
        # above already computes it without any I/O — use that, not the upload result.
        blob_client = self._container.get_blob_client(f"{approval_id}.json")
        body = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")
        await blob_client.upload_blob(body, overwrite=True, content_type="application/json")
        return blob_client.url


def build_approval_artefact_store(
    *, storage_account_url: str, credential: AsyncTokenCredential
) -> ApprovalArtefactStore:
    """Construct a real :class:`ApprovalArtefactStore` against the ``approvals`` container.

    The only place ``BlobServiceClient`` is constructed — everything else in this package works
    against the protocols above, so nothing downstream needs to know blob storage is involved.
    """
    from azure.storage.blob.aio import BlobServiceClient

    service_client = BlobServiceClient(account_url=storage_account_url, credential=credential)
    container_client = service_client.get_container_client("approvals")
    return ApprovalArtefactStore(container_client)  # type: ignore[arg-type]
