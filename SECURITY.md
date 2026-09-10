# Security policy

Groundwork executes Infrastructure-as-Code inside customer Azure tenants. If you find a way to get a
plan approved without a valid approval, get the orchestrator to execute something outside an approved
plan, or bypass the identity/authority boundaries described in
[`docs/adr/0001-ai-plans-deterministic-code-executes.md`](docs/adr/0001-ai-plans-deterministic-code-executes.md),
that's a real vulnerability, not a bug report.

## Reporting

Use [GitHub's private vulnerability reporting](../../security/advisories/new) for this repository
rather than opening a public issue. Include:

- What you did and what you expected to happen instead.
- The affected component (control plane, orchestrator, voice channel, or infrastructure).
- Whether you've confirmed it against a real deployment or read it from the code.

## What's already documented

[`docs/threat-model.md`](docs/threat-model.md) covers the agentic threat model (STRIDE-for-agentic-AI
plus OWASP Agentic AI) and lists known, accepted gaps. Check there first: a report that matches an
already-tracked gap still helps by adding a real-world reproduction, but it isn't new.
