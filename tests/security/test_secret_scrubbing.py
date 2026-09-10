"""SC-013 — zero secrets in logs, traces, reports, or notifications.

The cases here are the ones that actually happen: an Azure SDK error carrying a SAS URL, a
connection string echoed into a message, a token in an Authorization header. Contrived inputs would
prove less.

Every literal below is a synthetic test value. Nothing here is a real credential.
"""

from __future__ import annotations

import pytest

from groundwork_shared.telemetry.scrubbing import scrub_text, scrub_value

pytestmark = pytest.mark.security

# Synthetic, structurally valid, not issued by anything.
FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IlRlc3QifQ"
    ".dBjftJeZ4CVPmB92K27uhbUJU1p1r0W1nFDcSCTFmJk"
)
FAKE_TENANT_GUID = "00000000-0000-4000-8000-000000000001"


def test_jwt_is_redacted() -> None:
    result = scrub_text(f"auth failed with token {FAKE_JWT}")
    assert FAKE_JWT not in result
    assert "[REDACTED:jwt]" in result


def test_storage_sas_url_is_redacted() -> None:
    """The classic leak: an SDK error echoing a signed URL."""
    url = (
        "https://acct.blob.core.windows.net/c/b.txt"
        "?sv=2023-11-03&sig=abcDEF123%2Fxyz%3D&se=2026-08-01T00:00:00Z"
    )
    result = scrub_text(f"upload failed: {url}")
    assert "sig=" not in result
    assert "[REDACTED:sas]" in result
    # The host stays readable so the error is still diagnosable.
    assert "acct.blob.core.windows.net" in result


def test_connection_string_account_key_is_redacted() -> None:
    conn = (
        "DefaultEndpointsProtocol=https;AccountName=acct;"
        "AccountKey=Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA==;EndpointSuffix=core.windows.net"
    )
    result = scrub_text(conn)
    assert "Zm9vYmFy" not in result
    assert "[REDACTED:connection-string]" in result
    assert "AccountName=acct" in result


def test_bearer_header_is_redacted() -> None:
    result = scrub_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789")
    assert "abcdefghijklmnop" not in result
    assert "[REDACTED:bearer]" in result


def test_private_key_block_is_redacted() -> None:
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ\n"
        "-----END PRIVATE KEY-----"
    )
    result = scrub_text(f"cert load failed: {pem}")
    assert "MIIEvQ" not in result
    assert "[REDACTED:private-key]" in result


def test_tenant_guid_is_redacted() -> None:
    """FR-049 names tenant identifiers as sensitive.

    A customer's tenant GUID in a shared log discloses who our customers are.
    """
    result = scrub_text(f"tenant {FAKE_TENANT_GUID} validation failed")
    assert FAKE_TENANT_GUID not in result
    assert "[REDACTED:guid]" in result


def test_guid_redaction_can_be_disabled_for_internal_correlation() -> None:
    """Correlation IDs must stay readable in internal traces to be useful."""
    result = scrub_text(f"correlation {FAKE_TENANT_GUID}", redact_guids=False)
    assert FAKE_TENANT_GUID in result


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "clientSecret",
        "client_secret",
        "api-key",
        "apiKey",
        "Authorization",
        "connectionString",
        "refreshToken",
        "sasToken",
    ],
)
def test_sensitive_keys_are_redacted_regardless_of_value(key: str) -> None:
    """Some values are sensitive because of what they are, not what they look like."""
    result = scrub_value({key: "any-value-at-all"})
    assert isinstance(result, dict)
    assert result[key] == "[REDACTED:sensitive-key]"


@pytest.mark.parametrize(
    "key",
    ["partitionKey", "publicKey", "keyVaultName", "rowKey", "idempotencyKey"],
)
def test_allowlisted_keys_are_not_redacted(key: str) -> None:
    """Over-redaction makes logs useless, which pushes people to disable scrubbing entirely."""
    result = scrub_value({key: "readable-value"})
    assert isinstance(result, dict)
    assert result[key] == "readable-value"


def test_nested_structures_are_scrubbed() -> None:
    payload = {
        "deployment": {
            "stages": [
                {"name": "infrastructure", "error": f"failed for {FAKE_TENANT_GUID}"},
                {"name": "fabric", "clientSecret": "should-not-appear"},
            ]
        }
    }
    result = scrub_value(payload)
    flattened = repr(result)
    assert FAKE_TENANT_GUID not in flattened
    assert "should-not-appear" not in flattened
    assert "infrastructure" in flattened


def test_bytes_are_never_emitted() -> None:
    """A byte blob could be a certificate or key; there is no safe way to render it."""
    result = scrub_value({"blob": b"\x00\x01binary-cert-material"})
    assert isinstance(result, dict)
    assert result["blob"] == "[REDACTED:bytes]"


def test_unknown_objects_are_stringified_and_scrubbed() -> None:
    """Fail closed: a custom type whose __str__ embeds a token must not pass through."""

    class Opaque:
        def __str__(self) -> str:
            return f"Opaque(token={FAKE_JWT})"

    result = scrub_value(Opaque())
    assert FAKE_JWT not in str(result)
    assert "[REDACTED:jwt]" in str(result)


def test_deeply_nested_structure_is_bounded() -> None:
    """Depth-bounded so a cyclic or pathological structure cannot hang the logger."""
    payload: dict[str, object] = {"level": 0}
    current = payload
    for i in range(1, 30):
        nxt: dict[str, object] = {"level": i}
        current["child"] = nxt
        current = nxt

    result = scrub_value(payload)
    assert "[REDACTED:depth-exceeded]" in repr(result)


def test_scalars_pass_through_unchanged() -> None:
    """Scrubbing must not corrupt ordinary telemetry values."""
    assert scrub_value(42) == 42
    assert scrub_value(3.14) == 3.14
    assert scrub_value(True) is True
    assert scrub_value(None) is None


def test_clean_message_is_unchanged() -> None:
    """No false positives on ordinary operational text."""
    message = "stage 'infrastructure' completed in 8m12s with idempotence outcome no_op"
    assert scrub_text(message) == message
