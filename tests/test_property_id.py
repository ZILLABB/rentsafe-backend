"""PropertyID minting and parsing.

The ID is the app's public handle for a building — printed on cards, shared in
WhatsApp messages, typed into search — so format and parse have to agree
exactly, in both directions.
"""

from __future__ import annotations

import pytest

from app.core import property_id


def test_area_codes_may_contain_digits():
    """The importer mints numeric-suffixed codes when suburb names collide.

    "Lekki Phase I" and "Lekki Phase II" both reduce to LEK, so the second
    becomes LE2. A letters-only rule meant `format_property_id` raised before
    anything was written, so no property could be registered in fifteen real
    Lagos areas — Lekki Phase I and II, Victoria Garden City, Surulere and
    Apapa among them.
    """
    value = property_id.format_property_id("ETI", "LE2", "7F3A2B", 41)
    assert value == "ETI-LE2-7F3A2B-0041"
    assert property_id.parse(value).area_code == "LE2"


def test_a_digit_area_code_round_trips():
    """format and parse have to agree, or IDs mint but never resolve."""
    for area in ("LE2", "SU2", "VI2", "AP2", "LEK"):
        value = property_id.format_property_id("ETI", area, "7F3A2B", 1)
        assert property_id.parse(value).value == value


def test_an_area_code_still_cannot_be_empty_or_punctuated():
    for bad in ("", "A", "LE-2", "LE_2", "LEKKIPHASEONE1"):
        with pytest.raises(ValueError):
            property_id.format_property_id("ETI", bad, "7F3A2B", 1)
