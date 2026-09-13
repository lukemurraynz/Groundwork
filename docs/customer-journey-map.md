# Customer Journey Map: Groundwork, First Engagement (Onboarding Through Steady-State Operation)

## Executive Summary

- Two friction points are already named and documented in this codebase (`AADSTS65001` at
  token acquisition during onboarding, and `step-up-authentication-required` at approval), which
  means the team has already felt them, but hasn't yet fixed them at the product-experience level
  (only in a doc callout). **Correction below: `AADSTS65001` hits a Groundwork operator, not the
  customer** — see the next bullet.
- **Stage 2 (Onboarding) previously mis-attributed the four-call onboarding sequence, and the
  `AADSTS65001` pain point riding on it, to the customer.** `docs/first-tenant-walkthrough.md`'s
  own text says otherwise for "a real engagement": the four calls (`create_tenant`,
  `confirm_customer_consent`, `record offshore-inference consent`, `enable voice`) are made by a
  Groundwork operator attesting to actions the customer completed separately, outside Groundwork's
  system entirely, in their own Entra admin center and Azure DevOps org ("For your own tenant, you
  *are* the admin" only applies to self-service dev-tenant onboarding — a *real* engagement has the
  operator attesting on the customer's behalf). A 2026-09-13 audit of the voice channel's
  authorization independently confirmed this: `create_tenant`, `confirm_customer_consent`,
  `grant_ado_org_access`, `trigger_bootstrap_identity`, and `quick_onboard` all require
  `CallerRole.OPERATOR` — a `Groundwork.*` app role, not something an external customer's token
  carries. The customer's real Stage 2 actions are narrower and happen entirely outside Groundwork:
  complete the admin-consent redirect, run the Lighthouse delegation command, optionally grant
  Azure DevOps Project Collection Administrator access. `AADSTS65001` is friction a Groundwork
  operator hits acquiring a properly-scoped token for those four attesting calls; the customer
  never sees the error, and feels it only indirectly, as onboarding taking longer than expected if
  an operator stalls on it. Stage 2, the Critical Moments table, and the Pain Point Prioritisation
  table below are corrected accordingly; the underlying Quick Win (turn the doc callout into an
  in-product error or CLI helper) is unchanged, but it is operator-tooling/DevEx work, not a
  customer-drop-off fix.
- **Correction, 2026-09-13: rollback execution is real, not a `501`.** This map previously
  carried a "trust breaker" critical moment and a top-ranked pain point built on ADR-0005's
  original text (rollback gated but not executed). ADR-0009 (2026-08-26, implemented same day)
  supersedes that: a fully approved rollback request now redeploys the last known-good
  configuration through the customer's own Azure DevOps pipeline. ADR-0005 itself had not been
  corrected to say so until this same audit fixed it. On permanent stage failure, the orchestrator
  still halts and preserves partially-built, billable infrastructure in the customer's tenant
  (that part of ADR-0005 is unchanged) — but the customer has three real recovery options
  (retry, forward-fix, rollback), not two real ones and a disclosed gap. The halt notification now
  states this explicitly (Quick Win, **implemented** 2026-09-13 — see Recommended Improvements).
  Residual risk, smaller than previously stated: partial, billable infrastructure still accrues
  cost for as long as the customer takes to choose a recovery path.
- A working notification path exists (Azure Communication Services Email, `NotificationDispatcher`).
  Delivery is guaranteed by an approval-time gate: `record_approval` refuses (409,
  `notification-email-missing`) until a recipient is recorded on the tenant, and the operator
  mutation path (`POST /v1/tenants/{id}/notification-email`) exists to record it outside the
  conversational flow — the silent-skip failure is closed. Drift detection
  (`engine/drift_watch.py`) still runs every 15 minutes with no code path to the customer at all;
  that wiring remains a cheap, reuse-not-rebuild fix.
- The clearest "aha" moment by design (seeing a full plan and AUD cost estimate before anything
  touches Azure) is a differentiator (ADR-0001) worth protecting as the product grows.
- No customer interviews, support tickets, or usage analytics exist yet for this journey (the ADRs
  behind this map are dated August–September 2026 and read as pre-GA/design-partner stage).
  Confidence throughout is Low–Medium; treat this as a hypothesis-led first pass to validate against
  a real cohort, not a finished CX artefact.

The sections below define the scope and actors, walk through each stage of the journey with evidence and metrics, and close with prioritised improvements.

## Scope and Scenario

| Item | Detail |
|---|---|
| Persona / segment | **Tenant Approver** — the customer-side platform administrator (or delegated cloud engineer) who completes their organisation's side of bringing Groundwork into their Azure tenant (admin consent, Lighthouse delegation, optional Azure DevOps access — see Stage 2's Actor note for who runs the corresponding Groundwork-side attestation), requests a deployment by voice/chat/REST, and approves the resulting plan. See `## Persona and Job To Be Done`. |
| Job to be done | Get a governed, production-ready enterprise data platform running in our own Azure tenant, without hand-building it or waiting on a platform-engineering queue, while staying in control of what actually gets approved and staying inside our residency/compliance boundaries. |
| Start point | The organisation recognises it needs a new enterprise data platform and is introduced to Groundwork as the way to request one (voice, chat, Teams, portal, REST, or CLI — `docs/product-specification.md` "Supported channels"). |
| End point | The requested platform is live in the customer's tenant, ownership of further Fabric changes has explicitly passed to the customer's own Azure DevOps pipeline (the literal handover copy in `notify/dispatcher.py`'s `_OWNERSHIP_NOTICE`), and Groundwork continues in steady-state, read-only drift-watch mode. |
| Context | Enterprise/regulated customer; Microsoft Entra ID authentication; may use voice, chat, REST, or CLI today — the web portal and Teams app are Phase 2 roadmap items (`product-specification.md`), not yet built; only a reference `voice.html` client exists in `src/groundwork_controlplane/static/`. |
| Exclusions | Groundwork's own deployment onto its own AKS cluster (`azd up`) — that is an operator/internal journey, not this customer journey. The PSTN telephony leg (`telephony.py`) — confirmed unwired and unrouted (ADR-0011), so it is out of scope until it ships. Phase 3 "autonomous platform operations" (self-healing, AI-assisted scaling, compliance remediation) — roadmap only, not built. |
| Success criteria | **Customer**: deployment completes in under an hour with zero manual infra configuration and full visibility into what was approved. **Business**: 95% first-run deployment success, reduced onboarding cost (`product-specification.md` Success metrics). **Operational**: every tenant-affecting action traces to an explicit, attributable approval — no unattended writes to a customer tenant. |

## Evidence Summary

| Source | What it suggests | Confidence |
|---|---|---|
| `docs/product-specification.md` | Target users, deployment workflow phases, spec-level notification channel list (voice callback, email, Teams, SMS, portal, REST) | Medium — describes intent; several channels aren't built yet (see below) |
| `docs/first-tenant-walkthrough.md` | Exact four-call onboarding sequence and its enforced ordering (consent before voice enablement); explicitly documents the `AADSTS65001` token-acquisition gotcha; its own text states the four calls are made by a Groundwork operator attesting to actions the customer completed separately, for "a real engagement" (self-service dev-tenant onboarding, where operator and customer are the same person, is the stated exception) | High — direct, current operational doc |
| `docs/adr/0011-voice-alone-authorises-irreversible-actions.md` | Voice can authorise an irreversible action from a single utterance once the session is authenticated; step-up (MFA-or-fresh-token) gate now defaults on (`GROUNDWORK_REQUIRE_STEP_UP_APPROVAL=true`, fixed 2026-09-06) | High — code- and settings-verified in the ADR itself |
| `docs/adr/0005-halt-and-preserve-rollback-501.md` | On permanent stage failure, the orchestrator halts and preserves state. Its original "recovery endpoint returns `501` by design" text is superseded by [ADR-0009](../../docs/adr/0009-proposal-rollback-via-customer-pipeline.md) (2026-08-26, implemented same day): rollback execution is real, redeploying the last known-good configuration through the customer's own Azure DevOps pipeline. ADR-0005 was corrected in-place 2026-09-13 to state this; every gating rule it describes (distinct approval, plan-hash-bound, etc.) is unchanged. | High — explicit accepted decisions, cite source modules directly |
| `src/groundwork_shared/notify/dispatcher.py` | Real, working outcome notification via ACS Email only; Teams delivery not attempted (blocked on a separate SDK decision); silently skips sending if `notification_email` was never captured; ships the literal ownership-handover copy | High — read directly from the implementation and its docstring |
| `src/groundwork_orchestrator/engine/drift_watch.py`, `api/readiness_reports.py` | Periodic (15-minute) readiness/drift re-evaluation exists and is real; it writes to a repository and metrics only — no call into `NotificationDispatcher` was found; the only customer-facing surface is a pull-based JSON/HTML report endpoint | High for "drift watch exists and doesn't notify" (verified by reading the module and grepping for a notify call); Medium for "customers actually feel this as a gap" (inferred, not yet validated) |
| `docs/runbook.md`, `docs/waf-assessment.md` | Groundwork's own operator alerting ships with zero action-group receivers by default, added manually post-deploy | High — but this is an internal/operator risk, not a customer-facing journey step (see Risks) |

## Assumption Register

| Assumption | Why it matters | Confidence | Validation method |
|---|---|---|---|
| The Tenant Approver is a single person who both requests and approves a standard deployment (the distinct-identity rule only bites for a *second* approval on higher-risk plans) | Shapes whether Approval reads as one continuous moment or a handoff between two people | Medium | Interview 3–5 design-partner customers on who actually holds each role |
| Most customers reach Groundwork through chat/REST first, not voice, because voice today is an authenticated web client (`voice.html`), not a phone line, and needs three extra onboarding steps before it will even connect | Changes which channel's friction (token acquisition vs. voice enablement ordering) is felt first | Medium | Channel-of-first-contact analytics once a cohort exists |
| The "days or weeks" manual baseline in the product spec's problem statement is a general industry claim, not a number measured against this journey's own customers | Affects how strongly the Push force (`## Forces of Progress`) should be weighted | Low | Discovery interviews asking each design partner what their prior process actually took |
| Emotion scores below are inferred from documented, code-verified friction points (403s, 501s, silent skips), not from customer quotes, tickets, or surveys — none exist yet for this pre-GA product | The whole emotion column is a hypothesis, not measured CX | Low | First design-partner cohort: support-ticket thematic review + short post-deployment survey |
| Real customer engagements go through a Groundwork-operator-mediated onboarding (the four attesting API calls run by Groundwork staff, per `first-tenant-walkthrough.md`'s "real engagement" text), rather than design-partner customers self-onboarding their own dev tenant via `quick-onboard` (where operator and customer are the same person) | Determines whether `AADSTS65001` and the Stage 2 token-acquisition friction reach the customer directly today, or only Groundwork's own operators | Low | Check how the actual design-partner cohort onboarded — self-service `quick-onboard` vs. a Groundwork operator running the four-call sequence on their behalf |

## Persona and Job To Be Done

- **Persona**: Tenant Approver, a platform administrator or delegated cloud engineer inside the customer organisation, acting with Entra-authenticated identity and an Approver/Requester app role.
- **Goals**: get a governed data platform running fast, without ceding control over what actually executes against the tenant.
- **Motivations**: reduce manual provisioning cost and lead time; avoid configuration drift and inconsistent builds across environments.
- **Constraints**: enterprise governance requirements (RBAC, approval trails, data residency); may need a second, distinct approver for higher-cost or higher-risk plans.
- **Success definition**: an approved plan becomes a running platform inside an hour, with an auditable record of exactly what was approved and by whom.
- **Job to be done**: make progress on standing up production data-platform infrastructure without hiring or queuing for a platform-engineering team, while staying inside the organisation's compliance and residency boundaries.
- **Push** (status quo isn't working): manual provisioning is slow, inconsistent, and error-prone, driven by multiple approval hops and repetitive deployment work (`product-specification.md`, Problem statement).
- **Pull** (attractive about Groundwork): a conversational request turns into a priced, validated plan in under a minute (spec NFR), and an approved plan becomes running infrastructure in under an hour, with no hand-written Terraform/Bicep.
- **Anxiety** (risk of switching): an AI agent is involved in a request that ends with changes to our production Azure tenant. Will it do something nobody approved? Voice alone can authorise an irreversible action once a session is open (ADR-0011); in-call audio may be processed outside the data-residency region under per-tenant consent (PD-005).
- **Habit** (attachment to current way): existing hand-built landing zones and an existing platform team relationship, which at least fail in ways the organisation already understands.

## Journey Map

| Stage | Customer goal | Touchpoints | Customer actions | Thoughts and questions | Emotion | Pain points | Root causes | Opportunities | Evidence | Metrics |
|---|---|---|---|---|---|---|---|---|---|---|
| **1. Trigger** | Recognise the need for a governed data platform and find a faster way to get one | Word of mouth, sales/CS conversation, internal mandate | Compares current manual provisioning cost/timeline against Groundwork's pitch | "Could this actually be safe to point at our own tenant?" | Frustrated with status quo (-1) | Manual provisioning is slow and inconsistent | Multiple approval hops, repetitive manual deployment work, human config error (`product-specification.md` Problem statement) | Lead with the plan-before-execution design (ADR-0001) as the trust argument, not just the speed one | `product-specification.md` Problem statement (Medium — describes the market claim, not this journey's own baseline) | Time from first contact to first tenant-creation call |
| **2. Onboarding** | Have the org's admin-consent, delegation, and access grants completed so Groundwork can be trusted with the tenant | Entra admin-consent redirect, Lighthouse delegation command, Azure DevOps PCA grant — all outside Groundwork's own system | Completes the admin-consent redirect, runs the Lighthouse delegation command in their own tenant, and (if applicable) grants Groundwork Project Collection Administrator access in Azure DevOps — three actions taken entirely outside Groundwork; does **not** call Groundwork's tenant/onboarding API directly (see Actor note below) | "Did I do my part right? How will I know when this is actually ready?" | Neutral, mildly anticipatory (0) while waiting for confirmation | No proactive signal back to the customer that their three steps were seen and recorded, until an operator separately runs the matching attestation call | The four attesting API calls (`create_tenant`, `/onboarding/confirm`, offshore-inference consent, voice-channel enable) are a distinct, operator-run step decoupled in time from the customer's own actions — nothing today notifies the customer when an attestation lands | Notify the customer (reusing the existing `NotificationDispatcher`) as each of their three completed actions gets attested, instead of leaving them to assume it happened silently | `docs/first-tenant-walkthrough.md` (High — see Actor note); RBAC audit 2026-09-13 (`CallerRole.OPERATOR` required on all four attesting calls) | Time between the customer completing their three actions and the corresponding operator attestation landing |
| **3. Requesting & Planning** | Turn a natural-language ask into a priced, validated plan | Voice/chat/Teams/portal conversation with the Planning Agent | Describes the desired platform; answers clarifying questions (e.g., notification email, region) | "Does it actually understand what I asked for? What will this cost?" | Cautiously confident (+1) once plan and cost appear | None evidenced yet at this stage | — | Reinforce that nothing has touched Azure yet — this is the moment to sell "plan first, execute never without you" | `product-specification.md` Phase 1–2, ADR-0001 (Medium) | Time from first message to plan+cost surfaced (spec target: <60s) |
| **4. Approval** | Review and authorise the plan with confidence | Approval prompt over voice/chat/API; token-based auth | Confirms the plan; if the caller's token doesn't show MFA or was issued >10 min ago, must re-authenticate | "I approved this — why did it just reject me with `step-up-authentication-required`?" | Frustrated/anxious at first rejection (-2), relieved after re-auth (+1) | Step-up MFA/fresh-token requirement (on by default since 2026-09-06) isn't surfaced until the approval call itself fails | `GROUNDWORK_REQUIRE_STEP_UP_APPROVAL` defaults `true`; the approver has no visibility into their own token's `amr` claim or age before submitting | Pre-check the caller's token claims at the start of the approval prompt and tell them to re-authenticate before they submit, not after a 403 | `docs/adr/0011-...` (High), `README.md` (calls this out as an unexpected-403 case) | First-attempt approval failure rate due to `step-up-authentication-required` |
| **5. Provisioning** | Watch the approved plan become running infrastructure | Deployment status (`GET` route), Cosmos-backed checkpoint state | Waits, optionally polls status | "Is this still running? How would I know if it stalled?" | Neutral, mildly anxious while waiting (-1) | No evidence of proactive progress push beyond a pull-based status route | Deployment can run up to the ~60-minute NFR target with only a poll-based status check | Consider a mid-run progress notification (stage N of M) reusing the existing notification channel | `product-specification.md` NFRs (Medium) | Deployment success rate on first execution (spec target: 95%); `alert-gw-stage-duration-breach` rate |
| **6. Completion & Handover** | Confirm success and understand what happens next | Outcome email (ACS Email via `NotificationDispatcher`) | Reads the completion email, including the explicit ownership-handover line | "Good, it worked. Who owns changes to this from here?" | Confident/relieved (+2) when notified; **unaware and exposed** if not | `notify_deployment_outcome` silently skips sending (only a log warning) if `CustomerTenant.notification_email` was never captured/confirmed | `notification_email` is gathered conversationally (FR-002/FR-004b) but isn't a hard precondition of approval — its absence fails silently, not loudly | Make `notification_email` a blocking precondition of approval, not an optional field that fails silently later | `src/groundwork_shared/notify/dispatcher.py` (High) | % of completed deployments with a recorded `notification_email` and a confirmed send |
| **7. Ongoing Operation** | Trust that the platform stays healthy without having to babysit it | Periodic drift re-evaluation (`engine/drift_watch.py`, every 15 min); pull-based readiness report (`GET /v1/tenants/{id}/onboarding/readiness-report`) | Would need to actively pull the readiness report to learn about drift; otherwise finds out only if something visibly breaks | "Is anyone watching this, or do I have to keep checking?" | Neutral, mildly exposed (-1) | Drift is detected on a schedule but there is no code path from a drift verdict to the customer — only to a repository and internal metrics | `drift_watch.py` was built to "emit a signal without mutating customer state"; wiring that signal to the customer was out of scope when it shipped | Wire blocking drift verdicts into the existing `NotificationDispatcher` (reuse the email channel already built for FR-051; no new channel needed) | `src/groundwork_orchestrator/engine/drift_watch.py`, `api/readiness_reports.py` (High for the gap existing; Medium for customer-felt impact) | Mean time between a blocking drift verdict and the customer being notified (today: unbounded/never until they pull the report) |

> **Actor note (Stage 2)**: the four onboarding-attestation API calls (`create_tenant`,
> `/onboarding/confirm`, offshore-inference consent, voice-channel enable) are made by a Groundwork
> operator, not the customer — `docs/first-tenant-walkthrough.md` states this directly for "a real
> engagement" ("For your own tenant, you *are* the admin" applies only to self-service dev-tenant
> onboarding, where operator and customer are the same person), and a 2026-09-13 RBAC audit
> confirmed all four calls require `CallerRole.OPERATOR`, a `Groundwork.*` app role no external
> customer token carries. The operator's own experience of this step, including the `AADSTS65001`
> token-acquisition friction, is real and worth fixing (see the Critical Moments and Pain Point
> Prioritisation tables below), but it is an internal onboarding-tooling problem, not a customer
> journey step — it reaches the customer only indirectly, as their onboarding taking longer than
> expected if an operator stalls on it. The one design-partner exception is self-service dev-tenant
> onboarding (the walkthrough's own primary scenario and its `quick-onboard` shortcut), where the
> customer's own admin *is* the operator and would feel `AADSTS65001` directly — worth confirming
> against the first real cohort (see Validation Plan) rather than assumed either way.

> **Loop note**: Stage 7 is where a request for additional capability, an upgrade, or an expansion would re-enter the journey at Stage 3 (Requesting & Planning) for a new plan. The spec's Phase 2/3 roadmap (multi-region, upgrades, AI-assisted scaling) extends this loop but isn't built yet, so it's recorded here as a future loop-back, not a shipped capability.

### Sub-map: Provisioning Failure Branch (materially different, scenario sweep)

This branch diverges enough from the happy path (different actions, a different emotional trajectory, and a decision the customer has no real control over) that it is broken out here rather than folded into Stage 5 above, per the scenario-sweep rule.

| Stage | Customer goal | Touchpoints | Customer actions | Thoughts and questions | Emotion | Pain points | Root causes | Opportunities | Evidence | Metrics |
|---|---|---|---|---|---|---|---|---|---|---|
| 5a. Stage failure & halt | Understand what broke and what happens to what was already built | Halt notification (`_mark_halted` → `NotificationDispatcher`) | Reads the halt notification | "What state is my tenant in right now?" | Anxious (-2) | Partially-built, billable infrastructure stays in the tenant until a human decides what to do | `denySettings.mode: denyDelete` on the Deployment Stack protects resources from deletion by design; no automatic teardown is attempted | State the halt clearly and immediately, including that partial infrastructure remains and is billable | `docs/adr/0005-...` (High) | Time from halt to customer notification |
| 5b. Recovery decision | Get the deployment moving again or undo it | `POST /deployments/{id}/recovery` | Chooses `retry`, `forward_fix`, or `rollback` — the halt notification (fixed 2026-09-13) already told them all three are real options | "Which one gets me back on track fastest?" | Neutral, deciding (0) — the halt notification already set expectations correctly | None evidenced as a customer-facing gap since the halt-notification fix; `rollback` redeploys the last known-good configuration through the customer's own Azure DevOps pipeline (ADR-0009) rather than instantly undoing anything, which a customer expecting an instant teardown could still find slower than hoped | Rollback's execution model (redeploy via the customer's own pipeline, not a teardown) is real but not instant — worth confirming customers understand this from the notification alone, not just that rollback "works" | Consider naming the expected rollback duration (a pipeline run, not an instant action) in the halt notification, so "how long will this take" doesn't become a new support question | `docs/adr/0009-...` (High) | Time from recovery-choice submission to the chosen path's completion, split by `retry`/`forward_fix`/`rollback` |
| 5c. Resume | Get back on the happy path | Requeue via `retry` or `forward_fix` | Confirms the chosen path | "Will this pick up where it left off, or start over?" | Recovering (0 to +1) | None evidenced beyond 5b | `requeue_after_recovery_choice` resumes from the last checkpoint, not from scratch | Say this explicitly in the recovery response so the customer isn't afraid of a full re-run | `docs/adr/0005-...` (High) | Resume success rate after `retry`/`forward_fix` |

**Scenario sweep classification**: Primary happy path, in scope (main table above). Failure cascade / recovery path, in scope, represented as this separate sub-map. State-transition path (halted → resumed), in scope, folded into 5c. Data-lag/incomplete-data path, out of scope for this pass; no evidence gathered on stale-data scenarios (e.g., a plan validated against a tenant state that changes before approval), flagged as a gap in the Validation Plan.

## Critical Moments

| Moment | Stage | Why it matters | Evidence/confidence | Action |
|---|---|---|---|---|
| Aha moment | 3. Requesting & Planning | Seeing a full plan and AUD cost estimate before anything touches Azure is the concrete proof of the "plan first, execute never without you" pitch | `docs/adr/0001-...` (High) | Protect this moment explicitly in any future redesign; don't let plan review get compressed into a rubber-stamp step |
| Moment of truth | 4. Approval | First time governance actually challenges the approver (step-up gate) rather than just describing itself | `docs/adr/0011-...` (High) | Pre-check token freshness/MFA before submission, not after a rejection |
| Resolved (was: trust breaker) | 5b. Recovery decision (failure branch) | Previously: discovering "rollback" returned `501` when invoked. Corrected 2026-09-13 — rollback is real (ADR-0009) and the halt notification (fixed the same day) now states all three recovery options proactively, before the customer calls the endpoint | `docs/adr/0009-...` (High) | Watch point, not a fix: confirm customers understand rollback redeploys via their own pipeline (not instant) rather than assuming "works" means "immediate" |
| Drop-off risk | 2. Onboarding | A Groundwork operator without CLI-savvy could stall a customer engagement at the `AADSTS65001` token step before the customer's own three onboarding actions are ever attested — an operator-onboarding-tooling risk, not a direct customer drop-off, except in the self-service dev-tenant case where the customer's own admin is the operator (see Actor note) | `docs/first-tenant-walkthrough.md` (Medium — documented as a known issue, but no abandonment data exists; actor attribution corrected 2026-09-13) | Turn the doc callout into an in-product error message or a one-line CLI helper |
| Delight moment | 6. Completion & Handover | The completion email states plainly who owns what next ("further Fabric changes run through your own Azure DevOps pipeline") instead of leaving the customer to guess | `src/groundwork_shared/notify/dispatcher.py` `_OWNERSHIP_NOTICE` (High) | Keep this explicit ownership line in any future notification redesign |

## Pain Point Prioritisation

Priority = (Customer Impact + Business Impact + Risk of Doing Nothing) × Confidence ÷ Effort (High=3, Medium=2, Low=1).

| Rank | Pain point | Stage | Customer impact | Business impact | Effort | Confidence | Risk of doing nothing | Priority | Owner |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Notification silently skipped when `notification_email` wasn't captured | 6. Completion & Handover | H | H | L | H | M | 24 | Control plane — Quick win |
| — | ~~Rollback offered as a choice but not disclosed as unavailable before the customer tries it~~ — **resolved 2026-09-13**: rollback is real (ADR-0009), and the halt notification now proactively states all three recovery options (see Recommended Improvements → Quick Wins) | 5b. Recovery decision | — | — | — | — | — | — | Orchestrator / Customer Success — Done |
| 3 | Step-up MFA rejection surfaces only after approval submission | 4. Approval | M | M | L | H | M | 18 | Control plane — Quick win |
| 4 | Drift detected but never pushed to the customer | 7. Ongoing Operation | M | M | L | M | M | 12 | Orchestrator — Near-term |
| 5 | `AADSTS65001` token-acquisition gotcha at onboarding (hits the Groundwork operator performing the attestation calls, not the customer directly — see Actor note; customer impact is indirect, via onboarding delay, except in self-service dev-tenant onboarding) | 2. Onboarding | M | M | M | H | M | 9 | DevEx / Operations — Near-term |

Rollback execution itself (the underlying capability, not the messaging gap above) is not scored here as a pain point. It is high-effort, dependency-laden engineering work (denyDelete reconciliation) rather than a fix ranked against the others. It is carried below as a Strategic Investment.

The next section translates these pain points into concrete recommendations, grouped by effort level.

## Recommended Improvements

### Quick Wins

| Recommendation | Customer outcome | Business outcome | Metric | Owner | Next step |
|---|---|---|---|---|---|
| Make `notification_email` a blocking precondition of approval instead of an optional field that fails silently at send time | Customer is guaranteed to hear about their deployment's outcome | Fewer "did my deployment finish?" support tickets | % completed deployments with a confirmed notification send | Control plane | **Implemented**: `record_approval` raises `NotificationEmailMissingError` (409) when absent; `POST /v1/tenants/{id}/notification-email` records it outside the conversational flow |
| Disclose the real recovery options (`retry`, `forward-fix`, `rollback`) in the halt notification itself, instead of leaving the customer to discover them by calling the recovery endpoint | Customer knows their options and that partial infrastructure is preserved, not deleted, the moment they're notified of a halt | Fewer "what do I do now?" support escalations after a halt | % of halted deployments where the customer's first recovery action matches what the notification suggested | Orchestrator / Customer Success | **Implemented** 2026-09-13: `notify/dispatcher.py`'s `build_notification_content` now adds a recovery-options notice for `HALTED` outcomes, naming all three options and stating rollback redeploys via the customer's own Azure DevOps pipeline (ADR-0009) — not that it's unavailable |
| Pre-check the approver's token claims (MFA/freshness) before they submit an approval, and prompt re-auth up front | Approver isn't rejected after already committing to approve | Fewer failed-then-retried approval calls | First-attempt approval failure rate on `step-up-authentication-required` | Control plane | Surface the check in the approval prompt across voice/chat/API |

### Near-Term Improvements

| Recommendation | Customer outcome | Business outcome | Metric | Owner | Next step |
|---|---|---|---|---|---|
| Wire `drift_watch.py`'s blocking verdicts into the existing `NotificationDispatcher` | Customer learns about configuration drift without having to pull the readiness report | Fewer undetected-drift incidents reaching support | Mean time between blocking drift and customer notification | Orchestrator | Add a notify call at the point a `DriftVerdict` crosses the blocking threshold |
| Turn the `AADSTS65001` doc callout into an in-product error or CLI helper | Faster engagement start (the customer's own three actions get attested sooner); direct benefit only in self-service dev-tenant onboarding, where the customer's own admin hits this directly | Fewer stalled operator onboarding attempts; lower time-to-first-attestation | % of operator onboarding attempts needing the doc/a ticket for token acquisition | DevEx / Operations | Detect the CLI's default-app-registration failure and return a specific, actionable message |

### Strategic Investments

| Recommendation | Customer outcome | Business outcome | Metric | Owner | Dependency |
|---|---|---|---|---|---|
| ~~Design and build rollback execution compatible with the Deployment Stack's `denyDelete` protection~~ — **shipped 2026-08-26** (ADR-0009): rollback redeploys the last known-good configuration through the customer's own Azure DevOps pipeline, which never fights `denyDelete` because it redeploys prior state rather than deleting | Customer can actually undo a halted deployment, not just retry or forward-fix it | Reduces financial exposure from stranded partial infrastructure | % halted deployments resolved via rollback | Orchestrator | Done — this ADR erroneously listed it as a future investment until this map's 2026-09-13 correction |
| Build the customer web portal and Teams app from the Phase 2 roadmap | Plan review and status checks stop depending on raw REST calls or the `voice.html` reference client | Broader addressable customer base beyond CLI/API-comfortable users | Portal/Teams adoption once shipped | Product / Frontend | Phase 2 roadmap prioritisation |

### Research or Validation Needed

| Hypothesis | Method | Data/sample needed | Success signal | Decision rule |
|---|---|---|---|---|
| Most first-time approvers hit the step-up `403` at Approval, and most first-time onboarders (Groundwork operators, or self-service design-partner customers acting as their own operator — see Actor note) hit `AADSTS65001` at token acquisition | Instrument the first-run funnel; review support tickets and first-cohort interviews | First N design-partner onboarding sessions | >30% of first-time users/operators hit one of the two | If confirmed, prioritise the step-up Quick Win pre-GA and the `AADSTS65001` Near-Term fix on whichever cadence matches actual operator/onboarding volume |
| Customers without a portal/Teams integration under-notice deployment outcomes because email is the only working channel | Support-ticket thematic review after first GA cohort | 4–6 weeks of post-launch tickets | Repeated "did my deployment finish?" tickets | If confirmed, prioritise unblocking Teams delivery (currently blocked on an M365 Agents SDK decision) |
| Design-partner customers self-onboard via `quick-onboard` (operator = customer, so `AADSTS65001` is a direct customer pain point today) rather than going through a Groundwork-operator-mediated four-call sequence | Review how the actual design-partner cohort onboarded (support tickets, deployment logs, or a direct question in the next design-partner call) | First N design-partner onboarding records | Most design-partner tenants created via `quick-onboard` by an identity matching the customer's own domain, vs. via the four-call sequence by a Groundwork-identity caller | If confirmed, re-rank the `AADSTS65001` pain point as directly customer-facing and move it back into customer-facing Quick Wins |

## Metrics and Instrumentation

| Stage | KPI | Baseline needed | Data source | Owner | Expected direction |
|---|---|---|---|---|---|
| 2. Onboarding | % operator onboarding attempts needing doc/support for token acquisition (customer-facing only in self-service dev-tenant onboarding); time between customer completing their three actions and operator attestation landing | Not yet measured | Support tickets, doc analytics | DevEx / Operations | Decrease |
| 3. Requesting & Planning | Time from first message to plan + cost surfaced | Spec target: <60s | OpenTelemetry traces | Control plane | Decrease / hold under 60s |
| 4. Approval | First-attempt approval failure rate (`step-up-authentication-required`) | Not yet measured | `approval/service.py` telemetry | Control plane | Decrease |
| 5. Provisioning | Deployment success rate on first execution | Spec target: 95% | `alert-gw-deployment-failures`, `alert-gw-stage-duration-breach` | Orchestrator / SRE | Increase toward 95% |
| 6. Completion & Handover | % completed deployments with confirmed notification sent | Not yet measured | `NotificationDispatcher` logs/telemetry | Control plane | Increase to 100% |
| 7. Ongoing Operation | Mean time between blocking drift detected and customer notified | Not yet measured (no path exists today) | `drift_watch.py` + future dispatcher wiring | Orchestrator | Decrease (from "never" today) |

## Validation Plan

| Hypothesis | Method | Sample/data | Success signal | Decision rule | Owner |
|---|---|---|---|---|---|
| The Tenant Approver persona (single person, both requester and approver) matches most real engagements | Customer interviews | 3–5 design-partner customers | Consistent role pattern across interviews | If most engagements split requester/approver, add a second persona row and revisit Stage 4 | Product |
| First-time users hit the two documented "surprise" moments | Funnel instrumentation + ticket review | First N onboarding sessions | >30% hit rate | Prioritise the related Quick Wins pre-GA | Product / Support |
| Drift-detection gap is felt as a real customer problem, not just an architectural gap | Post-deployment survey + support-ticket review | First GA cohort, 4–6 weeks | Customers report finding drift issues on their own before Groundwork tells them | Prioritise wiring drift notifications | Customer Success |
| Data-lag/incomplete-data path (tenant state changes between validation and approval) | Path analysis / support-ticket review | Not yet scoped — flagged gap from the scenario sweep | Evidence of plans approved against stale validation | Add a dedicated sub-map if confirmed material | Control plane |

## Risks, Dependencies, and Open Questions

| Type | Item | Impact | Owner | Next action |
|---|---|---|---|---|
| Risk | Halted deployments leave billable, partially-built infrastructure in a customer's tenant until the customer chooses a recovery path (retry, forward-fix, or rollback — all three real, ADR-0009); the cost exposure window is now bounded by customer decision time, not by a missing capability | Medium — financial exposure only while undecided, no longer a capability gap | Product / Orchestrator | Halt-messaging Quick Win shipped 2026-09-13; watch whether customers reliably act quickly once notified |
| Risk | Voice can authorise an irreversible action from a single spoken utterance once a session is authenticated, with no re-verification that the token holder is still speaking (ADR-0011 residual gap) | Medium–High — security | Security | Treat as a precondition before widening voice's authority further, per ADR-0011 |
| Dependency | Teams notification delivery is blocked on a separate M365 Agents SDK / app-registration decision | Medium — limits notification reach to email only | Conversational platform | Resolve the blocking decision referenced in `dispatcher.py` |
| Dependency | Action group for Groundwork's own operator alerts ships with zero receivers by default (deliberate, to keep addresses out of source control) | Medium — operator risk, not directly customer-facing, but a live environment could run unmonitored if the manual step is skipped | SRE / Platform | Confirm this manual step is enforced in `docs/release-checklist.md` |
| Open question | Does the Phase 2 web portal/Teams app land soon enough to change how Ongoing Operation and Completion & Handover should be designed, or should those stages optimise for email + REST for now? | Medium — affects near-term vs. strategic prioritisation | Product | Check against the actual Phase 2 roadmap timing |
| Open question | This map is built entirely from documentation and code, with no interviews, tickets, or analytics behind it | High — the whole map's confidence ceiling | Product / Customer Success | Run the Validation Plan above against the first real cohort and refresh this map |

## Visualisation Notes

- Recommended visual layout: horizontal stages Trigger → Onboarding → Requesting & Planning → Approval → Provisioning → Completion & Handover → Ongoing Operation, with the Provisioning Failure Branch drawn as a vertical fork off Provisioning rather than inline.
- Swimlanes: Customer actions, Emotion curve, Pain points/Opportunities, Channel (voice / chat / REST / CLI).
- Emotion curve: mostly flat-to-positive, with a mild, mostly-waiting dip at Onboarding (Stage 2 is neutral for the customer since the `AADSTS65001` friction sits with the Groundwork operator, not the customer — see Actor note), the sharpest dip of the whole map at Approval (step-up 403), and the failure branch's own deepest point now at 5a (halt/anxious, -2) rather than 5b — the rollback-`501` discovery that used to be the deepest point of the whole map is resolved (ADR-0009; see Critical Moments).
- Colour/tagging: tag each Opportunity Quick win / Near-term / Strategic / Validate with a consistent colour so the roadmap view can be lifted straight from the Recommended Improvements tables.
- Workshop notes: bring in whoever wrote `first-tenant-walkthrough.md` and ADR-0005/ADR-0011. They are the closest thing this map currently has to direct customer-facing evidence, and can confirm or correct the inferred emotion scores before this goes further.
