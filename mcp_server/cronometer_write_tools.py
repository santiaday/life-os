"""MCP write tools for Cronometer's mobile REST API.

Four tools, written entirely against `ingest_cronometer.mobile_client` (which
talks to mobile.cronometer.com, NOT the GWT-RPC API the nightly Go-binary
pipeline uses):

  search_foods(query)               POST /api/v2/find_food
  log_food(food_id, grams, ...)     POST /api/v2/add_serving
  create_custom_food(...)           POST /api/v2/add_food
  delete_food_entry(entry_ids, ..)  DELETE /api/v3/user/{id}/diary-entries

These DO NOT mirror into the local Supabase mart. Cronometer is the source of
truth; the nightly `cron_ingest.run_all()` pulls fresh entries into
fact_food_log / fact_food_daily on its next pass. Callers who need the write
to be visible in mart_daily / get_food_log immediately should follow with
`refresh_data('cronometer')` — slow, since it re-runs the Go binary across
every data type.

Returns the standard `_ok` / `_err` envelope from mcp_server.tools.
"""

from __future__ import annotations

from datetime import date, datetime

from ingest_cronometer.mobile_client import (
    MEAL_GROUPS,
    CronometerAPIError,
    CronometerAuthError,
    get_shared_client,
)
from lifeos_core.logging import get_logger
from mcp_server.tools import _err, _ok

log = get_logger(__name__)


# Default: every diary write triggers an immediate food-pipeline sync (Go
# binary → fact_food_log / fact_food_daily) so a subsequent get_food_log /
# get_meal_summary in the same session sees the new entry. Skipped only when
# the caller passes sync=False (useful for batch flows that want one sync
# after several writes).
_MART_NOTE = (
    "Synced fact_food_log + fact_food_daily. mart_daily columns are still "
    "stale until refresh_data('mart') runs — call that if a daily-grain "
    "query needs the new entry reflected."
)
_NO_SYNC_NOTE = (
    "sync=False: skipped the post-write sync. The entry is in Cronometer "
    "but won't appear in fact_food_log until the next nightly refresh (or "
    "manual refresh_data('cronometer'))."
)


def _sync_food_pipelines() -> dict | None:
    """Run servings + daily-nutrition ingest right after a write so the
    warehouse is fresh. Failures are logged but don't fail the write —
    Cronometer is source of truth and the nightly cron will reconcile."""
    try:
        from ingest_cronometer.ingest import run_food_pipelines
        result = run_food_pipelines()
        log.info("cronometer.write.sync.ok", result=str(result)[:200])
        return result
    except Exception as e:
        log.exception("cronometer.write.sync.failed")
        return {"error": f"{type(e).__name__}: {e}"}


# ---- search_foods ----------------------------------------------------------
def search_foods(query: str, limit: int = 25) -> dict:
    """Search Cronometer's food database by name. Returns ranked matches with
    enough info (food_id, measure_id, translation_id) to pass directly to
    log_food. Call this first whenever you need a food_id."""
    if not query or not query.strip():
        return _err("search_foods", ValueError("query is required"))
    try:
        raw = get_shared_client().search_food(query.strip())
    except (CronometerAuthError, CronometerAPIError) as e:
        return _err("search_foods", e)

    rows = []
    for f in raw[:limit]:
        rows.append({
            "food_id": f.get("id"),
            "name": f.get("name"),
            "brand": f.get("brand"),
            "source": f.get("source"),
            "measure_id": f.get("measureId"),
            "measure_display": f.get("measureDisplayName"),
            "translation_id": f.get("translationId") or 0,
            "score": f.get("score"),
            "global_popularity": f.get("globalPopularity"),
        })

    return _ok(
        "search_foods",
        rows,
        extra={"query": query, "result_count": len(rows), "total_returned": len(raw)},
    )


