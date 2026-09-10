# ADR-0004: Conversational surfaces: web frontend and Voice Live now; ACS/PSTN deferred; Teams removed from R1

**Date**: 2026-08-26
**Status**: Accepted (2026-08-26)
**Supersedes**: the Teams-via-M365-Agents-SDK decision recorded in the project's research notes, "Chat channel" (2026-07-30)

## Context

Three conversational surface options were evaluated across the project's history:

1. **Microsoft Teams** via the M365 Agents SDK (`microsoft-agents-hosting-teams`). Verified as
   technically viable on 2026-08-21 (V-005). Two structural constraints were recorded: Private Link
   is unsupported for Teams ingress, conflicting with the platform's private-by-default networking
   posture; and adaptive-card WCAG 2.2 AA conformance is an authoring responsibility, not automatic.

2. **ACS/PSTN telephony** via Azure Communication Services Call Automation bridging to Voice Live.
   Verified as viable (V-003, V-004). The browser-based Voice Live frontend became the working
   product surface on 2026-08-21 instead, making ACS telephony an as-yet-unbuilt extension rather
   than the primary channel.

3. **Web frontend** (`voice.html` over `/ws/voice` + the REST API). This has been the working
   product surface since 2026-08-21 and is the only channel verified against the live environment.

The product owner removed Teams from Release 1 scope on 2026-08-26. T052/T053/T054 are descoped
accordingly. The web frontend is the sole conversational surface for this release.

## Decision

For Release 1:

- The conversational surface is Groundwork's own **web frontend** (`voice.html` over `/ws/voice`,
  plus the REST API). This is the only channel built and verified against the live environment.
- **Voice Live** (Azure AI Speech service, `australiaeast`, Global standard deployment) provides
  the real-time speech-to-speech layer. The voice-approval decision recorded 2026-08-02 (see
  [ADR-0011](0011-voice-alone-authorises-irreversible-actions.md)) permits voice alone to authorise
  an irreversible action; caller-identity verification remains an unresolved gap separate from this
  channel decision.
- **ACS/PSTN telephony** is deferred. The verified design (ACS Call Automation bridging audio to
  Voice Live, with bidirectional streaming and barge-in confirmed) is preserved in the project's
  research notes, V-003/V-004, for a future release. `telephony.py` stays unwired by design.
- **Teams is not built for R1.** V-005's verification findings (SDK path confirmed, Private Link
  constraint recorded, WCAG authoring requirement noted) are preserved. V-005 reopens if Teams is
  added in a later release. Do not treat V-005 as settled for a future Teams implementation.

FR-004a's `en-AU` language obligation attaches to the frontend surfaces and report rendering.
FR-004d's guarantee that no capability is voice-only remains in force: every voice action is
reachable via the REST API.

## Consequences

**Positive**

- The web frontend is the only surface that has been end-to-end verified against live Azure
  infrastructure. No channel decision in R1 depends on unverified integration.
- Dropping Teams removes a Private Link exception decision from R1 scope.

**Negative / watch points**

- ACS/PSTN deferral means external callers cannot reach the system by telephone in R1. The
  product's voice identity exists only within browser sessions.
- If Teams is reintroduced, V-005 reopens with it: Private Link incompatibility, adaptive-card
  WCAG authoring requirements, and the Foundry-hosted-agent Teams-activity wiring all need
  re-evaluation against the state of the platform at that time.

**Sources**: the project's internal implementation notes (not included in this release) (2026-08-21 entry); the
research notes' "Chat channel" superseded block (2026-08-26); V-003, V-004, V-005
