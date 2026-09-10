# Contributing

## Before you write code

Read [`docs/adr/`](docs/adr/) first, starting with
[ADR-0001](docs/adr/0001-ai-plans-deterministic-code-executes.md). It states the non-negotiables
(deterministic execution boundary, gated approval for irreversible actions, secretless identity)
and the ADRs that follow explain why the infrastructure looks the way it does. A PR that reopens a
settled ADR without new information gets pointed back at it.

## Setup

```bash
uv sync --all-extras
```

## Before you open a PR

```bash
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src/groundwork_contracts   # strict; this package is the ADR-0001 boundary
uv run pytest -q
```

CI runs the same checks. `groundwork_contracts` blocks on mypy; `groundwork_controlplane`,
`groundwork_orchestrator`, and `groundwork_shared` currently carry a pre-existing error baseline and
run non-blocking: don't add to it, but you're not on the hook to clear it in an unrelated PR.

## Boundaries that are easy to break by accident

- **The orchestrator never imports a model client.** `tests/unit/test_import_boundaries.py` enforces
  this. If your change needs the orchestrator to reason about anything, that reasoning belongs in the
  control plane, producing a plan the orchestrator consumes.
- **No long-lived secrets.** Entra ID, workload identity, managed identity only. If you find yourself
  adding an API key or a connection string with a password, stop and use a managed identity instead.
- **A deployment plan is a schema-validated object, never free text.** `src/groundwork_contracts` is
  the schema. Widening it to accept something looser defeats the point of ADR-0001.

## Infrastructure changes

Bicep modules under `infra/` are commented with the live API version verification that justified
each choice (`az provider show`, `az aks get-versions`, and similar). If you bump an API version or a
SKU, verify it against your own subscription the same way and update the comment: a stale
justification is worse than none, because the next person trusts it instead of checking.

Some bicep modules have a compiled `.json` sibling checked in next to them (`infra/main.json`,
`infra/modules/foundry.json`, `infra/modules/budget.json`, `infra/lighthouse/delegation-reader.json`,
and the blueprint templates under `infra/blueprints/`). If you edit the `.bicep`, regenerate the
matching `.json` with `bicep build <file>.bicep --outfile <file>.json` before committing.

## Reporting a security issue

See [SECURITY.md](SECURITY.md). Don't open a public issue for anything that looks exploitable.
