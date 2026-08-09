"""Packing-list generation and gap analysis.

These are the pieces that produce a confidently wrong answer when they break:
a requirement set that averages destinations instead of unioning them sends
you to the cold city with the light jacket, and a gap that gets silently
dropped is a thing you discover you don't own at the airport.
"""

from __future__ import annotations

from datetime import date

import pytest

from possessions import packing

# ---------------------------------------------------------------------------
# Profile: destinations union, they don't average
# ---------------------------------------------------------------------------

def _trip(days: int = 5, destinations=None) -> dict:
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "test trip",
        "start_date": date(2026, 10, 7),
        "end_date": date(2026, 10, 7 + days - 1),
        "destinations": destinations or [],
    }


def test_profile_unions_across_destinations():
    profile = packing.build_profile(_trip(destinations=[
        {"city": "Tokyo", "weather_tags": ["rain", "humid"], "low_c": 16, "high_c": 24},
        {"city": "Sapporo", "weather_tags": ["cold", "wind"], "low_c": 2, "high_c": 9},
    ]))
    # The coldest low and the warmest high both survive; nothing is averaged.
    assert profile.low_c == 2
    assert profile.high_c == 24
    assert profile.weather_tags == {"rain", "humid", "cold", "wind"}
    assert profile.cities == ["Tokyo", "Sapporo"]


def test_profile_merges_override_on_top_of_stored_destinations():
    """A caller saying "it'll rain" must not erase the temperatures the trip
    already carries."""
    trip = _trip(destinations=[{"city": "Kyoto", "low_c": 6}])
    profile = packing.build_profile(trip, {"weather_tags": ["rain"]})
    assert profile.low_c == 6
    assert "rain" in profile.weather_tags


def test_cold_tag_is_implied_by_temperature():
    profile = packing.build_profile(_trip(destinations=[{"low_c": 8}]))
    assert "cold" in profile.weather_tags


def test_heat_tag_is_implied_by_temperature():
    profile = packing.build_profile(_trip(destinations=[{"high_c": 31}]))
    assert "heat" in profile.weather_tags


def test_nights_is_inclusive_of_both_end_days():
    profile = packing.build_profile(_trip(days=1))
    assert profile.nights == 1


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------

def _slot(slots, key):
    return next((s for s in slots if s.key == key), None)


def test_rain_creates_a_waterproof_slot():
    slots = packing.build_slots(packing.Profile(nights=4, weather_tags={"rain"}))
    rain = _slot(slots, "outerwear.rain")
    assert rain is not None and rain.require_weather == ("rain",)
    assert rain.essential


def test_deep_cold_demands_more_warmth_than_mild_cold():
    mild = _slot(packing.build_slots(
        packing.Profile(nights=4, weather_tags={"cold"}, low_c=10)), "outerwear.warm")
    deep = _slot(packing.build_slots(
        packing.Profile(nights=4, weather_tags={"cold"}, low_c=-2)), "outerwear.warm")
    assert mild.min_warmth == 3
    assert deep.min_warmth == 4


def test_formality_adds_smart_slots_only_when_demanded():
    casual = packing.build_slots(packing.Profile(nights=3))
    formal = packing.build_slots(packing.Profile(nights=3, max_formality=4))
    assert _slot(casual, "top.formal") is None
    assert _slot(formal, "top.formal").min_formality == 4


def test_clothing_quantities_scale_with_nights_and_stay_capped():
    short = _slot(packing.build_slots(packing.Profile(nights=2)), "top.base")
    long = _slot(packing.build_slots(packing.Profile(nights=30)), "top.base")
    assert short.qty == 2                       # the floor, not 2 * 0.8 = 2
    assert long.qty == 8                        # the cap; 30 nights is a laundry decision


def test_second_pair_of_shoes_only_from_three_nights():
    assert _slot(packing.build_slots(packing.Profile(nights=2)), "footwear.base").qty == 1
    assert _slot(packing.build_slots(packing.Profile(nights=3)), "footwear.base").qty == 2


