"""The shipped blueprint manifest must validate against the contract.

This is the test that proves FR-013b's extension path is real rather than aspirational: the
blueprint in `infra/blueprints/` is loaded, validated, and its stage graph resolved — with no code
change required to add another.

It also guards the two facts verified from Microsoft Learn on 2026-07-30: the F2 default
(research notes V-001, V-006) and the 15-minute Fabric private-endpoint retry interval (V-006).
Those were expensive to establish and easy to lose in a later edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundwork_contracts import FabricCapacitySku
from groundwork_shared.config.blueprints import (
    BlueprintLoadError,
    compute_digest,
    load_blueprint,
    load_catalogue,
)


def _write_manifest_with_digest(manifest: Path, content: str) -> None:
    """Write a manifest and its matching ``.sha256`` sidecar together — every test that isn't
    specifically exercising the digest check itself needs both, or every load fails on the
    digest gate before reaching whatever this test actually means to prove."""
    manifest.write_text(content, encoding="utf-8")
    manifest.with_name(manifest.name + ".sha256").write_text(
        compute_digest(content), encoding="utf-8"
    )


BLUEPRINTS_ROOT = Path(__file__).resolve().parents[2] / "infra" / "blueprints"
STANDARD = BLUEPRINTS_ROOT / "standard-production-fabric" / "blueprint.yaml"
DEV_SANDBOX = BLUEPRINTS_ROOT / "dev-sandbox" / "blueprint.yaml"


def test_shipped_blueprint_loads_and_validates() -> None:
    blueprint = load_blueprint(STANDARD)

    assert blueprint.blueprint_id == "standard-production-fabric"
    assert blueprint.version == "1.0.0"


def test_default_sku_is_the_minimum_f_sku() -> None:
    """Minimum secure sizing (V-006): private endpoints work on every F SKU."""
    blueprint = load_blueprint(STANDARD)

    assert blueprint.default_fabric_sku is FabricCapacitySku.F2
    assert blueprint.default_fabric_sku.capacity_units == 2


def test_default_sku_requires_powerbi_licensing_disclosure() -> None:
    """FR-013d — below F64, viewers need Pro or PPU, and that must be disclosed at plan time."""
    blueprint = load_blueprint(STANDARD)

    assert blueprint.default_fabric_sku.supports_free_powerbi_viewers is False


def test_fabric_stage_respects_private_endpoint_retry_interval() -> None:
    """V-006 — recreating a managed private endpoint needs a 15-minute gap after deletion.

    Retrying faster produces a spurious failure that looks like a defect in our code, so the
    blueprint must encode the wait rather than leaving it to be rediscovered in an incident.
    """
    blueprint = load_blueprint(STANDARD)
    fabric = next(s for s in blueprint.stages if s.name == "fabric")

    assert fabric.minimum_retry_interval_seconds >= 900


def test_stage_order_is_dependency_resolved() -> None:
    """FR-028 — stages execute in dependency order."""
    blueprint = load_blueprint(STANDARD)
    order = blueprint.execution_order()

    assert order.index("devops_project") < order.index("infrastructure")
    assert order.index("infrastructure") < order.index("networking")
    assert order.index("networking") < order.index("fabric")
    assert order.index("identity") < order.index("fabric")
    assert order.index("fabric") < order.index("monitoring")
    assert order.index("monitoring") < order.index("validation_tests")


def test_stage_order_is_deterministic() -> None:
    """Non-deterministic ordering would make the idempotence tests flaky for unrelated reasons."""
    blueprint = load_blueprint(STANDARD)

    assert blueprint.execution_order() == blueprint.execution_order()


def test_every_stage_declares_a_recovery_path() -> None:
    """FR-031 — a stage with no recovery path must not be marked complete."""
    blueprint = load_blueprint(STANDARD)

    for stage in blueprint.stages:
        assert stage.recovery_path.strip(), f"stage {stage.name} has no recovery path"
        assert stage.idempotence_contract.strip()


def test_every_permission_carries_a_justification() -> None:
    """FR-009 — least privilege with a recorded reason, not a shrug."""
    blueprint = load_blueprint(STANDARD)

    for permission in blueprint.required_permissions:
        assert len(permission.justification) >= 20

        # Broad roles need a substantive rationale explaining why narrower is insufficient.
        if permission.role.lower() in {"owner", "contributor"}:
            assert len(permission.justification) >= 60, (
                f"{permission.role} at {permission.scope} needs a substantive justification "
                f"(the secretless-identity/least-privilege rule)"
            )


def test_permissions_are_scoped_per_stage() -> None:
    """Nothing composes the union of all stages' permissions into one standing identity."""
    blueprint = load_blueprint(STANDARD)

    fabric_permissions = blueprint.permissions_for_stage("fabric")
    assert fabric_permissions
    assert all(p.stage == "fabric" for p in fabric_permissions)

    networking_roles = {p.role for p in blueprint.permissions_for_stage("networking")}
    assert "Fabric Administrator" not in networking_roles


def test_iac_artefacts_are_version_pinned() -> None:
    """FR-039 — pinned versions, never floating."""
    blueprint = load_blueprint(STANDARD)

    assert blueprint.iac_artefacts
    for artefact in blueprint.iac_artefacts:
        assert artefact.version
        assert "latest" not in artefact.version.lower()


def test_catalogue_loads_from_the_repository() -> None:
    catalogue = load_catalogue(BLUEPRINTS_ROOT)

    assert "standard-production-fabric" in catalogue
    assert "dev-sandbox" in catalogue


