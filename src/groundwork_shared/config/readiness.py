"""Landing zone contract (readiness assertions) manifest loading.

Sibling to ``groundwork_shared.config.blueprints`` and for the same reason: FR-014a's assertion
set is a versioned declarative artefact under ``infra/blueprints/<id>/assertions.yaml``, not code,
so adding or changing an assertion is a reviewable YAML diff rather than a release. This module
performs filesystem I/O and so cannot live in ``groundwork_contracts`` — see that package's own
docstring for why its zero-I/O guarantee is a mechanically tested fact, not a convention.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from groundwork_contracts.errors import ContractViolation
from groundwork_contracts.readiness import LandingZoneContract
from groundwork_shared.config.blueprints import to_snake_case


class ReadinessContractLoadError(ContractViolation):
    """A landing zone contract manifest is missing, unparseable, or invalid.

    Raised at startup, matching ``BlueprintLoadError``'s reasoning: a malformed contract must stop
    the service from starting rather than fail a customer's readiness check partway through.
    """


def load_landing_zone_contract(manifest_path: Path) -> LandingZoneContract:
    """Load and validate the readiness assertion manifest for one blueprint.

    Args:
        manifest_path: Path to an ``assertions.yaml``.

    Returns:
        The validated contract.

    Raises:
        ReadinessContractLoadError: If the file is missing, is not valid YAML, is not a mapping, or
            fails contract validation — including the ``LandingZoneContract`` invariant that every
            Azure Landing Zone design area must have at least one assertion.
    """
    if not manifest_path.is_file():
        raise ReadinessContractLoadError(f"readiness assertion manifest not found: {manifest_path}")

    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ReadinessContractLoadError(
            f"readiness assertion manifest {manifest_path} is not valid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise ReadinessContractLoadError(
            f"readiness assertion manifest {manifest_path} must be a mapping, "
            f"got {type(raw).__name__}"
        )

    try:
        return LandingZoneContract.model_validate(to_snake_case(raw))
    except ValueError as exc:
        raise ReadinessContractLoadError(
            f"readiness assertion manifest {manifest_path} failed contract validation: {exc}"
        ) from exc
