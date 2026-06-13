"""Pure transforms for Whoop journal payloads.

Four response shapes:
  1. Behavior-catalog entries → dim_whoop_behavior rows
  2. tracked_behaviors[] inside a daily draft → fact_habit_log rows
  3. integrations.tracker_inputs[] → fact_food_daily_apple_health row +
     synthesized fact_habit_log rows (one per recognized autofill name)
  4. The day's draft envelope → fact_journal_day row (notes, cycle_id, etc.)

Whoop's response field naming isn't stable across releases — we look up by
the documented field names with safe defaults so missing fields don't crash.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime
from typing import Any


# ---- behavior catalog (dim) ------------------------------------------------
def transform_behavior(api: dict) -> dict | None:
    """Map a /v2/journals/behaviors record to a dim_whoop_behavior row.

    Real v2 record shape:
        {"id": 338, "title": "Accutane", "internal_name": "accutane",
         "category": "Drugs & Medication", "question_text": "Took Accutane?",
         "status": "active", "sticky": false, "subtitle": null,
         "description": null, ...}

    The behavior_tracker rows nested in tracked_behaviors[] use the same
    shape plus a "magnitude" sub-object — accept both."""
    bid = api.get("id") or api.get("behavior_id")
    internal = api.get("internal_name") or api.get("name")
    title = api.get("title") or api.get("display_name") or internal
    if bid is None or not internal or not title:
        return None
    mag = api.get("magnitude") or api.get("magnitude_input") or {}
    if not isinstance(mag, dict):
        mag = {}
    return {
        "behavior_id": int(bid),
        "internal_name": str(internal),
        "title": str(title),
        "question_text": api.get("question_text") or api.get("question"),
        "category": api.get("category") or api.get("journal_view_category") or api.get("group"),
        "behavior_type": api.get("behavior_type") or api.get("type"),
        "question_type": api.get("question_type"),
        "magnitude_type": mag.get("type") or mag.get("magnitude_type"),
        # v2 records use "units" (plural) on the magnitude sub-object; accept both.
        "magnitude_unit": mag.get("units") or mag.get("unit"),
        "magnitude_min": _safe_num(mag.get("minimum_inclusive") or mag.get("min")),
        "magnitude_max": _safe_num(mag.get("maximum_inclusive") or mag.get("max")),
        "status": api.get("status") or "active",
    }


def synthesize_dim_from_autofill(name: str, behavior_id: int) -> dict:
    """When an autofill row references a behavior we don't have in the catalog
    yet, synthesize a minimal dim row so the FK survives. The next catalog
    refresh will overwrite our placeholder with the real metadata."""
    internal = _slugify(name)
    return {
        "behavior_id": behavior_id,
        "internal_name": internal,
        "title": name,
        "question_text": None,
        "category": "AUTOFILL",
        "behavior_type": "NORMAL",
        "question_type": None,
        "magnitude_type": None,
        "magnitude_unit": None,
        "magnitude_min": None,
        "magnitude_max": None,
        "status": "active",
    }


# ---- tracked behaviors (fact) ----------------------------------------------
def transform_tracked_behavior(api: dict, day: date) -> dict | None:
    """Map a tracked_behaviors[i] entry to a fact_habit_log row.

    Real shape (from /journal-service/v3/journals/drafts/mobile/{date}):
        {
          "tracker_input": {
            "source": "USER",
            "answered_yes": true|false|null,
            "magnitude_input_value": <number>|null,
            "time_input_value": <epoch_ms>|null,
            "behavior_tracker_id": <int>,
            "journal_entry_id": <int>,
            ...
          },
          "behavior_tracker": {
            "id": <int>,
            "internal_name": "alcohol",
            "title": "Alcohol",
            "magnitude": {"units": "drinks", ...} | null,
            "behavior_type": "POSITIVE|NEGATIVE|NORMAL",
            ...
          }
        }

    Returns None when behavior_id or internal_name can't be derived — both
    are non-nullable downstream. Falls back to legacy flat shape (used by
    the unit-test fixtures + older API versions) so existing tests keep
    working without changes.
    """
    tracker_input = api.get("tracker_input") or {}
    behavior_tracker = api.get("behavior_tracker") or api.get("behavior") or {}

    bid = (
        behavior_tracker.get("id")
        or tracker_input.get("behavior_tracker_id")
        or api.get("behavior_id")
    )
    internal = (
        behavior_tracker.get("internal_name")
        or behavior_tracker.get("name")
        or api.get("internal_name")
    )
    if bid is None or not internal:
        return None

    # Answers can be at tracker_input.* (real) or top-level (legacy/test fixtures).
    answered_yes = tracker_input.get("answered_yes", api.get("answered_yes"))
    magnitude = _safe_num(
        tracker_input.get("magnitude_input_value", api.get("magnitude_input_value"))
    )
    magnitude_meta = behavior_tracker.get("magnitude") or {}
    magnitude_unit = magnitude_meta.get("units") or magnitude_meta.get("unit")
    time_input = _to_dt(
        tracker_input.get("time_input_value", api.get("time_input_value"))
    )
    journal_entry_id = tracker_input.get("journal_entry_id", api.get("journal_entry_id"))
    cycle_id = tracker_input.get("cycle_id", api.get("cycle_id"))
    src = tracker_input.get("source")
    # Whoop tags Apple-Health-imported rows with source != USER. Surface that
    # cleanly so downstream queries can distinguish.
    fact_source = "whoop_apple_health" if src and src != "USER" else "whoop_private_api"

    row = {
        "day": day,
        "source": fact_source,
        "habit_key": str(internal),
        "behavior_id": int(bid),
        "whoop_journal_entry_id": _safe_int(journal_entry_id),
        "whoop_cycle_id": _safe_int(cycle_id),
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
    row. Returns None if no integrations or no recognized fields.

    Real shape per entry:
        {"behavior_tracker_id": 145, "source_tracking_key": "Calories",
         "magnitude_input_value": 2400.0, "answered_yes": true,
         "source_display_name": "Apple Health", ...}
    """
    integrations = payload.get("integrations") or {}
    inputs = integrations.get("tracker_inputs") or []
    if not inputs:
        return None

    row: dict[str, Any] = {"day": day, "source": "whoop_apple_health"}
    for entry in inputs:
        # Prefer source_tracking_key (Apple-Health-style), fall back to
        # name/input_name (legacy/test fixtures).
        name = (
            entry.get("source_tracking_key")
            or entry.get("name")
            or entry.get("input_name")
            or ""
        ).strip().lower()
        value = _safe_num(
            entry.get("magnitude_input_value")
            or entry.get("value")
            or entry.get("amount")
        )
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


