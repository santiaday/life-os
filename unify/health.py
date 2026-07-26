"""source_health refresh -- GAP-8.

Recomputes `last_row_day` for every registered stream by looking at the table
and source filter the registry names, so `vw_data_freshness` answers "is what
you're telling me current?" without running max(day) across nine tables by hand.

Registry-driven on purpose: adding a source means one INSERT into
`source_health`, not a code change here.
"""

from __future__ import annotations

import psycopg
from psycopg import sql

from lifeos_core.logging import get_logger

log = get_logger(__name__)


def refresh(conn: psycopg.Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT source_key, table_name, day_column, source_filter "
                    "FROM source_health WHERE table_name IS NOT NULL")
        streams = cur.fetchall()

    updated = failed = 0
    for s in streams:
        query = sql.SQL("SELECT max({day}) AS d FROM {tbl}").format(
            day=sql.Identifier(s["day_column"]),
            tbl=sql.Identifier(s["table_name"]),
        )
        params: list = []
        if s["source_filter"]:
            query = sql.SQL("{q} WHERE source = %s").format(q=query)
            params.append(s["source_filter"])
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                last = cur.fetchone()["d"]
                cur.execute(
                    "UPDATE source_health SET last_row_day = %s, "
                    "last_checked_at = now(), updated_at = now() "
                    "WHERE source_key = %s",
                    [last, s["source_key"]],
                )
            updated += 1
        except Exception as e:
            conn.rollback()
            failed += 1
            log.warning("health.refresh_failed", source_key=s["source_key"],
                        error=str(e))
    log.info("health.refresh", updated=updated, failed=failed)
    return {"updated": updated, "failed": failed}


def summary(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_key, domain, display_name, mode, status, "
            "       last_row_day, lag_hours, sla_hours, coverage_start, "
            "       coverage_end, notes "
            "FROM vw_data_freshness "
            "ORDER BY CASE status WHEN 'stale' THEN 0 WHEN 'lagging' THEN 1 "
            "                     WHEN 'no_data' THEN 2 ELSE 3 END, source_key"
        )
        return cur.fetchall()