# ---- log_food --------------------------------------------------------------
def log_food(
    food_id: int,
    grams: float,
    measure_id: int | None = None,
    meal_window: str | None = None,
    eaten_at: str | None = None,
    translation_id: int = 0,
    sync: bool = True,
) -> dict:
    """Log a food serving to the Cronometer diary.

    Inputs:
      food_id          Cronometer's food id (int). Get via search_foods.
      grams            Weight in grams. Cronometer is gram-native; for
                       'X servings' compute grams = X * serving_gram_weight
                       (visible on a food's measures).
      measure_id       Cronometer measure (unit) id. Search results carry one
                       in `measure_id`; pass it through. REQUIRED for
                       database foods (CRDB / NCCDB / FDC). If omitted, the
                       tool auto-resolves the food's defaultMeasureId via an
                       extra round-trip. 0 is valid only for user-created
                       custom foods.
      meal_window      'breakfast' | 'lunch' | 'dinner' | 'snacks' | 'auto'
                       (default 'auto' — server picks from time-of-day, which
                       can mis-bucket morning meals logged late).
      eaten_at         ISO date (YYYY-MM-DD) or datetime to backdate the
                       entry. Default: now. Timezone offsets are stripped —
                       Cronometer stores diary time as naive local.
      translation_id   Pass through from search results; usually 0.
      sync             Default True: after a successful write, re-run the
                       servings + daily-nutrition ingest pipelines so the new
                       entry lands in fact_food_log / fact_food_daily
                       immediately (adds ~5-15s). Set False when batching
                       several writes and you'll sync once at the end.

    Returns the created entry_id.
    """
    if not isinstance(food_id, int) or food_id <= 0:
        return _err("log_food", ValueError("food_id must be a positive int"))
    if not isinstance(grams, (int, float)) or grams <= 0:
        return _err("log_food", ValueError("grams must be > 0"))

    window_key = (meal_window or "auto").strip().lower()
    group_int = MEAL_GROUPS.get(window_key)
    if group_int is None:
        return _err(
            "log_food",
            ValueError(
                f"meal_window must be one of {sorted(set(MEAL_GROUPS))} or omitted"
            ),
        )

    target_day: date | None = None
    eaten_time: datetime | None = None
    if eaten_at:
        raw = eaten_at.strip()
        try:
            if "T" in raw or " " in raw:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                # Cronometer's diary time is naive-local. Strip tz info if
                # present so we don't accidentally bump to another day.
                if parsed.tzinfo is not None:
                    parsed = parsed.replace(tzinfo=None)
                eaten_time = parsed
                target_day = parsed.date()
            else:
                target_day = date.fromisoformat(raw)
        except ValueError as e:
            return _err("log_food", ValueError(f"eaten_at: bad ISO format ({e})"))

    try:
        c = get_shared_client()
        resolved_measure_id = measure_id
        if resolved_measure_id is None:
            # Auto-resolve: hit get_food for the defaultMeasureId. One
            # extra request, but it means search → log works without the
            # caller threading measure_id through.
            detail = c.get_food(food_id)
            resolved_measure_id = detail.get("defaultMeasureId")
            if resolved_measure_id is None:
                return _err(
                    "log_food",
                    CronometerAPIError(
                        f"food {food_id}: no defaultMeasureId in response; "
                        f"pass measure_id explicitly from search_foods"
                    ),
                )
        resp = c.add_serving(
            food_id=food_id,
            grams=float(grams),
            measure_id=int(resolved_measure_id),
            translation_id=int(translation_id),
            day=target_day,
            diary_group=group_int,
            eaten_time=eaten_time,
        )
    except (CronometerAuthError, CronometerAPIError) as e:
        return _err("log_food", e)

    # add_serving's response shape carries the new serving id under "id"
    # (servingId is what the diary fetch later returns; for a fresh write,
    # `id` is canonical).
    entry_id = resp.get("id") or resp.get("servingId")

    sync_result = _sync_food_pipelines() if sync else None
    return _ok(
        "log_food",
        [{
            "entry_id": entry_id,
            "food_id": food_id,
            "grams": grams,
            "measure_id": resolved_measure_id,
            "meal_window": window_key,
            "day": str(target_day or date.today()),
            "sync_result": sync_result,
        }],
        warnings=[_MART_NOTE if sync else _NO_SYNC_NOTE],
    )


