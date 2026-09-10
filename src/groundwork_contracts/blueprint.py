"""Platform blueprints — approved, versioned, declarative platform topologies.

FR-013b requires a blueprint to be addable as a versioned artefact without code change to the
planning, validation, or orchestration components. That is why this module models a blueprint as
data loaded from ``infra/blueprints/<id>/blueprint.yaml`` rather than as a Python subclass.

Adding a blueprint must not require a release of the service itself.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

StrictModel = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class FabricCapacitySku(StrEnum):
    """Microsoft Fabric capacity SKUs.

    Verified against Microsoft Learn 2026-07-30 (research notes § V-001): F SKUs run F2 to F8192,
    capacity units equal the SKU number, and **F64 is the threshold** at which users with only a
    Fabric Free licence can view Power BI content given a workspace viewer role.

    This enum stops at F512 deliberately. It is a guard against a model proposing an implausibly
    large capacity, not a claim that larger SKUs do not exist.
    """

    F2 = "F2"
    F4 = "F4"
    F8 = "F8"
    F16 = "F16"
    F32 = "F32"
    F64 = "F64"
    F128 = "F128"
    F256 = "F256"
    F512 = "F512"

    @property
    def capacity_units(self) -> int:
        """Capacity units, which equal the numeric part of the SKU name."""
        return int(self.value[1:])

    @property
    def supports_free_powerbi_viewers(self) -> bool:
        """Whether Fabric Free licence holders can view Power BI content at this SKU.

        True at F64 and above. Below it, each viewer needs Power BI Pro or PPU — the trade-off
        FR-013d requires be disclosed at plan time.
        """
        return self.capacity_units >= 64


class DesignAreaName(StrEnum):
    """The eight Azure Landing Zone design areas.

    Verified against the Cloud Adoption Framework 2026-07-30 (research notes § V-002). CAF frames
    these as considerations to evaluate, not as a machine-checkable conformance test — so Groundwork
    borrows the taxonomy for familiarity and defines its own assertions underneath.
    """

    BILLING_AND_TENANT = "azure-billing-and-tenant"
    IDENTITY_AND_ACCESS = "identity-and-access-management"
    RESOURCE_ORGANIZATION = "resource-organization"
    NETWORK_TOPOLOGY = "network-topology-and-connectivity"
    SECURITY = "security"
    MANAGEMENT = "management"
    GOVERNANCE = "governance"
    PLATFORM_AUTOMATION = "platform-automation-and-devops"


class RequiredPermission(BaseModel):
    """A role the deployment identity needs, at a stated scope, with a reason.

    FR-009 requires a recorded justification for every permission. The justification is a required
    field rather than an optional note, so a blueprint cannot quietly request broad rights.
    """

    model_config = StrictModel

    role: Annotated[str, Field(min_length=1)]
    scope: Annotated[str, Field(min_length=1)]
    justification: Annotated[str, Field(min_length=20)]
    stage: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def _reject_owner_without_strong_reason(self) -> Self:
        # The secretless-identity/least-privilege rule calls Subscription Owner as a convenience
        # default a defect. This does not forbid it outright — some operations genuinely need it —
        # but it forces the justification to be substantive rather than a shrug.
        if self.role.lower() in {"owner", "contributor"} and len(self.justification) < 60:
            raise ValueError(
                f"role {self.role!r} is broad; a substantive justification is required "
                f"(at least 60 characters) explaining why a narrower role is insufficient"
            )
        return self


class IacArtefact(BaseModel):
    """A version-pinned infrastructure module (FR-039)."""

    model_config = StrictModel

    module: Annotated[str, Field(min_length=1)]
    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    source: Annotated[str, Field(min_length=1)]


class BlueprintStage(BaseModel):
    """One deterministic execution stage.

    ``recovery_path`` is required. FR-031 says a stage with no declared recovery path must not be
    marked complete, so a blueprint cannot declare a stage without one.
    """

    model_config = StrictModel

    name: Annotated[str, Field(min_length=1)]
    depends_on: tuple[str, ...] = ()
    idempotence_contract: Annotated[str, Field(min_length=20)]
    retry_budget: Annotated[int, Field(ge=0, le=10)]
    recovery_path: Annotated[str, Field(min_length=20)]
    minimum_retry_interval_seconds: Annotated[int, Field(ge=0)] = 0
    """Some resources cannot be recreated immediately after deletion.

    Fabric managed private endpoints require at least 15 minutes (research notes § V-006), so the
    Fabric stage sets 900 here. Retrying faster than this produces a spurious failure that looks
    like a defect in our code.
    """


class PlatformBlueprint(BaseModel):
    """An approved platform topology, loaded from a versioned declarative artefact."""

    model_config = StrictModel

    blueprint_id: Annotated[str, Field(pattern=r"^[a-z0-9-]+$")]
    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    display_name: Annotated[str, Field(min_length=1)]
    default_fabric_sku: FabricCapacitySku
    iac_artefacts: Annotated[tuple[IacArtefact, ...], Field(min_length=1)]
    required_permissions: Annotated[tuple[RequiredPermission, ...], Field(min_length=1)]
    stages: Annotated[tuple[BlueprintStage, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _stage_graph_is_sound(self) -> Self:
        names = [s.name for s in self.stages]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate stage names: {sorted(duplicates)}")

        known = set(names)
        for stage in self.stages:
            unknown = set(stage.depends_on) - known
            if unknown:
                raise ValueError(
                    f"stage {stage.name!r} depends on undeclared stage(s) {sorted(unknown)}"
                )

        # A cycle would make FR-028's dependency ordering unsatisfiable. Detect it here, at load
        # time, rather than discovering it when a deployment deadlocks in a customer tenant.
        self._assert_acyclic()
        return self

    def _assert_acyclic(self) -> None:
        graph = {s.name: set(s.depends_on) for s in self.stages}
        resolved: set[str] = set()
        while len(resolved) < len(graph):
            ready = {
                name for name, deps in graph.items() if name not in resolved and deps <= resolved
            }
            if not ready:
                remaining = sorted(set(graph) - resolved)
                raise ValueError(
                    f"stage dependency cycle detected among {remaining}; FR-028 requires a "
                    f"resolvable dependency order"
                )
            resolved |= ready

    def execution_order(self) -> tuple[str, ...]:
        """Stages in dependency-resolved order (FR-028).

        Deterministic: ties are broken alphabetically so the same blueprint always produces the
        same order. Non-deterministic ordering would make the idempotence tests flaky for reasons
        unrelated to the code under test.
        """
        graph = {s.name: set(s.depends_on) for s in self.stages}
        order: list[str] = []
        resolved: set[str] = set()
        while len(resolved) < len(graph):
            ready = sorted(
                name for name, deps in graph.items() if name not in resolved and deps <= resolved
            )
            order.extend(ready)
            resolved |= set(ready)
        return tuple(order)

    def permissions_for_stage(self, stage_name: str) -> tuple[RequiredPermission, ...]:
        """Least-privilege permission set for one stage (FR-009).

        Stages request only their own permissions. Nothing composes the union of all stages'
        permissions into a single identity, because that would recreate the broad standing access
        the secretless-identity/least-privilege rule prohibits.
        """
        return tuple(p for p in self.required_permissions if p.stage == stage_name)
