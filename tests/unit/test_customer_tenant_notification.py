"""CustomerTenant.notification_email and contact_display_name — gathered during conversation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from groundwork_contracts.tenant import CustomerTenant

_NOW = datetime(2026, 8, 15, tzinfo=UTC)
_TENANT_ID = "11111111-1111-1111-1111-111111111111"

_BASE: dict = {
    "tenant_id": _TENANT_ID,
    "display_name": "Contoso",
    "approved_regions": frozenset({"australiaeast"}),
    "data_residency_regions": frozenset({"australiaeast"}),
}


def test_notification_email_defaults_to_none() -> None:
    tenant = CustomerTenant(**_BASE)
    assert tenant.notification_email is None


def test_notification_email_can_be_set() -> None:
    tenant = CustomerTenant(**_BASE, notification_email="admin@contoso.onmicrosoft.com")
    assert tenant.notification_email == "admin@contoso.onmicrosoft.com"


def test_contact_display_name_defaults_to_none() -> None:
    tenant = CustomerTenant(**_BASE)
    assert tenant.contact_display_name is None


def test_contact_display_name_can_be_set_independently() -> None:
    tenant = CustomerTenant(
        **_BASE,
        notification_email="admin@contoso.onmicrosoft.com",
        contact_display_name="Jane Doe",
    )
    assert tenant.contact_display_name == "Jane Doe"


def test_contact_display_name_without_notification_email_is_valid() -> None:
    """contact_display_name has no dependency on notification_email — the two fields are
    independently optional and neither implies the other."""
    tenant = CustomerTenant(**_BASE, contact_display_name="Jane Doe")
    assert tenant.notification_email is None
    assert tenant.contact_display_name == "Jane Doe"


def test_notification_email_too_short_is_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CustomerTenant(**_BASE, notification_email="a@")
