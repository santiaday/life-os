"""Tool implementations for the MCP server.

Each tool returns a dict matching the SPEC.md §6.2 envelope:

    {ok, tool, rows, row_count, truncated, warnings}
    or
    {ok=False, tool, error, error_type}

Tools use `db.conn()` for the admin pool (mart/fact reads) and
`db.reader_conn()` for the read-only `ask_sql` escape hatch.
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime
from typing import Any

from psycopg import sql

from lifeos_core.db import conn, reader_conn
from lifeos_core.logging import get_logger
from mcp_server.schema_docs import docs_for
from mcp_server.sql_safety import UnsafeQueryError, ensure_limit, validate

log = get_logger(__name__)

ASK_SQL_DEFAULT_LIMIT = 200
ASK_SQL_TIMEOUT_MS = 15_000  # raised from 5s — analytical queries on full
                             # mart_daily history regularly need ~6-10s, and
                             # the 5s ceiling is what tripped the pool in the
                             # transcripts.

# Allowlist of mart_daily columns that may be passed to correlate_metrics.
# Updated alongside any schema change.
CORRELATE_ALLOWLIST = {
    "recovery_score", "hrv_rmssd_ms", "resting_heart_rate", "spo2_percentage",
    "skin_temp_celsius",
    "sleep_total_hours", "sleep_rem_hours", "sleep_slow_wave_hours",
    "sleep_efficiency_pct", "sleep_performance_pct", "sleep_consistency_pct",
    "nap_count", "nap_total_min",
    "strain", "day_kilojoules",
    "workout_count", "workout_total_min", "workout_total_kj", "workout_max_strain",
    "meeting_count", "meeting_hours", "meeting_internal_hours", "meeting_external_hours",
    "longest_focus_block_min", "total_focus_block_min",
    "total_kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "alcohol_g", "caffeine_mg",
    "meal_count", "eating_window_hours",
    "breakfast_kcal", "lunch_kcal", "dinner_kcal", "snack_kcal",
    "total_spend", "food_spend", "restaurant_spend", "groceries_spend", "transportation_spend",
    "alcohol_spend", "bars_spend", "entertainment_spend", "shopping_spend", "travel_spend",
    "dining_out_txn_count", "dining_out_txn_max",
    "weight_kg", "body_fat_pct",
}


# ---- envelope helpers -------------------------------------------------------
def _ok(tool: str, rows: list[dict], *, truncated: bool = False, warnings: list[str] | None = None,
        extra: dict | None = None) -> dict:
    out: dict = {
        "ok": True,
        "tool": tool,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "warnings": warnings or [],
    }
    if extra:
        out.update(extra)
    return out


def _err(tool: str, exc: BaseException) -> dict:
    return {
        "ok": False,
        "tool": tool,
        "error": str(exc),
        "error_type": type(exc).__name__,
    }


def _serialize(rows: list[dict]) -> list[dict]:
    """Cast non-JSON-native values (date, datetime, Decimal, UUID) to strings.
    psycopg's dict_row already returns Python types; we just stringify the
    ones that don't json.dumps cleanly."""
    out = []
    for r in rows:
        out.append({k: _coerce(v) for k, v in r.items()})
    return out


def _coerce(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, bool)):
        return v
    if isinstance(v, float):
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    # psycopg returns Decimal for NUMERIC; preserve precision via float (we're
    # already lossy via JSON).
    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return float(v)
    except ImportError:
        pass
    return str(v)


# ---- get_schema_docs --------------------------------------------------------
def get_schema_docs(table_name: str | None = None) -> dict:
    return _ok("get_schema_docs", [docs_for(table_name)])


# ---- get_daily_summary ------------------------------------------------------
DEFAULT_DAILY_COLS = [
    "day", "recovery_score", "hrv_rmssd_ms", "sleep_total_hours",
    "strain", "meeting_hours", "total_kcal", "total_spend",
]
DAILY_MAX_ROWS = 366


def get_daily_summary(start_date: date, end_date: date, columns: list[str] | None = None) -> dict:
    cols = columns or DEFAULT_DAILY_COLS
    bad = [c for c in cols if c not in CORRELATE_ALLOWLIST and c != "day"]
    if bad:
        return _err("get_daily_summary", ValueError(f"Unknown columns: {bad}"))

    select = sql.SQL(", ").join(map(sql.Identifier, cols))
    q = sql.SQL(
        "SELECT {cols} FROM mart_daily WHERE day BETWEEN %s AND %s ORDER BY day"
    ).format(cols=select)

    with conn() as c, c.cursor() as cur:
        cur.execute(q, [start_date, end_date])
        rows = _serialize(cur.fetchall())

    truncated = len(rows) > DAILY_MAX_ROWS
    warnings: list[str] = []
    if truncated:
        rows = rows[:DAILY_MAX_ROWS]
        warnings.append(f"Result truncated to {DAILY_MAX_ROWS} rows. Narrow the date window.")
    return _ok("get_daily_summary", rows, truncated=truncated, warnings=warnings)


# ---- get_recovery_trend -----------------------------------------------------
def get_recovery_trend(start_date: date, end_date: date, smoothing: int | None = None) -> dict:
    cols = ["day", "recovery_score", "hrv_rmssd_ms", "resting_heart_rate", "sleep_total_hours"]
    select = sql.SQL(", ").join(map(sql.Identifier, cols))
    q = sql.SQL(
        "SELECT {cols} FROM mart_daily WHERE day BETWEEN %s AND %s ORDER BY day"
    ).format(cols=select)

    with conn() as c, c.cursor() as cur:
        cur.execute(q, [start_date, end_date])
        rows = _serialize(cur.fetchall())

    if smoothing and smoothing > 1:
        rows = _add_rolling(rows, ["recovery_score", "hrv_rmssd_ms", "resting_heart_rate"], smoothing)

    return _ok("get_recovery_trend", rows)


