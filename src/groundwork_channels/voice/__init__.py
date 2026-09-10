"""Voice channel -- ACS Call Automation + Azure AI Voice Live (Phase 6, T099-T107).

Groundwork's primary entry point. An inbound PSTN call reaches ACS Call Automation, which bridges
call audio bidirectionally to a Voice Live WebSocket in australiaeast, which runs the same
planning agent the chat channel uses. Consent is per-tenant and must be recorded before the first
call; enablement is gated on both ``voice_channel_enabled`` and ``offshore_inference_consent``,
never default-allow (FR-053d, FR-053e). As of 2026-08-02 (see ADR-0011), spoken agreement
alone MAY authorise execution — see ``handoff.py``.
"""
