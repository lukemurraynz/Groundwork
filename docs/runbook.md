# Groundwork Platform: Operator Runbook

**Audience**: on-call operator (likely the repo owner).
**Scope**: service health, halted deployments, recovery decisions, zone failure, Fabric gotchas.
**Not in scope**: releasing a new version; see `docs/release-checklist.md`.

---

## 1. Service overview

Two AKS deployments run in the `groundwork` namespace, both using workload identity (no secrets).

| Deployment | Label | What it does |
|---|---|---|
| `controlplane` | `app=controlplane` | FastAPI service. Accepts inbound calls (voice, chat, REST). Plans, validates, costs, seals, and approves deployments. Writes to Cosmos. |
| `orchestrator` | `app=orchestrator` | Worker process. Runs the queue-consumption loop (`engine/queue_loop.py`). Picks up queued deployments, executes the stage sequencer, triggers customer ADO pipelines. |

The two services have distinct managed identities and distinct Cosmos containers. The orchestrator
must never import from `groundwork_controlplane`: enforced by the import-boundary test.

---

## 2. Health and scale checks

```powershell
# Current pod names (change on every azd deploy)
kubectl get pods -n groundwork -l app=orchestrator -o wide
kubectl get pods -n groundwork -l app=controlplane -o wide
```

```powershell
# Readiness and restart counts
kubectl describe pod -n groundwork -l app=orchestrator
kubectl describe pod -n groundwork -l app=controlplane
```

**Readiness semantics**: pods are not ready until their startup probe passes. A pod stuck in
`0/1 Running` for more than a few minutes failed startup validation: almost always a missing
required environment variable (services fail fast at startup on any missing config key).

```powershell
# Logs for the last 200 lines
kubectl logs -n groundwork -l app=orchestrator --tail=200
kubectl logs -n groundwork -l app=controlplane --tail=200
```

**Alert names** (all in `infra/modules/observability.bicep`, fire to action group `ag-groundwork-<resourceToken>`):

| Alert | Severity | Meaning |
|---|---|---|
| `alert-gw-deployment-failures-<token>` | 1 | More than 2 `deployment_failed` events in 15 minutes |
| `alert-gw-availability-slo-<token>` | 1 | Request surface (auth, plan, approval, status) below 99.9% in 30 minutes |
| `alert-gw-queue-depth-<token>` | 2 | Queue depth above 20 sustained over 30 minutes |
| `alert-gw-stage-duration-breach-<token>` | 2 | Any single stage attempt over 600 seconds in 15 minutes |
| `alert-gw-dependency-unavailability-<token>` | 2 | Outbound Azure/ADO dependency failure rate above 10% in 15 minutes |

The availability SLO alert covers auth, plan, approval, and status (GET) routes only. In-flight
deployment progression is held to durable resumability (FR-030), not uptime; a pod restart does
not violate the SLO.

**Configure receivers**: the action group ships with no email or webhook receivers. Add them
post-deployment in Azure Portal (or `az monitor action-group update`): no operator address
belongs in source control.

---

## 3. Reading Cosmos state from a pod

