"""Pure transforms: Whoop private-API JSON -> fact-row dicts.

No DB access, no I/O. Each function takes one API payload and returns plain
dicts/lists matching the fact_* columns (minus raw_id / updated_at, which the
ingester wires in).

The trends endpoint returns a heavy graph BFF rather than a clean data series:
per-day values live in nested ``data_scrubber_details`` nodes carrying a
human-formatted date ("MON, MAY 11") and a formatted value string ("5,442").
We collect those recursively (robust to bar-graph vs line-graph layouts), parse
both, and keep the formatted string verbatim in ``value_display`` as a fidelity
anchor in case a future format defeats the numeric parser.
"""

from __future__ import annotations

import re
from datetime import date, datetime

# Segment keys on the trend payload, finest/freshest first so day-level dedup
# prefers the most recently recomputed window.
_TREND_SEGMENTS = ("week_time_segment", "month_time_segment", "six_month_time_segment")

_TIME_RE = re.compile(r"^\d+:\d{2}$")
_PCT_RE = re.compile(r"-?\d+(?:\.\d+)?")
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


# ---- helpers ---------------------------------------------------------------
def _safe_num(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v) -> int | None:
    n = _safe_num(v)
    return int(n) if n is not None else None


def _parse_pct(s: str | None) -> float | None:
    """'+5%' -> 5.0, '-10%' -> -10.0, '0%' -> 0.0, None/'' -> None."""
    if not s:
        return None
    m = _PCT_RE.search(s.replace(",", ""))
    return float(m.group()) if m else None


def _parse_metric_value(value_display: str | None) -> float | None:
    """Parse Whoop's formatted display value into a number.

    Handles thousands separators ('5,442' -> 5442), percentages ('84%' -> 84),
    and clock-style durations ('7:51' -> 471.0 minutes). Returns None when the
    string can't be coerced — value_display is preserved separately regardless.
    """
    if value_display is None:
        return None
    s = value_display.strip().replace(",", "")
    if not s or s in {"-", "--"}:
        return None
    if _TIME_RE.match(s):
        h, m = s.split(":")
        return float(int(h) * 60 + int(m))
    s = s.rstrip("%").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_point_date(s: str | None, *, ref_year: int) -> date | None:
    """'MON, MAY 11' (year-less -> ref_year) or 'FRI, DEC 12, 2025' -> date.

    Year-less points always fall in the trend window's own year, so ref_year
    (the fetch end_date's year) is correct for them; prior-calendar-year points
    carry an explicit year and ignore ref_year.
    """
    if not s:
        return None
    parts = [p.strip() for p in s.split(",")]
    # Drop the leading day-of-week token ("MON").
    if parts and len(parts[0]) <= 3 and parts[0].upper() not in _MONTHS:
        parts = parts[1:]
    if not parts:
        return None
    md = parts[0].upper().split()
    if len(md) != 2 or md[0] not in _MONTHS:
        return None
    month = _MONTHS[md[0]]
    try:
        day = int(md[1])
    except ValueError:
        return None
    year = ref_year
    if len(parts) >= 2:
        try:
            year = int(parts[1])
        except ValueError:
            year = ref_year
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _collect_scrubbers(node) -> list[dict]:
    """Recursively gather every ``data_scrubber_details`` dict under a node.

    Robust to the two graph layouts the trends BFF uses: bar graphs nest them
    under plots[].plot.bar_groups[].bars[], line graphs under
    plots[].plot.segments[].points[]. We don't care which — just find them all.
    """
    found: list[dict] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "data_scrubber_details" and isinstance(v, dict):
                found.append(v)
            else:
                found.extend(_collect_scrubbers(v))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_scrubbers(item))
    return found


# ---- trends ----------------------------------------------------------------
def _segment_unit(segment: dict) -> str | None:
    metrics = segment.get("metrics") or []
    if metrics and isinstance(metrics[0], dict):
        return metrics[0].get("metric_units_display")
    return None