def _add_rolling(rows: list[dict], cols: list[str], window: int) -> list[dict]:
    """Trailing N-day rolling average. NULL values skipped from the window."""
    for col in cols:
        rolling_col = f"{col}_roll{window}"
        for i, r in enumerate(rows):
            window_slice = rows[max(0, i - window + 1) : i + 1]
            vals = [w[col] for w in window_slice if w.get(col) is not None]
            r[rolling_col] = round(sum(vals) / len(vals), 2) if vals else None
    return rows


# ---- get_sleep_summary ------------------------------------------------------
def get_sleep_summary(start_date: date, end_date: date, include_naps: bool = False) -> dict:
    base_cols = [
        "day", "sleep_total_hours", "sleep_rem_hours", "sleep_slow_wave_hours",
        "sleep_efficiency_pct", "sleep_performance_pct", "sleep_consistency_pct",
        "sleep_start_ts", "sleep_end_ts",
    ]
    if include_naps:
        base_cols += ["nap_count", "nap_total_min"]

    select = sql.SQL(", ").join(map(sql.Identifier, base_cols))
    q = sql.SQL(
        "SELECT {cols} FROM mart_daily WHERE day BETWEEN %s AND %s ORDER BY day"
    ).format(cols=select)
    with conn() as c, c.cursor() as cur:
        cur.execute(q, [start_date, end_date])
        return _ok("get_sleep_summary", _serialize(cur.fetchall()))


# ---- get_workouts -----------------------------------------------------------
def get_workouts(start_date: date, end_date: date, sport_name: str | None = None) -> dict:
    where = "day BETWEEN %s AND %s"
    params: list = [start_date, end_date]
    if sport_name:
        where += " AND sport_name ILIKE %s"
        params.append(sport_name)
    q = f"""
        SELECT workout_id, day, start_ts, end_ts, sport_name, strain, kilojoules,
               avg_heart_rate, max_heart_rate, distance_meters,
               zone_two_min, zone_three_min, zone_four_min, zone_five_min
        FROM fact_workout WHERE {where} ORDER BY start_ts DESC LIMIT 200
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = _serialize(cur.fetchall())
    truncated = len(rows) >= 200
    warnings = ["Limit 200 workouts. Narrow the window if needed."] if truncated else []
    return _ok("get_workouts", rows, truncated=truncated, warnings=warnings)


# ---- get_food_log -----------------------------------------------------------
FOOD_LOG_LIMIT = 500


def get_food_log(
    start_date: date,
    end_date: date,
    meal_window: str | None = None,
    search: str | None = None,
) -> dict:
    where = ["day BETWEEN %s AND %s"]
    params: list = [start_date, end_date]
    if meal_window:
        where.append("meal_group ILIKE %s")
        params.append(meal_window if "%" in meal_window else f"{meal_window}%")
    if search:
        where.append("food_name ILIKE %s")
        params.append(f"%{search}%")

    where_clause = " AND ".join(where)
    q = f"""
        SELECT id, eaten_at, day, meal_group, food_name, amount, unit,
               energy_kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g,
               sodium_mg, caffeine_mg, alcohol_g
        FROM fact_food_log
        WHERE {where_clause}
        ORDER BY eaten_at DESC
        LIMIT {FOOD_LOG_LIMIT + 1}
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = _serialize(cur.fetchall())

    truncated = len(rows) > FOOD_LOG_LIMIT
    warnings: list[str] = []
    if truncated:
        rows = rows[:FOOD_LOG_LIMIT]
        warnings.append(
            f"More than {FOOD_LOG_LIMIT} matches; truncated. "
            "Try narrowing date range or use get_meal_summary for aggregates."
        )
    return _ok("get_food_log", rows, truncated=truncated, warnings=warnings)


# ---- get_meal_summary -------------------------------------------------------
def get_meal_summary(start_date: date, end_date: date, meal_window: str | None = None) -> dict:
    where = ["day BETWEEN %s AND %s"]
    params: list = [start_date, end_date]
    if meal_window:
        where.append("meal_window = %s")
        params.append(meal_window)
    q = f"""
        SELECT day, meal_window, start_ts, end_ts, duration_min, item_count,
               total_kcal, protein_g, carbs_g, fat_g, fiber_g, food_names
        FROM mart_meal WHERE {" AND ".join(where)} ORDER BY start_ts
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        return _ok("get_meal_summary", _serialize(cur.fetchall()))


# ---- get_calendar_load ------------------------------------------------------
def get_calendar_load(start_date: date, end_date: date) -> dict:
    cols = [
        "day", "meeting_count", "meeting_hours", "meeting_internal_hours",
        "meeting_external_hours", "first_meeting_time", "last_meeting_time",
        "longest_focus_block_min", "total_focus_block_min",
    ]
    select = sql.SQL(", ").join(map(sql.Identifier, cols))
    q = sql.SQL(
        "SELECT {cols} FROM mart_daily WHERE day BETWEEN %s AND %s ORDER BY day"
    ).format(cols=select)
    with conn() as c, c.cursor() as cur:
        cur.execute(q, [start_date, end_date])
        return _ok("get_calendar_load", _serialize(cur.fetchall()))


# ---- get_calendar_events ----------------------------------------------------
def get_calendar_events(
    start_date: date,
    end_date: date,
    classification: str | None = None,
    search: str | None = None,
) -> dict:
    where = ["day BETWEEN %s AND %s"]
    params: list = [start_date, end_date]
    if classification:
        where.append("classification = %s")
        params.append(classification)
    if search:
        where.append("title ILIKE %s")
        params.append(f"%{search}%")
    q = f"""
        SELECT calendar_id, event_id, start_ts, end_ts, day, duration_min,
               title, status, classification, attendee_count, attendee_internal_count,
               attendee_external_count, response_status, has_video_link, location
        FROM fact_calendar_event
        WHERE {" AND ".join(where)}
        ORDER BY start_ts DESC
        LIMIT 500
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        return _ok("get_calendar_events", _serialize(cur.fetchall()))


