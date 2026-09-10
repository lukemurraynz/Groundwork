"""``_normalise_subscription_id`` — repairs GUID dash placement from a voice-reconstructed id
without loosening what actually counts as a valid subscription id (see its own docstring)."""

from groundwork_controlplane.api.voice import _normalise_subscription_id


def test_correct_guid_passes_through_unchanged() -> None:
    guid = "c85c98e0-7313-4616-9602-1a22b29d29d2"
    assert _normalise_subscription_id(guid) == guid


def test_wrong_dash_placement_is_repaired() -> None:
    # Real failure observed live: the voice model reconstructed all 32 correct hex digits but
    # grouped the dashes differently than the canonical 8-4-4-4-12 form.
    malformed = "c85c98e0-7313-461-69602-1a22b29d29d2"
    assert _normalise_subscription_id(malformed) == "c85c98e0-7313-4616-9602-1a22b29d29d2"


def test_missing_or_extra_hex_digits_are_not_repaired() -> None:
    too_short = "c85c98e0-7313-4616-9602-1a22b29d29d"  # 31 hex digits
    assert _normalise_subscription_id(too_short) == too_short


def test_no_dashes_at_all_is_still_repaired() -> None:
    assert (
        _normalise_subscription_id("c85c98e07313461696021a22b29d29d2")
        == "c85c98e0-7313-4616-9602-1a22b29d29d2"
    )