def transform_trend_points(payload: dict, metric: str, end_date: date) -> list[dict]:
    """Flatten a trend graph BFF into per-day rows:
    {day, metric, value, value_display, unit}.

    Dedups within the payload on day, preferring the finest/freshest segment
    (week > month > six_month). Drops points whose date or value won't parse.
    Returns [] for an empty/404 payload.
    """
    if not payload:
        return []
    seen: dict[date, dict] = {}
    for seg_key in _TREND_SEGMENTS:
        segment = payload.get(seg_key)
        if not isinstance(segment, dict):
            continue
        unit = _segment_unit(segment)
        for sc in _collect_scrubbers(segment.get("graph")):
            day = _parse_point_date(
                sc.get("primary_contextual_display"), ref_year=end_date.year
            )
            # Drop unparseable, already-seen, or impossible future dates. Some
            # payloads (HR-zone weekly buckets) carry year-less range labels that
            # can resolve past end_date; a trend can't have data after its own
            # window, so guard against it.
            if day is None or day > end_date or day in seen:
                continue
            vdisp = sc.get("value_display")
            value = _parse_metric_value(vdisp)
            if value is None and not vdisp:
                continue
            seen[day] = {
                "day": day,
                "metric": metric,
                "value": value,
                "value_display": vdisp,
                "unit": unit or sc.get("unit_display"),
            }
    return list(seen.values())


def slim_trend_payload(payload: dict, metric: str, end_date: date) -> dict:
    """Strip the trend BFF down to data-bearing keys before persisting raw.

    The wire payload is ~120 KB of graph geometry, education carousels, and
    upsell cards per metric. We keep each segment's summary `metrics` plus the
    parsed point list, which is all the raw layer needs for provenance / future
    re-derivation, and a fraction of the size.
    """
    out: dict = {"metric": metric, "end_date": end_date.isoformat(), "segments": {}}
    for seg_key in _TREND_SEGMENTS:
        segment = payload.get(seg_key)
        if not isinstance(segment, dict):
            continue
        pts = []
        for sc in _collect_scrubbers(segment.get("graph")):
            pts.append(
                {
                    "date": sc.get("primary_contextual_display"),
                    "value_display": sc.get("value_display"),
                    "value": sc.get("value"),
                }
            )
        out["segments"][seg_key] = {
            "metrics": segment.get("metrics"),
            "unit": _segment_unit(segment),
            "points": pts,
        }
    return out


# ---- sleep need ------------------------------------------------------------
def transform_sleep_need(payload: dict, day: date) -> dict | None:
    """coaching-service/v2/sleepneed -> fact_whoop_sleep_need row (data fields).

    need_breakdown values are milliseconds. recommended_tib_minutes is the
    full-need ("100") recommended time in bed, converted ms -> minutes.
    """
    if not payload:
        return None
    nb = payload.get("need_breakdown") or {}
    rec_tib_ms = None
    rec = payload.get("recommended_time_in_bed_formatted") or {}
    full = rec.get("100") if isinstance(rec, dict) else None
    if isinstance(full, dict):
        rec_tib_ms = _safe_num(full.get("recommended_time_in_bed"))
    return {
        "day": day,
        "recommended_tib_minutes": round(rec_tib_ms / 60000.0, 1) if rec_tib_ms else None,
        "total_need_ms": _safe_int(nb.get("total")),
        "baseline_ms": _safe_int(nb.get("baseline")),
        "debt_ms": _safe_int(nb.get("debt")),
        "strain_ms": _safe_int(nb.get("strain")),
        "nap_credit_ms": _safe_int(nb.get("naps")),
        "smart_alarm_eligible": payload.get("eligible_for_smart_alarms"),
        "schedule_state": payload.get("alarm_schedule_state"),
    }


# ---- behavior impact -------------------------------------------------------
_IMPACT_STYLE_DIRECTION = {
    "POSITIVE": "positive",
    "NEGLIGIBLE_POSITIVE": "neutral",
    "NEGLIGIBLE_NEGATIVE": "neutral",
    "NEGATIVE": "negative",
    "INSUFFICIENT": "insufficient",
}
_IMPACT_TILE_TYPES = {"IMPACT_TILE", "INSUFFICIENT_IMPACT_TILE"}


def _answer_count(card: dict, key: str) -> int | None:
    block = card.get(key)
    if isinstance(block, dict):
        return _safe_int((block.get("answer_count_text_display") or "").replace(",", ""))
    return None


