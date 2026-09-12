# Groundwork Platform: Release Checklist

**Applies to**: every `azd provision` or `azd deploy` against any non-local environment.
**Authority**: PD-004: `azd` delivers Groundwork. Never `kubectl apply`. Never deploy uncommitted code.
**Why this exists**: `azd` delivers Groundwork with no CI/CD pipeline behind it (ADR-0003), so
these steps are the compensating controls a pipeline would otherwise enforce automatically.

---

## Pre-flight gates (automated; run by `azd provision` hook)

`scripts/preprovision.ps1` runs automatically before every provision. It blocks if any check fails.
You do not need to run it manually, but you do need to read its output before proceeding.

The checks it enforces:

| Check | Remediation if it fails |
|---|---|
| `AZURE_LOCATION` is set | `azd env set AZURE_LOCATION eastus2` |
| `AZURE_SUBSCRIPTION_ID` is set | `azd env set AZURE_SUBSCRIPTION_ID <id>` |
| `GROUNDWORK_APPROVAL_THRESHOLD_AUD` is set | `azd env set GROUNDWORK_APPROVAL_THRESHOLD_AUD 1000` |
| `GROUNDWORK_APPROVER_ROLE` is set | `azd env set GROUNDWORK_APPROVER_ROLE Groundwork.Approver` |
| `GROUNDWORK_TENANT_CONCURRENCY_CAP` is set | `azd env set GROUNDWORK_TENANT_CONCURRENCY_CAP 3` |
| `GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS` is set | `azd env set GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS 3` |
| Region is in the `@allowed` list on `infra/main.bicep`'s `location` parameter | Pick a supported region, or narrow the list if you have a data-residency requirement (see ADR-0008) |
| Azure CLI is authenticated | `az login` |
| Region supports at least 3 availability zones | FR-041a — choose a different region otherwise |
| All 8 resource providers registered | `az provider register --namespace <provider>` for each failing one |

The four `GROUNDWORK_*` values have no IaC default on purpose (a default here could point at the
wrong tenant). Without this gate, the first `azd up` into a fresh environment provisions the whole
platform successfully and only then discovers the gap, as a `controlplane`/`orchestrator`
`CrashLoopBackOff` during `azd deploy`. This check turns that into a five-second failure before
any Azure resource exists.

If preprovision fails, stop. Do not run `azd provision` with `--skip-hooks`.

---

## Step 1: clean tree

**Rule**: `azd` deploys whatever is on disk, including uncommitted edits.

```powershell
git status
```

**Pass criterion**: output is empty (or shows only untracked files that are not source code).
If there are staged or unstaged changes, either commit them or stash them before continuing.

**Also check**:

```powershell
git log --oneline -5
```

Confirm the HEAD commit is the one you intend to release.

---

## Step 2: tag the release

**Rule**: without a pipeline, the git tag is the only link between running code and source.

```powershell
git tag v<version>   # e.g. v1.0.0
git push origin v<version>
```

**Pass criterion**: `git tag --list "v<version>"` shows the tag, and it points to HEAD.

Record the tag in your deployment notes before proceeding.

---

## Step 3: full test suite

**Rule**: nothing else stops a failing build from reaching Azure.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

**Pass criterion**: all tests pass, zero failures, zero errors.