# ---- get_spending -----------------------------------------------------------
GROUP_BY_OPTIONS = {"day", "week", "month", "category", "merchant", "account"}


def _escape_like(s: str) -> str:
    """Escape ILIKE special chars (%, _, \\) so a substring match treats them
    literally. Used everywhere we accept user-supplied substrings — without
    this, a category named 'Bars & Nightlife' or anything containing _ silently
    matched the wrong thing."""
    return s.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def get_spending(
    start_date: date,
    end_date: date,
    category: str | None = None,
    group_by: str = "day",
    account_id: str | None = None,
    account: str | None = None,
    exact_category: bool = False,
    merchant: str | None = None,
) -> dict:
    """Aggregated spending with rich filtering.

    `category`     ILIKE substring (escapes %, _) unless `exact_category=True`.
    `account_id`   Exact dim_account.account_id.
    `account`      ILIKE substring against dim_account.name (e.g. 'Amazon Card').
    `merchant`     ILIKE substring against fact_transaction.merchant.
    `group_by`     day | week | month | category | merchant | account.
    """
    if group_by not in GROUP_BY_OPTIONS:
        return _err("get_spending", ValueError(f"group_by must be one of {sorted(GROUP_BY_OPTIONS)}"))

    where = ["t.date BETWEEN %s AND %s", "NOT t.is_excluded", "t.amount > 0"]
    params: list = [start_date, end_date]
    if category:
        if exact_category:
            where.append("c.name = %s")
            params.append(category)
        else:
            where.append(r"c.name ILIKE %s ESCAPE '\'")
            params.append(f"%{_escape_like(category)}%")
    if account_id:
        where.append("t.account_id = %s")
        params.append(account_id)
    if account:
        where.append(r"a.name ILIKE %s ESCAPE '\'")
        params.append(f"%{_escape_like(account)}%")
    if merchant:
        where.append(r"t.merchant ILIKE %s ESCAPE '\'")
        params.append(f"%{_escape_like(merchant)}%")

    where_clause = " AND ".join(where)
    if group_by == "day":
        bucket_select, bucket_alias = "t.date", "bucket"
    elif group_by == "week":
        bucket_select, bucket_alias = "date_trunc('week', t.date)::date", "bucket"
    elif group_by == "month":
        bucket_select, bucket_alias = "date_trunc('month', t.date)::date", "bucket"
    elif group_by == "category":
        bucket_select, bucket_alias = "COALESCE(c.name, 'Uncategorized')", "bucket"
    elif group_by == "merchant":
        bucket_select, bucket_alias = "t.merchant", "bucket"
    else:  # account
        bucket_select, bucket_alias = "COALESCE(a.name, t.account_id, 'unknown')", "bucket"

    order_by = "total DESC" if group_by in ("category", "merchant", "account") else "bucket"

    q = f"""
        SELECT {bucket_select} AS {bucket_alias},
               SUM(t.amount) AS total,
               COUNT(*)      AS txn_count
        FROM fact_transaction t
        LEFT JOIN dim_category c ON c.category_id = t.category_id
        LEFT JOIN dim_account  a ON a.account_id  = t.account_id
        WHERE {where_clause}
        GROUP BY {bucket_alias}
        ORDER BY {order_by}
        LIMIT 500
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        return _ok("get_spending", _serialize(cur.fetchall()))


# ---- get_transactions -------------------------------------------------------
def get_transactions(
    start_date: date,
    end_date: date,
    category: str | None = None,
    merchant: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    tag: str | None = None,
    has_no_tags: bool = False,
    untagged_for_couples: bool = False,
    account_id: str | None = None,
    account: str | None = None,
    account_ids: list[str] | None = None,
    exclude_excluded: bool = True,
    only_charges: bool = False,
    exact_category: bool = False,
    limit: int = 500,
) -> dict:
    """Individual transactions with rich filters.

    Filters (combine freely; AND semantics):
        category               ILIKE substring (auto-escapes %, _) unless
                               exact_category=True.
        merchant               ILIKE substring (auto-escapes %, _).
        min_amount/max_amount  Compared against ABS(amount).
        tag                    ILIKE against any element of tags[].
        has_no_tags            Only rows with empty tag list.
        untagged_for_couples   Only rows missing me/partner/joint tags.
        account_id             Exact match.
        account                ILIKE against dim_account.name.
        account_ids            List of account_ids (any match).
        exclude_excluded       Drop rows with is_excluded=true (default true).
        only_charges           Drop refunds/income; keep amount > 0.
        limit                  1..1000.
    """
    if limit < 1 or limit > 1000:
        return _err("get_transactions", ValueError("limit must be between 1 and 1000"))

    where = ["t.date BETWEEN %s AND %s"]
    params: list = [start_date, end_date]
    if exclude_excluded:
        where.append("NOT t.is_excluded")
    if only_charges:
        where.append("t.amount > 0")
    if category:
        if exact_category:
            where.append("c.name = %s")
            params.append(category)
        else:
            where.append(r"c.name ILIKE %s ESCAPE '\'")
            params.append(f"%{_escape_like(category)}%")
    if merchant:
        where.append(r"t.merchant ILIKE %s ESCAPE '\'")
        params.append(f"%{_escape_like(merchant)}%")
    if min_amount is not None:
        where.append("ABS(t.amount) >= %s")
        params.append(min_amount)
    if max_amount is not None:
        where.append("ABS(t.amount) <= %s")
        params.append(max_amount)
    if account_id:
        where.append("t.account_id = %s")
        params.append(account_id)
    if account_ids:
        where.append("t.account_id = ANY(%s)")
        params.append(list(account_ids))
    if account:
        where.append(r"a.name ILIKE %s ESCAPE '\'")
        params.append(f"%{_escape_like(account)}%")
    if tag:
        where.append(r"EXISTS (SELECT 1 FROM unnest(t.tags) x WHERE x ILIKE %s ESCAPE '\')")
        params.append(f"%{_escape_like(tag)}%")
    if has_no_tags:
        where.append("(t.tags IS NULL OR cardinality(t.tags) = 0)")
    if untagged_for_couples:
        from lifeos_core.settings import settings as _s
        couple_names = [
            _s.COUPLE_TAG_ME.lower(),
            _s.COUPLE_TAG_PARTNER.lower(),
            _s.COUPLE_TAG_JOINT.lower(),
        ]
        where.append(
            "NOT EXISTS (SELECT 1 FROM unnest(t.tags) x WHERE LOWER(x) = ANY(%s))"
        )
        params.append(couple_names)
    q = f"""
        SELECT t.transaction_id, t.date, t.amount, t.merchant, t.description,
               c.name AS category, c.category_id, t.is_pending, t.is_recurring,
               t.is_excluded, t.is_reviewed, t.notes, t.tip_amount, t.parent_id,
               t.copilot_type, t.tags, t.tag_ids,
               a.name AS account, t.account_id
        FROM fact_transaction t
        LEFT JOIN dim_category c ON c.category_id = t.category_id
        LEFT JOIN dim_account  a ON a.account_id  = t.account_id
        WHERE {" AND ".join(where)}
        ORDER BY t.date DESC, t.amount DESC
        LIMIT %s
    """
    params.append(limit + 1)
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = _serialize(cur.fetchall())
    truncated = len(rows) > limit
    if truncated:
        rows = rows[:limit]
    warnings = (
        [f"More than {limit} matches; truncated. Narrow the filter or raise limit."]
        if truncated else []
    )
    return _ok("get_transactions", rows, truncated=truncated, warnings=warnings)


# ---- get_biometrics ---------------------------------------------------------
def get_biometrics(
    metric: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    if metric is None:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT metric, COUNT(*) AS n,
                       MIN(measured_at) AS first_seen,
                       MAX(measured_at) AS last_seen
                FROM fact_biometric GROUP BY metric ORDER BY n DESC
                """
            )
            return _ok("get_biometrics", _serialize(cur.fetchall()))

    where = ["metric = %s"]
    params: list = [metric]
    if start_date is not None:
        where.append("day >= %s")
        params.append(start_date)
    if end_date is not None:
        where.append("day <= %s")
        params.append(end_date)
    q = f"""
        SELECT id, measured_at, day, metric, value, unit, note, source
        FROM fact_biometric WHERE {" AND ".join(where)}
        ORDER BY measured_at DESC LIMIT 1000
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        return _ok("get_biometrics", _serialize(cur.fetchall()))


# ---- correlate_metrics ------------------------------------------------------
def correlate_metrics(
    metric_a: str,
    metric_b: str,
    start_date: date,
    end_date: date,
    lag_days: int = 0,
    method: str = "pearson",
    lag_range: list[int] | None = None,
    return_series: bool = True,
) -> dict:
    """Correlate two mart_daily metrics over a range.

    `lag_range`     Optional [min, max] inclusive. If passed, runs the
                    correlation at each integer lag in that range (capped
                    at 21 lags to stay under typical token budgets) and
                    returns them in `lags`. Useful for finding the
                    strongest predictive lag in one call.
    `return_series` Set to false to skip the row-level paired data — handy
                    for sweep mode where the agent only needs aggregate
                    stats per lag.
    """
    if metric_a not in CORRELATE_ALLOWLIST or metric_b not in CORRELATE_ALLOWLIST:
        bad = [m for m in (metric_a, metric_b) if m not in CORRELATE_ALLOWLIST]
        return _err("correlate_metrics", ValueError(f"Not in allowlist: {bad}"))
    if method not in ("pearson", "spearman"):
        return _err("correlate_metrics", ValueError("method must be 'pearson' or 'spearman'"))

    a = sql.Identifier(metric_a)
    b = sql.Identifier(metric_b)

    try:
        from scipy import stats  # local import keeps test imports light
    except Exception as e:
        log.exception("correlate_metrics.scipy_failed")
        return _err("correlate_metrics", e)

    def _run(lag: int) -> tuple[list[dict], dict]:
        q = sql.SQL(
            """
            SELECT m1.day AS day,
                   m1.{a} AS a,
                   m2.{b} AS b
            FROM mart_daily m1
            JOIN mart_daily m2 ON m2.day = m1.day + %s::int
            WHERE m1.day BETWEEN %s AND %s
              AND m1.{a} IS NOT NULL
              AND m2.{b} IS NOT NULL
            ORDER BY m1.day
            """
        ).format(a=a, b=b)
        with conn() as c, c.cursor() as cur:
            cur.execute(q, [lag, start_date, end_date])
            rows = cur.fetchall()
        n = len(rows)
        if n < 3:
            return rows, {"n": n, "pearson_r": None, "p_value": None,
                          "spearman_r": None, "lag_days": lag}
        a_vals = [float(r["a"]) for r in rows]
        b_vals = [float(r["b"]) for r in rows]
        p_r, p_p = stats.pearsonr(a_vals, b_vals)
        s_r, s_p = stats.spearmanr(a_vals, b_vals)
        primary_p = float(p_p if method == "pearson" else s_p)
        return rows, {
            "n": n,
            "pearson_r": _safe(p_r),
            "p_value": _safe(primary_p),
            "spearman_r": _safe(s_r),
            "lag_days": lag,
        }

    # ---- sweep mode ----
    if lag_range is not None:
        if (
            len(lag_range) != 2 or not all(isinstance(x, int) for x in lag_range)
            or lag_range[0] > lag_range[1]
        ):
            return _err(
                "correlate_metrics",
                ValueError("lag_range must be [min, max] integers with min <= max"),
            )
        lo, hi = lag_range
        lag_count = hi - lo + 1
        if lag_count > 21:
            return _err(
                "correlate_metrics",
                ValueError(f"lag_range covers {lag_count} lags; max is 21."),
            )
        results = []
        for lag in range(lo, hi + 1):
            _, stats_dict = _run(lag)
            results.append(stats_dict)
        # Highest-magnitude lag is what the user usually wants surfaced.
        non_null = [r for r in results if r.get("pearson_r") is not None]
        best = max(non_null, key=lambda r: abs(r["pearson_r"]), default=None)
        return _ok(
            "correlate_metrics",
            [],
            extra={
                "metric_a": metric_a,
                "metric_b": metric_b,
                "method": method,
                "lag_range": [lo, hi],
                "lags": results,
                "best_lag": best,
            },
        )

    # ---- single-lag mode (default) ----
    rows, stats_dict = _run(lag_days)
    if stats_dict["n"] < 3:
        return _ok(
            "correlate_metrics",
            _serialize(rows) if return_series else [],
            warnings=["Fewer than 3 paired observations; correlation undefined."],
            extra={**stats_dict, "metric_a": metric_a, "metric_b": metric_b,
                   "method": method},
        )
    capped = _serialize(rows[:366]) if return_series else []
    return _ok(
        "correlate_metrics",
        capped,
        truncated=len(rows) > 366 if return_series else False,
        extra={**stats_dict, "metric_a": metric_a, "metric_b": metric_b,
               "method": method},
    )


def _safe(x: float) -> float | None:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return float(x)


# ---- Whoop journal reads ---------------------------------------------------
def list_behaviors(category: str | None = None, search: str | None = None) -> dict:
    """List Whoop's behavior catalog. Filter by category (DAYTIME, NIGHTTIME,
    YOUR WEEKLY PLAN, ...) or substring search on title/internal_name."""
    where: list[str] = []
    params: list = []
    if category:
        where.append("category ILIKE %s")
        params.append(category)
    if search:
        where.append("(title ILIKE %s OR internal_name ILIKE %s)")
        params.append(f"%{search}%")
        params.append(f"%{search}%")
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    q = f"""
        SELECT behavior_id, internal_name, title, question_text, category,
               behavior_type, question_type, magnitude_type, magnitude_unit,
               magnitude_min, magnitude_max, status
        FROM dim_whoop_behavior {where_clause}
        ORDER BY category NULLS LAST, title
        LIMIT 500
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        return _ok("list_behaviors", _serialize(cur.fetchall()))


