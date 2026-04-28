"""Pure transforms for Whoop journal payloads.

Three response shapes:
  1. Behavior-catalog entries → dim_whoop_behavior rows
  2. tracked_behaviors[] inside a daily draft → fact_habit_log rows
  3. integrations.tracker_inputs[] → fact_food_daily_apple_health row

Whoop's response field naming isn't stable across releases — we look up by
the documented field names with safe defaults so missing fields don't crash.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Any

# ---- behavior catalog (dim) ------------------------------------------------
def transform_behavior(api: dict) -> dict | None:
    """Map a /v3/journals/behaviors entry to dim_whoop_behavior row."""
    bid = api.get("id") or api.get("behavior_id")
    internal = api.get("internal_name") or api.get("name")
    title = api.get("title") or api.get("display_name") or internal
    if bid is None or not internal or not title:
        return None
    mag = api.get("magnitude") or api.get("magnitude_input") or {}
    return {
        "behavior_id": int(bid),
        "internal_name": str(internal),
        "title": str(title),
        "question_text": api.get("question_text") or api.get("question"),
        "category": api.get("category") or api.get("group"),
        "behavior_type": api.get("behavior_type") or api.get("type"),
        "question_type": api.get("question_type"),
        "magnitude_type": mag.get("type") or mag.get("magnitude_type"),
        "magnitude_unit": mag.get("unit"),
        "magnitude_min": _safe_num(mag.get("min")),
        "magnitude_max": _safe_num(mag.get("max")),
        "status": api.get("status") or "active",
    }


# ---- tracked behaviors (fact) ----------------------------------------------
def transform_tracked_behavior(api: dict, day: date) -> dict | None:
    """Map a tracked_behaviors[i] entry to a fact_habit_log row.

    Whoop nests behavior metadata at api['behavior']; the user's answer is
    at the top level (answered_yes, magnitude_input_value, time_input_value).
    """
    behavior = api.get("behavior") or {}
    bid = behavior.get("id") or api.get("behavior_id")
    internal = behavior.get("internal_name") or behavior.get("name") or api.get("internal_name")
    if bid is None or not internal:
        return None

    answered_yes = api.get("answered_yes")
    magnitude = _safe_num(api.get("magnitude_input_value"))
    magnitude_unit = (behavior.get("magnitude") or {}).get("unit")
    time_input = _to_dt(api.get("time_input_value"))

    row = {
        "day": day,
        "source": "whoop_journal",
        "habit_key": str(internal),
        "whoop_behavior_id": int(bid),
        "whoop_journal_entry_id": _safe_int(api.get("journal_entry_id")),
        "whoop_cycle_id": _safe_int(api.get("cycle_id")),
        "answered_yes": bool(answered_yes) if answered_yes is not None else None,
        "magnitude_value": magnitude,
        "magnitude_unit": magnitude_unit,
        "time_input_value": time_input,
        "user_reviewed": bool(api.get("user_reviewed")) if api.get("user_reviewed") is not None else None,
        "notes": api.get("notes"),
    }
    row["source_row_hash"] = hashlib.sha256(
        f"{day.isoformat()}|{bid}|{answered_yes}|{magnitude}|{time_input}".encode()
    ).hexdigest()
    return row


# ---- Apple Health integrations ---------------------------------------------
# Whoop's tracker_inputs are name → value pairs. Names observed:
#   Calories, Protein, Carbs, Fats, Fiber, Sodium, Calcium, Magnesium, Water
# Mapping to fact_food_daily_apple_health columns:
TRACKER_FIELD_MAP = {
    "calories": "energy_kcal",
    "energy": "energy_kcal",
    "protein": "protein_g",
    "carbs": "carbs_g",
    "carbohydrates": "carbs_g",
    "fats": "fat_g",
    "fat": "fat_g",
    "fiber": "fiber_g",
    "sodium": "sodium_mg",
    "calcium": "calcium_mg",
    "magnesium": "magnesium_mg",
    "water": "water_servings",
}


def transform_tracker_inputs(payload: dict, day: date) -> dict | None:
    """Pull integrations.tracker_inputs[] into a fact_food_daily_apple_health
    row. Returns None if no integrations or no recognized fields."""
    integrations = payload.get("integrations") or {}
    inputs = integrations.get("tracker_inputs") or []
    if not inputs:
        return None

    row: dict[str, Any] = {"day": day, "source": "apple_health_via_whoop"}
    for entry in inputs:
        name = (entry.get("name") or entry.get("input_name") or "").strip().lower()
        value = _safe_num(entry.get("value") or entry.get("amount"))
        if not name or value is None:
            continue
        col = TRACKER_FIELD_MAP.get(name)
        if col:
            row[col] = value

    if len(row) <= 2:  # only day + source = no recognized macros
        return None

    # Stash the original payload so future column additions can backfill.
    row["payload"] = inputs
    return row


# ---- helpers ---------------------------------------------------------------
def _safe_num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None