Also run the import-boundary test explicitly:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_import_boundaries.py -q
```

**Pass criterion**: 0 failures. `groundwork_orchestrator` must not import `groundwork_controlplane`.

Record the test count and result in your deployment notes.

---

## Step 4: select the target environment

```powershell
azd env list
azd env select <env-name>   # e.g. groundwork-prod-2
```

**Pass criterion**: `azd env list` shows the selected environment as active, and
`.azure/<env-name>/.env` exists and is not committed.

Confirm `AZURE_LOCATION` and `AZURE_SUBSCRIPTION_ID` are correct for this environment:

```powershell
azd env get-values | Select-String "AZURE_LOCATION|AZURE_SUBSCRIPTION_ID"
```

---

## Step 5: provision preview

**Rule** (the gated-approval rule, PD-004): read the what-if before every provision. This is the reviewable
preview for Groundwork's own infrastructure.

```powershell
azd provision --preview
```

**Pass criterion**: read the output in full. Confirm:

- No unexpected resource deletions.
- No resources outside the expected resource group.
- Changes match what the release is intended to do.

Keep the preview output with your deployment notes. If the preview shows something unexpected,
stop and investigate before proceeding.

---

## Step 6: provision

```powershell
azd provision
```

This runs `scripts/preprovision.ps1` (pre-flight checks) and `scripts/postprovision.ps1`
(Entra app registration, cert-manager, ingress setup) automatically via `azure.yaml` hooks.

**Pass criterion**: `azd provision` exits 0, and the `postprovision.ps1` output shows:

- Entra application found or created, `appId` recorded.
- App roles applied.
- `access_as_user` OAuth2 scope applied.
- Service principal found or created.
- Operator app-role grant succeeded (or a warning explaining why it was skipped).
- Ingress controller IP resolved, cert-manager installed, `letsencrypt-prod` ClusterIssuer applied.
- `GROUNDWORK_ENTRA_APP_CLIENT_ID`, `GROUNDWORK_ENTRA_APP_AUDIENCE`, `GROUNDWORK_PUBLIC_URL` set in the environment.

If `postprovision.ps1` prints any `Write-Warning` lines, read them before continuing: some
(e.g. failed SPA redirect URI) will cause runtime failures.

---

## Step 7: deploy services

Deploy one service at a time. The orchestrator worker's queue loop starts as soon as the pod is
ready. Deploy it last if you want to minimise the window where a new control plane is paired
with an old orchestrator.

```powershell
azd deploy controlplane --no-prompt
azd deploy orchestrator --no-prompt
```

Each takes approximately 3-4 minutes (remote ACR build + AKS rollout).

**Pass criterion** for each:

```powershell
kubectl get pods -n groundwork -l app=controlplane -o wide
kubectl get pods -n groundwork -l app=orchestrator -o wide
```

All pods `Running` with `READY 1/1`. If any pod is stuck in `0/1`, check logs:

```powershell
kubectl logs -n groundwork -l app=controlplane --tail=100
kubectl logs -n groundwork -l app=orchestrator --tail=100
```

A startup failure is almost always a missing required environment variable: the service fails
fast with a clear error message naming the missing key.

---

## Step 8: post-provision verification

Confirm the control-plane health endpoint responds:

```powershell
$host = azd env get-value GROUNDWORK_PUBLIC_URL
curl -s "https://$host/health/ready" | ConvertFrom-Json
```

**Pass criterion**: HTTP 200, body indicates healthy.

Confirm telemetry is flowing (wait 2-3 minutes after deploy):

```powershell
az monitor log-analytics query \
  --workspace "$(azd env get-value AZURE_LOG_ANALYTICS_WORKSPACE_ID)" \
  --analytics-query "AppTraces | where TimeGenerated > ago(5m) | count" \
  --output table
```

**Pass criterion**: row count greater than 0. If zero after 5 minutes, workload identity ingestion
is broken: check Monitoring Metrics Publisher role assignment on the App Insights resource.

**If `enableNetworkHardening` is set on this environment** (`docs/waf-assessment.md` §2.11): the
five Network Security Perimeter resource associations (Key Vault, Storage, Cosmos, Foundry,
Speech) ship in `Learning` mode — they log what would be allowed or denied without blocking
anything yet. This is not a one-time step but a recurring follow-up: review the NSP diagnostic
logs (Log Analytics, `NetworkSecurityPerimeterAccessLogs` category) for a representative period
after each provision that touches these resources, confirm nothing unexpected is being denied,
then flip the associations you've reviewed to `accessMode: 'Enforced'` in
`infra/modules/network-security-perimeter.bicep` as a deliberate, reviewed change — never as a
default.

---

## Step 9: validation gates

```powershell
.\.venv\Scripts\python.exe -m pytest tests/contract -q
.\.venv\Scripts\python.exe -m pytest tests/security -q
```

**Pass criterion**: all pass. `tests/contract` is the deterministic-execution-boundary guard: malformed model output
must fail closed at the schema boundary. If any contract test fails, the release is not safe.

If you have a disposable customer-like test subscription (not a real customer tenant):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/idempotence tests/resilience -q
```

**Pass criterion**: all pass. These tests write to the target subscription; never point them at a
real customer subscription.

---

## Step 10: record the deployment

**Rule**: record who deployed, when, and which tag (quickstart.md control 6).

Write a deployment record with:

| Field | Value |
|---|---|
| Date/time (UTC) | |
| Deployer | |
| Git tag | |
| Environment | |
| Test count at deploy | |
| `azd provision --preview` output | (attach or link) |
| Any warnings from postprovision | |

This record is the audit trail. There is no automated pipeline record.

---

## Rollback / abort path

**If `azd provision` fails mid-way**: the Bicep deployment is transactional at the resource-group
scope. ARM will roll back the failed deployment automatically. Run `azd provision` again (with
`--preview` first) once the root cause is fixed.

**If a service fails to start after `azd deploy`**: roll back to the previous image by redeploying
from the previous tagged commit:

```powershell
git checkout v<previous-version>
azd deploy <service> --no-prompt
git checkout v<new-version>   # or wherever you were
```

**If you need to tear down the environment entirely**:

```powershell
azd down --purge
```

`--purge` is required. Without it, soft-deleted Key Vaults and Foundry resources block the next
`azd provision` in the same environment.

**Never**: `kubectl delete`, `kubectl apply`, `kubectl set image`. Cluster state must come from
`azd` alone (PD-004). Direct kubectl changes produce a running service that exists in no commit
and will be overwritten on the next deploy without warning.