# ---- create_custom_food ----------------------------------------------------
def create_custom_food(
    name: str,
    serving_size_g: float,
    calories: float,
    protein_g: float,
    fat_g: float,
    carbs_g: float,
    fiber_g: float = 0,
    sugar_g: float = 0,
    sodium_mg: float = 0,
    serving_name: str = "1 serving",
) -> dict:
    """Create a custom food in Cronometer. Use for restaurant meals, recipes,
    and home-cooked items not in Cronometer's DB. Macros are PER SERVING; the
    tool re-normalizes to Cronometer's per-100g storage internally.

    Returns the new food_id + measure_id ready to pass to log_food."""
    if not name or not name.strip():
        return _err("create_custom_food", ValueError("name is required"))
    if serving_size_g is None or serving_size_g <= 0:
        return _err("create_custom_food", ValueError("serving_size_g must be > 0"))

    try:
        c = get_shared_client()
        created = c.create_custom_food(
            name.strip(),
            calories=float(calories),
            protein_g=float(protein_g),
            fat_g=float(fat_g),
            carbs_g=float(carbs_g),
            fiber_g=float(fiber_g),
            sugar_g=float(sugar_g),
            sodium_mg=float(sodium_mg),
            serving_name=serving_name,
            serving_grams=float(serving_size_g),
        )
        # Re-fetch so we get the server-assigned measure_id (the add_food
        # response carries id but not always the realized measure row).
        detail = c.get_food(created["id"])
    except (CronometerAuthError, CronometerAPIError) as e:
        return _err("create_custom_food", e)

    measure_id = detail.get("defaultMeasureId")
    return _ok(
        "create_custom_food",
        [{
            "food_id": detail.get("id") or created.get("id"),
            "measure_id": measure_id,
            "name": detail.get("name") or name,
            "serving_size_g": serving_size_g,
            "calories_per_serving": calories,
        }],
        warnings=[
            "Pass food_id + measure_id to log_food to add this to a diary day.",
        ],
    )


# ---- delete_food_entry -----------------------------------------------------
def delete_food_entry(
    entry_ids: list[int | str] | int | str,
    day: str | None = None,
    sync: bool = True,
) -> dict:
    """Delete one or more diary entries by their serving id (the `entry_id`
    returned by log_food, or `servingId` surfaced from a diary fetch).

    `day` defaults to today. If you're deleting an entry from a past date,
    you MUST pass that date in YYYY-MM-DD format — Cronometer's v3 DELETE
    requires the full serving object, which we fetch from get_diary(day).

    `sync=True` (default) re-runs the food ingest pipelines after the
    delete so fact_food_log reflects the removal immediately. Set False
    when batching several deletes.
    """
    if entry_ids in (None, "", []):
        return _err("delete_food_entry", ValueError("entry_ids is required"))
    if isinstance(entry_ids, (int, str)):
        entry_ids = [entry_ids]
    if not isinstance(entry_ids, list) or not entry_ids:
        return _err(
            "delete_food_entry",
            ValueError("entry_ids must be a non-empty list"),
        )

    target_day: date | None = None
    if day:
        try:
            target_day = date.fromisoformat(day)
        except ValueError as e:
            return _err("delete_food_entry", ValueError(f"day: bad ISO format ({e})"))

    try:
        result = get_shared_client().delete_entries(
            [str(e) for e in entry_ids], day=target_day,
        )
    except (CronometerAuthError, CronometerAPIError) as e:
        return _err("delete_food_entry", e)

    sync_result = (
        _sync_food_pipelines()
        if sync and (result.get("count") or 0) > 0
        else None
    )
    warnings = [_MART_NOTE if sync else _NO_SYNC_NOTE]
    if result.get("missing"):
        warnings.insert(
            0,
            f"Some entry_ids not found in the {target_day or date.today()} "
            f"diary (already deleted? wrong day?): {result['missing']}",
        )
    return _ok(
        "delete_food_entry",
        [{
            "removed_ids": result.get("removed") or [],
            "count": result.get("count") or 0,
            "missing": result.get("missing") or [],
            "day": str(target_day or date.today()),
            "sync_result": sync_result,
        }],
        warnings=warnings,
    )
