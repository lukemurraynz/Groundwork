from __future__ import annotations

import json
from urllib.parse import unquote, urlparse

from groundwork_contracts.tenant import CustomerTenant
from groundwork_controlplane.api.lighthouse_onboarding import (
    LIGHTHOUSE_TEMPLATE_URI,
    LighthouseConstants,
    build_azure_devops_instructions,
    build_lighthouse_command,
    build_lighthouse_portal_link,
)

TENANT_ID = "11111111-1111-1111-1111-111111111111"
ORCHESTRATOR_ID = "67ecfae8-fb74-41f7-ac60-e979d8b56eab"
ORG_URL = "https://dev.azure.com/customer-org"


class _FakeReadiness:
    def __init__(
        self,
        *,
        orchestrator_principal_id: str = ORCHESTRATOR_ID,
        orchestrator_display_name: str | None = None,
        controlplane_principal_id: str | None = None,
    ) -> None:
        self.orchestrator_principal_id = orchestrator_principal_id
        self.orchestrator_display_name = orchestrator_display_name
        self.controlplane_principal_id = controlplane_principal_id
        self.devops_organization_url: str | None = None


class _FakeSettings:
    def __init__(self, *, readiness: _FakeReadiness) -> None:
        self.readiness = readiness


def _tenant() -> CustomerTenant:
    return CustomerTenant(
        tenant_id=TENANT_ID,
        display_name="Test Customer",
        approved_regions=frozenset({"australiaeast"}),
        data_residency_regions=frozenset({"australiaeast"}),
    )


def test_build_lighthouse_command_uses_prefilled_constants() -> None:
    constants = LighthouseConstants(
        managed_by_tenant_id="11111111-1111-1111-1111-111111111111",
        principal_id="22222222-2222-2222-2222-222222222222",
    )

    command = build_lighthouse_command(
        subscription_id="33333333-3333-3333-3333-333333333333",
        deployment_location="australiaeast",
        constants=constants,
    )

    assert command == (
        "az deployment sub create --subscription 33333333-3333-3333-3333-333333333333 "
        "--location australiaeast --template-uri "
        f"{LIGHTHOUSE_TEMPLATE_URI} --parameters "
        "managedByTenantId=11111111-1111-1111-1111-111111111111 "
        "principalId=22222222-2222-2222-2222-222222222222 "
        'principalIdDisplayName="Groundwork Platform bootstrap identity"'
    )


def test_build_lighthouse_portal_link_encodes_parameters() -> None:
    constants = LighthouseConstants(
        managed_by_tenant_id="11111111-1111-1111-1111-111111111111",
        principal_id="22222222-2222-2222-2222-222222222222",
    )

    link = build_lighthouse_portal_link(
        subscription_id="33333333-3333-3333-3333-333333333333",
        constants=constants,
    )

    parsed = urlparse(link)
    assert parsed.fragment.startswith("create/Microsoft.Template/uri/")
    assert LIGHTHOUSE_TEMPLATE_URI in unquote(parsed.fragment)
    parameters_fragment = parsed.fragment.split("/parameters/", maxsplit=1)[1]
    parameters = json.loads(unquote(parameters_fragment))
    assert parameters == {
        "managedByTenantId": {"value": "11111111-1111-1111-1111-111111111111"},
        "principalId": {"value": "22222222-2222-2222-2222-222222222222"},
        "principalIdDisplayName": {"value": "Groundwork Platform bootstrap identity"},
    }


def test_pca_instruction_names_the_display_name_when_configured() -> None:
    """Found live 2026-09-07: the ADO people-picker resolves a display name far more reliably
    than the bare object id, and the instruction previously never gave one."""
    settings = _FakeSettings(
        readiness=_FakeReadiness(orchestrator_display_name="id-gw-orch-kyhwiekc5y3uo")
    )

    instructions = build_azure_devops_instructions(
        tenant=_tenant(), settings=settings, organization_url=ORG_URL  # type: ignore[arg-type]
    )

    assert "id-gw-orch-kyhwiekc5y3uo" in instructions.pca_instruction_text
    assert "by name" in instructions.pca_instruction_text
    assert "Organization Settings > Permissions" in instructions.pca_instruction_text
    assert "Project Collection Administrators" in instructions.pca_instruction_text
    assert instructions.orchestrator_display_name == "id-gw-orch-kyhwiekc5y3uo"


def test_pca_instruction_falls_back_to_object_id_when_display_name_absent() -> None:
    """A tenant onboarded before GROUNDWORK_ORCHESTRATOR_DISPLAY_NAME existed, or a deployment
    that has not been re-provisioned since, must still get a usable — if worse — instruction."""
    settings = _FakeSettings(readiness=_FakeReadiness(orchestrator_display_name=None))

    instructions = build_azure_devops_instructions(
        tenant=_tenant(), settings=settings, organization_url=ORG_URL  # type: ignore[arg-type]
    )

    assert ORCHESTRATOR_ID in instructions.pca_instruction_text
    assert "not configured on this deployment" in instructions.pca_instruction_text
    assert instructions.orchestrator_display_name is None
