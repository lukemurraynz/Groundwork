"""Voice Live WebSocket bridge (T104; FR-004a, FR-053a).

Connects to Azure AI Voice Live over WebSocket, forwarding bidirectional audio between a
client (the custom web frontend as of the 2026-08-21 scope decision; ``telephony.py``'s ACS
call leg remains parked) and the realtime speech-to-speech model. Speech recognition and
synthesis locales are **pinned to ``en-AU``** — FR-004a requires Australian English this
release, and there is deliberately no fallback locale (FR-004b: a failed confirmation must
surface as an explicit re-ask, not a silent default).

FR-053a: raw audio is discarded at transcription — this module never persists audio data.

**Verified 2026-08-02**: Voice Live WebSocket API supports server-to-server integration,
Azure OpenAI Realtime API-compatible events, ``en-AU`` locale for both STT and TTS,
function calling, and server-side VAD (Voice Activity Detection). Model selection:
``gpt-realtime-mini`` (Voice Live basic tier, lowest cost) for Release 1 — confirmed
available in ``australiaeast`` as Global standard (research notes § V-003).

**Re-verified 2026-08-21** against learn.microsoft.com/azure/ai-services/speech-service/
voice-live-how-to: GA endpoint is
``wss://<resource>.services.ai.azure.com/voice-live/realtime?api-version=2026-04-10&model=<model>``,
Entra auth via ``Bearer`` token scoped to ``https://ai.azure.com/.default``, events are Azure
OpenAI Realtime-compatible JSON (audio in via ``input_audio_buffer.append`` with base64 PCM16;
audio out via ``response.audio_delta``; function results via ``conversation.item.create`` with
a ``function_call_output`` item followed by ``response.create``).
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp
from azure.core.credentials_async import AsyncTokenCredential

logger = logging.getLogger(__name__)

_VERIFIED_VOICE_LIVE = (
    "2026-08-02 learn.microsoft.com / voice-live — WebSocket, en-AU, "
    "gpt-realtime-mini in australiaeast, function calling, server VAD; "
    "re-verified 2026-08-21 — endpoint path /voice-live/realtime, api-version 2026-04-10, "
    "Entra scope https://ai.azure.com/.default"
)

# The Voice Live GA API version the wire format below is written against. Pinned, not "latest":
# an unannounced preview bump must not silently change event shapes under a deployed pod.
VOICE_LIVE_API_VERSION = "2026-04-10"

# Entra token scope for Voice Live on a Foundry resource — the same scope api/voice.py's TTS
# route already used, so no new credential configuration is introduced. Not a secret: it is a
# public identifier of the resource's token audience (hence the noqa on the password heuristic).
VOICE_LIVE_TOKEN_SCOPE = "https://ai.azure.com/.default"  # noqa: S105


class VoiceLiveSessionLike(Protocol):
    """Injectable seam — tests supply a fake WebSocket with no Azure resource behind it."""

    async def send_audio(self, chunk: bytes) -> None: ...

    async def send_text_input(self, text: str) -> None: ...

    def receive_events(self) -> AsyncIterator[dict[str, object]]: ...

    async def send_event(self, event: dict[str, object]) -> None: ...

    async def send_function_result(
        self, call_id: str, result: object, *, previous_item_id: str | None = None
    ) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class VoiceLiveConfig:
    """Parameters for one Voice Live session. ``model`` is ``gpt-realtime-mini`` for Release 1
    (Voice Live basic tier — lowest cost model in ``australiaeast``). ``locale`` is pinned
    to ``en-AU`` per FR-004a and is deliberately not configurable — there is no fallback."""

    endpoint_url: str
    locale: str = "en-AU"
    model: str = "gpt-realtime-mini"
    temperature: float = 0.7
    voice: str = "en-AU-NatashaNeural"


class VoiceLiveError(Exception):
    """Voice Live WebSocket connection or session failed."""


def _realtime_url(endpoint_base: str, model: str) -> str:
    """Build the Voice Live WebSocket URL from a Foundry resource base URL.

    Accepts either scheme ("https://" or "wss://") and any trailing slash/path state, because
    the configured setting is shared with other voice tooling that documents the plain resource
    endpoint. Raises rather than guessing when the base is not a host URL this codebase can
    meaningfully connect to.
    """
    base = endpoint_base.strip().rstrip("/")
    if base.startswith("wss://"):
        host = base[len("wss://") :]
    elif base.startswith("https://"):
        host = base[len("https://") :]
    else:
        raise VoiceLiveError(
            f"voice live endpoint must be an https:// or wss:// Foundry resource URL, got "
            f"{endpoint_base!r}"
        )
    if "/" in host:  # a full path was configured, not a resource base — refuse to guess
        raise VoiceLiveError(
            f"voice live endpoint must be the resource root (no path), got {endpoint_base!r}"
        )
    return f"wss://{host}/voice-live/realtime?api-version={VOICE_LIVE_API_VERSION}&model={model}"


class AiohttpVoiceLiveSession:
    """Concrete :class:`VoiceLiveSessionLike` over one aiohttp WebSocket to Voice Live.

    Thin by design: transport + wire-format only. Session policy (instructions, tools, VAD,
    voice) is composed by the API layer as ``session.update`` events; nothing here knows about
    planning or approvals.
    """

    def __init__(self, ws: Any) -> None:  # aiohttp ClientWebSocketResponse
        self._ws = ws

    async def send_audio(self, chunk: bytes) -> None:
        await self._ws.send_str(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                }
            )
        )

    async def send_text_input(self, text: str) -> None:
        """Inject one typed user utterance into the conversation and request a response."""
        await self._ws.send_str(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                }
            )
        )
        await self._ws.send_str(json.dumps({"type": "response.create"}))

    async def send_event(self, event: dict[str, object]) -> None:
        """Send one raw client event (e.g. ``session.update``) to Voice Live."""
        await self._ws.send_str(json.dumps(event))

    async def receive_events(self) -> AsyncIterator[dict[str, object]]:
        # Found live: these were bare magic numbers (9/8/13) that did not actually match
        # aiohttp's real WSMsgType values — TEXT is 1, not 9 (9 is PING); ERROR is 258, not 13.
        # Every real Voice Live event (session.updated, transcripts, function calls, everything)
        # was silently falling through unhandled for the life of this module; only CLOSE (8,
        # which happened to be numerically correct by coincidence) was ever actually detected,
        # via the loop simply ending. Comparing against the enum directly makes this class of
        # silent mismatch impossible — a typo'd member name is a real ImportError/AttributeError
        # at import time, not a value that quietly never matches anything at runtime.
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                yield json.loads(msg.data)
            elif msg.type == aiohttp.WSMsgType.CLOSE:
                # This silently discarded Voice Live's own close code/reason (e.g. a
                # session.update config the service rejected) — exactly the information needed
                # to tell "the service closed us, on purpose, for a reason" apart from a generic
                # dropped connection; the caller only ever saw an unrelated downstream symptom (a
                # later send failing because the socket was already gone), never why.
                logger.warning(
                    "voice live websocket closed by server: code=%s reason=%r",
                    self._ws.close_code,
                    msg.extra,
                )
                break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise VoiceLiveError(f"voice live websocket error: {self._ws.exception()}")

    async def send_function_result(
        self, call_id: str, result: object, *, previous_item_id: str | None = None
    ) -> None:
        """Respond to a Voice Live function-call with its result.

        ``previous_item_id`` is the id of the ``function_call`` conversation item the service
        emitted in ``conversation.item.created`` — the canonical sample
        (microsoft-foundry/voicelive-samples function-calling-quickstart) passes it so the
        output item is positioned directly after its call. Then a new response is requested so
        the model speaks the outcome.
        """
        item: dict[str, object] = {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(result),
        }
        create: dict[str, object] = {"type": "conversation.item.create", "item": item}
        if previous_item_id:
            create["previous_item_id"] = previous_item_id
        await self._ws.send_str(json.dumps(create))
        await self._ws.send_str(json.dumps({"type": "response.create"}))

    async def close(self) -> None:
        await self._ws.close()


async def open_voice_live_session(
    *, endpoint_base: str, credential: AsyncTokenCredential, config: VoiceLiveConfig
) -> AiohttpVoiceLiveSession:
    """Open one authenticated Voice Live WebSocket session.

    Raises :class:`VoiceLiveError` on a misconfigured endpoint base or a failed connection —
    callers surface that as "built but not configured/broken", never as a silent fallback.
    """
    url = _realtime_url(endpoint_base, config.model)
    token = await credential.get_token(VOICE_LIVE_TOKEN_SCOPE)
    try:
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(url, headers={"Authorization": f"Bearer {token.token}"})
    except Exception as exc:
        raise VoiceLiveError(f"could not connect to Voice Live at {url!r}: {exc}") from exc
    return AiohttpVoiceLiveSession(ws)


class VoiceLiveBridge:
    """Connects a client's bidirectional audio stream to a Voice Live session.

    This is the middle tier between the client frontend and the conversational AI model —
    forwards inbound audio to Voice Live, then forwards the synthesized audio back for
    playback. All audio is transient — nothing is persisted (FR-053a).

    The bridge owns the Voice Live WebSocket lifecycle and exposes the session for the
    planning/confirmation/handoff modules (T105/T106) to send their own text messages
    and function-call results through. Audio is discarded at transcription; the transcript
    (a sequence of ``ConversationTurn`` objects) is returned by ``run_session`` for the
    caller to persist as a ``ConversationRecord`` (FR-053b).
    """

    def __init__(self, session: VoiceLiveSessionLike, *, config: VoiceLiveConfig) -> None:
        self._session = session
        self.config = config

    async def send_audio(self, chunk: bytes) -> None:
        """Forward one audio chunk from the client to Voice Live."""
        await self._session.send_audio(chunk)

    async def receive_events(self) -> AsyncIterator[dict[str, object]]:
        """Yield Voice Live events as they arrive — the caller (planning/confirmation/handoff)
        interprets them."""
        async for event in self._session.receive_events():
            yield event

    async def send_function_result(
        self, call_id: str, result: object, *, previous_item_id: str | None = None
    ) -> None:
        """Respond to a Voice Live function-call with its result."""
        await self._session.send_function_result(call_id, result, previous_item_id=previous_item_id)

    async def close(self) -> None:
        """Close the Voice Live session — called when the call ends."""
        await self._session.close()
