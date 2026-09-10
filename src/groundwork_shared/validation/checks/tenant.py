"""Tenant and subscription reachability assertions (T036).

Verified 2026-07-31 against the installed ``azure-mgmt-subscription`` SDK: ``Subscription.state``
is a server-populated, read-only ``str`` with possible values ``Enabled``, ``Warned``, ``PastDue``,
``Disabled``, ``Deleted`` — not an enum instance, and not ``Active``.

Split into a real Azure-calling wrapper and a pure decision function, same reasoning as
``groundwork_controlplane.api.main``'s health checks: the API call itself is Microsoft's SDK to get
right, not ours to re-verify with a fake client standing in for the whole service. What belongs to
this module and is worth unit testing is the decision — what a given subscription state means for
readiness — which :func:`_evaluate_subscription_state` isolates and
``tests/unit/test_readiness_check_decisions.py`` exercises directly.
"""

from __future__ import annotations

from azure.mgmt.subscription.aio import SubscriptionClient
from azure.mgmt.subscription.models import SubscriptionState

from groundwork_contracts.readiness import ValidationStatus
from groundwork_shared.validation.engine import ValidationContext

ASSERTION_ID = "tenant.subscription-reachable"


def _evaluate_subscription_state(subscription_id: str, state: str) -> tuple[ValidationStatus, str]:
    if state != SubscriptionState.ENABLED:
        return (
            ValidationStatus.FAILED,
            f"subscription {subscription_id} billing state is {state!r}, not "
            f"{SubscriptionState.ENABLED.value!r}",
        )
    return (
        ValidationStatus.PASSED,
        f"subscription {subscription_id} is reachable and {SubscriptionState.ENABLED.value}",
    )


async def subscription_reachable(context: ValidationContext) -> tuple[ValidationStatus, str]:
    """FR-014 assertion ``tenant.subscription-reachable``.

    A subscription that exists but is not ``Enabled`` cannot accept new resources — that is a real,
    actionable ``FAILED`` finding, not an error calling the API. An API call that itself fails
    (auth, network, throttling) is left to raise and is turned into ``UNREACHABLE`` by the engine.
    """
    async with SubscriptionClient(context.credential) as client:
        subscription = await client.subscriptions.get(context.subscription_id)

    return _evaluate_subscription_state(context.subscription_id, subscription.state or "")
