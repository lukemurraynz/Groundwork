"""Startup configuration validation.

A service that starts with missing configuration and fails on the first request has turned a
deployment error into a customer-visible incident. These tests assert it fails at startup instead,
and that the error names the key an operator has to fix.

The environment is injected rather than monkeypatched, so no test mutates real process state.
"""

from __future__ import annotations

import pytest

from groundwork_shared.config.settings import (
    ConfigurationError,
    OrchestratorSettings,
    ResidencySettings,
    Settings,
)

VALID_ENV: dict[str, str] = {
    "AZURE_ENV_NAME": "groundwork-dev",
    "AZURE_LOCATION": "australiaeast",
    "GROUNDWORK_COSMOS_ENDPOINT": "https://example.invalid/",
    "GROUNDWORK_STORAGE_ACCOUNT_URL": "https://example.invalid/",
    "GROUNDWORK_KEY_VAULT_URI": "https://example.invalid/",
    "GROUNDWORK_APPROVAL_THRESHOLD_AUD": "1000",
    "GROUNDWORK_APPROVER_ROLE": "Groundwork.Approver",
    "GROUNDWORK_TENANT_CONCURRENCY_CAP": "3",
    "AZURE_TENANT_ID": "00000000-0000-4000-8000-000000000001",
    "GROUNDWORK_ENTRA_APP_CLIENT_ID": "01f24f06-00ff-4712-a735-c43119da597a",
    "GROUNDWORK_ENTRA_APP_AUDIENCE": "api://01f24f06-00ff-4712-a735-c43119da597a",
    "GROUNDWORK_FOUNDRY_PROJECT_ENDPOINT": "https://example.invalid/api/projects/groundwork-planning",
    "GROUNDWORK_FOUNDRY_MODEL_DEPLOYMENT": "gpt-4o",
    "GROUNDWORK_ORCHESTRATOR_PRINCIPAL_ID": "3f6b1c8a-9e2d-4a5b-8c7f-1d2e3f4a5b6c",
}


def test_valid_environment_loads() -> None:
    settings = Settings.from_environment(VALID_ENV)

    assert settings.azure_location == "australiaeast"
    assert settings.governance.approval_threshold_aud == 1000.0
    assert settings.governance.require_step_up_approval is True  # defaults on (ADR-0011)
    assert settings.residency.storage_region == "australiaeast"


@pytest.mark.parametrize("missing_key", sorted(VALID_ENV))
def test_missing_required_key_fails_at_startup(missing_key: str) -> None:
    """Every required key is checked, and the error names it."""
    env = {k: v for k, v in VALID_ENV.items() if k != missing_key}

    with pytest.raises(ConfigurationError, match=missing_key):
        Settings.from_environment(env)


def test_empty_value_is_treated_as_missing() -> None:
    """An empty variable is a configuration mistake, not an intentional empty string."""
    env = VALID_ENV | {"GROUNDWORK_COSMOS_ENDPOINT": "   "}

    with pytest.raises(ConfigurationError, match="GROUNDWORK_COSMOS_ENDPOINT"):
        Settings.from_environment(env)


def test_go_template_missing_key_sentinel_is_treated_as_missing() -> None:
    """A real 2026-08-06 incident: `k8s/*/deployment.tmpl.yaml` substituting
    `{{.Env.SOME_VAR}}` for an azd env key that was never set at all renders the literal string
    `<no value>` (Go text/template's own behaviour for a missing map key), not an empty string.
    That string is non-empty, so without this check a required setting would silently hold
    garbage instead of failing closed — which is exactly what happened to the live orchestrator's
    GROUNDWORK_FABRIC_CAPACITY_ADMIN_UPN."""
    env = VALID_ENV | {"GROUNDWORK_COSMOS_ENDPOINT": "<no value>"}

    with pytest.raises(ConfigurationError, match="GROUNDWORK_COSMOS_ENDPOINT"):
        Settings.from_environment(env)


def test_non_numeric_threshold_is_rejected() -> None:
    env = VALID_ENV | {"GROUNDWORK_APPROVAL_THRESHOLD_AUD": "lots"}

    with pytest.raises(ConfigurationError, match="must be a number"):
        Settings.from_environment(env)


def test_concurrency_cap_out_of_range_is_rejected() -> None:
    env = VALID_ENV | {"GROUNDWORK_TENANT_CONCURRENCY_CAP": "500"}

    with pytest.raises(ConfigurationError, match="between 1 and 50"):
        Settings.from_environment(env)


def test_non_australian_storage_region_is_rejected() -> None:
    """FR-053b enforced at startup, so a misconfigured environment cannot run at all."""
    env = VALID_ENV | {"GROUNDWORK_STORAGE_REGION": "eastus"}

    with pytest.raises(ConfigurationError, match="not in the allowed set"):
        Settings.from_environment(env)


def test_residency_rejects_non_australian_region_directly() -> None:
    with pytest.raises(ConfigurationError, match="FR-053b"):
        ResidencySettings(storage_region="westeurope")


def test_settings_are_immutable() -> None:
    """Governance thresholds must not be mutable at runtime (FR-020a)."""
    settings = Settings.from_environment(VALID_ENV)

    with pytest.raises(AttributeError):
        settings.governance.__setattr__("approval_threshold_aud", 0.0)


def test_second_approver_threshold_applies_only_to_production() -> None:
    settings = Settings.from_environment(VALID_ENV)
    governance = settings.governance

    assert governance.requires_second_approver(1500.0, "production") is True
    assert governance.requires_second_approver(999.0, "production") is False
    assert governance.requires_second_approver(1500.0, "non-production") is False


