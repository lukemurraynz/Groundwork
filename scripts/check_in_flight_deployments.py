"""Predeploy warning: is anything currently EXECUTING before `azd deploy` restarts the pod?

Closes the gap docs/waf-assessment.md §1.4 names: "no pre-deploy check that drains or quarantines
in-flight work first." Auto-recovery already handles a pod restart correctly (the subscription
lease is released in `_attempt_deployment`'s own `finally`, and the queue loop resumes an orphaned
deployment from its last checkpoint on the very next poll) — this script adds visibility, not a
gate. Advisory only: always exits 0, so it can never block a legitimate deploy. An operator who
sees the warning can choose to wait; one who doesn't still gets the same safe, if disruptive,
auto-recovery this codebase already has.

Deliberately reuses the one sanctioned way this codebase enumerates deployments across tenants
(`TenantRegistry` + a per-tenant `deployment_repository` query, the same pair queue_loop.py's own
`poll_once` uses) rather than a cross-partition query against `deployments` directly — that
container's own docstring is explicit that only `TenantRegistry` (scoped to `tenants`, never
`deployments`) is allowed to bypass tenant-scoping, precisely to protect FR-032 isolation.
"""

from __future__ import annotations

import asyncio
import os
import sys

from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential

from groundwork_orchestrator.state.cosmos import CosmosStateStore
from groundwork_orchestrator.state.repositories import deployment_repository, tenant_registry

_EXECUTING_QUERY = "SELECT * FROM c WHERE c.status = 'executing'"


async def _main() -> int:
    endpoint = os.environ.get("GROUNDWORK_COSMOS_ENDPOINT")
    if not endpoint:
        # Not every environment this hook runs in has provisioned yet (e.g. the very first
        # `azd up`, where `azd deploy` runs immediately after a fresh `azd provision` and the env
        # var is already set by then) — but if it's ever missing, say so and move on rather than
        # failing a deploy over an advisory check.
        print("check-in-flight-deployments: GROUNDWORK_COSMOS_ENDPOINT not set, skipping.")
        return 0

    credential = DefaultAzureCredential()
    try:
        client = CosmosClient(endpoint, credential=credential)
        try:
            store = CosmosStateStore(client)
            registry = tenant_registry(store)
            deployments = deployment_repository(store)

            executing: list[tuple[str, str, str | None]] = []
            async for tenant_id in registry.list_tenant_ids():
                async for deployment in deployments.query(tenant_id, _EXECUTING_QUERY):
                    executing.append(
                        (tenant_id, deployment.deployment_id, deployment.current_stage)
                    )

            if not executing:
                print("check-in-flight-deployments: no deployments currently executing.")
                return 0

            print(
                f"check-in-flight-deployments: WARNING - {len(executing)} deployment(s) "
                "currently executing. `azd deploy` restarts the pod; each will be orphaned and "
                "auto-recovered from its last checkpoint on the next poll cycle (expected, not a "
                "defect - see docs/runbook.md §10), but that is unnecessary disruption if it can "
                "wait:"
            )
            for tenant_id, deployment_id, current_stage in executing:
                print(
                    f"  - tenant={tenant_id} deployment={deployment_id} "
                    f"stage={current_stage or '(not yet started)'}"
                )
            print("Consider waiting for these to finish, or proceed if the deploy is urgent.")
            return 0
        finally:
            await client.close()
    finally:
        await credential.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