def get_journal_entries(start_date: date, end_date: date, day: date | None = None) -> dict:
    """Daily journal entries from raw_whoop_journal. If `day` given, returns
    just that day's full payload. Otherwise returns one row per day in the
    window with the parsed habit log + free-text notes."""
    if day is not None:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT day, fetched_at, payload FROM raw_whoop_journal WHERE day = %s",
                [day],
            )
            row = cur.fetchone()
        if row is None:
            return _ok("get_journal_entries", [])
        # Return the parsed habit_log alongside.
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT habit_key, answered_yes, magnitude_value, magnitude_unit,
                       time_input_value, user_reviewed
                FROM fact_habit_log
                WHERE day = %s
                ORDER BY habit_key
                """,
                [day],
            )
            habits = _serialize(cur.fetchall())
        out = _serialize([{**dict(row), "habits": habits}])
        return _ok("get_journal_entries", out)

    # Window summary: per-day habit count + notes.
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT
              j.day,
              j.fetched_at,
              j.payload->'journal'->>'notes' AS notes,
              (SELECT COUNT(*) FROM fact_habit_log h WHERE h.day = j.day) AS habit_count,
              (SELECT COUNT(*) FROM fact_habit_log h WHERE h.day = j.day AND h.answered_yes IS TRUE) AS yes_count
            FROM raw_whoop_journal j
            WHERE j.day BETWEEN %s AND %s
            ORDER BY j.day DESC
            """,
            [start_date, end_date],
        )
        return _ok("get_journal_entries", _serialize(cur.fetchall()))