def test_adapter_slot_only_for_international_trips():
    domestic = packing.build_slots(packing.Profile(nights=3))
    abroad = packing.build_slots(packing.Profile(nights=3, international=True))
    assert _slot(domestic, "tech.adapter") is None
    assert _slot(abroad, "tech.adapter") is not None


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def _item(**kw) -> dict:
    base = {
        "id": kw.pop("id", "aaaaaaaa-0000-0000-0000-000000000001"),
        "name": "thing", "kind": "top", "subcategory": None, "brand": None,
        "primary_color": "black", "formality": 2, "warmth": 2,
        "weather_tags": [], "packed_volume_l": None, "weight_g": None,
        "condition": 3, "reviewed": True, "photo_thumb": None,
        "wear_count_365": 5, "days_since_worn": 10,
    }
    base.update(kw)
    return base


def test_eligibility_requires_all_of_the_slots_weather_tags():
    slot = packing.Slot(key="x", label="x", kind="outerwear", qty=1,
                        require_weather=("rain", "wind"))
    assert not packing._eligible(_item(kind="outerwear", weather_tags=["rain"]), slot)
    assert packing._eligible(_item(kind="outerwear", weather_tags=["rain", "wind"]), slot)


def test_eligibility_enforces_warmth_floor_and_ceiling():
    warm_slot = packing.Slot(key="x", label="x", kind="top", qty=1, min_warmth=4)
    assert not packing._eligible(_item(warmth=3), warm_slot)
    assert packing._eligible(_item(warmth=5), warm_slot)

    light_slot = packing.Slot(key="y", label="y", kind="top", qty=1, max_warmth=2)
    assert not packing._eligible(_item(warmth=4), light_slot)
    assert packing._eligible(_item(warmth=1), light_slot)


def test_missing_warmth_does_not_disqualify_against_a_ceiling():
    """An unmeasured item shouldn't be excluded from a hot-weather slot just
    because nobody has filled in its warmth yet."""
    slot = packing.Slot(key="y", label="y", kind="top", qty=1, max_warmth=2)
    assert packing._eligible(_item(warmth=None), slot)


def test_subcategory_restriction_is_exact():
    slot = packing.Slot(key="b", label="b", kind="bag", qty=1,
                        subcategories=("duffel", "backpack"))
    assert packing._eligible(_item(kind="bag", subcategory="duffel"), slot)
    assert not packing._eligible(_item(kind="bag", subcategory="tote"), slot)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def test_weather_match_outranks_a_non_matching_item():
    profile = packing.Profile(nights=3, weather_tags={"rain"})
    slot = packing.Slot(key="o", label="o", kind="outerwear", qty=1)
    matching, _ = packing._score(
        _item(kind="outerwear", weather_tags=["rain"]), slot, profile)
    plain, _ = packing._score(_item(kind="outerwear"), slot, profile)
    assert matching > plain


def test_worn_items_outrank_never_worn_ones():
    profile = packing.Profile(nights=3)
    slot = packing.Slot(key="t", label="t", kind="top", qty=1)
    worn, _ = packing._score(_item(wear_count_365=20), slot, profile)
    never, _ = packing._score(_item(wear_count_365=0), slot, profile)
    assert worn > never


def test_being_too_cold_is_penalised_harder_than_being_too_warm():
    """Overdressed is uncomfortable; underdressed is a problem."""
    profile = packing.Profile(nights=3, weather_tags={"cold"}, low_c=2)
    slot = packing.Slot(key="t", label="t", kind="top", qty=1)
    over, _ = packing._score(_item(warmth=5), slot, profile)   # wanted 4, +1
    under, _ = packing._score(_item(warmth=3), slot, profile)  # wanted 4, -1
    assert over > under


def test_unreviewed_items_are_deprioritised_but_still_eligible():
    profile = packing.Profile(nights=3)
    slot = packing.Slot(key="t", label="t", kind="top", qty=1)
    confirmed, _ = packing._score(_item(reviewed=True), slot, profile)
    provisional, reasons = packing._score(_item(reviewed=False), slot, profile)
    assert confirmed > provisional
    assert "tags unreviewed" in reasons


# ---------------------------------------------------------------------------
# End-to-end selection
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_inventory(monkeypatch):
    """A wardrobe with tops, bottoms, shoes and a bag — but no rain shell."""
    pool = [
        _item(id=f"aaaaaaaa-0000-0000-0000-00000000000{i}", name=f"tee {i}")
        for i in range(1, 5)
    ] + [
        _item(id="bbbbbbbb-0000-0000-0000-000000000001", name="jeans", kind="bottom"),
        _item(id="bbbbbbbb-0000-0000-0000-000000000002", name="chinos", kind="bottom"),
        _item(id="cccccccc-0000-0000-0000-000000000001", name="sneakers", kind="footwear"),
        _item(id="cccccccc-0000-0000-0000-000000000002", name="boots", kind="footwear"),
        _item(id="dddddddd-0000-0000-0000-000000000001", name="duffel", kind="bag",
              subcategory="duffel"),
    ]
    monkeypatch.setattr(packing, "_candidates", lambda: pool)
    return pool


