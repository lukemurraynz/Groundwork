"""Secret scrubbing for logs, traces, errors, reports, and notifications.

FR-049 and SC-013 require zero secrets, tokens, credentials, or PII in any output. The realistic
leak is not someone logging a password on purpose — it is an Azure SDK error containing a SAS URL,
or a provider response echoed into an exception message.

Design decisions worth knowing:

**Redact, never truncate silently.** Every redaction leaves a visible ``[REDACTED:kind]`` marker, so
an operator debugging an incident can see that something was removed rather than reading a mangled
string and drawing the wrong conclusion.

**Fail closed on unknown structure.** Nested containers are walked; anything not walkable is
stringified and scrubbed rather than passed through on the assumption it is safe.

**Tenant identifiers are treated as sensitive.** FR-049 names them explicitly. A customer's tenant
GUID in a shared log is a disclosure about who our customers are.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

_REDACTED: Final = "[REDACTED:{kind}]"

# Key names whose *value* is sensitive regardless of what it looks like. Matched case-insensitively
# against the whole key and against snake/camel/kebab segments.
_SENSITIVE_KEY_PARTS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "key",
        "apikey",
        "credential",
        "credentials",
        "authorization",
        "auth",
        "sas",
        "signature",
        "sig",
        "connectionstring",
        "connstr",
        "clientsecret",
        "privatekey",
        "certificate",
        "cert",
        "pfx",
        "bearer",
        "cookie",
        "session",
        "refreshtoken",
        "accesstoken",
        "idtoken",
    }
)

# Keys that are safe even though they contain a sensitive-looking substring. Without this,
# "partitionKey" and "publicKey" get redacted and the logs become useless for debugging.
_ALLOWLISTED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "partitionkey",
        "publickey",
        "keyvaultname",
        "keyvaulturi",
        "keyname",
        "primarykeyname",
        "rowkey",
        "keyspace",
        "idempotencykey",
        "correlationkey",
    }
)

_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    # JWT / Entra access tokens. Three base64url segments separated by dots.
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    # Azure Storage SAS query strings — the classic accidental leak in an SDK error.
    (
        "sas",
        re.compile(r"[?&](?:sig|sv|se|st|sp|srt|ss|spr)=[^\s&\"']+", re.IGNORECASE),
    ),
    # Storage / Service Bus / Cosmos connection strings.
    (
        "connection-string",
        re.compile(
            r"\b(?:AccountKey|SharedAccessKey|SharedAccessSignature|Password)=[^;\s\"']+",
            re.IGNORECASE,
        ),
    ),
    # Bearer tokens in an Authorization header value.
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE)),
    # PEM private key blocks.
    (
        "private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # Entra client secrets. Format is not contractual, so this is deliberately conservative and
    # catches the common shape rather than attempting to be exhaustive.
    ("client-secret", re.compile(r"\b[A-Za-z0-9~._-]{3}8Q~[A-Za-z0-9~._-]{30,}\b")),
    # GUIDs. Tenant and subscription identifiers are sensitive per FR-049. This is broad on
    # purpose: over-redacting an identifier is recoverable, disclosing a customer's tenant is not.
    (
        "guid",
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
    ),
)

_MAX_DEPTH: Final = 12


def _key_is_sensitive(key: str) -> bool:
    normalised = re.sub(r"[^a-z0-9]", "", key.lower())
    if normalised in _ALLOWLISTED_KEYS:
        return False
    if normalised in _SENSITIVE_KEY_PARTS:
        return True
    return any(part in normalised for part in _SENSITIVE_KEY_PARTS)


def scrub_text(value: str, *, redact_guids: bool = True) -> str:
    """Redact secret-shaped substrings from free text.

    Args:
        value: Text that may contain secrets, such as a provider error message.
        redact_guids: Whether to redact GUIDs. Keep True for anything customer-facing or
            externally stored. Set False only for internal correlation output where a
            deployment ID must remain readable and no tenant identifier is present.

    Returns:
        The text with each match replaced by a visible ``[REDACTED:kind]`` marker.
    """
    scrubbed = value
    for kind, pattern in _PATTERNS:
        if kind == "guid" and not redact_guids:
            continue
        scrubbed = pattern.sub(_REDACTED.format(kind=kind), scrubbed)
    return scrubbed


def scrub_value(value: object, *, redact_guids: bool = True, _depth: int = 0) -> object:
    """Recursively scrub a value of any shape.

    Unknown types are stringified and scrubbed rather than passed through, so a custom object whose
    ``__str__`` embeds a token cannot slip past.
    """
    if _depth > _MAX_DEPTH:
        # Depth-bounded to avoid unbounded recursion on cyclic or pathological structures.
        # Truncating loudly beats either hanging or silently emitting unscrubbed content.
        return _REDACTED.format(kind="depth-exceeded")

    if value is None or isinstance(value, bool | int | float):
        return value

    if isinstance(value, str):
        return scrub_text(value, redact_guids=redact_guids)

    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if _key_is_sensitive(key):
                result[key] = _REDACTED.format(kind="sensitive-key")
            else:
                result[key] = scrub_value(raw_value, redact_guids=redact_guids, _depth=_depth + 1)
        return result

    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [scrub_value(item, redact_guids=redact_guids, _depth=_depth + 1) for item in value]

    if isinstance(value, bytes | bytearray):
        # Never emit raw bytes: they may be a certificate or key blob.
        return _REDACTED.format(kind="bytes")

    return scrub_text(str(value), redact_guids=redact_guids)


class ScrubbingFilter:
    """A ``logging.Filter`` that scrubs messages, args, and structured extras.

    Applied at the handler so it cannot be bypassed by a module that forgets to scrub. Typed as a
    plain class rather than subclassing ``logging.Filter`` so it stays importable and testable
    without pulling logging configuration into the contracts test path.
    """

    def filter(self, record: object) -> bool:
        msg = getattr(record, "msg", None)
        if isinstance(msg, str):
            record.msg = scrub_text(msg)  # type: ignore[attr-defined]

        args = getattr(record, "args", None)
        if isinstance(args, tuple):
            record.args = tuple(scrub_value(a) for a in args)  # type: ignore[attr-defined]
        elif isinstance(args, Mapping):
            record.args = scrub_value(args)  # type: ignore[attr-defined]

        for attr in ("details", "extra", "structured"):
            existing = getattr(record, attr, None)
            if existing is not None:
                setattr(record, attr, scrub_value(existing))

        return True
