"""Pure transforms: Cal AI food objects -> fact_food_log rows.

The Cal AI "food object" shape is CONFIRMED from the mitmproxy capture (the
/v6/fixFood and /v6/health-score payloads). Top-level totals already reflect the
sum of the item's ingredients; `servings` is how many of the whole item were
logged. Example (verified):

    {"name": "Grilled Salmon with Roasted Potatoes and Lemon",
     "servings": 1, "calories": 752, "carbs": 66, "fats": 24, "protein": 68,
     "sugar": 0, "fiber": 0, "sodium": 0,
     "ingredients": [{"name": "Salmon", "calories": 426, "protein": 64,
                      "carbs": 0, "fats": 18, "ethanol": 0, "servings": 2, ...}, ...]}

These map cleanly onto fact_food_log (Cronometer-shaped). The DIARY WRAPPER
around this object (Firestore doc id, logged-at timestamp, meal type, image id)
is finalized against a real Firestore document — see ingest.py / RUNBOOK.md.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime


def _f(v) -> float | None:
    """Safe float: handles None, NaN, and Cal AI's stray 'NaN' strings."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _mul(v, factor: float) -> float | None:
    f = _f(v)
    return None if f is None else round(f * factor, 4)


def _first(food: dict, *keys):
    """First present, non-None value among keys — Cal AI uses different field
    names in the /v6 analysis payload vs the stored Firestore diary doc."""
    for k in keys:
        v = food.get(k)
        if v is not None:
            return v
    return None


def transform_food_object(food: dict) -> dict:
    """Normalize one Cal AI food object to the consumed-macro fields used by
    fact_food_log. Multiplies the item's totals by `servings` so the result is
    what was actually eaten. Stores the rich ingredient/health detail under
    `micros`. Returns macro fields only (no identity/time — see food_to_log_row).

    Handles BOTH Cal AI shapes (verified against real data):
      * /v6 analysis payload:  calories + servings
      * Firestore diary doc:   servingCalories + quantity (per-serving + multiplier)
    """
    servings = _f(_first(food, "servings", "quantity"))
    factor = servings if (servings and servings > 0) else 1.0

    carbs = _mul(food.get("carbs"), factor)
    fiber = _mul(food.get("fiber"), factor)
    net_carbs = None
    if carbs is not None:
        net_carbs = round(carbs - (fiber or 0.0), 4)

    # Alcohol: Cal AI tags it per-ingredient as `ethanol` (grams). Sum across
    # ingredients; ingredient.calories/etc. already include the ingredient's own
    # servings, so ethanol is taken as-is then scaled by the item factor.
    ethanol = 0.0
    has_ethanol = False
    for ing in food.get("ingredients") or []:
        e = _f(ing.get("ethanol"))
        if e:
            ethanol += e
            has_ethanol = True
    alcohol_g = round(ethanol * factor, 4) if has_ethanol else None

    return {
        "food_name": food.get("name"),
        "amount": factor,
        "unit": "serving",
        # diary doc: per-serving "servingCalories"; analysis payload: total "calories".
        "energy_kcal": _mul(_first(food, "calories", "servingCalories"), factor),
        "protein_g": _mul(food.get("protein"), factor),
        "carbs_g": carbs,
        "net_carbs_g": net_carbs,
        "fiber_g": fiber,
        "sugar_g": _mul(food.get("sugar"), factor),
        # Cal AI uses "fats" (plural) at the top level, "fat" inside servingTypes.
        "fat_g": _mul(food.get("fats", food.get("fat")), factor),
        "saturated_fat_g": None,
        "sodium_mg": _mul(food.get("sodium"), factor),
        "potassium_mg": None,
        "caffeine_mg": None,
        "alcohol_g": alcohol_g,
        "micros": {
            "ingredients": food.get("ingredients") or [],
            "servings": servings,
            "trace_id": food.get("traceId"),
            "ethanol_carb_ratio": food.get("ethanolCarbRatio"),
        },
    }


def meal_group_from_time(logged_at: datetime | None) -> str:
    """Heuristic meal bucket from LOCAL hour, used only if Cal AI doesn't carry
    an explicit meal type on the diary entry. logged_at is UTC; convert to the
    warehouse-local tz first or an 8pm dinner (00:00 UTC) buckets as breakfast."""
    if logged_at is None:
        return "uncategorized"
    from lifeos_core.tz import local_tz
    h = logged_at.astimezone(local_tz()).hour if logged_at.tzinfo else logged_at.hour
    if h < 11:
        return "breakfast"
    if h < 16:
        return "lunch"
    if h < 21:
        return "dinner"
    return "snack"


def food_to_log_row(
    food: dict,
    *,
    entry_id: str,
    logged_at: datetime | None,
    meal_group: str | None = None,
    image_id: str | None = None,
    health_score: dict | None = None,
) -> dict:
    """Build a fact_food_log row from a Cal AI food object + its diary metadata.
    Idempotent: source_row_hash is derived from Cal AI's stable entry_id, so a
    re-ingest updates in place instead of duplicating. Note: fact_food_log.day is
    a GENERATED column (from eaten_at), so it is intentionally NOT set here."""
    macros = transform_food_object(food)
    macros["micros"]["image_id"] = image_id
    macros["micros"]["health_score"] = health_score
    return {
        **macros,
        "eaten_at": logged_at,
        "meal_group": meal_group or meal_group_from_time(logged_at),
        "source": "calai",
        "source_row_hash": hashlib.sha256(f"calai|{entry_id}".encode()).hexdigest(),
        "updated_at": datetime.now().astimezone(),
    }
