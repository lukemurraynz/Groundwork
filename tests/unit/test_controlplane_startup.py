from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from groundwork_controlplane.api import main as main_module
from groundwork_shared.config.settings import FoundrySettings, GovernanceSettings, ReadinessSettings

BLUEPRINTS_ROOT = Path(__file__).resolve().parents[2] / "infra" / "blueprints"


@dataclass(slots=True)
class _FakeSettings:
    environment_name: str
    applicationinsights_connection_string: str | None
    blueprints_path: str
    cosmos_endpoint: str
    key_vault_uri: str
    storage_account_url: str
    foundry: FoundrySettings
    entra: SimpleNamespace
    readiness: ReadinessSettings
    governance: GovernanceSettings


class _FakeCredential:
    async def close(self) -> None:
        return None


class _FakeCosmosClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def close(self) -> None:
        return None


class _FakeSecretClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def close(self) -> None:
        return None


class _FakeRetailPricesClient:
    async def close(self) -> None:
        return None


class _EmptyTenantRegistry:
    async def list_tenant_ids(self) -> AsyncIterator[str]:
        if False:
            yield "unreachable"


@pytest.mark.anyio
async def test_lifespan_accepts_multiple_blueprints(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _FakeSettings(
        environment_name="test",
        applicationinsights_connection_string=None,
        blueprints_path=str(BLUEPRINTS_ROOT),
        cosmos_endpoint="https://example.invalid/",
        key_vault_uri="https://example.invalid/",
        storage_account_url="https://example.invalid/",
        foundry=FoundrySettings(
            project_endpoint="https://example.invalid/project",
            model_deployment="gpt-test",
        ),
        entra=SimpleNamespace(
            tenant_id="11111111-1111-1111-1111-111111111111",
            audience="api://groundwork-test",
        ),
        readiness=ReadinessSettings(
            orchestrator_principal_id="22222222-2222-2222-2222-222222222222"
        ),
        governance=GovernanceSettings(
            approval_threshold_aud=1000.0,
            approver_role="Groundwork.Approver",
            default_tenant_concurrency_cap=3,
        ),
    )

    monkeypatch.setattr(
        main_module.Settings,
        "from_environment",
        classmethod(lambda cls, env=None: settings),
    )
    monkeypatch.setattr(main_module, "configure_telemetry", lambda **_kwargs: None)
    monkeypatch.setattr(main_module, "DefaultAzureCredential", _FakeCredential)
    monkeypatch.setattr(main_module, "CosmosClient", _FakeCosmosClient)
    monkeypatch.setattr(main_module, "CosmosStateStore", lambda _client: object())
    monkeypatch.setattr(main_module, "SecretClient", _FakeSecretClient)
    monkeypatch.setattr(main_module, "RetailPricesClient", _FakeRetailPricesClient)
    monkeypatch.setattr(main_module, "tenant_registry", lambda _state_store: _EmptyTenantRegistry())
    monkeypatch.setattr(main_module, "plan_repository", lambda _state_store: object())
    monkeypatch.setattr(main_module, "customer_tenant_repository", lambda _state_store: object())
    monkeypatch.setattr(main_module, "conversation_repository", lambda _state_store: object())
    monkeypatch.setattr(main_module, "approval_repository", lambda _state_store: object())
    monkeypatch.setattr(main_module, "pending_approval_repository", lambda _state_store: object())
    monkeypatch.setattr(main_module, "deployment_repository", lambda _state_store: object())
    monkeypatch.setattr(main_module, "stage_record_repository", lambda _state_store: object())
    monkeypatch.setattr(main_module, "report_repository", lambda _state_store: object())
    monkeypatch.setattr(main_module, "drift_summary_repository", lambda _state_store: object())
    monkeypatch.setattr(main_module, "build_approval_artefact_store", lambda **_kwargs: object())
    monkeypatch.setattr(main_module, "build_consent_store", lambda **_kwargs: object())

    fake_engine = object()
    fake_agent = object()
    monkeypatch.setattr(
        main_module,
        "_build_blueprint_runtime",
        lambda **_kwargs: (
            {
                "standard-production-fabric": fake_engine,
                "dev-sandbox": fake_engine,
            },
            {
                "standard-production-fabric": fake_agent,
                "dev-sandbox": fake_agent,
            },
        ),
    )

    app = FastAPI()
    async with main_module.lifespan(app):
        assert sorted(app.state.blueprints) == ["dev-sandbox", "standard-production-fabric"]
        assert sorted(app.state.readiness_engines) == ["dev-sandbox", "standard-production-fabric"]
        assert sorted(app.state.planning_agents) == ["dev-sandbox", "standard-production-fabric"]