def test_step_up_approval_setting_parses_truthy_value() -> None:
    settings = Settings.from_environment(
        VALID_ENV | {"GROUNDWORK_REQUIRE_STEP_UP_APPROVAL": "true"}
    )

    assert settings.governance.require_step_up_approval is True


def test_step_up_approval_setting_rejects_invalid_value() -> None:
    with pytest.raises(ConfigurationError, match="GROUNDWORK_REQUIRE_STEP_UP_APPROVAL"):
        Settings.from_environment(VALID_ENV | {"GROUNDWORK_REQUIRE_STEP_UP_APPROVAL": "maybe"})


def test_telemetry_connection_string_is_optional() -> None:
    """Absent telemetry degrades to console logging — a real state, not a silent fallback."""
    settings = Settings.from_environment(VALID_ENV)

    assert settings.applicationinsights_connection_string is None


ORCHESTRATOR_VALID_ENV: dict[str, str] = {
    "AZURE_ENV_NAME": "groundwork-dev",
    "AZURE_LOCATION": "australiaeast",
    "GROUNDWORK_COSMOS_ENDPOINT": "https://example.invalid/",
    "GROUNDWORK_STORAGE_ACCOUNT_URL": "https://example.invalid/",
    "GROUNDWORK_KEY_VAULT_URI": "https://example.invalid/",
    "GROUNDWORK_APPROVAL_THRESHOLD_AUD": "1000",
    "GROUNDWORK_APPROVER_ROLE": "Groundwork.Approver",
    "GROUNDWORK_TENANT_CONCURRENCY_CAP": "3",
    "GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS": "10",
    "GROUNDWORK_ORCHESTRATOR_PRINCIPAL_ID": "3f6b1c8a-9e2d-4a5b-8c7f-1d2e3f4a5b6c",
    "GROUNDWORK_FABRIC_CAPACITY_ADMIN_UPN": "capacity-admin@customer.example",
}


def test_orchestrator_writes_are_disabled_by_default() -> None:
    """A local worker must not write into a tenant by accident."""
    settings = OrchestratorSettings.from_environment(ORCHESTRATOR_VALID_ENV)

    assert settings.allow_tenant_writes is False


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes"])
def test_orchestrator_writes_require_explicit_opt_in(truthy: str) -> None:
    settings = OrchestratorSettings.from_environment(
        ORCHESTRATOR_VALID_ENV | {"GROUNDWORK_ALLOW_WRITES": truthy}
    )

    assert settings.allow_tenant_writes is True


# Keys that became per-tenant fallbacks (CustomerTenant record is the primary source; see
# OrchestratorSettings' own docstrings) — legitimately absent at startup now.
_ORCHESTRATOR_OPTIONAL_KEYS = {
    "GROUNDWORK_FABRIC_CAPACITY_ADMIN_UPN",
    "GROUNDWORK_DEVOPS_ORGANIZATION_URL",
}


@pytest.mark.parametrize(
    "missing_key",
    sorted(set(ORCHESTRATOR_VALID_ENV) - _ORCHESTRATOR_OPTIONAL_KEYS),
)
def test_orchestrator_missing_required_key_fails_at_startup(missing_key: str) -> None:
    env = {k: v for k, v in ORCHESTRATOR_VALID_ENV.items() if k != missing_key}

    with pytest.raises(ConfigurationError, match=missing_key):
        OrchestratorSettings.from_environment(env)


def test_orchestrator_engagement_fallbacks_default_to_none() -> None:
    """The per-tenant engagement fields are optional: a worker whose tenants each carry their
    own values on their CustomerTenant records needs no worker-wide fallback at all."""
    env = {k: v for k, v in ORCHESTRATOR_VALID_ENV.items() if k not in _ORCHESTRATOR_OPTIONAL_KEYS}
    settings = OrchestratorSettings.from_environment(env)

    assert settings.fabric_capacity_admin_upn is None
    assert settings.devops_organization_url is None
    assert settings.drift_interval_seconds == 900


@pytest.mark.parametrize("key", sorted(_ORCHESTRATOR_OPTIONAL_KEYS))
def test_orchestrator_optional_sentinel_is_treated_as_unset(key: str) -> None:
    """Same class as the 2026-08-06 incident, one step further: an *optional* field rendered
    ``<no value>`` by the k8s manifest would carry that literal string as its value — the
    sentinel check must apply to optional getters too, mapping them to None."""
    env = {k: v for k, v in ORCHESTRATOR_VALID_ENV.items() if k != key} | {key: "<no value>"}
    settings = OrchestratorSettings.from_environment(env)

    assert (
        getattr(
            settings,
            "fabric_capacity_admin_upn" if key.endswith("UPN") else "devops_organization_url",
        )
        is None
    )


def test_orchestrator_drift_interval_allows_zero_to_disable() -> None:
    settings = OrchestratorSettings.from_environment(
        ORCHESTRATOR_VALID_ENV | {"GROUNDWORK_DRIFT_INTERVAL_SECONDS": "0"}
    )

    assert settings.drift_interval_seconds == 0


def test_orchestrator_drift_interval_rejects_negative_values() -> None:
    with pytest.raises(ConfigurationError, match="GROUNDWORK_DRIFT_INTERVAL_SECONDS"):
        OrchestratorSettings.from_environment(
            ORCHESTRATOR_VALID_ENV | {"GROUNDWORK_DRIFT_INTERVAL_SECONDS": "-1"}
        )
