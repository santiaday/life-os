"""CLI for the unification layer.

    python -m unify all                 project every source, dedupe, flag, refresh health
    python -m unify all --rebuild       drop the derived tables first (full re-derive)
    python -m unify source garmin       project one source
    python -m unify dedupe
    python -m unify quality
    python -m unify health
    python -m unify freshness           print the freshness table
    python -m unify coverage            print the day-by-day gap report

`all` is idempotent and is what the scheduler runs after every ingest. Use
`--rebuild` when a projection's logic changes: the canonical tables are pure
functions of `raw_import` plus the source-specific fact tables, so throwing
them away and re-deriving is always safe and is the only way to retire rows a
projection no longer emits.
"""

from __future__ import annotations

import argparse
import json
import sys

from lifeos_core.db import tx
from lifeos_core.logging import configure_logging, get_logger
from lifeos_core.runs import ingestion_run
from unify import dedupe, health, quality
from unify.common import seed_dimensions
from unify.sources import (
    cronometer,
    garmin,
    hevy,
    labs,
    loseit,
    nutrition,
    whoop,
    zero,
)

log = get_logger(__name__)

SOURCES = {
    "garmin": garmin.project_all,
    "whoop": whoop.project_all,
    "hevy": hevy.project_all,
    "zero": zero.project_all,
    "loseit": loseit.project_all,
    "nutrition": nutrition.project_all,
    "cronometer": cronometer.project_all,
    "labs": labs.project_all,
}

# Derived tables, in dependency order for truncation.
DERIVED_TABLES = (
    "fact_activity_set",
    "fact_activity_exercise",
    "fact_activity",
    "fact_sleep_session",
    "fact_nutrition_entry",
    "fact_nutrition_daily",
    "fact_body_composition",
    "fact_daily_metric",
    "fact_fast",
)


def rebuild_tables(conn) -> None:
    with conn.cursor() as cur:
        for t in DERIVED_TABLES:
            cur.execute(f"TRUNCATE {t} CASCADE")
    log.warning("unify.rebuild.truncated", tables=list(DERIVED_TABLES))


def run_all(*, rebuild: bool = False) -> dict:
    out: dict = {}
    with ingestion_run("unify", "all", rebuild=rebuild) as run, tx() as conn:
        if rebuild:
            rebuild_tables(conn)
        out["dimensions"] = seed_dimensions(conn)
        for name, fn in SOURCES.items():
            try:
                out[name] = fn(conn)
            except Exception as e:
                conn.rollback()
                out[name] = {"error": f"{type(e).__name__}: {e}"}
                log.exception("unify.source_failed", source=name)
        out["dedupe"] = dedupe.run_all(conn)
        out["quality"] = quality.run_all(conn)
        out["health"] = health.refresh(conn)
        run.upserted(sum(v for d in out.values() if isinstance(d, dict)
                         for v in d.values() if isinstance(v, int)))
    return out


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(description="Project every source into the unified layer.")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("all", help="Full projection + dedupe + quality + health.")
    a.add_argument("--rebuild", action="store_true",
                   help="TRUNCATE the derived tables first, then re-derive.")

    s = sub.add_parser("source", help="Project a single source.")
    s.add_argument("name", choices=sorted(SOURCES))

    sub.add_parser("dedupe")
    sub.add_parser("quality")
    sub.add_parser("health")
    sub.add_parser("freshness")
    sub.add_parser("coverage")

    args = p.parse_args(argv)

    if args.cmd == "all":
        _print(run_all(rebuild=args.rebuild))
    elif args.cmd == "source":
        with tx() as conn:
            _print(SOURCES[args.name](conn))
    elif args.cmd == "dedupe":
        with tx() as conn:
            _print(dedupe.run_all(conn))
    elif args.cmd == "quality":
        with tx() as conn:
            _print(quality.run_all(conn))
    elif args.cmd == "health":
        with tx() as conn:
            _print(health.refresh(conn))
    elif args.cmd == "freshness":
        with tx() as conn:
            health.refresh(conn)
            _print(health.summary(conn))
    elif args.cmd == "coverage":
        from unify.coverage import report

        with tx() as conn:
            _print(report(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