def test_adr_0010_activation_ships_two_blueprints() -> None:
    """ADR-0010 supersedes FR-013a's single-blueprint posture post-R1."""
    catalogue = load_catalogue(BLUEPRINTS_ROOT)

    assert len(catalogue) == 2


def test_dev_sandbox_blueprint_loads_and_validates() -> None:
    blueprint = load_blueprint(DEV_SANDBOX)

    assert blueprint.blueprint_id == "dev-sandbox"
    assert blueprint.version == "0.1.0"
    assert blueprint.default_fabric_sku is FabricCapacitySku.F8


def test_dev_sandbox_iac_artefacts_align_with_reference_versions() -> None:
    standard = load_blueprint(STANDARD)
    sandbox = load_blueprint(DEV_SANDBOX)
    reference_versions = {artefact.module: artefact.version for artefact in standard.iac_artefacts}

    assert {artefact.module: artefact.version for artefact in sandbox.iac_artefacts} == {
        module: reference_versions[module]
        for module in (
            "avm/res/resources/resource-group",
            "avm/res/network/virtual-network",
            "avm/res/operational-insights/workspace",
            "avm/res/insights/component",
            "avm/res/managed-identity/user-assigned-identity",
        )
    }


def test_missing_manifest_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(BlueprintLoadError, match="not found"):
        load_blueprint(tmp_path / "nope.yaml")


def test_malformed_yaml_fails_loudly(tmp_path: Path) -> None:
    manifest = tmp_path / "blueprint.yaml"
    _write_manifest_with_digest(manifest, "this: [is: not: valid")

    with pytest.raises(BlueprintLoadError, match="not valid YAML"):
        load_blueprint(manifest)


def test_invalid_blueprint_fails_contract_validation(tmp_path: Path) -> None:
    """A malformed blueprint must stop startup, not fail a customer deployment midway."""
    manifest = tmp_path / "blueprint.yaml"
    _write_manifest_with_digest(
        manifest, "blueprintId: broken\nversion: 1.0.0\ndisplayName: Broken\n"
    )

    with pytest.raises(BlueprintLoadError, match="failed contract validation"):
        load_blueprint(manifest)


def test_missing_digest_sidecar_fails_closed(tmp_path: Path) -> None:
    """threat-model.md T-009: an attacker able to modify the manifest could otherwise simply
    delete the sidecar to skip verification entirely — absence must fail closed, not pass."""
    manifest = tmp_path / "blueprint.yaml"
    manifest.write_text("blueprintId: x\nversion: 1.0.0\ndisplayName: X\n", encoding="utf-8")

    with pytest.raises(BlueprintLoadError, match="digest sidecar"):
        load_blueprint(manifest)


def test_digest_mismatch_fails_closed(tmp_path: Path) -> None:
    """A mirrored file that has changed since it was last reviewed must be caught before
    planning begins, not silently loaded."""
    manifest = tmp_path / "blueprint.yaml"
    content = "blueprintId: x\nversion: 1.0.0\ndisplayName: X\n"
    _write_manifest_with_digest(manifest, content)
    # Tamper with the manifest after the sidecar was written, exactly the T-009 scenario.
    manifest.write_text(content + "\n# tampered\n", encoding="utf-8")

    with pytest.raises(BlueprintLoadError, match="does not match its"):
        load_blueprint(manifest)


def test_shipped_blueprints_have_matching_digest_sidecars() -> None:
    """The real, committed manifests must actually pass their own digest check — a sidecar that
    silently drifted from the manifest it guards would defeat the whole point."""
    for manifest in (STANDARD, DEV_SANDBOX):
        digest_path = manifest.with_name(manifest.name + ".sha256")
        assert digest_path.is_file(), f"{manifest} has no committed {digest_path.name}"
        expected = digest_path.read_text(encoding="utf-8").strip().lower()
        actual = compute_digest(manifest.read_text(encoding="utf-8"))
        assert actual == expected, f"{digest_path} is stale; regenerate it with compute_digest"


def test_empty_catalogue_is_an_error(tmp_path: Path) -> None:
    """A service with no blueprints can accept requests it can never fulfil."""
    with pytest.raises(BlueprintLoadError, match="no blueprint manifests"):
        load_catalogue(tmp_path)


def test_stage_dependency_cycle_is_rejected(tmp_path: Path) -> None:
    """A cycle would deadlock a deployment inside a customer tenant.

    Detected at load time instead, where it is a startup failure nobody is waiting on.
    """
    manifest = tmp_path / "blueprint.yaml"
    manifest.write_text(
        """
blueprintId: cyclic
version: 1.0.0
displayName: Cyclic
defaultFabricSku: F2
iacArtefacts:
  - module: avm/res/resources/resource-group
    version: 0.4.0
    source: br/public
requiredPermissions:
  - role: Reader
    scope: subscription
    stage: alpha
    justification: Reading resources to validate the target environment before deployment.
stages:
  - name: alpha
    dependsOn: [beta]
    retryBudget: 1
    idempotenceContract: Re-running against converged state returns no_op every time.
    recoveryPath: Retry the stage from the last durable checkpoint recorded.
  - name: beta
    dependsOn: [alpha]
    retryBudget: 1
    idempotenceContract: Re-running against converged state returns no_op every time.
    recoveryPath: Retry the stage from the last durable checkpoint recorded.
""",
        encoding="utf-8",
    )

    with pytest.raises(BlueprintLoadError, match="cycle"):
        load_blueprint(manifest)
