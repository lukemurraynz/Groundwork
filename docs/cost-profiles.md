# Capacity profiles

**Date**: 2026-07-30

`infra/main.bicep` takes a `costProfile` parameter of `dev` or `production`, set via
`azd env set GROUNDWORK_COST_PROFILE <profile>`. It defaults to `dev`.

## The rule this follows

**Sizing changes. Controls do not.**

The dev profile is the same platform, smaller, not a degraded variant. Every security control,
architectural boundary, and identity separation behaves identically in both, so a change validated
in dev is validated for production. That is the whole point: a cheaper environment that behaves
differently is worth very little as a proving ground.

## What differs

| | dev | production |
| --- | --- | --- |
| Node VM size | `Standard_D2s_v5` (2 vCPU) | `Standard_D4s_v5` (4 vCPU) |
| System pool | 1–2 nodes | 3 nodes |
| Control-plane pool | 1–3 nodes | 2–6 nodes |
| Executor pool | 1–3 nodes | 2–6 nodes |
| vCPU at minimum | **6** | 28 |
| vCPU at maximum | 16 | 60 |
| Cosmos autoscale max | 1000 RU/s (100 idle) | 4000 RU/s (400 idle) |
| Log Analytics retention | 30 days | 365 days |

Roughly a 79% reduction in compute at rest, and a 75% reduction in Cosmos idle throughput.

## What is identical, deliberately

- Workload identity, OIDC issuer, managed AAD with Azure RBAC, local accounts disabled
- Entra-only data plane on Cosmos and Storage; no account keys, no connection strings
- No ACR admin user; AcrPull only, never AcrPush
- Immutable report storage with the 12-month retention FR-052a requires
- Key Vault RBAC authorisation, soft delete, purge protection
- **Three separate node pools:** Collapsing them into one would save more than any sizing change
  and would break FR-045c: executor load could then starve the planning surface. The *shape* is
  design-bound; only the *size* is cost-bound.
- **Zone redundancy across three zones:** An earlier revision made dev single-zone; that was a
  mistake. AKS bills per node regardless of zone, so restricting zones saves nothing and would have
  traded away FR-041a for no benefit.

## Two properties are immutable once deployed

Changing either forces node pool replacement, and ARM rejects the deployment with
`PropertyChangeNotAllowed` rather than doing it silently:

- `agentPoolProfile.vmSize`
- `agentPoolProfile.availabilityZones`

So switching an **existing** cluster between profiles is not an in-place edit. Either delete the
cluster and re-provision, or accept that only node counts will change. Node counts are mutable and
adjust in place.

This is worth knowing before promoting a dev environment to production: it is a rebuild, not a
resize.

## Cost control between sessions

The cluster bills continuously whether or not anything is deployed on it.

```bash
azd down --purge
```

`--purge` matters. Without it, soft-deleted Key Vault and Cognitive Services resources block the
next `azd provision` in the same environment.

To run production sizing:

```bash
azd env set GROUNDWORK_COST_PROFILE production
```

Check regional quota first. Production maximums total 60 vCPU; this subscription had 65 DSv5 vCPUs
available in `australiaeast` on 2026-07-30, which fits but leaves little headroom for a second
environment.

## Network hardening is a separate knob, and it has its own cost

`enableNetworkHardening` (default `false`) is orthogonal to `costProfile` — it doesn't change
sizing, it changes network exposure (`docs/waf-assessment.md` §2.11). It carries one real cost
line worth calling out explicitly rather than burying it in a bicep comment: enabling it bumps
Container Registry from **Basic to Premium SKU**, because ACR's IP-firewall (`networkRuleSet`) is
a Premium-only feature. This applies in both `dev` and `production` cost profiles — Premium ACR's
per-GB storage and higher base price is a standing cost from the day hardening is turned on, not
a one-time charge. Check current Premium ACR pricing before enabling in a cost-sensitive
environment; Network Security Perimeter itself (used for the other five resources) has no
separate charge.