def get_habit_history(
    habit_key: str,
    start_date: date,
    end_date: date,
) -> dict:
    """Time series for a single habit. `habit_key` is dim_whoop_behavior.
    internal_name (e.g. 'alcohol', 'caffeine', 'late-meal'). Use list_behaviors
    to discover available keys."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT day, answered_yes, magnitude_value, magnitude_unit,
                   time_input_value, user_reviewed
            FROM fact_habit_log
            WHERE habit_key = %s AND day BETWEEN %s AND %s
            ORDER BY day
            """,
            [habit_key, start_date, end_date],
        )
        rows = _serialize(cur.fetchall())

    yes_count = sum(1 for r in rows if r.get("answered_yes") is True)
    return _ok(
        "get_habit_history",
        rows,
        extra={
            "habit_key": habit_key,
            "n_days": len(rows),
            "yes_count": yes_count,
            "yes_rate": round(yes_count / len(rows), 3) if rows else None,
        },
    )


# ---- Whoop labs reads ------------------------------------------------------
def list_lab_tests() -> dict:
    """All ingested Advanced Labs panels — one row per test_id."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT r.test_id, r.test_name, r.test_date, r.fetched_at,
                   COUNT(f.id) AS biomarker_count,
                   COUNT(*) FILTER (WHERE f.status_type = 'OPTIMAL')      AS n_optimal,
                   COUNT(*) FILTER (WHERE f.status_type = 'SUFFICIENT')   AS n_sufficient,
                   COUNT(*) FILTER (WHERE f.status_type = 'OUT_OF_RANGE') AS n_out_of_range
            FROM raw_whoop_labs r
            LEFT JOIN fact_lab_result f ON f.test_id = r.test_id
            GROUP BY r.test_id, r.test_name, r.test_date, r.fetched_at
            ORDER BY r.test_date DESC NULLS LAST
            """
        )
        rows = _serialize(cur.fetchall())
    return _ok("list_lab_tests", rows)


