"""Coverage reporting -- which days have exercise, sleep, nutrition and weight,
and which don't.

The point is not the percentages. It's the *runs*: a contiguous stretch with
no data in a domain is either something to go dig up from another export, or a
real-life gap worth knowing about. Single missing days are noise; a 40-day
hole is a missing source.
"""

from __future__ import annotations

from datetime import date

import psycopg

DOMAINS = ("activity", "sleep", "nutrition", "weight")

# Runs shorter than this are not interesting -- people skip a day.
MIN_GAP_DAYS = 4


def _matrix(conn: psycopg.Connection, start: date | None = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT day, has_activity, has_sleep, has_nutrition, has_weight, "
            "       activity_sources, sleep_sources, nutrition_sources "
            "FROM vw_coverage_daily "
            + ("WHERE day >= %s " if start else "")
            + "ORDER BY day",
            [start] if start else [],
        )
        return cur.fetchall()


def gaps(rows: list[dict], domain: str, min_days: int = MIN_GAP_DAYS) -> list[dict]:
    """Contiguous runs of days with no data for `domain`."""
    col = f"has_{domain}"
    out, run_start, prev = [], None, None
    for r in rows:
        if not r[col]:
            if run_start is None:
                run_start = r["day"]
            prev = r["day"]
        else:
            if run_start is not None:
                n = (prev - run_start).days + 1
                if n >= min_days:
                    out.append({"domain": domain, "start": run_start,
                                "end": prev, "days": n})
                run_start = None
    if run_start is not None:
        n = (prev - run_start).days + 1
        if n >= min_days:
            out.append({"domain": domain, "start": run_start, "end": prev, "days": n})
    return out


def report(conn: psycopg.Connection, start: date | None = None) -> dict:
    rows = _matrix(conn, start)
    if not rows:
        return {"error": "no coverage rows -- run `python -m unify all` first"}

    first, last = rows[0]["day"], rows[-1]["day"]
    total = (last - first).days + 1

    summary = {}
    for d in DOMAINS:
        have = sum(1 for r in rows if r[f"has_{d}"])
        summary[d] = {
            "days_with_data": have,
            "days_total": total,
            "coverage_pct": round(have / total * 100, 1) if total else 0.0,
        }

    all_gaps = []
    for d in DOMAINS:
        all_gaps += gaps(rows, d)
    all_gaps.sort(key=lambda g: -g["days"])

    # Per-year coverage makes it obvious which era is thin.
    by_year: dict[int, dict] = {}
    for r in rows:
        y = r["day"].year
        slot = by_year.setdefault(y, {d: 0 for d in DOMAINS} | {"days": 0})
        slot["days"] += 1
        for d in DOMAINS:
            if r[f"has_{d}"]:
                slot[d] += 1

    return {
        "range": {"first": first, "last": last, "days": total},
        "summary": summary,
        "by_year": {y: {**{d: f"{v[d]}/{v['days']}" for d in DOMAINS}}
                    for y, v in sorted(by_year.items())},
        "gaps": all_gaps[:60],
        "gap_count": len(all_gaps),
    }


def missing_days(conn: psycopg.Connection, domain: str,
                 start: date | None = None) -> list[date]:
    rows = _matrix(conn, start)
    return [r["day"] for r in rows if not r[f"has_{domain}"]]
