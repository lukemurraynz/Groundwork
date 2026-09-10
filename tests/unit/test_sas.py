"""``groundwork_shared.storage.sas`` (WAF assessment §2.7): a permanent blob URL echoed straight
back to an API caller discloses account/container/path information indefinitely. This is the
follow-up ``api/reports.py``, ``api/approvals.py``, and ``api/deployments.py`` all now call at
response time to mint a fresh, short-lived, user-delegation SAS instead."""

from __future__ import annotations

from datetime import UTC
from typing import Any
from unittest.mock import AsyncMock, patch

from groundwork_shared.storage.sas import read_only_sas_url

BLOB_URL = "https://stgwexample.blob.core.windows.net/reports/deadbeef.json"


class _FakeCredential:
    async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        class _Token:
            token = "fake-token"  # noqa: S105

        return _Token()


class _FakeDelegationKey:
    pass


async def test_returns_unchanged_for_a_non_blob_url() -> None:
    # No Azure call should even be attempted for a URL that isn't shaped like a real Azure
    # Storage blob URL — the fake URLs test fixtures already use (https://example.invalid/...)
    # must keep working unchanged, not start throwing.
    url = await read_only_sas_url(
        "https://example.invalid/previews/x.json",
        credential=_FakeCredential(),  # type: ignore[arg-type]
    )
    assert url == "https://example.invalid/previews/x.json"


async def test_appends_a_sas_query_string_for_a_real_blob_url() -> None:
    fake_service_client = AsyncMock()
    fake_service_client.get_user_delegation_key = AsyncMock(return_value=_FakeDelegationKey())
    fake_service_client.close = AsyncMock()

    with (
        patch(
            "azure.storage.blob.aio.BlobServiceClient", return_value=fake_service_client
        ) as service_client_cls,
        patch(
            "azure.storage.blob.generate_blob_sas", return_value="sv=fake&sig=fake"
        ) as generate_sas,
    ):
        url = await read_only_sas_url(BLOB_URL, credential=_FakeCredential())  # type: ignore[arg-type]

    assert url == f"{BLOB_URL}?sv=fake&sig=fake"
    service_client_cls.assert_called_once()
    called_kwargs = generate_sas.call_args.kwargs
    assert called_kwargs["account_name"] == "stgwexample"
    assert called_kwargs["container_name"] == "reports"
    assert called_kwargs["blob_name"] == "deadbeef.json"
    fake_service_client.close.assert_awaited_once()


async def test_ttl_is_reflected_in_the_delegation_key_window() -> None:
    fake_service_client = AsyncMock()
    fake_service_client.get_user_delegation_key = AsyncMock(return_value=_FakeDelegationKey())
    fake_service_client.close = AsyncMock()

    from datetime import timedelta

    with (
        patch("azure.storage.blob.aio.BlobServiceClient", return_value=fake_service_client),
        patch("azure.storage.blob.generate_blob_sas", return_value="sv=fake"),
    ):
        await read_only_sas_url(
            BLOB_URL, credential=_FakeCredential(), ttl=timedelta(hours=1)  # type: ignore[arg-type]
        )

    _, kwargs = fake_service_client.get_user_delegation_key.call_args
    delta = kwargs["key_expiry_time"] - kwargs["key_start_time"]
    assert delta == timedelta(hours=1)
    assert kwargs["key_start_time"].tzinfo is UTC


async def test_falls_back_to_the_plain_url_when_sas_minting_fails() -> None:
    # Best-effort: a durable audit record must remain readable even if a transient Storage/Entra
    # error means SAS minting itself fails — never worse than today's unchanged permanent URL.
    with patch(
        "azure.storage.blob.aio.BlobServiceClient", side_effect=RuntimeError("boom")
    ):
        url = await read_only_sas_url(BLOB_URL, credential=_FakeCredential())  # type: ignore[arg-type]

    assert url == BLOB_URL


async def test_extracts_a_nested_blob_path_correctly() -> None:
    nested_url = "https://stgwexample.blob.core.windows.net/reports/2026/09/deadbeef.json"
    fake_service_client = AsyncMock()
    fake_service_client.get_user_delegation_key = AsyncMock(return_value=_FakeDelegationKey())
    fake_service_client.close = AsyncMock()

    with (
        patch("azure.storage.blob.aio.BlobServiceClient", return_value=fake_service_client),
        patch(
            "azure.storage.blob.generate_blob_sas", return_value="sv=fake"
        ) as generate_sas,
    ):
        url = await read_only_sas_url(nested_url, credential=_FakeCredential())  # type: ignore[arg-type]

    assert url == f"{nested_url}?sv=fake"
    called_kwargs = generate_sas.call_args.kwargs
    assert called_kwargs["container_name"] == "reports"
    assert called_kwargs["blob_name"] == "2026/09/deadbeef.json"
