# Groundwork

Groundwork provisions an enterprise data platform in a customer's own Azure tenant, triggered by
voice or chat. A Foundry-hosted agent plans the request, prices it, and validates it against the
target tenant. A separate, deterministic orchestrator running on AKS then carries out the approved
plan as Infrastructure-as-Code. The agent never touches Azure directly: it produces a
schema-validated plan, a human approves it, and only then does code execute.

That split is the whole point of the design. A model that can reason its way into calling an Azure
API is a model that can also be prompt-injected into calling one it shouldn't. Groundwork keeps
reasoning and execution in separate services with separate identities, so the worst a compromised
agent can do is produce a plan nobody approves.

New to the repo? [`docs/customer-journey-map.md`](docs/customer-journey-map.md) walks through what
Groundwork actually does end to end: onboarding, planning and approval, provisioning, and ongoing
operation, with sequence and architecture diagrams, before you're in the code.

## What's in the box

| Piece | What it does |
| --- | --- |
| **Control plane** (`src/groundwork_controlplane`) | Talks to the Foundry planning agent, prices the request against the Azure Retail Prices API, and validates it against the target tenant before anyone approves it. |
| **Orchestrator** (`src/groundwork_orchestrator`) | Executes an approved plan stage by stage (landing zone, Fabric capacity, DevOps project, pipeline) with checkpointed state, so a failure resumes rather than restarts. |
| **Voice channel** (`src/groundwork_channels/voice`) | The same conversational agent as chat, over real-time speech (Azure AI Voice Live) instead of text, it can onboard a tenant or request/approve a deployment by talking, using the same tool set as the chat surface (create tenant, quick-onboard for the operator's own tenant, confirm consent, grant ADO access, trigger bootstrap, generate plan, check plan status). Approval stays on the HTTP route, never a voice tool. Gated behind consent and explicit per-tenant enablement. `voice.html` is the reference client. |
| **Contracts** (`src/groundwork_contracts`) | The schema every plan has to satisfy before the orchestrator will look at it. Strict-typed on purpose: this is the boundary in ADR-0001. |

Both services run as separate AKS workloads with separate managed identities. The control plane can
call a model and holds read-only access to a customer's tenant; the orchestrator writes to that
tenant and never imports a model client. `tests/unit/test_import_boundaries.py` enforces the second
half of that mechanically, not just by convention.

## Deploying it

Two different things get deployed here, and it's worth keeping them apart. `azd up` deploys
**Groundwork itself** (AKS, Cosmos, Foundry, the two services) into your own subscription. The
**customer's data platform** (the thing Groundwork provisions on request) is deployed separately,
by the orchestrator, into the customer's tenant. Nothing below touches a customer tenant.

You need the Azure Developer CLI, Python 3.13 with `uv`, and an Azure subscription.

```bash
git clone https://github.com/lukemurraynz/Groundwork.git
cd Groundwork
uv sync
azd auth login
azd env new groundwork-dev
```

Four governance values have no default on purpose: a wrong default could point at the wrong
tenant. Set them before `azd up`, or `preprovision` stops you in seconds and tells you which
one is missing:

```bash
azd env set GROUNDWORK_APPROVAL_THRESHOLD_AUD 1000
azd env set GROUNDWORK_APPROVER_ROLE Groundwork.Approver
azd env set GROUNDWORK_TENANT_CONCURRENCY_CAP 3
azd env set GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS 3
azd up
```

One more that has a default, not a hard requirement, but changes what you'll see: approvals
default to requiring MFA-or-fresh-token evidence in the caller's token
(`GROUNDWORK_REQUIRE_STEP_UP_APPROVAL`, see [ADR-0011](docs/adr/0011-voice-alone-authorises-irreversible-actions.md)).
If you're testing locally and hit a `step-up-authentication-required` 403 you didn't expect, that's
why: `azd env set GROUNDWORK_REQUIRE_STEP_UP_APPROVAL false` turns it off for that environment.

`azd up` provisions AKS, Cosmos DB, Key Vault, Container Registry, Foundry, and a dedicated Speech
resource for Voice Live, then builds and deploys both services. A `preprovision` hook checks your
environment first (region, subscription, the four values above, node quota), so a bad setting
fails in seconds instead of twenty minutes into a cluster build. `azd up` is `azd provision` and
`azd deploy` in one step; run them separately when you want to read the provisioning what-if
before committing to it:

```bash
azd provision --preview   # what-if for your own subscription; read it before every provision
azd provision
azd deploy
```

The `location` parameter in `infra/main.bicep` ships with a broad regional allow-list (Foundry, AKS,
Cosmos, and Speech all need to be available together). The platform this was built for originally
locked that parameter to Australian regions only, for a customer data-residency requirement (see
[ADR-0008](docs/adr/0008-residency-boundary-persisted-au-only-transient-exception.md)). If you have
the same kind of requirement, narrow the `@allowed` list back down; nothing else in the code assumes
a specific region.

### Getting voice and chat working

`azd up` deploys the platform; it doesn't create a customer engagement. Voice and chat both refuse
every request until a tenant record exists with consent granted and the voice channel explicitly
enabled: that's four API calls, not a checkbox in the deployment. See
[`docs/first-tenant-walkthrough.md`](docs/first-tenant-walkthrough.md) for the exact sequence,
including how to get a token when `az account get-access-token` hits `consent_required`.

### Running a service locally

```bash
uv run uvicorn groundwork_controlplane.api.main:app --reload
```

This authenticates with `DefaultAzureCredential`, so it uses your own developer identity: there's no
local secret to manage and nothing to leak. The orchestrator worker refuses to start unless
`GROUNDWORK_ALLOW_WRITES` is explicitly set, so a local run can't accidentally write into a tenant.

### Tearing it down

```bash
azd down --purge
```

Use `--purge`. Without it, soft-deleted Key Vault and Foundry resources block the next `azd
provision` into the same environment.

## Why AKS, not Container Apps

Zone-redundant node pools, workload identity, and independently scalable control-plane and executor
tiers, per [ADR-0001](docs/adr/0001-ai-plans-deterministic-code-executes.md). The two services are
separate `azd` services on purpose: keeping them as distinct deployments enforces the authority split
at deploy time, not only in code review. `azure.yaml` has the full service definition; `docs/adr/`
has the reasoning behind every non-obvious infrastructure choice.

## Documentation

- [`docs/customer-journey-map.md`](docs/customer-journey-map.md) — start here: onboarding, planning and
  approval, provisioning, and ongoing operation end to end, with sequence and architecture diagrams
  traced to the real code.
- [`docs/adr/`](docs/adr/) — fourteen decisions, each self-contained: why AKS, why no auto-rollback,
  why the orchestrator never imports a model library, why voice alone can authorise an irreversible
  action, and so on.
- [`docs/product-specification.md`](docs/product-specification.md) — the requirements this was built
  against.
- [`docs/threat-model.md`](docs/threat-model.md) — STRIDE-for-agentic-AI plus OWASP Agentic AI,
  covering the conversational surface and the MCP tool-authority split.
- [`docs/waf-assessment.md`](docs/waf-assessment.md) — a Well-Architected Framework review of
  Groundwork's own infrastructure and code.
- [`docs/runbook.md`](docs/runbook.md) / [`docs/release-checklist.md`](docs/release-checklist.md) —
  what an operator does when something breaks, and the discipline around an `azd` release.
The non-negotiables every design decision gets checked against (deterministic execution boundary,
  gated approval for irreversible actions, secretless identity) are stated directly in
[ADR-0001](docs/adr/0001-ai-plans-deterministic-code-executes.md) and enforced in code
(`tests/unit/test_import_boundaries.py`), not kept in a separate governing document.

The original feature spec and planning narrative behind these decisions (numbered requirements,
session-by-session clarifications, an agent handoff, dated research citations you'll still see
referenced in code comments) isn't included in this release: it carried real subscription and
tenant identifiers from the environment it was built against. Every decision from that narrative
that still matters is distilled into the ADRs above, most recently ADR-0011 (voice-alone
authorisation) and ADR-0012 (MCP tool authority tiers), both backfilled when the original planning
notes were removed, and ADR-0013/ADR-0014 (the planning agent's client was an OpenAI-compatible
workaround for two weeks, diagnosed down to an IaC role-scope gap and reverted to the native Agent
Framework client), recorded from live deployment checks rather than the archive.

## Testing

```bash
uv run pytest -q       # 949 tests: unit, contract, integration, security, resilience
uv run ruff check src tests
uv run mypy src/groundwork_contracts   # strict; the ADR-0001 boundary is typed, not just documented
```

CI (`.github/workflows/ci.yml`) runs the same checks on every push and PR.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE).