def transform_autofill_input(entry: dict, day: date) -> dict | None:
    """Map one ``integrations.tracker_inputs[i]`` autofill record to a
    fact_habit_log row.

    Synthesizes ``habit_key`` from ``source_tracking_key`` (slugified) when
    the API doesn't expose a real ``internal_name``. If the user later logs
    that same behavior manually, the ON CONFLICT (day, behavior_id) upsert
    overwrites the synthesized record with the real one — correct.

    Real shape: see transform_tracker_inputs above. ``magnitude_input_value``
    + ``time_input_value`` (epoch ms) carry the data; behavior metadata is
    referenced by ``behavior_tracker_id`` only (no nested behavior dict).
    """
    bid = (
        entry.get("behavior_tracker_id")
        or (entry.get("behavior_tracker") or {}).get("id")
        or entry.get("behavior_id")
        or (entry.get("behavior") or {}).get("id")
    )
    if bid is None:
        return None

    raw_name = (
        (entry.get("behavior_tracker") or {}).get("internal_name")
        or (entry.get("behavior") or {}).get("internal_name")
        or entry.get("internal_name")
        or entry.get("source_tracking_key")
        or entry.get("name")
        or entry.get("input_name")
    )
    if not raw_name:
        return None

    habit_key = _slugify(raw_name)
    value = _safe_num(
        entry.get("magnitude_input_value")
        or entry.get("value")
        or entry.get("amount")
    )
    unit = (
        entry.get("unit")
        or (entry.get("behavior_tracker") or {}).get("magnitude", {}).get("units")
        or (entry.get("behavior") or {}).get("magnitude", {}).get("unit")
    )
    time_input = _to_dt(
        entry.get("time_input_value") or entry.get("recorded_at")
    )

    # answered_yes is explicit on the entry; only fall back to "True iff
    # we have a magnitude" when the field is missing.
    if "answered_yes" in entry and entry["answered_yes"] is not None:
        answered_yes = bool(entry["answered_yes"])
    else:
        answered_yes = True if value is not None else None

    row = {
        "day": day,
        "source": "whoop_apple_health",
        "habit_key": habit_key,
        "behavior_id": int(bid),
        "whoop_journal_entry_id": _safe_int(entry.get("journal_entry_id")),
        "whoop_cycle_id": _safe_int(entry.get("cycle_id")),
        "answered_yes": answered_yes,
        "magnitude_value": value,
        "magnitude_unit": unit,
        "time_input_value": time_input,
        "user_reviewed": None,
        "notes": None,
    }
    row["source_row_hash"] = hashlib.sha256(
        f"{day.isoformat()}|{bid}|{habit_key}|{value}|{time_input}".encode()
    ).hexdigest()
    return row


# ---- day-level envelope (fact_journal_day) --------------------------------
def transform_journal_day(payload: dict, day: date) -> dict | None:
    """Pull the typed day-level fields out of the full draft payload.

    Real shape:
        payload = {
          "journal": {"cycle_id", "journal_entry_id", "notes",
                      "tracked_behaviors", "user_id", "user_reviewed"},
          "metadata": {"sleep_during": {...}, "date_picker", ...},
          ...
        }

    Returns None when the payload is empty (Whoop returned 404 → {}).
    """
    if not payload:
        return None
    journal = payload.get("journal") or {}
    metadata = payload.get("metadata") or {}
    # sleep_during lives on metadata in the real API; fall through to legacy
    # locations so old fixtures keep working.
    sleep_during = (
        metadata.get("sleep_during")
        or journal.get("sleep_during")
        or payload.get("sleep_during")
    )
    return {
        "day": day,
        "journal_entry_id": _safe_int(
            journal.get("journal_entry_id") or journal.get("id")
        ),
        "cycle_id": _safe_int(journal.get("cycle_id") or payload.get("cycle_id")),
        "notes": journal.get("notes"),
        "user_reviewed": (
            bool(journal.get("user_reviewed"))
            if journal.get("user_reviewed") is not None
            else None
        ),
        "sleep_during": sleep_during,
    }


# ---- helpers ---------------------------------------------------------------
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return s or "unknown"


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
    """Parse Whoop's various time-input shapes into a UTC datetime.

    The API returns time_input_value as **unix epoch milliseconds** for
    user-logged behaviors (e.g. 1777939200000 → 2026-05-04 16:00:00 UTC).
    Older fixtures + tests use ISO strings — we accept both.
    """
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, (int, float)):
        # Heuristic: Whoop's epochs >= 10**12 (post-2001 in milliseconds);
        # epochs < 10**12 are seconds. Both are reasonable in the wild.
        n = float(v)
        if n > 1e12:
            return datetime.fromtimestamp(n / 1000.0, tz=UTC)
        if n > 1e9:
            return datetime.fromtimestamp(n, tz=UTC)
        return None
    s = str(v).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None
