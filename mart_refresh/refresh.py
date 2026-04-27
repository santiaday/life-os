"""Rebuild every mart_* table from fact_*.

Order matters:
  1. mart_daily   — depends on every fact_*
  2. mart_meal    — depends on fact_food_log only
  3. mart_weekly  — depends on mart_daily

Each step is its own transaction so a failure in mart_meal doesn't roll back
mart_daily. Every refresh is a single ingestion_runs row with source='mart'.
"""

from __future__ import annotations

import time

from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run
from lifeos_core.settings import settings
from mart_refresh.sql import (
    MART_DAILY_REBUILD,
    MART_MEAL_REBUILD,
    MART_WEEKLY_REBUILD,
)

log = get_logger(__name__)


def refresh_mart_daily() -> int:
    with tx() as c, c.cursor() as cur:
        cur.execute(MART_DAILY_REBUILD, {"tz": settings.LOCAL_TZ})
        cur.execute("SELECT COUNT(*) AS n FROM mart_daily")
        return int(cur.fetchone()["n"])


def refresh_mart_meal() -> int:
    with tx() as c, c.cursor() as cur:
        cur.execute(MART_MEAL_REBUILD)
        cur.execute("SELECT COUNT(*) AS n FROM mart_meal")
        return int(cur.fetchone()["n"])


def refresh_mart_weekly() -> int:
    with tx() as c, c.cursor() as cur:
        cur.execute(MART_WEEKLY_REBUILD)
        cur.execute("SELECT COUNT(*) AS n FROM mart_weekly")
        return int(cur.fetchone()["n"])


def refresh_all() -> dict:
    """Returns per-table rowcounts and timings."""
    out: dict = {}
    with ingestion_run("mart", "refresh_all") as run:
        for name, fn in [
            ("mart_daily", refresh_mart_daily),
            ("mart_meal", refresh_mart_meal),
            ("mart_weekly", refresh_mart_weekly),
        ]:
            t0 = time.perf_counter()
            try:
                rows = fn()
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                out[name] = {"rows": rows, "ms": elapsed_ms}
                log.info("mart.refresh.table", table=name, rows=rows, ms=elapsed_ms)
            except Exception as e:
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                out[name] = {"error": f"{type(e).__name__}: {e}", "ms": elapsed_ms}
                log.exception("mart.refresh.table_failed", table=name)

        total_rows = sum(v["rows"] for v in out.values() if "rows" in v)
        run.upserted(total_rows)
        run.add_metadata(per_table=out)
    return out
