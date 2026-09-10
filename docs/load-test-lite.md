# Queue loop load test lite

## Scope

This is a local, in-memory replay of the orchestrator queue loop only.

- Harness: `scripts/benchmarks/queue_replay.py`
- Command shape: `.venv\Scripts\python.exe scripts/benchmarks/queue_replay.py --tenants N --deployments-per-tenant M`
- Dependencies exercised: `poll_once`, tenant-scoped repositories, sealed-plan lookup, lease acquire/release, fake credential scoping, fake sequencer completion
- Dependencies deliberately excluded: Cosmos DB latency, Azure SDK/network latency, Azure DevOps/Fabric/pipeline stage work, AKS scheduling, telemetry export cost

The harness copies the minimal fake-container and lease-container patterns from `tests/unit/test_queue_consumption.py`, seeds queued deployments and sealed plans entirely in memory, then runs repeated `poll_once` cycles until no queued or executing deployments remain.

Injectable clocks are respected with a stepped in-memory clock; no wall-clock timestamps drive state transitions.

## Method

For each run:

1. Seed `N` tenants.
2. Seed `M` queued deployments per tenant.
3. Seed one sealed plan per deployment.
4. Run `poll_once` until the fake repositories contain no `queued` or `executing` deployments.
5. Record wall-clock duration around the replay loop.

Each deployment uses a unique subscription id, so this measures the queue loop's happy-path ceiling without lease contention. The fake sequencer marks each deployment `succeeded` immediately after `poll_once` hands it off, so all measured time is queue-loop/repository/lease overhead, not stage execution time.

## Measured results

Actual local runs on this repository checkout:

| Tenants | Deployments / tenant | Total deployments | Cycles | Wall-clock (s) | Cycle overhead (ms) | Per-deployment overhead (ms) | Deployments / second | Outcomes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 10 | 5 | 50 | 1 | 0.011940 | 11.940 | 0.239 | 4187.57 | executed=50 |
| 50 | 5 | 250 | 1 | 0.041975 | 41.975 | 0.168 | 5955.98 | executed=250 |

Commands run:

```powershell
.venv\Scripts\python.exe scripts/benchmarks/queue_replay.py --tenants 10 --deployments-per-tenant 5
.venv\Scripts\python.exe scripts/benchmarks/queue_replay.py --tenants 50 --deployments-per-tenant 5
```

## Interpretation

- Local queue-loop overhead is small: about 12 ms for 50 queued deployments and about 42 ms for 250.
- The measured ceiling is thousands of deployments per second on the in-memory happy path, so the loop itself is not the limiting factor for SC-022's design point of 10 concurrent deployments.
- At the 10-concurrent design point, this suggests control-loop bookkeeping is negligible compared with any real deployment stage, because even the 50-tenant/250-deployment replay drained in a single sub-50-ms cycle.
- The better 250-deployment throughput is expected here: fixed Python/process overhead is amortised across more work, not evidence that larger queues are inherently cheaper in production.

## What this does not prove

This lite harness does **not** prove:

- Cosmos DB throughput, RU usage, partition hot-spotting, or optimistic-concurrency behaviour under real network latency
- Azure SDK, Entra token, Azure DevOps, ARM, or Fabric latency
- End-to-end orchestrator throughput with real stage execution time
- Behaviour under lease contention, long-running executing deployments, retries, halts, or mixed tenant queue states
- AKS pod CPU/memory pressure, horizontal scaling, or telemetry-export overhead

It is only a local ceiling check for the queue loop's own bookkeeping path.

## Decision note

This document is **not** the acceptance gate for T110. Full T110 remains the decision gate because only a broader run can answer the real production questions: backing-store latency, contention, long-lived execution, and behaviour near the 10-concurrent deployment target.
