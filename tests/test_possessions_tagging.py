"""Vision-tagger schema and the post-schema repair pass.

Structured outputs guarantee the *shape* of what comes back. What it can't
guarantee is that the subcategory belongs to the kind (a conditional the
format doesn't express), so `_repair` is the only thing standing between a
plausible-looking "running shoe" on a t-shirt and the database. These tests
cover that boundary.
"""

from __future__ import annotations

import pytest

from possessions import tagging

COLORS = ["black", "navy", "white", "olive", "multicolor"]
SUBCATS = {
    "top": ["crew tee", "hoodie"],
    "footwear": ["running shoe"],
    "outerwear": ["rain shell"],
}


@pytest.fixture(autouse=True)
def stub_vocab(monkeypatch):
    """Keep these tests off the database — the vocabulary is a DB read."""
    monkeypatch.setattr(tagging.vocab, "colors", lambda: COLORS)
    monkeypatch.setattr(tagging.vocab, "subcategories", lambda: SUBCATS)
    monkeypatch.setattr(
        tagging.vocab, "is_valid_subcategory",
        lambda kind, sub: sub is None or sub in SUBCATS.get(kind, []))
    monkeypatch.setattr(
        tagging.vocab, "normalize_color",
        lambda v: (v.strip().lower() if v and v.strip().lower() in COLORS else None))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_schema_pins_colour_to_the_vocabulary():
    schema = tagging.build_schema(COLORS)
    assert schema["properties"]["primary_color"]["enum"] == COLORS


def test_schema_allows_null_for_the_optional_fields():
    schema = tagging.build_schema(COLORS)
    for field in ("brand", "subcategory", "material", "size", "secondary_color"):
        assert {"type": "null"} in schema["properties"][field]["anyOf"], field


def test_schema_is_closed_and_requires_every_field():
    schema = tagging.build_schema(COLORS)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["confidence"]["additionalProperties"] is False


def test_schema_expresses_the_1_to_5_scales_as_enums():
    """Structured outputs doesn't support minimum/maximum, so a range has to
    be an enum or it isn't enforced at all."""
    schema = tagging.build_schema(COLORS)
    assert schema["properties"]["formality"]["enum"] == [1, 2, 3, 4, 5]
    assert schema["properties"]["warmth"]["enum"] == [1, 2, 3, 4, 5]


def test_prompt_carries_the_colour_list_and_the_per_kind_subcategories():
    prompt = tagging.build_prompt(COLORS, SUBCATS)
    assert "navy" in prompt and "olive" in prompt
    assert "running shoe" in prompt
    # The brand rule is the one that prevents fiction; it must survive edits.
    assert "Never infer a brand" in prompt


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------

def _record(**kw) -> dict:
    base = {
        "name": "navy tee", "kind": "top", "subcategory": "crew tee",
        "brand": None, "colorway": None, "primary_color": "navy",
        "secondary_color": None, "pattern": "solid", "material": "cotton",
        "size": None, "formality": 2, "warmth": 2, "weather_tags": [],
        "confidence": {"kind": 0.9, "primary_color": 0.95},
    }
    base.update(kw)
    return base


def test_subcategory_from_the_wrong_kind_is_dropped_not_stored():
    out = tagging._repair(_record(kind="top", subcategory="running shoe"),
                          model="test")
    assert out["subcategory"] is None
    assert out["confidence"]["subcategory"] == 0.0


def test_valid_subcategory_survives():
    out = tagging._repair(_record(kind="top", subcategory="hoodie"), model="test")
    assert out["subcategory"] == "hoodie"


def test_off_vocabulary_colour_is_nulled_rather_than_coerced():
    out = tagging._repair(_record(secondary_color="midnight"), model="test")
    assert out["secondary_color"] is None
    assert out["confidence"]["secondary_color"] == 0.0


def test_unusable_primary_colour_falls_back_without_dropping_the_item():
    """primary_color is NOT NULL. Losing the whole photo over one bad field
    would be a worse trade than parking it for review."""
    out = tagging._repair(_record(primary_color="chartreuse"), model="test")
    assert out["primary_color"] == "multicolor"
    assert out["confidence"]["primary_color"] == 0.0


def test_unknown_weather_tags_are_filtered_and_the_rest_deduplicated():
    out = tagging._repair(
        _record(weather_tags=["rain", "rain", "monsoon", "wind"]), model="test")
    assert out["weather_tags"] == ["rain", "wind"]


def test_confidence_is_clamped_and_non_numeric_becomes_none():
    out = tagging._repair(
        _record(confidence={"kind": 1.7, "brand": -3, "material": "high"}),
        model="test")
    assert out["confidence"]["kind"] == 1.0
    assert out["confidence"]["brand"] == 0.0
    assert out["confidence"]["material"] is None


def test_repair_records_which_model_produced_the_tags():
    out = tagging._repair(_record(), model="claude-opus-5")
    assert out["tag_model"] == "claude-opus-5"


# ---------------------------------------------------------------------------
# Review flagging
# ---------------------------------------------------------------------------

def test_low_confidence_fields_include_missing_ones():
    """A field the model didn't score is exactly as unverified as one it
    scored badly — both belong in the review queue's Tab path."""
    flagged = tagging.low_confidence_fields(
        {"brand": 0.2, "primary_color": 0.98, "material": None}, threshold=0.7)
    assert flagged == ["brand", "material"]


def test_everything_confident_needs_no_review():
    assert tagging.low_confidence_fields({"brand": 0.9, "kind": 0.95},
                                         threshold=0.7) == []
