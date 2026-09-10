# ADR-0011: Voice alone may authorise an irreversible action

**Date**: 2026-08-02
**Status**: Accepted (2026-08-02)
**Supersedes**: the prior rule barring voice as the sole confirmation channel for an irreversible
action

## Context

Groundwork's original approval design required an irreversible action's confirmation to yield a
durable artefact (a channel a customer could not later deny having used). Voice did not qualify:
`ApprovalChannel.yields_durable_artefact` returned `False` for `VOICE`, and three validators
(`_channel_must_be_durable` on `Approval`, `SecondApproval`, and `PendingApproval`) rejected any
approval or second approval carried on that channel. A voice-only customer confirming a deployment
had to switch to chat or the API to complete it.

Voice is Groundwork's primary entry point. The product owner judged the forced channel switch not
worth the friction it added to the one path most customers were expected to use, and approved
removing the durable-artefact requirement for voice specifically, in conversation, on 2026-08-02.

## Decision

Voice may now authorise an irreversible action on its own, at any cost or environment threshold,
including a deployment that would otherwise require a second, distinct approver.

`yields_durable_artefact` stays on `ApprovalChannel` as an informational property; it no longer
gates anything. The three `_channel_must_be_durable` validators were removed rather than loosened,
so there is no rejecting code path left to bypass.

**What did not change**: the distinct-identity requirement. A first and second approval from the
same identity still fail validation regardless of channel, voice included. Two people, or one
person confirming twice, are not the same thing, and this decision does not touch that boundary.

**Accepted risk, recorded here because it is the one that matters most**: this removes the primary
structural defence against a spoofed or impersonated voice approval. The current voice design does
not authenticate the caller beyond what Entra sign-in already verifies for any other channel; that
gap is distinct from this decision and remains open. Treat it as a blocker for any deployment where
an attacker plausibly could act as the approver, until the mitigation below is actually turned on
for a real customer engagement.

**[VERIFIED] 2026-09-06: this paragraph was wrong about the shape of the gap, corrected here
rather than left standing.** The original wording spoke of "caller ID," "DTMF PIN," and "voice
biometrics", language for a PSTN telephony channel. `telephony.py` (the ACS Call Automation
inbound handler) is never imported or routed anywhere in this codebase; ADR-0004 already records
that voice shipped as the authenticated web frontend, not the phone line. There is no caller ID to
spoof, because there is no phone call: every voice session opens over a WebSocket that requires a
validated Entra bearer token in its first frame, the same authentication every REST caller
presents, before any Voice Live connection opens (`voice.py`, the WebSocket auth handshake).

The real, narrower gap is this: once that WebSocket is open, a single spoken utterance from
whoever is on the other end of that already-authenticated session is enough to approve an
irreversible action, with no check that the *token holder* is still the one speaking. That is a
session/token-strength question, not a caller-identity one, and a mitigation for exactly that
question already exists in this codebase: `approval/service.py`'s `_require_step_up_authentication`,
gated by `GROUNDWORK_REQUIRE_STEP_UP_APPROVAL`, refuses an approval unless the caller's token shows
MFA in its `amr` claim or was issued within the last 10 minutes. It is threaded through
`record_approval` identically for every channel, voice included (`api/voice.py`'s approval route
passes `channel=ApprovalChannel.VOICE` through the same call). As found on 2026-09-06 it defaulted
to `False`, and nothing in this ADR, the WAF assessment, or the threat model mentioned it existed:
"no caller-authentication mechanism... has been decided or built" was true of a PSTN scenario that
was never live, and inaccurate about the codebase, which already shipped a real, tested lever for
the risk that's actually present.

**Resolved 2026-09-06, same day**: the default is now `True` (`settings.py`), wired into both k8s
deployment templates so an operator can still opt out per environment with `azd env set
GROUNDWORK_REQUIRE_STEP_UP_APPROVAL false`. A fresh deployment now requires MFA-or-fresh-token
evidence for every approval, voice included, unless someone deliberately turns it off.

## Consequences

**Positive**

- Removes the one forced channel switch left in the primary voice flow: a customer approving by
  voice completes the action without touching chat or the API.
- The distinct-identity rule, the part of the approval gate that stops one person from being both
  approvers, is unaffected and still enforced identically across every channel.

**Negative / watch points**

- A real compensating control exists (`GROUNDWORK_REQUIRE_STEP_UP_APPROVAL`) and now defaults on
  for every channel (resolved 2026-09-06, above). It is a token-evidence gate, not a re-verification
  of who is speaking mid-call: a session that already presented MFA once still passes for its full
  token lifetime, or until 10 minutes after a non-MFA token was issued. That is the residual gap
  worth tracking, not "no control exists."
- This is a genuine reduction in the approval gate's assurance for voice specifically, accepted
  deliberately rather than discovered later. Any change proposing to widen voice's authority
  further should treat this gap as a precondition, not a detail to note in passing.
- `tests/security/test_approval_gate.py` asserts the new behaviour (voice accepted) and the
  unchanged behaviour (distinct-identity survives) side by side; a future change to either should
  keep both assertions, not just the one it's touching.

**Sources**: the voice-approval decision this ADR records (2026-08-02); `src/groundwork_contracts/approval.py`
(`ApprovalChannel`, `yields_durable_artefact`); `src/groundwork_controlplane/approval/service.py`
(`_require_step_up_authentication`); `src/groundwork_channels/voice/telephony.py` (confirmed unwired
by grep, zero importers outside its own module); `tests/security/test_approval_gate.py`;
`docs/waf-assessment.md` §2.4 (voice caller-identity verification gap, corrected 2026-09-06)
