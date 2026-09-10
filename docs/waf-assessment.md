# Well-Architected Framework Assessment: Groundwork Platform

> *Historical citations to `AGENT_HANDOFF.md` reference the original release's internal handoff
> notes, not included in this public release.*

**Date**: 2026-08-26 (original assessment) · **refreshed 2026-09-07**, 897 tests passing.
**Assessed state**: post-deployment `67f6e287`, all seven stages succeeded live; 816 tests passing
at original assessment time.
**Audience**: product owner + future reviewers deciding investment priorities.
**Scope**: Groundwork's own platform infrastructure and code. Customer tenant resources are
provisioned by the customer's own ADO pipeline; they are out of scope here except where Groundwork's
controls directly govern them (approval gating, consent gates, Lighthouse delegation boundary).
**Task**: T108 (Phase 7).

**2026-09-07 refresh**: a live end-to-end test of the full voice → plan → approval → orchestration
path found and fixed six real production bugs unrelated to this document's own findings (a
notification_email prompt/schema mismatch, a duplicated-constant drift in an Azure DevOps API
version, a missing subscription-entitlement API, and others — not itemised here since none map to
a WAF pillar this document tracks). Of *this* document's own findings: §2.7 (blob URL disclosure)
resolved; §3.6 (no CI/CD) was already resolved by the time of this refresh and the section
corrected; §2.6 (tenant enumeration) gained a detective control without full resolution — see each
section. `threat-model.md`'s T-009 (blueprint digest verification) is also now implemented,
unrelated to this specific document but from the same pass. A same-day follow-up found and wired
§4.3's `GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS` (validated at provision time, never previously read
by the queue loop) — see that section for the fix and its disclosed soft-ceiling limitation.

Each finding states severity (`Critical` / `High` / `Medium` / `Low`), evidence path(s), and an
owner placeholder. Severity reflects risk to Groundwork's own posture, not the customer tenant.

---

## 1. Reliability

### 1.1 Zone-redundant infrastructure, single-region trade-off [Medium]

AKS node pools, Cosmos DB, and Storage are all zone-redundant within a single Australian region
(`infra/modules/aks.bicep` line 151, `infra/modules/cosmos.bicep` line 60,
`infra/modules/storage.bicep` SKU `Standard_ZRS`). The preprovision script enforces a minimum of
three availability zones (`docs/release-checklist.md` step 0). This satisfies FR-041a.

**Gap**: no cross-region failover exists. FR-041a explicitly accepts single-region with
zone-redundancy; the 99.9% SLO covers the request surface only, not in-flight deployment
progression (`infra/modules/observability.bicep` line 286). A full-region outage loses the
Groundwork platform for the outage duration. Customer deployments resume from checkpoint on
recovery (FR-030), but the RPO for control-plane state is bounded by Cosmos's 7-day continuous
backup (`infra/modules/cosmos.bicep` line 74) with no automatic failover trigger.

**Owner**: product-owner

---

### 1.2 Durable state, checkpoint/resume, and orphan recovery [Strength]

