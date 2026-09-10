"""Agent integration: the deterministic-execution boundary (ADR-0001) between conversation and
orchestration.

Everything a model may produce and everything the control plane does with that output before it
becomes a validated ``DeploymentPlan`` — nothing here executes anything (that is
``groundwork_orchestrator``'s job, and it never imports this package).
"""