def test_missing_rain_shell_is_reported_as_an_essential_gap(monkeypatch, fake_inventory):
    monkeypatch.setattr(packing.service, "get_trip",
                        lambda _tid: _trip(destinations=[{"city": "Kyoto",
                                                          "weather_tags": ["rain"]}]))
    result = packing.build_packing_list("11111111-1111-1111-1111-111111111111")
    rain_gap = next(g for g in result["gaps"] if g["slot"] == "outerwear.rain")
    assert rain_gap["essential"]
    assert rain_gap["found"] == 0
    assert "rain" in rain_gap["reason"]
    assert "Kyoto" in rain_gap["reason"]


def test_an_item_can_satisfy_two_slots_without_being_packed_twice(monkeypatch):
    """The smart trousers are also a pair of trousers. Counting them once per
    slot is what makes a three-night trip ask for four bottoms."""
    smart = _item(id="bbbbbbbb-0000-0000-0000-000000000009", name="wool trousers",
                  kind="bottom", formality=4)
    monkeypatch.setattr(packing, "_candidates", lambda: [smart])
    monkeypatch.setattr(packing.service, "get_trip",
                        lambda _tid: _trip(days=2, destinations=[{"formality": [1, 4]}]))
    result = packing.build_packing_list("11111111-1111-1111-1111-111111111111")

    bottoms = [i for i in result["items"] if i["kind"] == "bottom"]
    assert len(bottoms) == 1
    assert set(bottoms[0]["slots"]) == {"bottom.base", "bottom.formal"}


def test_selection_never_exceeds_the_slot_quantity(monkeypatch, fake_inventory):
    monkeypatch.setattr(packing.service, "get_trip", lambda _tid: _trip(days=2))
    result = packing.build_packing_list("11111111-1111-1111-1111-111111111111")
    tops = [i for i in result["items"] if i["kind"] == "top"]
    assert len(tops) == 2                       # nights=2 -> floor of 2


def test_read_only_by_default(monkeypatch, fake_inventory):
    """Generating a list to look at must not write anything."""
    monkeypatch.setattr(packing.service, "get_trip", lambda _tid: _trip())
    called = []
    monkeypatch.setattr(packing.service, "save_packing_candidates",
                        lambda *a, **k: called.append(a))
    result = packing.build_packing_list("11111111-1111-1111-1111-111111111111")
    assert called == []
    assert result["persisted"] is False


def test_persist_writes_the_selection(monkeypatch, fake_inventory):
    monkeypatch.setattr(packing.service, "get_trip", lambda _tid: _trip())
    called = []
    monkeypatch.setattr(packing.service, "save_packing_candidates",
                        lambda trip_id, sel: called.append(sel) or len(sel))
    packing.build_packing_list("11111111-1111-1111-1111-111111111111", persist=True)
    assert called and len(called[0]) > 0


def test_unknown_trip_is_a_caller_error(monkeypatch):
    monkeypatch.setattr(packing.service, "get_trip", lambda _tid: None)
    with pytest.raises(packing.service.PossessionError):
        packing.build_packing_list("11111111-1111-1111-1111-111111111111")


# ---------------------------------------------------------------------------
# Standalone gap query
# ---------------------------------------------------------------------------

def test_find_wardrobe_gaps_rejects_unknown_weather_tags(monkeypatch):
    monkeypatch.setattr(packing, "_candidates", list)
    with pytest.raises(packing.service.PossessionError):
        packing.find_wardrobe_gaps(["monsoon"])


def test_find_wardrobe_gaps_reports_an_empty_wardrobe_as_all_gaps(monkeypatch):
    monkeypatch.setattr(packing, "_candidates", list)
    result = packing.find_wardrobe_gaps(["rain"], nights=4)
    assert result["essential_gaps"] > 0
    assert result["covered"] == []
    assert any(g["slot"] == "outerwear.rain" for g in result["gaps"])