The established methodology (the project's internal handoff notes (not included in this release) §6) is to run a script inside a running pod via stdin,
using the pod's own workload identity. Do not try to connect from outside the cluster.

```powershell
# Save this as a local .py file, then pipe it in
Get-Content "<local-script.py>" -Raw | kubectl exec -i <orchestrator-pod-name> -n groundwork -- python3 -
```

**Minimal deployment-status script** (paste into a `.py` file first):

```python
import asyncio, os


async def main():
    from azure.cosmos.aio import CosmosClient
    from azure.identity.aio import DefaultAzureCredential
    from groundwork_orchestrator.state.cosmos import CosmosStateStore
    from groundwork_orchestrator.state.repositories import (
        deployment_repository,
        stage_record_repository,
    )

    credential = DefaultAzureCredential()
    client = CosmosClient(os.environ["GROUNDWORK_COSMOS_ENDPOINT"], credential=credential)
    store = CosmosStateStore(client)
    repo = deployment_repository(store)
    stage_repo = stage_record_repository(store)

    tenant_id = "<tenant-id>"
    deployment_id = "<deployment-id>"

    d = await repo.read(tenant_id, deployment_id)
    print("status:", d.status, "current_stage:", d.current_stage)
    print("checkpoint:", d.checkpoint, "lease:", d.lease)

    async for r in stage_repo.query(
        tenant_id, f"SELECT * FROM c WHERE c.deployment_id = '{deployment_id}'"
    ):
        print(r.stage_name, r.status, r.idempotence_outcome, r.started_at, r.ended_at, r.error)

    await client.close()
    await credential.close()


asyncio.run(main())
```

`GROUNDWORK_COSMOS_ENDPOINT` is already set in the pod's environment. Use
`deployment_repository()` and `stage_record_repository()`. Do not construct
`TenantScopedRepository` directly.

---

## 4. Halted-deployment triage

A `status=halted` deployment stopped because a stage's failure was not classified as transient, or
the transient retry budget was exhausted. Nothing was torn down automatically.

### 4.1 Classify the failure

Run the Cosmos script above. The most-recently-ended `DeploymentStageRecord` with `status=failed`
is the halting record. Read `r.error` for the `code` and `message`.

**Transient vs permanent** (from `engine/retry.py`):

| Classification | Signal |
|---|---|
| Transient | HTTP 408, 429, 500, 502, 503, 504; `httpx.TransportError`; `azure.core.ServiceRequestError/ResponseError` |
| Permanent | Everything else — including every named `*StageError` (domain errors retry with identical inputs and fail identically) |

If the stage was classified transient but budget is exhausted, the error message will reflect the
final attempt. Budget exhaustion is itself a permanent halt.

### 4.2 Recovery options by stage

`engine/halt.py`'s `recovery_options_for_stage` is the single source of truth. What the API
actually offers per stage:

| Stage / pseudo-stage | Available actions |
|---|---|
| `what_if_preview` | `retry`, `forward_fix` |
| `policy_preflight` | `retry`, `forward_fix` |
| `cost_reapproval` | `retry` (requires a fresh distinct `approvalId`) |
| `consent_check` | `retry` (after customer re-consents) |
| `ado_org_access_check` | `retry` (after ADO org access is re-granted) |
| All real blueprint stages | `retry`, `forward_fix` (plus `rollback` where the blueprint's `recovery_path` prose says so) |

**Rollback is gated but not yet executed.** Every check before a rollback request (distinct
approval, bound to the same plan hash) is real and enforced. The execution step returns HTTP 501:
this is a disclosed scope boundary, not a silent stub. A halted deployment stays halted until
`retry` or `forward_fix` is chosen, or the human resolves the underlying issue.

### 4.3 Issue the recovery call

```bash
curl -X POST "https://<CONTROL_PLANE_HOST>/v1/deployments/<deploymentId>/recovery?api-version=2026-07-30" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action": "retry"}'
```

For `forward_fix` (operator has resolved the root cause out-of-band, e.g. fixed a quota, patched a
policy):

```bash
  -d '{"action": "forward_fix"}'
```

Both actions requeue the deployment from the last checkpoint; completed stages are not re-run.

### 4.4 When NOT to requeue immediately

- **`fabric` stage**: the `minimum_retry_interval_seconds` is 900 (15 minutes). The sequencer
  enforces this: requeuing before the interval elapses costs an ADO pipeline run that will fail
  identically at the interval check. Wait 15 minutes after the last failed fabric attempt before
  requeuing. Fabric-specific failures often stem from SPN group-membership propagation lag, which
  can take hours (see section 7).
- **`policy_preflight` false positive**: confirm whether the blocking Deny assignment actually
  applies to the plan's target region before choosing `forward_fix`. The check is conservative on
  unrecognised policy shapes.
- **`cost_reapproval`**: requires a new, distinct `approvalId`; a bare retry without one returns
  HTTP 403. The re-approval must itself carry a second approver if the error code is
  `CostReapprovalEscalationRequired`.

---

## 5. Orphaned executing deployments

An `executing` deployment whose subscription lease has expired (e.g. because its pod was killed
mid-run by a rolling update) is automatically recovered by the queue loop. On the next poll cycle,
`poll_once` queries `status='executing'` per tenant, attempts to acquire the lease, and, if
successful, requeues the deployment via `requeue_after_recovery_choice` (etag-guarded to close the
TOCTOU window). A `LeaseHeldError` means a healthy worker still holds it; leave it alone.

**If the automatic recovery fails** (logged at ERROR level, scrubbed):

```python
# Run inside an orchestrator pod
from groundwork_orchestrator.engine.halt import requeue_after_recovery_choice
from groundwork_orchestrator.state.repositories import deployment_repository

# ... (set up store as in section 3) ...
d = await repo.read(tenant_id, deployment_id)
updated = requeue_after_recovery_choice(d)
await repo.replace(tenant_id, updated)
```

`requeue_after_recovery_choice` clears `status`, `current_stage`, `started_at`, `completed_at`,
and `lease`. Do not hand-roll field clearing: that function is the single source of truth for
what a safe requeue clears.

**Before requeuing manually**: re-read the deployment with `read_with_etag` and check
`current.status` is still `executing`. Automatic recovery may have already handled it between your
check and your write.

---

## 6. Consent revocation mid-deployment

The sequencer checks `tenant.consent_state` before every stage (not just at admission). If a
customer revokes consent after a deployment starts:

**What the operator sees**: the deployment halts with `status=halted`, stage name `consent_check`,
error code `ConsentNoLongerGranted`. The message records which stage was about to run.

**What has already happened**: all stages completed before the revocation point are durable and
unchanged. No automatic teardown occurs.

**To resume**: the customer must re-grant consent via `POST .../onboarding/confirm`. Once
`consent_state` is back to `granted`, retry the deployment via the recovery API:

```bash
curl -X POST ".../v1/deployments/<id>/recovery" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"action": "retry"}'
```

The `recovery_options` for `consent_check` are `["retry"]` only; `forward_fix` is not offered
because there is nothing to forward-fix until consent is re-granted.

---

## 7. Zone loss (FR-041b)

Groundwork's own AKS cluster uses zone-redundant node pools (enforced by `scripts/preprovision.ps1`,
which requires the region to support at least three availability zones). Zone loss does not halt the cluster.

**In-flight deployment state**: each `Sequencer.run` call processes all remaining stages in one
continuous in-process call. A zone failure that kills the pod mid-run orphans the deployment at
`status=executing`. The queue loop's automatic orphan recovery (section 5) picks it up on the next
poll cycle once the lease expires.

**Resumability guarantee (FR-030)**: the deployment resumes from its last persisted checkpoint.
Completed stages are not re-run. No stage triggers a customer-facing write without first checking
idempotently whether the resource already exists (`GET`-before-write discipline).

**Alert that fires**: `alert-gw-deployment-failures-<token>` (severity 1) if a halted deployment
is recorded. `alert-gw-stage-duration-breach-<token>` (severity 2) if a stage stalls before the
pod is killed. The availability SLO alert (`alert-gw-availability-slo-<token>`) fires if the
request surface (auth, plan, approval, status) drops below 99.9% in 30 minutes; in-flight
progression is excluded.

---

## 8. Fabric-specific gotchas

These caused real failures on the first live deployment (2026-08-25).

| Issue | Detail |
|---|---|
| **Capacity name rejects hyphens** | `Microsoft.Fabric/capacities` names must be lowercase alphanumeric only. The codebase uses `fabricgw{resourceToken}` — do not rename it with hyphens. |
| **Capacity is billable immediately** | A Fabric capacity starts billing the moment ARM reports `Succeeded`. The `fabric` stage declares halt-and-preserve as the correct response to failure here — there is no automatic teardown. |
| **SPN group-membership propagation** | After adding a service principal to a Fabric tenant setting's security group, the Fabric REST API may return non-200 for up to several hours. This is documented Fabric/Entra lag, not a code defect. Wait for propagation, then requeue. |
| **Fabric workspace managed PE** | A deleted Fabric managed private endpoint cannot be recreated for at least 15 minutes (`minimum_retry_interval_seconds = 900` in the blueprint). The sequencer enforces this wait automatically. |
| **Fabric tenant settings** | "Service principals can use Fabric APIs" and "Service principals can access read-only admin APIs" must be enabled for the security group that includes the bootstrap identity. Do this in the Fabric Admin portal before the fabric stage runs. |

---

## 9. Secret-handling rules for operators

- **Never paste secrets, tokens, Cosmos connection strings, or client credentials into a log, a
  ticket, a chat message, or a kubectl exec command**. The `scrub_text` function (`groundwork_shared/
  telemetry/scrubbing.py`) exists because log lines cross multiple systems: scrubbing is in the
  code, not just a policy.
- The pod-side script pattern in section 3 uses the pod's own workload identity. There are no
  client secrets anywhere in this system. If you are being asked to paste a secret somewhere, that
  is a sign something has gone wrong.
- If you suspect a secret was exposed: rotate through Key Vault (managed identity / federated
  credential rotation, no password to reset), then open an incident record.

---

## 10. Deploying a fix (PD-004)

The only sanctioned way to update running code. Never `kubectl apply` or `kubectl set image`.

```bash
azd deploy orchestrator --no-prompt
# or
azd deploy controlplane --no-prompt
```

Both take approximately 3-4 minutes (remote ACR build + AKS rollout). Pod names change after every
deploy: re-fetch them before running any follow-up pod script.

If you deploy while a deployment is executing, the pod restart will orphan it. The queue loop will
auto-recover it on the next poll cycle once the lease expires. This is expected behaviour, not a
defect.
## 11. DR drill - controlled pod-kill during an active deployment (T111)

**Purpose**: prove, with evidence, that a worker loss mid-deployment resumes without repeating
completed stages and without tearing anything down. Run this in a maintenance window; it requires
one real (billable) deployment as the drill subject.

**Preconditions**
- A deployment is queued or executing (`kubectl get pods` + Cosmos check via section 3).
- Note the deployment id and its current stage BEFORE the kill.
- Confirm `alert-gw-drift-loop-heartbeat` and `alert-gw-queue-poll-heartbeat` are enabled so the
  exercise also validates paging.

**Procedure**
1. Start (or requeue) the subject deployment and wait until a durable stage checkpoint exists
   (stage record SUCCEEDED for at least one real stage).
2. Kill BOTH orchestrator pods simultaneously:
   ```powershell
   kubectl delete pod -n groundwork -l app=orchestrator
   ```
   This simulates a node/zone loss plus a rolling update in one action.
3. Immediately record: pod names gone, lease state in Cosmos for the deployment (expect the lease
   document left behind with an expiry in the past within ~1 hour), and whether either heartbeat
   alert fired.
4. Within one poll interval after replacement pods are Ready, the queue loop should pick the
   deployment back up from its last checkpoint. Watch for:
   - `drift_evaluation_cycle_completed` resuming on the new pods (loop liveness).
   - The deployment transitioning `executing -> queued -> executing` WITHOUT any stage that had a
     SUCCEEDED record being invoked again (verify via fresh stage records only for remaining stages).
   - No `ROLLED_BACK` status and no deletion calls anywhere.
5. Capture evidence: pod events timeline, stage-record diff before/after, alert state changes,
   and the final outcome.

**Pass criteria**: zero repeated completed stages; zero deletions; automatic recovery without
manual `requeue_after_recovery_choice`; alerts behaved per section 7.

**Fail actions**: if the deployment orphans (stays `executing` past two poll cycles after pods are
Ready), recover manually per section 5, then file the gap against the loop-recovery code path -
do NOT paper over it by weakening the checkpoint logic.
