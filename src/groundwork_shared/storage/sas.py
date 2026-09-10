"""Time-bounded read access to a permanently-stored blob URL (WAF assessment §2.7).

``ReportArchiveStore``/``WhatIfPreviewStore``/``approval/artefacts.py`` all persist a plain,
deterministic blob URL (``blob_client.url``) into a durable record — correctly, since those records
outlive any reasonable SAS TTL (reports carry a 365-day retention) and a rotating URL embedded in a
permanent field would go stale long before the record does. The disclosure this module closes is a
different one, named explicitly in ``api/reports.py``'s own docstring as a disclosed, scoped
follow-up: the *response* layer echoed that same permanent URL straight back to every caller,
unbounded, rather than minting a short-lived credential at the moment someone actually asks to read
it. This module is that follow-up — called from the three route handlers that expose one of these
URLs (``api/reports.py``, ``api/approvals.py``, ``api/deployments.py``), never from the write path.

User-delegation SAS only (the secretless-identity rule): the caller's own ``AsyncTokenCredential``
mints an Entra-backed delegation key, never an account key.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from azure.core.credentials_async import AsyncTokenCredential

logger = logging.getLogger(__name__)

DEFAULT_SAS_TTL = timedelta(hours=24)
"""Long enough for a caller who just received the URL in an API response to act on it without a
second round trip; short enough that a leaked link (log line, browser history, forwarded message)
stops working within a day rather than indefinitely."""

_BLOB_URL_PATTERN = re.compile(
    r"^https://(?P<account>[a-z0-9]+)\.blob\.core\.windows\.net/(?P<container>[^/]+)/(?P<blob>.+)$"
)


async def read_only_sas_url(
    blob_url: str,
    *,
    credential: AsyncTokenCredential,
    ttl: timedelta = DEFAULT_SAS_TTL,
) -> str:
    """Return ``blob_url`` with a fresh, read-only, user-delegation SAS query string appended.

    Best-effort: a permanent audit/approval/preview record must remain readable by an authorised
    caller even if SAS minting itself fails (a transient Storage or Entra error), so this logs a
    warning and returns ``blob_url`` unchanged rather than turning a decorative security
    enhancement into an outage of an otherwise-working read. ``blob_url`` unchanged is exactly
    today's behaviour, never a regression, only a missed improvement on that one call.
    """
    match = _BLOB_URL_PATTERN.match(blob_url)
    if match is None:
        logger.warning(
            "read_only_sas_url: %r does not look like a blob.core.windows.net URL; returning it "
            "unchanged",
            blob_url,
        )
        return blob_url

    try:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas
        from azure.storage.blob.aio import BlobServiceClient

        account = match.group("account")
        container = match.group("container")
        blob = match.group("blob")
        now = datetime.now(UTC)
        expiry = now + ttl

        service_client = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=credential,
        )
        try:
            delegation_key = await service_client.get_user_delegation_key(
                key_start_time=now, key_expiry_time=expiry
            )
        finally:
            await service_client.close()

        sas = generate_blob_sas(
            account_name=account,
            container_name=container,
            blob_name=blob,
            user_delegation_key=delegation_key,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
            start=now,
        )
        return f"{blob_url}?{sas}"
    except Exception:
        logger.warning(
            "read_only_sas_url: SAS minting failed for %r; returning it unchanged",
            blob_url,
            exc_info=True,
        )
        return blob_url