`Sequencer.run` processes all stages in a single call. A pod killed mid-run orphans the
deployment at `status=executing`. The queue loop now queries `executing` deployments whose lease
has expired and requeues them via the single authoritative `requeue_after_recovery_choice`
function, guarded by an etag-checked TOCTOU window
(the project's internal implementation notes (not included in this release), `src/groundwork_orchestrator/engine/halt.py`). Completed stages
are never re-run; `GET`-before-write idempotence is enforced across all blueprint stages
(AGENT_HANDOFF §1). Verified live 2026-08-25 when rolling pod updates orphaned two in-flight runs
that auto-recovered.

**Remaining gap**: stage-level bounded timeouts do not exist. A future novel exception inside
`Sequencer.run` (outside `_run_one_stage`'s guard) could still orphan a deployment until the
lease expires naturally. AGENT_HANDOFF §4 bug #6 is structurally closed for the known case but
the general pattern is unresolved. **Owner**: engineer

---

### 1.3 Halt-and-preserve, no automatic rollback [Strength]

`engine/halt.py` reconstructs the halting record from the append-only `DeploymentStageRecord`
history; nothing is torn down automatically on failure (FR-031). `rollback` is a gated action
returning HTTP 501 (disclosed scope boundary, `docs/runbook.md` §4.2). Fabric capacity starts
billing immediately on `ARM Succeeded`; halt-and-preserve is the correct response here and is
documented (`docs/runbook.md` §8). No silent discard of billable resources.

---

### 1.4 Rolling upgrade discipline and upgrade surge bounds [Medium]

`aks.bicep` sets `maxSurge: '1'` on user node pools (lines 181, 200) and `upgradeChannel: 'patch'`
for auto-upgrade (line 219). Minor Kubernetes upgrades are reviewed changes, not drift.

**Gap**: a `azd deploy` while a deployment is executing kills its pod. This is documented as
expected behaviour (`docs/runbook.md` §10) but there is no pre-deploy check that drains or
quarantines in-flight work first. Auto-recovery handles it, but it adds unnecessary disruption.

**Owner**: engineer

---

### 1.5 No cross-region DR exercise documented [High]

the project's internal implementation notes (not included in this release) notes Phase 7 items (T108 through T115) are all deferred per
the project's single-engagement scope decision (not included in this release). A DR exercise (T115) has never been run. The recovery
path (restore from Cosmos 7-day continuous backup, re-provision from `azd`) exists in principle
but is untested. Given single-region deployment, a full-region outage has no tested recovery runbook.

**Owner**: product-owner

---

## 2. Security

### 2.1 Secretless workload identity throughout [Strength]

Both AKS services authenticate via federated credentials bound to Kubernetes service accounts
(`infra/modules/aks.bicep` lines 250-266). Cosmos `disableLocalAuth: true` and
`disableKeyBasedMetadataWriteAccess: true` (`infra/modules/cosmos.bicep` lines 51-52). Storage
`allowSharedKeyAccess: false` (`infra/modules/storage.bicep` line 47). App Insights
`DisableLocalAuth: true` (`infra/modules/observability.bicep` line 62). Key Vault holds no
application credentials (`infra/modules/keyvault.bicep` lines 1-7). AKS `disableLocalAccounts: true`
(line 141). No client secrets exist anywhere in the system.

---

### 2.2 Schema boundary fail-closed [Strength]

Model output crosses into orchestration only as a schema-validated `groundwork_contracts` object.
`tests/contract/test_plan_boundary.py` is the deterministic-execution-boundary guard; `tests/security/test_prompt_injection_planning.py`
drives attacker-influenced model output through the real `PlanningAgent.generate_plan` path and
asserts schema-rejection-or-baseline-equivalence across 8 attack classes including subscription
redirection, approval-claim injection, and secret-exfiltration patterns
(the project's internal implementation notes (not included in this release) wave-2 note). Import-boundary test
(`tests/unit/test_import_boundaries.py`) enforces that the orchestrator cannot import the
control-plane package, preventing lateral trust escalation.

---

### 2.3 Approval gating and distinct second-approver requirement [Strength]

No writes to a customer tenant without an approval bound to a specific plan identity. Approval
gating is enforced before every rollback or cost-reapproval path; `cost_reapproval` requires a
fresh distinct `approvalId` (`engine/halt.py` lines 44-47, `docs/runbook.md` §4.4). Tested in
`tests/security/test_approval_gate.py` and `tests/security/test_cost_escalation.py`.

---

### 2.4 Voice-channel session-strength gap, corrected from an earlier "caller-identity" framing [High]

**Corrected 2026-09-06.** This entry originally described the gap as Groundwork being unable to
confirm "who is speaking," in caller-ID/telephony language, and rated it Critical on that basis. A
live grep found `telephony.py` (the ACS Call Automation inbound handler) is never imported or
routed anywhere in this codebase: voice shipped as the authenticated web frontend only (ADR-0004),
and every WebSocket session requires a validated Entra bearer token in its first frame before any
connection opens. There is no unauthenticated caller reaching this endpoint; the original claim
that a caller "can trigger planning and approval without a verified identity" was not accurate.

The real gap, narrower than originally stated: once an authenticated WebSocket session is open, a
single spoken utterance is sufficient to approve an irreversible action, with no check that the
token holder is still the one speaking (a session/token-strength question, not an identity one).
`approval/service.py` already ships a compensating control for exactly this
(`_require_step_up_authentication`, gated by `GROUNDWORK_REQUIRE_STEP_UP_APPROVAL`, refusing
approval without MFA-in-`amr` or a token issued in the last 10 minutes), applied uniformly to every
approval channel including voice: as found, it defaulted to off, and nothing in this document,
ADR-0011, or the threat model credited it before now. **Resolved the same day**: the default is
now on (`settings.py`), wired into both k8s deployment templates, opt-out per environment via
`azd env set GROUNDWORK_REQUIRE_STEP_UP_APPROVAL false`. Downgraded from Critical to High rather
than closed: this is a token-evidence gate, not continuous re-verification of who is speaking, so
a session that presented MFA once still passes for its full token lifetime.

**This was the highest-risk item for a system whose primary approval surface is voice; the default
now closes it for any deployment that doesn't deliberately opt out**; see
ADR-0011's own 2026-09-06 correction for the full account.

**Owner**: product-owner (requires design decision before any code change)

---

### 2.5 Multi-tenant inbound JWT validation now wired [Strength]

`MultiTenantTokenDecoder` routes tokens by unverified `tid` peek to per-tenant JWKS verifiers.
`TokenPolicy.allowed_issuers` is seeded at startup from home + every onboarded tenant
(the project's internal implementation notes (not included in this release) §9, item 11). Covered by `tests/security/test_inbound_auth.py`
and `tests/unit/test_multi_tenant_auth.py`. Previously a critical gap; shipped 2026-08-25.

---

### 2.6 Operator-scoped tenant enumeration oracle, now with a detective control [High]

The Lighthouse onboarding endpoint (`api/lighthouse_onboarding.py`) returns RFC 9457 404 for an
unknown tenant and 503 for an unconfigured ARM credential. A 404 vs 200 response pattern discloses
whether a given tenant GUID is onboarded to anyone holding a valid operator token. `CallerRole.OPERATOR`
is a broad, non-tenant-scoped role used identically by every other tenant-lookup route in this
codebase (including the canonical `GET /v1/tenants/{tenant_id}`), so narrowing this one endpoint's
response shape would not close the same oracle — the same information is already available via the
canonical route. Left at High because a compromised operator token or insider misuse can still
enumerate every customer relationship this way; the real fix is a tenant-scoped-operator trust
model, which is a genuine architecture change, not a quick pre-showcase pass.

**Added**: a WARNING log line (`lighthouse onboarding facts requested for unknown tenant_id=... by
operator=...`) fires on every unknown-tenant probe against this endpoint, giving an alert rule
something real to fire on (§3.3's own precedent) — a detective control, not a preventive one.
`tests/contract/test_tenants_endpoint.py::test_lighthouse_onboarding_unknown_tenant_logs_a_detective_signal`.

**Owner**: engineer (tenant-scoped operator roles, if pursued)

---

### 2.7 Blob URLs are time-bounded at response time [Strength]

**Resolved.** `engine/preview.py`, `state/report_archive.py`, and `channels/voice/consent.py` still
persist the blob client's plain, deterministic `.url` into their durable records — correctly, since
those records outlive any reasonable SAS TTL (a report carries 365-day retention; a rotating URL
baked into a permanent field would go stale long before the record does). What changed: the three
routes that ever echo one of these URLs to a caller (`api/reports.py`'s `blobUri`,
`api/approvals.py`'s `artefactUri`, `api/deployments.py`'s `whatIfArtefactUri`, also reused by
`api/recovery.py`) now mint a fresh, read-only, 24-hour Entra user-delegation SAS at response time
via `groundwork_shared.storage.sas.read_only_sas_url`, instead of echoing the permanent URL
unbounded. Secretless: the SAS is delegation-based, never an account key.
Best-effort by design — a transient Storage/Entra error falls back to the unchanged permanent URL
rather than turning a security enhancement into an outage of an otherwise-working read
(`tests/unit/test_sas.py`).

---

### 2.8 Immutable artefact containers enforced by policy [Strength]

`reports`, `approvals`, `previews`, and `consent` containers have time-based immutability policies
with `allowProtectedAppendWrites: false` and 365-day lifecycle deletion
(`infra/modules/storage.bicep` lines 109-154). Overwriting a finished report is architecturally
impossible, not a convention. `tests/integration/test_report_immutability.py` covers this.

---

### 2.9 Voice consent REST-twin gap (xfail) [Medium]

`tests/security/test_voice_consent_gate.py` exists but the voice consent REST twin (a non-voice
path to record the same consent) is not fully exercised under the same gate. The xfail marker
records an acknowledged gap. `channels/voice/consent.py` writes consent artefacts to the immutable
container but the REST path parity is incomplete.

**Owner**: engineer

---

### 2.10 Secret scrubbing wired in telemetry [Strength]

`src/groundwork_shared/telemetry/scrubbing.py` redacts sensitive key-value pairs and patterns
(SAS URLs, bearer tokens, UUIDs treated as tenant IDs) from all log/trace output, with visible
`[REDACTED:kind]` markers rather than silent removal. `tests/security/test_secret_scrubbing.py`
covers the scrubbing logic. `docs/runbook.md` §9 instructs operators never to paste secrets into
kubectl commands or incident tickets.

---

### 2.11 No private endpoints; every data-plane resource is publicly reachable [High]

Cosmos DB, Key Vault, Foundry, Container Registry, Speech, and Storage all set
`publicNetworkAccess: 'Enabled'` (`infra/modules/cosmos.bicep` line 79,
`infra/modules/keyvault.bicep` line 62, `infra/modules/foundry.bicep` line 63,
`infra/modules/registry.bicep` line 48, `infra/modules/speech.bicep` line 48,
`infra/modules/storage.bicep` line 51); Log Analytics sets both
`publicNetworkAccessForIngestion` and `publicNetworkAccessForQuery` to `Enabled`
(`infra/modules/observability.bicep` lines 49-50). No `Microsoft.Network/privateEndpoints`
resource exists anywhere under `infra/modules/`. Network exposure of every one of Groundwork's own
data-plane resources is authorization-only (Entra RBAC, Key Vault access policies, workload
identity), not network-boundary-defended.

Notably, the *customer-facing* blueprints Groundwork provisions do this better than Groundwork
provisions itself: `infra/blueprints/standard-production-fabric/main.bicep` builds a dedicated
`snet-private-endpoints` subnet and wires private endpoints (line 138 onward) for the platform it
deploys into a customer's tenant. The pattern exists in this codebase; it just isn't applied to
Groundwork's own control plane and orchestrator.

**Gap**: a leaked credential or an over-broad RBAC assignment is reachable from the public
internet, not contained behind a VNet boundary. This section was absent from earlier versions of
this assessment despite the rest of §2 being self-critical — added 2026-09-08.

**Owner**: product-owner

---

## 3. Operational Excellence

### 3.1 azd-only deployment discipline enforced [Strength]

PD-004 prohibits `kubectl apply` or `kubectl set image`. `docs/release-checklist.md` and
`docs/runbook.md` §10 both state this explicitly. The preprovision hook (`scripts/preprovision.ps1`)
enforces region, AZ count, and provider registration before every provision.

---

### 3.2 Runbook and release checklist exist and are command-verified [Strength]

`docs/runbook.md` (T112) covers service overview, health checks, Cosmos state inspection,
halted-deployment triage, orphan recovery, consent revocation, zone loss, Fabric gotchas, and
deployment discipline. `docs/release-checklist.md` (T113) is a 10-step gate covering clean tree,
tagging, full test suite, environment selection, provision preview, provision, deploy, post-deploy
verification, validation gate runs, and deployment record. Both were command-verified on
2026-08-26 (the project's internal implementation notes (not included in this release)).

---

### 3.3 Alert rules wired to real emitted signals [Strength]

Five alert rules in `infra/modules/observability.bicep` (lines 124-314) query real custom metrics
and events: `deployment_failed` from `AppTraces`, `groundwork_queue_depth` and
`groundwork_stage_duration_seconds` from `AppMetrics`, dependency success rate from
`AppDependencies`, and availability per-mille from `AppRequests`. The observability module comment
(line 197-205) explicitly records the gap that existed before: earlier rules queried metrics
nothing emitted. The current set was built together with their emitters.

**Gap**: action group receivers are intentionally empty in the template
(`infra/modules/observability.bicep` line 115). No email or webhook is configured until a human
adds one post-deployment. An environment without configured receivers is silently deaf to all
alerts.

**Owner**: product-owner (requires an operator address to be nominated)

---

### 3.4 Test suite breadth [Strength]

816 tests across `unit/` (80+ files covering every stage, repository, telemetry module, and
contract boundary), `contract/` (12 files covering every API endpoint), `security/` (14 files),
`resilience/` (3 files: no-auto-rollback, resume, zone-loss), `idempotence/`, `integration/`,
and `accessibility/`. Prompt-injection corpus (`tests/security/test_prompt_injection_planning.py`)
runs attacker payloads through the real planning agent. Import-boundary enforcement is a separate
gate in the release checklist.

**Gap**: T030 (plan-readonly live integration test against a real deployed environment) is open;
tests currently run against mocked Azure surfaces for the live-round-trip cases. T059-T064
(dedicated idempotence/resilience/isolation/concurrency suites) exist as files but were written
without a real Azure target.

---

### 3.5 Structured logs and correlation IDs [Strength]

`src/groundwork_shared/telemetry/correlation.py` provides correlation ID injection.
`src/groundwork_shared/telemetry/otel.py` configures OpenTelemetry with Azure Monitor export via
workload identity (no instrumentation key). Every outgoing httpx/Azure SDK call is auto-instrumented
as an `AppDependencies` entry. `tests/unit/test_correlation.py` covers the ID threading.

---

### 3.6 CI pipeline gates every push and PR [Strength]

**Resolved since this assessment.** `.github/workflows/ci.yml` runs `ruff format --check`,
`ruff check`, `pytest`, and `mypy` on every push to `main` and every PR — a failed step is now
caught before merge, not discovered by the deploying person mid-checklist. Deployment itself
remains azd-only by design (PD-004; §3.1), so this pipeline never deploys anything; it closes the
gap this section originally flagged (no automated test→build gate) without weakening the
deploy-only-via-azd discipline that gap sat next to. Mypy runs non-blocking outside
`groundwork_contracts` against a documented pre-existing baseline (`ci.yml`'s own header comment) —
that residual gap is real but small and already disclosed at the point a reviewer would find it.

---

### 3.7 Single-operator deployment record is a manual audit trail [Medium]

Step 10 of the release checklist writes a deployment record to a human-maintained document or
notes file. There is no automated deployment record. The git tag is the only link between running
code and source (`docs/release-checklist.md` line 52). Tag push is a manual step; no enforcement
prevents deploying untagged code.

**Owner**: product-owner

---

## 4. Performance Efficiency

### 4.1 5-second queue poll cadence and per-subscription serialisation ceiling [Medium]

`engine/queue_loop.py` polls every few seconds. All tenants are iterated each cycle. The
per-subscription serialisation invariant (FR-045b, enforced by `subscription_leases` container
with 1-hour TTL) means only one deployment runs per subscription at a time. For a customer with
multiple concurrent provisioning requests, earlier requests block later ones serially. This is a
deliberate design constraint (CL-010), not an oversight, but it bounds throughput for any
single subscription to one deployment at a time.

**Owner**: product-owner (accepted design constraint; worth revisiting at scale)

---

### 4.2 Pipeline polling budget in `infrastructure` and `fabric` stages [Medium]

`stages/infrastructure.py` and `stages/fabric.py` trigger customer ADO pipeline runs and poll to
completion. Poll intervals and maximum wait budgets are bounded by `minimum_retry_interval_seconds`
(900s for `fabric`, `docs/runbook.md` §4.4) but per-stage wall-clock caps are not enforced in
code. A stuck customer pipeline can hold the subscription lease for its full lease TTL (1 hour,
`infra/modules/cosmos.bicep` line 156) before the orphan recovery path reclaims it.

**Owner**: engineer

---

### 4.3 Single-worker sequential Sequencer runs [Medium]

`Sequencer.run` processes every stage in one continuous in-process call in a single async task.
There is no parallelism within a deployment. All stages for one deployment execute serially on one
worker — `_attempt_deployment` (`engine/queue_loop.py`) awaits a queued deployment's entire
`Sequencer.run` to completion before its own pod's poll loop even looks at the next one, so real
cross-tenant concurrency comes only from multiple orchestrator pod *replicas* each running an
independent `run_forever` loop. At the current scale (one Groundwork environment, one customer
subscription in testing) this is not a bottleneck. At multi-tenant scale, a Sequencer call
occupying the executor pool for a long-running deployment (infrastructure + fabric can exceed 30
minutes) starves other tenants from getting a worker slot.

**Resolved in part 2026-09-07**: `GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS` was required at provision
time and validated into settings but never actually read anywhere — found live while explaining
this exact section to a reviewer. `poll_once` now enforces it as a platform-wide ceiling on
deployments EXECUTING at once, on top of the existing per-tenant cap (`concurrency_cap`, admission
time) and per-subscription lease (execution-time serialisation). This bounds how much of the
starvation risk above can materialise at once; it does not remove the underlying single-worker
constraint itself, and it is a *soft* ceiling — each pod's own count starts from a real, shared
Cosmos read but two replicas racing the same poll cycle could together exceed it by a small margin
(documented in the function's own docstring). A real distributed semaphore would be the next step
if this ever needs to be an exact limit rather than an operational safety valve.

**Owner**: engineer (soft-vs-hard ceiling revisit, if genuinely needed) / product-owner (whether
true per-deployment parallelism is worth building)

---

### 4.4 No load test exists [High]

T110 (load test) is deferred (the project's internal implementation notes (not included in this release), Phase 7). Throughput ceilings,
latency under concurrent planning requests, and AKS autoscaler behaviour under load are all
uncharacterised. Alert thresholds in `infra/modules/observability.bicep` are explicitly noted as
starting points to be tuned against real telemetry.

**Owner**: product-owner (T110 prerequisite: a synthetic load-generation harness)

---

### 4.5 F2 minimum Fabric SKU at fixed billing floor [Low]

Blueprint Fabric capacity uses `fabricgw{resourceToken}` naming and an F2 minimum SKU
(`docs/runbook.md` §8). F2 starts billing immediately on ARM success. There is no SKU
right-sizing based on actual workspace demand; F2 is the minimum available SKU. At low utilisation
this is the correct floor choice.

---

## 5. Cost Optimization

### 5.1 Billable-capacity consent gating and cost re-check at execution time [Strength]

`engine/cost_preflight.py` re-checks estimated cost at deployment time (FR-020). A cost overrun
since plan approval triggers a `cost_reapproval` halt requiring a new distinct `approvalId`
(`engine/halt.py` lines 44-47). Fabric capacity billing starts immediately on `ARM Succeeded`;
halt-and-preserve is the only response on failure past that point. Licensing disclosure gate
tested in `tests/unit/test_licensing_disclosure.py`.

---

### 5.2 No auto-teardown; orphan RG cleanup is manual [Medium]

Halt-and-preserve is the correct response to mid-deployment failure. However, this means failed
partial deployments leave billable Azure resources in the customer tenant with no automatic cleanup
path. The orphan RG `rg-groundwork-c16273638565` (detached duplicate resources from the 2026-08-25
bring-up) was verified unreferenced on 2026-08-26 but has not been deleted pending owner approval
(the project's internal implementation notes (not included in this release)). At scale, half-provisioned customer environments accumulate
without a notification or sweep mechanism.

**Owner**: product-owner

---

### 5.3 Sponsorship-credit fragility [Low]

AGENT_HANDOFF §9 item 8 and the project's internal implementation notes (not included in this release) note that the live environment runs
on a subscription with specific tenant/CDX constraints. No cost guardrails (budget alerts,
spending limits) are applied to Groundwork's own subscription in the IaC. If sponsorship credits
expire, billing converts to pay-as-you-go without warning.

**Owner**: product-owner

---

### 5.4 Cosmos autoscale floor at 4000 RU/s [Low]

`infra/modules/cosmos.bicep` line 29 defaults `maxThroughput` to 4000 RU/s. Autoscale bills a
minimum of 10% = 400 RU/s idle cost. At current scale (one active deployment at a time), 1000 RU/s
maximum (billing floor: 100 RU/s idle) would likely be sufficient and would reduce idle cost by 4x.
No utilisation data exists yet to confirm this.

**Owner**: engineer

---

### 5.5 Separate node pools prevent executor-starvation, at a cost floor [Low]

Three AKS node pools (system, controlplane, executor) with `Standard_D4s_v5` nodes and minimum
counts of 3/2/2 (`infra/modules/aks.bicep` lines 66-88) represent a fixed cost floor of 7 nodes
minimum. The separation is required by FR-045c (executor load must not starve planning). At low
utilisation the cluster runs near its minimum node count. No cluster autoprovisioner or spot-node
configuration exists; scale-down to zero is not possible with the current pool structure.

**Owner**: engineer

---

## Prioritised Top-5 Follow-up Table

| Priority | Finding | Pillar | Severity | Recommended action | Owner |
|---|---|---|---|---|---|
| 1 | **Voice-channel session-strength gap** (§2.4) | Security | High | Resolved 2026-09-06: `GROUNDWORK_REQUIRE_STEP_UP_APPROVAL` now defaults on. Remaining watch point: it's a token-evidence gate, not continuous re-verification — decide whether that's sufficient for a real customer engagement. | product-owner |
| 2 | **No cross-region DR exercise** (§1.5) | Reliability | High | Run at least one documented Cosmos backup-restore exercise against a non-production environment; write a DR runbook section in `docs/runbook.md`. This is T115, currently deferred. | product-owner |
| 3 | **No load test** (§4.4) | Performance Efficiency | High | Implement T110: a synthetic load harness that exercises concurrent planning requests and multi-tenant queue behaviour; use results to tune the five alert thresholds in `observability.bicep`. | product-owner |
| 4 | **Action group receivers unconfigured** (§3.3) | Operational Excellence | Medium | Nominate an operator address (email or webhook) and apply it post-provision. Without receivers, all five alert rules are silent. Add a check to the release checklist (step 8). | product-owner |
| 5 | **No private endpoints on Groundwork's own data-plane resources** (§2.11) | Security | High | Add private endpoints + VNet integration for Cosmos, Key Vault, Foundry, ACR, Speech, and Storage, following the pattern `infra/blueprints/standard-production-fabric/main.bicep` already uses for customer tenants. Set `publicNetworkAccess: 'Disabled'` once done. Newly added 2026-09-08; §2.7 (permanent blob URL disclosure) was resolved 2026-09-07 and rotates out of this slot, and §2.6's tenant-scoped-operator trust model remains the next candidate after this. | product-owner |
