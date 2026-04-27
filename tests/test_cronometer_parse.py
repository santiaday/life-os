"""Tests for ingest_cronometer.parsers.

Coverage:
  - Servings: macro/micro split, eaten_at TZ conversion, source_row_hash
    stability, missing-time fallback to local noon.
  - Daily nutrition: typed-column mapping, micros catch-all.
  - Biometrics: metric normalization, source_row_hash stability.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from ingest_cronometer import parsers


@pytest.fixture
def fx() -> Path:
    return Path(__file__).parent / "fixtures" / "cronometer"


def _load(p: Path) -> str:
    return p.read_text()


# ---- servings --------------------------------------------------------------
def test_servings_basic_count(fx):
    rows = parsers.parse_servings(_load(fx / "servings.csv"))
    assert len(rows) == 7


def test_servings_macros_typed(fx):
    rows = parsers.parse_servings(_load(fx / "servings.csv"))
    oats = next(r for r in rows if "Oatmeal" in r["food_name"])
    assert oats["energy_kcal"] == pytest.approx(158)
    assert oats["protein_g"] == pytest.approx(5.5)
    assert oats["carbs_g"] == pytest.approx(28.0)
    assert oats["fat_g"] == pytest.approx(3.2)
    assert oats["fiber_g"] == pytest.approx(4.0)
    assert oats["meal_group"] == "Breakfast"
    assert oats["amount"] == pytest.approx(250)
    assert oats["unit"] == "g"


def test_servings_micros_overflow(fx):
    """Calcium and Iron aren't in fact_food_log columns; should land in micros JSONB."""
    rows = parsers.parse_servings(_load(fx / "servings.csv"))
    oats = next(r for r in rows if "Oatmeal" in r["food_name"])
    assert "Calcium (mg)" in oats["micros"]
    assert oats["micros"]["Calcium (mg)"] == pytest.approx(30.0)
    assert oats["micros"]["Iron (mg)"] == pytest.approx(1.7)


def test_servings_eaten_at_local_to_utc(fx):
    """Cronometer reports local-tz wall clock; we store UTC.
    07:30 EDT (UTC-4) on 2025-04-01 = 11:30 UTC."""
    rows = parsers.parse_servings(_load(fx / "servings.csv"))
    oats = next(r for r in rows if "Oatmeal" in r["food_name"])
    assert oats["eaten_at"].isoformat() == "2025-04-01T11:30:00+00:00"


def test_servings_alcohol_extracted(fx):
    rows = parsers.parse_servings(_load(fx / "servings.csv"))
    wine = next(r for r in rows if "Red wine" in r["food_name"])
    assert wine["alcohol_g"] == pytest.approx(15.6)
    assert wine["meal_group"] == "Snack 1"


def test_servings_caffeine_extracted(fx):
    rows = parsers.parse_servings(_load(fx / "servings.csv"))
    coffee = next(r for r in rows if "Coffee" in r["food_name"])
    assert coffee["caffeine_mg"] == pytest.approx(95.0)


def test_servings_source_row_hash_stable(fx):
    """Re-parsing the same CSV produces the same hash → upsert is a no-op."""
    rows1 = parsers.parse_servings(_load(fx / "servings.csv"))
    rows2 = parsers.parse_servings(_load(fx / "servings.csv"))
    assert {r["source_row_hash"] for r in rows1} == {r["source_row_hash"] for r in rows2}
    assert len({r["source_row_hash"] for r in rows1}) == 7


def test_servings_empty_time_defaults_to_noon():
    """If user lacks Cronometer Gold, time column is empty; we fall back to
    noon-local so the row still lands in the right `day` bucket."""
    csv = "Day,Time,Group,Food Name,Amount,Unit,Energy (kcal)\n2025-04-01,,Lunch,Apple,150,g,80\n"
    rows = parsers.parse_servings(csv)
    assert len(rows) == 1
    # 12:00 EDT = 16:00 UTC
    assert rows[0]["eaten_at"].hour == 16


# ---- daily nutrition -------------------------------------------------------
def test_daily_nutrition_count(fx):
    rows = parsers.parse_daily_nutrition(_load(fx / "daily_nutrition.csv"))
    assert len(rows) == 3


def test_daily_nutrition_typed_macros(fx):
    rows = parsers.parse_daily_nutrition(_load(fx / "daily_nutrition.csv"))
    apr1 = next(r for r in rows if r["day"].isoformat() == "2025-04-01")
    assert apr1["energy_kcal"] == pytest.approx(1810)
    assert apr1["protein_g"] == pytest.approx(95.7)
    assert apr1["alcohol_g"] == pytest.approx(15.6)


def test_daily_nutrition_micros(fx):
    rows = parsers.parse_daily_nutrition(_load(fx / "daily_nutrition.csv"))
    apr1 = next(r for r in rows if r["day"].isoformat() == "2025-04-01")
    assert "Calcium (mg)" in apr1["micros"]
    assert apr1["micros"]["Calcium (mg)"] == pytest.approx(180.0)


# ---- biometrics ------------------------------------------------------------
def test_biometrics_count_and_metric_normalization(fx):
    rows = parsers.parse_biometrics(_load(fx / "biometrics.csv"))
    assert len(rows) == 5
    metrics = {r["metric"] for r in rows}
    assert metrics == {
        "weight",
        "body_fat",
        "systolic_bp",
        "diastolic_bp",
        "resting_heart_rate",
    }


def test_biometrics_values(fx):
    rows = parsers.parse_biometrics(_load(fx / "biometrics.csv"))
    weight = next(r for r in rows if r["metric"] == "weight")
    assert weight["value"] == pytest.approx(82.4)
    assert weight["unit"] == "kg"
    bp_sys = next(r for r in rows if r["metric"] == "systolic_bp")
    assert bp_sys["note"] == "after lunch"


def test_biometrics_source_row_hash_stable(fx):
    rows1 = parsers.parse_biometrics(_load(fx / "biometrics.csv"))
    rows2 = parsers.parse_biometrics(_load(fx / "biometrics.csv"))
    assert {r["source_row_hash"] for r in rows1} == {r["source_row_hash"] for r in rows2}
    assert len({r["source_row_hash"] for r in rows1}) == 5


def test_biometrics_skip_rows_without_amount():
    csv = "Date,Time,Metric,Amount,Unit\n2025-04-01,07:00,Weight,,kg\n"
    rows = parsers.parse_biometrics(csv)
    assert rows == []


def test_metric_normalization_generic():
    """Unknown metrics get snake_cased rather than dropped."""
    csv = "Date,Time,Metric,Amount,Unit\n2025-04-01,07:00,VO2 Max,55.5,ml/kg/min\n"
    rows = parsers.parse_biometrics(csv)
    assert rows[0]["metric"] == "vo2_max"
