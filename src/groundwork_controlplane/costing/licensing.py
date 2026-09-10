"""The FR-013d below-F64 Power BI viewer-licensing disclosure (T081b).

Below F64, every user who views Power BI content in the deployed workspace needs their own
Power BI Pro or PPU licence — free-licence report consumption is unavailable below F64
(research notes § V-001). FR-013d's rule is that a customer must be *shown* this at plan time
and must not reach approval without it; the enforcement split is:

- the **statement** lives here, one canonical text every rendering surface shows;
- the **predicate** is ``DeploymentPlan.requires_licensing_disclosure`` (contracts);
- the **gate** is ``approval/service.record_approval``, which requires an explicit caller
  acknowledgement for a below-F64 plan before any approval record is constructed — the same
  shape as FR-019's ``acknowledged_cost_aud``, because it is the same class of guarantee: the
  caller must confirm they were shown something material before authorising it.
"""

from __future__ import annotations

from groundwork_contracts.blueprint import FabricCapacitySku

POWER_BI_VIEWER_LICENSING_DISCLOSURE = (
    "Users who view Power BI content in this workspace will each need a Power BI Pro or "
    "Power BI Premium Per User (PPU) licence. Free-licence report consumption is not "
    "available on the selected capacity size; it becomes available at F64 and above. By "
    "approving this plan you confirm this licensing requirement has been explained and "
    "accepted."
)
"""The one disclosure text, shown verbatim on every channel that presents a below-F64 plan."""


def licensing_disclosure_for(sku: FabricCapacitySku) -> str | None:
    """The disclosure statement when ``sku`` is below F64, ``None`` at or above.

    ``None`` is the honest "nothing to disclose" — a rendering surface shows nothing at all,
    rather than an empty string that could be mistaken for a missing translation.
    """
    if sku.capacity_units < FabricCapacitySku.F64.capacity_units:
        return POWER_BI_VIEWER_LICENSING_DISCLOSURE
    return None
