"""ACS Call Automation inbound handler (T103; V-004 verified 2026-08-02).

Answers the Event Grid ``IncomingCall`` event using the Azure Communication Services Call
Automation Python SDK, configures bidirectional audio streaming into the call leg
(``enableBidirectional`` confirmed against live Microsoft Learn docs), and persists
``ServerCallId`` in an unbounded string field — per the cross-cutting rule in
the original design notes: "Opaque ACS identifiers... must use unbounded string fields. Never parse,
decode, or length-constrain them."

**Verified 2026-08-02**: ACS Call Automation v1.6.0 (``azure-communication-callautomation``)
supports ``MediaStreamingOptions(enable_bidirectional=True, start_media_streaming=True,
transport_type=StreamingTransportType.WEBSOCKET, audio_format=AudioFormat.PCM24_K_MONO)`` —
confirmed against live Context7 query of Microsoft Learn's audio-streaming quickstart.
``CallAutomationClient.answer_call(incoming_call_context, callback_url, media_streaming=...)``
returns a ``CallConnectionProperties`` carrying ``server_call_id`` (variable-length Base64,
do not length-constrain).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

_VERIFIED_ACS_API = "2026-08-02 Context7 / learn.microsoft.com — answer_call + bidirectional"


class CallAutomationClientLike(Protocol):
    """Injectable seam — tests supply a fake with no ACS resource behind it, the same pattern
    every other Azure client in this codebase uses (``ContainerClientLike`` for blob storage,
    ``EmailSenderLike`` for notification)."""

    async def answer_call(
        self,
        *,
        incoming_call_context: str,
        callback_url: str,
        media_streaming: Any | None = None,
        **kwargs: Any,
    ) -> CallConnectionPropertiesLike: ...


class CallConnectionPropertiesLike(Protocol):
    @property
    def server_call_id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class AnsweredCall:
    """The identifiers ACS assigned to an answered call — everything a downstream module
    (Voice Live, DTMF) needs to reference this specific call leg."""

    server_call_id: str
    """Unbounded string — Base64-encoded by ACS, never parsed or length-constrained."""

    call_connection_id: str
    """Fixed-length GUID for the call connection."""

    correlation_id: str
    """Fixed-length GUID correlating ACS events for this call."""


class InboundCallError(Exception):
    """ACS Call Automation refused or failed to answer an incoming call."""


class InboundCallHandler:
    """Answers one inbound call via the ACS Call Automation SDK and bridges its audio
    to the Voice Live WebSocket (T104, ``voicelive.py``).

    ``callback_url`` is the Event Grid callback endpoint ACS uses to deliver subsequent
    events (``CallConnected``, ``MediaStreamingStarted``, etc.) — set once per handler.
    ``media_streaming_options`` configures bidirectional audio streaming; if ``None``,
    no media streaming is enabled (the call is answered as bare telephony).
    """

    def __init__(
        self,
        client: CallAutomationClientLike,
        *,
        callback_url: str,
        media_streaming_options: Any | None = None,
    ) -> None:
        self._client = client
        self._callback_url = callback_url
        self._media_streaming = media_streaming_options

    async def answer(self, incoming_call_context: str) -> AnsweredCall:
        """Answer the call. ``incoming_call_context`` is the opaque string from the
        Event Grid ``IncomingCall`` event — forward it directly, never parse it."""
        try:
            result = await self._client.answer_call(
                incoming_call_context=incoming_call_context,
                callback_url=self._callback_url,
                media_streaming=self._media_streaming,
            )
        except Exception as exc:
            raise InboundCallError(f"ACS answer_call failed: {exc}") from exc

        return AnsweredCall(
            server_call_id=result.server_call_id,
            call_connection_id=getattr(result, "call_connection_id", ""),
            correlation_id=getattr(result, "correlation_id", ""),
        )


def build_inbound_call_handler(
    *,
    acs_connection_string: str,
    callback_url: str,
    websocket_url: str,
) -> InboundCallHandler:
    """Construct a real :class:`InboundCallHandler` against the ACS resource. The only place
    ``CallAutomationClient`` is imported."""
    from azure.communication.callautomation import (
        AudioFormat,
        CallAutomationClient,
        MediaStreamingAudioChannelType,
        MediaStreamingContentType,
        MediaStreamingOptions,
        StreamingTransportType,
    )

    client = CallAutomationClient.from_connection_string(acs_connection_string)

    media_streaming = MediaStreamingOptions(
        transport_url=websocket_url,
        transport_type=StreamingTransportType.WEBSOCKET,
        content_type=MediaStreamingContentType.AUDIO,
        audio_channel_type=MediaStreamingAudioChannelType.MIXED,
        start_media_streaming=True,
        enable_bidirectional=True,
        enable_dtmf_tones=True,
        audio_format=AudioFormat.PCM24_K_MONO,
    )

    return InboundCallHandler(
        client, callback_url=callback_url, media_streaming_options=media_streaming
    )