def transform_behavior_impact(payload: dict, captured_on: date) -> list[dict]:
    """behavior-impact-service/v1/impact -> fact_whoop_behavior_impact rows.

    Walks the BFF tile list, pulling impact_cards from both the ranked
    IMPACT_TILE and the INSUFFICIENT_IMPACT_TILE (which carries yes/no answer
    counts instead of a percentage). outcome is always 'recovery' today.
    """
    if not payload:
        return []
    rows: list[dict] = []
    seen_uuids: set[str] = set()
    for tile in payload.get("tiles") or []:
        if not isinstance(tile, dict) or tile.get("type") not in _IMPACT_TILE_TYPES:
            continue
        content = tile.get("content") or {}
        for card in content.get("impact_cards") or []:
            uuid = card.get("impact_uuid")
            if not uuid or uuid in seen_uuids:
                continue
            seen_uuids.add(uuid)
            style = card.get("impact_style")
            display = card.get("impact_percentage_display")
            rows.append(
                {
                    "captured_on": captured_on,
                    "impact_uuid": uuid,
                    "behavior_name": (card.get("impact_card_title_display") or "").strip(),
                    "outcome": "recovery",
                    "direction": _IMPACT_STYLE_DIRECTION.get(style),
                    "impact_pct": _parse_pct(display),
                    "impact_display": display,
                    "has_sufficient_data": style != "INSUFFICIENT",
                    "yes_answer_count": _answer_count(card, "yes_answer_count"),
                    "no_answer_count": _answer_count(card, "no_answer_count"),
                }
            )
    return rows


# ---- strength trainer (lift) -----------------------------------------------
def transform_lift_workout(rec: dict) -> dict | None:
    """One Whoop Strength Trainer workout record -> fact_whoop_lift_workout row.

    msk_total_volume_kg is already kilograms; duration_ms -> minutes. The
    per-exercise breakdown is carried through verbatim as `exercises` (stored
    JSONB) so per-exercise queries don't need a separate table. Returns None if
    the record lacks an activity_id or a parseable date.
    """
    activity_id = rec.get("activity_id")
    day_str = rec.get("date")
    if not activity_id or not day_str:
        return None
    try:
        day = date.fromisoformat(str(day_str)[:10])
    except ValueError:
        return None
    dur_ms = _safe_num(rec.get("duration_ms"))
    return {
        "activity_id": activity_id,
        "day": day,
        "name": rec.get("name"),
        "duration_minutes": round(dur_ms / 60000.0, 1) if dur_ms else None,
        "strain": _safe_num(rec.get("strain")),
        "total_volume_kg": _safe_num(rec.get("msk_total_volume_kg")),
        "intensity_pct": _safe_num(rec.get("msk_intensity_pct")),
        "exercise_count": _safe_int(rec.get("exercise_count")),
        "set_count": _safe_int(rec.get("set_count")),
        "exercises": rec.get("exercises") or [],
    }


# ---- strength trainer: exact per-set detail --------------------------------
LB_TO_KG = 0.45359237


def _parse_int_str(s) -> int | None:
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _parse_float_str(s) -> float | None:
    """Like _parse_int_str but preserves fractional values — use for weights,
    where 22.5 lb dumbbells / 2.5 lb micro-plates must not truncate to 22."""
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_clock_seconds(s) -> int | None:
    """'1:00' -> 60, '0:45' -> 45, '1:02:03' -> 3723."""
    if not s:
        return None
    try:
        parts = [int(p) for p in str(s).split(":")]
    except ValueError:
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def extract_activity_ids(payload: dict) -> list[str]:
    """Pull every workout activity_id out of a day's strain deep-dive SDUI
    payload (they sit on tile destinations, e.g.
    sections[].items[].content.destination.parameters.activity_id). Order-
    preserving + de-duplicated. Used to discover that day's workouts from the
    PRIVATE API without the public-OAuth activity feed."""
    out: list[str] = []
    seen: set[str] = set()

    def walk(o) -> None:
        if isinstance(o, dict):
            v = o.get("activity_id") or o.get("activityId")
            if isinstance(v, str) and len(v) == 36 and v not in seen:
                seen.add(v)
                out.append(v)
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(payload or {})
    return out


