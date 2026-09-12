# Teams as a conversational surface: scoping notes

**Date**: 2026-09-12
**Status**: Scoping only — no decision made here. This document reopens and updates
[ADR-0004](adr/0004-conversational-surfaces-web-frontend-voice-live.md)'s V-005 findings for
whoever picks up Phase 2 ("GitHub support, advanced approval workflows, ... Teams application" —
`docs/product-specification.md`'s Roadmap), per ADR-0004's own instruction: "V-005 reopens if
Teams is added in a later release. Do not treat V-005 as settled for a future Teams
implementation." It does not replace ADR-0004's R1 decision (Teams is not built for R1) and does
not commit to building Teams in any future release.

## Why this exists

ADR-0004 deferred Teams from Release 1 on two grounds: Private Link is unsupported for Teams
ingress, which it framed as conflicting with "the platform's private-by-default networking
posture," and Adaptive Card WCAG 2.2 AA conformance is an authoring responsibility, not automatic.
Both claims are re-verified below against current Microsoft Learn documentation (checked
2026-09-12), and one of them needs correcting.

## Networking: still unsupported, but more precisely so than ADR-0004 recorded

**Confirmed still true**: the Teams channel on Azure Bot Service has no Private Link or
VNet-restricted inbound path. Traffic from Teams to a bot always transits the public Bot Framework
connector — there is no VNet-to-VNet route.

What's more precise than ADR-0004's original framing:

- Azure Bot Service's *only* private-endpoint option is the Direct Line App Service Extension
  (DL-ASE) — and it does not cover Teams. Microsoft's current guidance actively discourages DL-ASE
  in favour of the `AzureBotService` service tag for NSG/firewall scoping ("commencing September 1,
  2023, it is strongly advised to employ the Azure Service Tag method for network isolation... DL-ASE
  should be limited to highly specific scenarios").
- Disabling public network access on the bot's hosting app doesn't harden the Teams channel — it
  **breaks** it outright ("this will unconfigure the Teams channels").
- Tenant restriction cannot be done at the network layer at all. Microsoft is explicit: "You can't
  prevent Teams from sending you messages from various tenants... All you can do is to prevent your
  bot from processing the undesired messages." Tenant scoping is an application-layer concern —
  inspect the tenant ID on the incoming Teams activity payload and reject there, the same shape as
  every other authorization decision this codebase already makes at the application boundary rather
  than the network boundary.

Sources: [Network isolation in Azure AI Bot Service](https://learn.microsoft.com/azure/bot-service/dl-network-isolation-concept?view=azure-bot-service-4.0),
[Configure network isolation](https://learn.microsoft.com/azure/bot-service/dl-network-isolation-how-to?view=azure-bot-service-4.0),
[Azure Private Link availability](https://learn.microsoft.com/azure/private-link/availability),
[Bot Framework Security and Privacy FAQ](https://learn.microsoft.com/azure/bot-service/bot-service-resources-faq-security?view=azure-bot-service-4.0).

## Correction to ADR-0004's framing: this isn't actually a conflict with Groundwork's own posture

ADR-0004 framed the Private Link gap as conflicting with "the platform's private-by-default
networking posture." That framing doesn't hold up against what the platform's posture actually is
today. The network-hardening work landing alongside this document
(`docs/waf-assessment.md` §2.11, `infra/modules/network-security-perimeter.bicep`) restricts only
the six *backend data-plane* resources the control plane and orchestrator call — Cosmos, Key
Vault, Storage, Foundry, Speech, and Container Registry. It deliberately leaves the control
plane's own ingress untouched: `voice.html` and the REST/WebSocket API have to stay publicly
reachable for voice/chat to work at all, and there was never a plan to change that.

A Teams channel sits at exactly that same layer, not the backend layer. It needs a public HTTPS
endpoint for the Bot Framework connector to reach — mitigated only by `AzureBotService`
service-tag-scoped firewall rules plus strong application-layer auth, not true network isolation.
That's a real, disclosed limitation, but it is consistent with the same
public-ingress-for-the-conversational-surface reality voice already lives with, not a new
exception carved out of a private-by-default posture that was never applied to this layer in the
first place. Any future Teams implementation still needs its network exposure documented
explicitly — the service-tag rule, the application-layer tenant filter — but it doesn't need a
special-case waiver from a rule the backend-hardening work never claimed to cover.

## WCAG 2.2 AA: no formal conformance statement exists — compliance is a checklist plus manual testing

There is no dedicated, versioned "Adaptive Cards conform to WCAG 2.2 AA" statement to cite.
Microsoft's compliance program centers on a WCAG 2.1-based conformance report process, and Teams'
own EN 301 549 conformance statement is still mapped to WCAG 2.1 success criteria (last dated
2021) — the formal VPAT hasn't been refreshed to reference 2.2 criteria for Teams itself, even
though newer product-specific docs elsewhere at Microsoft now cite 2.2 directly.

For Adaptive Cards specifically, the guidance that exists is an authoring checklist
("Accessibility tips for Adaptive Cards"), not a conformance table:

- Use `label`, not `placeholder`, on every input — placeholders aren't reliably read by screen
  readers and disappear on typing.
- Use `errorMessage` + `isRequired` for validation feedback (WCAG 3.3.1/3.3.3).
- Structure the card JSON so visual/DOM tab order matches — `ColumnSet` layouts are a common source
  of mismatch between visual order and keyboard/reading order (WCAG 1.3.2, 2.4.3).
- Use `"style": "heading"` on `TextBlock` for real heading semantics (WCAG 1.3.1, 2.4.6).
- Never hide a required field with `isVisible: false` — screen readers skip hidden elements
  entirely, producing a confusing validation state.
- Never remove focus indicators via custom `style`/`inputStyle` (WCAG 2.4.7).
- Set `wrap: true` on text so it doesn't truncate at zoom or small viewports (WCAG 1.4.4/1.4.10).
- Known platform gap: `Action.Submit`'s `isEnabled` property isn't supported in Teams, and Teams
  mobile only renders up to Adaptive Cards schema v1.6.
- Microsoft's own recommended test method is manual — validate tab order and screen-reader
  announcements with NVDA or Windows Narrator directly inside Teams, because Teams' card renderer
  has known quirks versus other Adaptive Card hosts. There's no automated gate to point at instead.

Source: [Accessibility tips for Adaptive Cards](https://learn.microsoft.com/microsoft-copilot-studio/adaptive-card-accessibility-tips).

## What a Teams implementation would actually need

- An Entra app registration and an Azure Bot resource with the Teams channel enabled, per the
  standard [Publish agents built using the Microsoft 365 Agents SDK](https://learn.microsoft.com/microsoft-365/agents-sdk/publish-agent)
  path — this hasn't changed with the M365 Agents SDK, which rides on the same Azure Bot
  Service/Teams channel transport as classic Bot Framework.
- The M365 Agents SDK Teams host wired to the **same tool set** voice and chat already share.
  `api/voice.py`'s tool-core functions (`_generate_plan_tool_core`, `_create_tenant_tool_core`,
  `_confirm_customer_consent_tool_core`, etc.) are already channel-agnostic — this is a new
  transport for existing tools, not a new surface to design from scratch.
- Carrying forward the rule this codebase already enforces for voice — "approval must never be one
  more thing an LLM can decide" (`api/voice.py`'s own module docstring) — a Teams Adaptive Card's
  Approve action would need to invoke the same HTTP approval route directly (the equivalent of
  `/v1/voice/approve`), never a bot-framework tool the model calls mid-conversation. This is the
  same boundary voice already draws between its conversational tools and its one approval route.
- Tenant-ID filtering at the application layer, since (per the networking section above) it can't
  be done at the network layer.
- The accessibility checklist above, applied to every card the Teams surface renders, plus manual
  NVDA/Narrator verification inside Teams before shipping.

## Recommendation

This is scoping, not a recommendation to build or not build — that's a product-owner decision, the
same way ADR-0004 recorded it. What's changed since ADR-0004: the WCAG authoring burden is
unchanged and real, but the networking gap now has a documented, named mitigation
(`AzureBotService` service-tag rules plus application-layer tenant filtering) rather than reading
as an unmitigated wall, and it isn't actually in tension with Groundwork's own backend-hardening
work the way ADR-0004 implied. Whether a service-tag-scoped public endpoint is an acceptable
posture for a Teams-facing surface, and whether the WCAG authoring and manual-testing effort is
worth taking on for Phase 2, are the two open questions for whoever picks this up next.