def get_lab_results(
    biomarker_id: str | None = None,
    status: str | None = None,
    category: str | None = None,
    test_id: str | None = None,
    search: str | None = None,
) -> dict:
    """Lab biomarker results joined with their reference info.

    Defaults to the most recent panel. Filters compose with AND.
    Returns one row per biomarker with: current value+unit, status,
    optimal/sufficient bands, description, what high/low means, and
    the indicator's percentile on Whoop's range meter.
    """
    where: list[str] = []
    params: list = []

    if test_id is None:
        where.append("f.test_id = (SELECT test_id FROM raw_whoop_labs ORDER BY test_date DESC NULLS LAST LIMIT 1)")
    else:
        where.append("f.test_id = %s")
        params.append(test_id)

    if biomarker_id:
        where.append("f.biomarker_id = %s")
        params.append(biomarker_id)

    if status:
        where.append("f.status_type = %s")
        params.append(status.upper())

    if category:
        where.append("d.category ILIKE %s")
        params.append(category)

    if search:
        where.append("(d.title ILIKE %s OR d.biomarker_id ILIKE %s OR d.description ILIKE %s)")
        params.append(f"%{search}%")
        params.append(f"%{search}%")
        params.append(f"%{search}%")

    where_clause = " WHERE " + " AND ".join(where) if where else ""
    q = f"""
        SELECT
          f.biomarker_id,
          d.title,
          d.category,
          f.value_text       AS value,
          f.value_numeric,
          COALESCE(f.unit, d.unit) AS unit,
          f.status_type,
          f.trend,
          f.trend_display,
          d.optimal_low,
          d.optimal_high,
          d.sufficient_low,
          d.sufficient_high,
          d.description,
          d.what_high_means,
          d.what_low_means,
          d.influenced_by,
          d.notes,
          f.indicator_percent,
          f.test_id,
          f.test_date
        FROM fact_lab_result f
        JOIN dim_lab_biomarker d ON d.biomarker_id = f.biomarker_id
        {where_clause}
        ORDER BY
          CASE f.status_type
            WHEN 'OUT_OF_RANGE' THEN 0
            WHEN 'SUFFICIENT'   THEN 1
            WHEN 'OPTIMAL'      THEN 2
            ELSE 3
          END,
          d.category, d.title
        LIMIT 200
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(q, params)
        rows = _serialize(cur.fetchall())

    summary = {
        "total": len(rows),
        "n_optimal":      sum(1 for r in rows if r.get("status_type") == "OPTIMAL"),
        "n_sufficient":   sum(1 for r in rows if r.get("status_type") == "SUFFICIENT"),
        "n_out_of_range": sum(1 for r in rows if r.get("status_type") == "OUT_OF_RANGE"),
    }
    return _ok("get_lab_results", rows, extra={"summary": summary})


def get_biomarker_info(biomarker_id: str) -> dict:
    """Reference card for a biomarker: description, optimal/sufficient
    ranges, what high/low means, influenced_by — plus the user's most
    recent measured value if a panel has been ingested."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT
              d.biomarker_id, d.title, d.category, d.unit, d.description,
              d.optimal_low, d.optimal_high, d.sufficient_low, d.sufficient_high,
              d.what_high_means, d.what_low_means, d.influenced_by, d.notes,
              f.value_text         AS most_recent_value,
              f.value_numeric      AS most_recent_value_numeric,
              f.unit               AS most_recent_unit,
              f.status_type        AS most_recent_status,
              f.trend_display      AS most_recent_trend_display,
              f.test_date          AS most_recent_test_date
            FROM dim_lab_biomarker d
            LEFT JOIN LATERAL (
              SELECT * FROM fact_lab_result
              WHERE biomarker_id = d.biomarker_id
              ORDER BY test_date DESC NULLS LAST
              LIMIT 1
            ) f ON TRUE
            WHERE d.biomarker_id = %s
            """,
            [biomarker_id],
        )
        row = cur.fetchone()
    if row is None:
        return _err("get_biomarker_info", ValueError(
            f"Unknown biomarker_id '{biomarker_id}'. Use get_lab_results(search=...) to discover."
        ))
    return _ok("get_biomarker_info", _serialize([row]))


# ---- ask_sql ----------------------------------------------------------------
def ask_sql(
    query: str,
    max_rows: int = 200,
    timeout_ms: int | None = None,
    explain: bool = False,
) -> dict:
    """Read-only SQL escape hatch.

    `timeout_ms` overrides the default per-statement timeout (default 15s,
    bounded at 60s). `explain=True` returns the EXPLAIN plan instead of
    executing — handy for diagnosing why a query is slow without burning the
    full timeout."""
    try:
        validate(query)
    except UnsafeQueryError as e:
        return _err("ask_sql", e)

    final = ensure_limit(query, max_rows)
    if explain:
        final = "EXPLAIN (ANALYZE FALSE, BUFFERS FALSE, VERBOSE FALSE) " + final

    timeout = timeout_ms if timeout_ms is not None else ASK_SQL_TIMEOUT_MS
    timeout = max(500, min(timeout, 60_000))

    # Retry once if the pool is contended — transient on personal-scale data,
    # but the user shouldn't see "PoolTimeout" mid-conversation.
    last_err: Exception | None = None
    for attempt in range(2):
        t0 = time.perf_counter()
        try:
            with reader_conn() as c, c.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {timeout}")
                cur.execute(final)
                try:
                    rows = _serialize(cur.fetchall())
                except Exception:
                    rows = []
                cols = [d.name for d in (cur.description or [])]
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            truncated = len(rows) >= max_rows
            return _ok(
                "ask_sql",
                rows,
                truncated=truncated,
                warnings=[],
                extra={
                    "columns": cols,
                    "execution_ms": elapsed_ms,
                    "query_executed": final,
                    "attempts": attempt + 1,
                },
            )
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            # Only retry pool-timeout / transient connection errors.
            if attempt == 0 and ("pool" in msg or "timeout" in msg or "ssl" in msg):
                log.warning("ask_sql.retry", error=str(e))
                time.sleep(0.25)
                continue
            log.warning("ask_sql.exec_failed", error=str(e))
            return _err("ask_sql", e)

    return _err("ask_sql", last_err or RuntimeError("ask_sql: unknown failure"))


# ---- registry ---------------------------------------------------------------
# Tool name → callable + JSON schema (input). Used by server.py to wire MCP.
TOOLS: dict[str, dict] = {
    "get_schema_docs": {
        "fn": get_schema_docs,
        "description": "Return curated documentation about life-os tables, columns, conventions. CALL THIS FIRST for any analytical question.",
        "input": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string", "description": "Optional table to scope the docs."},
            },
        },
    },
    "get_daily_summary": {
        "fn": get_daily_summary,
        "description": "Daily-grain summary from mart_daily. Default columns are recovery, hrv, sleep, strain, meeting hours, kcal, spend.",
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "columns": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    "get_recovery_trend": {
        "fn": get_recovery_trend,
        "description": "Daily recovery, HRV, RHR, sleep duration. Optional trailing-N-day rolling averages.",
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "smoothing": {"type": "integer", "minimum": 2},
            },
        },
    },
    "get_sleep_summary": {
        "fn": get_sleep_summary,
        "description": "Per-day sleep metrics. Set include_naps=true to add nap counts/minutes.",
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "include_naps": {"type": "boolean", "default": False},
            },
        },
    },
    "get_workouts": {
        "fn": get_workouts,
        "description": "Individual workouts from fact_workout. Optional sport_name ILIKE filter.",
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "sport_name": {"type": "string"},
            },
        },
    },
    "get_food_log": {
        "fn": get_food_log,
        "description": "Per-item food log. Optional meal_window and food name search.",
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "meal_window": {"type": "string"},
                "search": {"type": "string"},
            },
        },
    },
    "get_meal_summary": {
        "fn": get_meal_summary,
        "description": "Per-meal rollup from mart_meal: kcal, macros, food names per meal_window.",
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "meal_window": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack"]},
            },
        },
    },
    "get_calendar_load": {
        "fn": get_calendar_load,
        "description": "Per-day meeting load and focus blocks from mart_daily.",
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
            },
        },
    },
    "get_calendar_events": {
        "fn": get_calendar_events,
        "description": "Individual calendar events. Optional classification + title search.",
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "classification": {
                    "type": "string",
                    "enum": ["meeting", "focus", "all_day", "declined", "personal"],
                },
                "search": {"type": "string"},
            },
        },
    },
    "get_spending": {
        "fn": get_spending,
        "description": (
            "Aggregated spending. group_by: day|week|month|category|merchant|account. "
            "Filters: category (ILIKE — pass exact_category=true for strict match; "
            "auto-escapes %, _, & so 'Bars & Nightlife' works), account_id (exact), "
            "account (ILIKE on account name), merchant (ILIKE)."
        ),
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "category": {"type": "string"},
                "group_by": {"type": "string", "enum": list(GROUP_BY_OPTIONS), "default": "day"},
                "account_id": {"type": "string"},
                "account": {"type": "string"},
                "exact_category": {"type": "boolean", "default": False},
                "merchant": {"type": "string"},
            },
        },
    },
    "get_transactions": {
        "fn": get_transactions,
        "description": (
            "Individual transactions with rich filters. "
            "category/merchant/account/tag are ILIKE substrings (auto-escape %, _, &); "
            "pass exact_category=true for strict category match. "
            "account_id / account_ids are exact. "
            "min_amount / max_amount compare against ABS(amount). "
            "only_charges=true drops income/refunds. "
            "Returns full Copilot metadata: tags, tag_ids, is_reviewed, tip_amount, parent_id, copilot_type."
        ),
        "input": {
            "type": "object",
            "required": ["start_date", "end_date"],
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "category": {"type": "string"},
                "exact_category": {"type": "boolean", "default": False},
                "merchant": {"type": "string"},
                "min_amount": {"type": "number"},
                "max_amount": {"type": "number"},
                "tag": {"type": "string"},
                "has_no_tags": {"type": "boolean", "default": False},
                "untagged_for_couples": {"type": "boolean", "default": False},
                "account_id": {"type": "string"},
                "account": {"type": "string"},
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "exclude_excluded": {"type": "boolean", "default": True},
                "only_charges": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 500, "minimum": 1, "maximum": 1000},
            },
        },
    },
    "get_biometrics": {
        "fn": get_biometrics,
        "description": "If metric is omitted, lists available metrics with counts and date ranges. Otherwise returns measurements.",
        "input": {
            "type": "object",
            "properties": {
                "metric": {"type": "string"},
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
            },
        },
    },
    "correlate_metrics": {
        "fn": correlate_metrics,
        "description": (
            "Pearson + Spearman correlation between two mart_daily columns over "
            "a date range. Single-lag mode (default) returns the paired series + "
            "stats. Sweep mode (pass lag_range=[min,max], up to 21 lags) returns "
            "stats per lag plus the best-magnitude lag — use this to find when "
            "an effect is strongest without 21 separate calls. "
            "Available metrics include all spend categories: total_spend, "
            "alcohol_spend, bars_spend, entertainment_spend, restaurant_spend, "
            "dining_out_txn_count, etc."
        ),
        "input": {
            "type": "object",
            "required": ["metric_a", "metric_b", "start_date", "end_date"],
            "properties": {
                "metric_a": {"type": "string", "enum": sorted(CORRELATE_ALLOWLIST)},
                "metric_b": {"type": "string", "enum": sorted(CORRELATE_ALLOWLIST)},
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "lag_days": {"type": "integer", "default": 0},
                "lag_range": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 2, "maxItems": 2,
                },
                "method": {"type": "string", "enum": ["pearson", "spearman"], "default": "pearson"},
                "return_series": {"type": "boolean", "default": True},
            },
        },
    },
    "list_lab_tests": {
        "fn": list_lab_tests,
        "description": (
            "List all ingested Whoop Advanced Labs panels with per-panel "
            "biomarker counts (n optimal / sufficient / out-of-range). One "
            "row per test_id."
        ),
        "input": {"type": "object", "properties": {}},
    },
    "get_lab_results": {
        "fn": get_lab_results,
        "description": (
            "Whoop Advanced Labs biomarker results joined with reference info. "
            "Defaults to the most recent panel. Filters: biomarker_id (exact), "
            "status (OPTIMAL|SUFFICIENT|OUT_OF_RANGE), category (ILIKE — e.g. "
            "'Cardiometabolic', 'Hormones', 'Liver', 'Kidney', 'Inflammation', "
            "'Blood Count', 'Iron Metabolism', 'Vitamins & Minerals'), test_id, "
            "or search (substring across title/description). Each row carries "
            "the user's value, unit, optimal/sufficient ranges, description, "
            "what high/low means, and influencing factors. Out-of-range rows "
            "sort first. ALWAYS check this for any health/biomarker question."
        ),
        "input": {
            "type": "object",
            "properties": {
                "biomarker_id": {"type": "string"},
                "status": {"type": "string", "enum": ["OPTIMAL", "SUFFICIENT", "OUT_OF_RANGE"]},
                "category": {"type": "string"},
                "test_id": {"type": "string"},
                "search": {"type": "string"},
            },
        },
    },
    "get_biomarker_info": {
        "fn": get_biomarker_info,
        "description": (
            "Reference card for a single biomarker: description, optimal "
            "and sufficient ranges, clinical interpretation of high/low, "
            "what influences it, plus the user's most recent measured "
            "value if available. biomarker_id is the Whoop slug "
            "(e.g. 'apolipoprotein_b', 'vitamin_d', 'estradiol')."
        ),
        "input": {
            "type": "object",
            "required": ["biomarker_id"],
            "properties": {"biomarker_id": {"type": "string"}},
        },
    },
    "ask_sql": {
        "fn": ask_sql,
        "description": (
            "Read-only SQL escape hatch against curated tables/views. Forbidden "
            "keywords (INSERT/UPDATE/DELETE/...) rejected; runs as the lifeos_mcp "
            "role. Default statement_timeout=15s (override via timeout_ms, max 60s). "
            "On pool exhaustion, retried once automatically. Pass explain=true to "
            "get the EXPLAIN plan without executing."
        ),
        "input": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "max_rows": {"type": "integer", "default": ASK_SQL_DEFAULT_LIMIT, "minimum": 1, "maximum": 5000},
                "timeout_ms": {"type": "integer", "minimum": 500, "maximum": 60000},
                "explain": {"type": "boolean", "default": False},
            },
        },
    },
}


def call(name: str, args: dict) -> dict:
    """Invoke a tool by name with a kwargs dict. Coerce date strings to date()."""
    spec = TOOLS.get(name)
    if spec is None:
        return _err(name, ValueError(f"Unknown tool: {name}"))

    coerced = _coerce_args(args, spec["input"])
    try:
        return spec["fn"](**coerced)
    except TypeError as e:
        return _err(name, e)


def _coerce_args(args: dict, schema: dict) -> dict:
    """Convert ISO date strings into date objects so tool fns get the types
    they declared, regardless of what the MCP client passed."""
    out = dict(args)
    props = schema.get("properties", {})
    for key, spec in props.items():
        if key not in out or out[key] is None:
            continue
        if spec.get("format") == "date" and isinstance(out[key], str):
            out[key] = date.fromisoformat(out[key])
    return out
