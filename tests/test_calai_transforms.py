"""Tests for Cal AI food-object transforms.

Grounded in the REAL captured /v6/fixFood payload (tests/fixtures/calai/), so
these pin the mapping against actual Cal AI data, not a guess.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ingest_calai.transforms import (
    food_to_log_row,
    meal_group_from_time,
    transform_food_object,
)

FIX = Path(__file__).parent / "fixtures" / "calai" / "fixfood_grilled_salmon.json"


def _food() -> dict:
    return json.loads(FIX.read_text())


def test_transform_matches_captured_macros():
    out = transform_food_object(_food())
    assert out["food_name"] == "Grilled Salmon with Roasted Potatoes and Lemon"
    assert out["energy_kcal"] == 752
    assert out["protein_g"] == 68
    assert out["carbs_g"] == 66
    assert out["fat_g"] == 24          # Cal AI's top-level "fats"
    assert out["fiber_g"] == 0
    assert out["net_carbs_g"] == 66    # carbs - fiber
    assert out["sugar_g"] == 0
    # rich detail preserved
    assert len(out["micros"]["ingredients"]) == 5
    assert out["micros"]["trace_id"] == "edc0bc80-6174-4dc4-b5e8-e11a29978102"


def test_servings_multiplier():
    food = _food()
    food["servings"] = 2          # logged two of the whole meal
    out = transform_food_object(food)
    assert out["energy_kcal"] == 1504
    assert out["protein_g"] == 136
    assert out["amount"] == 2


def test_zero_or_missing_servings_defaults_to_one():
    food = _food()
    food["servings"] = 0
    assert transform_food_object(food)["energy_kcal"] == 752
    del food["servings"]
    assert transform_food_object(food)["energy_kcal"] == 752


def test_nan_and_none_are_safe():
    out = transform_food_object({"name": "x", "servings": 1, "calories": "NaN",
                                 "protein": None, "carbs": 10, "fats": 2})
    assert out["energy_kcal"] is None
    assert out["protein_g"] is None
    assert out["carbs_g"] == 10


def test_alcohol_summed_from_ingredient_ethanol():
    food = {"name": "Wine + cheese", "servings": 1, "calories": 200,
            "carbs": 5, "fats": 8, "protein": 6,
            "ingredients": [{"name": "Wine", "ethanol": 12}, {"name": "Cheese", "ethanol": 0}]}
    assert transform_food_object(food)["alcohol_g"] == 12
    # no ethanol anywhere -> None, not 0
    assert transform_food_object(_food())["alcohol_g"] is None


def test_real_diary_doc_shape():
    """The stored Firestore diary doc uses servingCalories + quantity (verified
    against the user's real docs), not the analysis payload's calories+servings."""
    doc = {"name": "Grandma's Cookies", "servingCalories": 320, "quantity": 2.0,
           "protein": 2, "carbs": 46, "fats": 14, "ingredients": [],
           "date": "2025-04-03T18:00:00Z", "ethanolCarbRatio": 0.0}
    out = transform_food_object(doc)
    assert out["energy_kcal"] == 640    # 320 per serving * quantity 2
    assert out["protein_g"] == 4
    assert out["carbs_g"] == 92
    assert out["fat_g"] == 28
    assert out["amount"] == 2.0


def test_extract_handles_real_diary_doc():
    from ingest_calai.ingest import _extract
    doc = {"_name": "projects/calai-app/databases/(default)/documents/foods/ABC",
           "id": "ABC", "name": "Bagel", "servingCalories": 210, "quantity": 1.0,
           "protein": 7, "carbs": 42, "fats": 1, "date": "2025-04-04T15:30:00Z",
           "image": "IMG-123", "healthRating": {"rating": 4}}
    ex = _extract(doc)
    assert ex is not None
    assert ex["entry_id"] == "ABC"
    assert ex["image_id"] == "IMG-123"
    assert ex["health_score"] == {"rating": 4}
    assert ex["logged_at"].year == 2025 and ex["logged_at"].month == 4
    assert ex["food"]["servingCalories"] == 210
    # a non-food doc is skipped
    assert _extract({"_name": ".../x", "id": "x", "referralCode": "JYMMOU"}) is None


def test_meal_group_buckets():
    assert meal_group_from_time(datetime(2026, 6, 14, 8, tzinfo=UTC)) == "breakfast"
    assert meal_group_from_time(datetime(2026, 6, 14, 13, tzinfo=UTC)) == "lunch"
    assert meal_group_from_time(datetime(2026, 6, 14, 19, tzinfo=UTC)) == "dinner"
    assert meal_group_from_time(datetime(2026, 6, 14, 23, tzinfo=UTC)) == "snack"
    assert meal_group_from_time(None) == "uncategorized"


def test_food_to_log_row_idempotent_hash_and_shape():
    when = datetime(2026, 6, 14, 19, 30, tzinfo=UTC)
    row = food_to_log_row(_food(), entry_id="abc123", logged_at=when, image_id="IMG1")
    assert row["source"] == "calai"
    assert row["meal_group"] == "dinner"
    assert row["eaten_at"] == when
    assert row["energy_kcal"] == 752
    assert row["micros"]["image_id"] == "IMG1"
    assert "day" not in row  # fact_food_log.day is a generated column
    # stable hash from entry_id -> re-ingest updates in place
    row2 = food_to_log_row(_food(), entry_id="abc123", logged_at=when)
    assert row["source_row_hash"] == row2["source_row_hash"]
    row3 = food_to_log_row(_food(), entry_id="different", logged_at=when)
    assert row["source_row_hash"] != row3["source_row_hash"]