def _parse_iso_ts(s):
    """Parse Whoop's ISO timestamps (e.g. '2026-06-15T10:34:27.899+0000')."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    # normalize '+0000' -> '+00:00' for fromisoformat, and trailing Z
    s = s.replace("Z", "+00:00")
    m = re.match(r"^(.*[+-]\d{2})(\d{2})$", s)
    if m:
        s = f"{m.group(1)}:{m.group(2)}"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def transform_strain_feed_workouts(payload: dict) -> list[dict]:
    """Build fact_workout rows from a day's strain deep-dive. Each workout tile
    carries clean fields (no SDUI text parsing): activity_v2_id, internal_name
    (sport), score_display (strain), and during.{lower,upper}_endpoint (start/end).
    HR/zones/kJ are NOT here — they stay whatever the public ingester left, or
    NULL for privately-discovered workouts (the per-set strength detail still
    comes from the lift pipeline). De-duplicated by workout_id."""
    rows: list[dict] = []
    seen: set[str] = set()

    def walk(o) -> None:
        if isinstance(o, dict):
            aid = o.get("activity_v2_id")
            if (isinstance(aid, str) and len(aid) == 36 and aid not in seen
                    and o.get("during") and (o.get("internal_name") or o.get("score_display"))):
                during = o.get("during") or {}
                start = _parse_iso_ts(during.get("lower_endpoint"))
                end = _parse_iso_ts(during.get("upper_endpoint"))
                if start and end:
                    seen.add(aid)
                    rows.append({
                        "workout_id": aid,
                        "sport_name": o.get("internal_name"),
                        "strain": _safe_num(o.get("score_display")),
                        "start_ts": start,
                        "end_ts": end,
                    })
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(payload or {})
    return rows


def transform_cardio_details(payload: dict, activity_id: str, day: date) -> tuple[dict | None, list[dict]]:
    """Parse /core-details-bff/v1/cardio-details for a strength workout into
    (workout_aggregate_row, [per_set_rows]).

    The set data lives at weightlifting_cardio_details.weightlifting_exercises.
    exercise_summary.exercise_card_groups[*].cards[*].stat_rows[*]. Card groups
    are UI carousel paging — flatten them. Each stat_row is one set in performed
    order; volume_display is reps (when volume_title_display == 'REPS') or a
    clock string (when 'TIME'). Weights are pounds (0 = bodyweight). Returns
    (None, []) if the payload carries no weightlifting breakdown.
    """
    summary = (
        ((payload or {}).get("weightlifting_cardio_details") or {})
        .get("weightlifting_exercises") or {}
    ).get("exercise_summary") or {}
    groups = summary.get("exercise_card_groups") or []

    set_rows: list[dict] = []
    exercises: list[dict] = []
    per_ex_idx: dict[str, int] = {}
    for group in groups:
        for card in group.get("cards") or []:
            ex_id = card.get("exercise_id")
            if not ex_id:
                continue
            name = card.get("title_display")
            vtype = (card.get("volume_title_display") or "").upper() or None
            ex_set_count = 0
            for sr in card.get("stat_rows") or []:
                idx = per_ex_idx.get(ex_id, 0) + 1
                per_ex_idx[ex_id] = idx
                ex_set_count += 1
                weight_lb = _parse_float_str(sr.get("weight_display"))
                if vtype == "TIME":
                    reps, tsec = None, _parse_clock_seconds(sr.get("volume_display"))
                else:
                    reps, tsec = _parse_int_str(sr.get("volume_display")), None
                set_rows.append({
                    "activity_id": activity_id,
                    "day": day,
                    "exercise_id": ex_id,
                    "exercise_name": name,
                    "set_index": idx,
                    "volume_type": vtype,
                    "reps": reps,
                    "time_seconds": tsec,
                    "weight_lb": float(weight_lb) if weight_lb is not None else None,
                    "weight_kg": round(weight_lb * LB_TO_KG, 2) if weight_lb is not None else None,
                    "avg_hr": _parse_int_str(sr.get("avg_hr_display")),
                    "is_pr": bool(sr.get("achievement_icon")),
                })
            bs = card.get("bottom_stats") or {}
            exercises.append({
                "exercise_id": ex_id,
                "name": name,
                "set_count": ex_set_count,
                "volume_type": vtype,
                "tonnage_display": bs.get("tonnage_display"),
            })

    if not set_rows:
        return None, []

    tonnage_lb = _parse_int_str(summary.get("tonnage_display"))
    workout = {
        "activity_id": activity_id,
        "day": day,
        "total_volume_kg": round(tonnage_lb * LB_TO_KG, 2) if tonnage_lb else None,
        "set_count": len(set_rows),
        "exercise_count": len(exercises),
        "exercises": exercises,
    }
    return workout, set_rows
