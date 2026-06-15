"""Pure CSV → row-dict parsers for Cronometer's export formats.

Each function takes the CSV string and yields fact-table-row dicts. No DB,
no I/O. Tested against fixtures so format drifts are caught.

Cronometer's CSV columns vary slightly across exports; we use DictReader and
look up by header name with safe defaults so missing columns don't crash.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections import OrderedDict
from datetime import date, datetime, time

from lifeos_core.tz import from_local_naive

# Header → (column_name_in_fact_food_log, parser)
# Anything not in this map gets bucketed into the `micros` JSONB.
FOOD_LOG_TYPED = {
    "Energy (kcal)": "energy_kcal",
    "Protein (g)": "protein_g",
    "Carbs (g)": "carbs_g",
    "Net Carbs (g)": "net_carbs_g",
    "Fiber (g)": "fiber_g",
    "Sugars (g)": "sugar_g",
    "Fat (g)": "fat_g",
    "Saturated (g)": "saturated_fat_g",
    "Sodium (mg)": "sodium_mg",
    "Potassium (mg)": "potassium_mg",
    "Caffeine (mg)": "caffeine_mg",
    "Alcohol (g)": "alcohol_g",
}

# Daily-nutrition CSV maps the same nutrient names to fact_food_daily columns.
FOOD_DAILY_TYPED = {
    "Energy (kcal)": "energy_kcal",
    "Protein (g)": "protein_g",
    "Carbs (g)": "carbs_g",
    "Net Carbs (g)": "net_carbs_g",
    "Fiber (g)": "fiber_g",
    "Fat (g)": "fat_g",
    "Saturated (g)": "saturated_fat_g",
    "Sodium (mg)": "sodium_mg",
    "Alcohol (g)": "alcohol_g",
    "Caffeine (mg)": "caffeine_mg",
}


def parse_servings(csv_text: str) -> list[dict]:
    """Servings export → fact_food_log rows.

    Expected columns include: Day, Time, Group, Food Name, Amount, Unit,
    plus dozens of nutrient columns. Time may be empty if the user is on
    a non-Gold tier — in that case eaten_at gets coerced to noon-local.
    """
    rows: list[dict] = []
    seen: dict[tuple, int] = {}  # ordinal per (day, meal, food) for genuine repeats
    reader = csv.DictReader(io.StringIO(csv_text))
    for r in reader:
        day_str = (r.get("Day") or "").strip()
        if not day_str:
            continue
        d = date.fromisoformat(day_str)
        time_str = (r.get("Time") or "").strip()
        eaten_at = _combine_local_dt(d, time_str)

        food_name = (r.get("Food Name") or "").strip()
        if not food_name:
            continue
        amount = _num(r.get("Amount"))
        unit = (r.get("Unit") or "").strip() or None

        row = {
            "eaten_at": eaten_at,
            "meal_group": (r.get("Group") or "").strip() or "Uncategorized",
            "food_name": food_name,
            "amount": amount,
            "unit": unit,
        }
        # Typed macro/micro columns
        micros: dict = OrderedDict()
        for header, val in r.items():
            if header in (
                "Day", "Time", "Group", "Food Name", "Amount", "Unit", "Quantity",
            ):
                continue
            if header in FOOD_LOG_TYPED:
                row[FOOD_LOG_TYPED[header]] = _num(val)
            elif header and val not in (None, ""):
                # Stash everything else as a micro. Keep numeric where possible.
                parsed = _num(val)
                micros[header] = parsed if parsed is not None else val
        row["micros"] = dict(micros)

        # Default nulls for typed columns we didn't see in this CSV.
        for col in FOOD_LOG_TYPED.values():
            row.setdefault(col, None)

        # Key on IMMUTABLE identity only — NOT amount/unit/macros. Editing a
        # serving's quantity in Cronometer must UPDATE the row in place, not
        # insert a stale duplicate (the fact_biometric bug class; see 0035).
        # An ordinal disambiguates genuinely-repeated (day, meal, food) entries.
        meal_group = row["meal_group"]
        _key = (day_str, meal_group, food_name)
        ordinal = seen.get(_key, 0)
        seen[_key] = ordinal + 1
        row["source_row_hash"] = hashlib.sha256(
            f"cronometer|{day_str}|{meal_group}|{food_name}|{ordinal}".encode()
        ).hexdigest()
        rows.append(row)
    return rows


def parse_daily_nutrition(csv_text: str) -> list[dict]:
    """Daily nutrition export → fact_food_daily rows.

    Columns: Date, Energy (kcal), Protein (g), ... — one row per day.
    """
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for r in reader:
        day_str = (r.get("Date") or r.get("Day") or "").strip()
        if not day_str:
            continue
        row = {"day": date.fromisoformat(day_str)}
        micros: dict = OrderedDict()
        for header, val in r.items():
            if header in ("Date", "Day"):
                continue
            if header in FOOD_DAILY_TYPED:
                row[FOOD_DAILY_TYPED[header]] = _num(val)
            elif header and val not in (None, ""):
                parsed = _num(val)
                micros[header] = parsed if parsed is not None else val
        for col in FOOD_DAILY_TYPED.values():
            row.setdefault(col, None)
        row["micros"] = dict(micros)
        rows.append(row)
    return rows


def parse_biometrics(csv_text: str) -> list[dict]:
    """Biometrics export → fact_biometric rows.

    Columns observed: Date,Metric,Amount,Unit (sometimes also Time, Note).
    Cronometer uses humans-readable metric names like "Weight" — we
    normalize to snake_case lowercase for fact_biometric.metric.
    """
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for r in reader:
        day_str = (r.get("Date") or r.get("Day") or "").strip()
        if not day_str:
            continue
        d = date.fromisoformat(day_str)
        time_str = (r.get("Time") or "").strip()
        measured_at = _combine_local_dt(d, time_str)

        metric_raw = (r.get("Metric") or "").strip()
        if not metric_raw:
            continue
        metric = _normalize_metric(metric_raw)

        amount = _num(r.get("Amount") or r.get("Value"))
        if amount is None:
            continue
        unit = (r.get("Unit") or "").strip() or ""
        note = (r.get("Note") or "").strip() or None

        row = {
            "measured_at": measured_at,
            "metric": metric,
            "value": amount,
            "unit": unit,
            "note": note,
            "source": "cronometer",
        }
        row["source_row_hash"] = hashlib.sha256(
            f"{day_str}|{time_str}|{metric}|{amount}|{unit}".encode()
        ).hexdigest()
        rows.append(row)
    return rows


# ---- helpers ---------------------------------------------------------------
def _num(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _combine_local_dt(d: date, time_str: str) -> datetime:
    """Cronometer's Time is local-tz wall clock; we store UTC. If empty,
    default to local noon (a stable, queryable midpoint that won't get
    bucketed into either yesterday or tomorrow under any reasonable tz)."""
    if not time_str:
        t = time(12, 0)
    else:
        # Cronometer formats times as "HH:MM" or "HH:MM:SS".
        try:
            t = time.fromisoformat(time_str)
        except ValueError:
            t = time(12, 0)
    naive = datetime.combine(d, t)
    return from_local_naive(naive)


_METRIC_OVERRIDES = {
    "weight": "weight",
    "body fat": "body_fat",
    "body fat %": "body_fat",
    "blood pressure (systolic)": "systolic_bp",
    "blood pressure (diastolic)": "diastolic_bp",
    "blood glucose": "fasting_glucose",
    "fasting blood glucose": "fasting_glucose",
    "heart rate": "heart_rate",
    "resting heart rate": "resting_heart_rate",
}


def _normalize_metric(raw: str) -> str:
    key = raw.strip().lower()
    if key in _METRIC_OVERRIDES:
        return _METRIC_OVERRIDES[key]
    # Generic: lowercase, replace spaces and slashes with _
    return (
        key.replace("/", "_")
        .replace("-", "_")
        .replace(" ", "_")
        .replace("(", "")
        .replace(")", "")
        .replace("%", "pct")
    )


def normalize_unit(_v) -> str:
    """Stub for future unit normalization (kg vs lb, mg vs mcg). Currently a
    pass-through — we store whatever Cronometer reports and let queries
    filter by unit if needed."""
    return _v
