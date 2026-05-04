"""One-shot backfill: derive `events` rows from existing raw_whoop_*.

Use this after applying migration 0012 to populate the events table from
the historical Whoop payloads already in the warehouse — no API calls,
no rate limits. The standard incremental ingest will keep things fresh
from this point forward.

Usage:
    python -m scripts.backfill_events                 # all sources
    python -m scripts.backfill_events --source sleep  # just sleep
    python -m scripts.backfill_events --since 2024-01-01

After it finishes, calendar_sync's next tick will publish everything to
Google Calendar. For a one-shot push:

    python -m calendar_sync sync
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime

from ingest_whoop import transforms
from lifeos_core.db import tx
from lifeos_core.events import upsert_events
from lifeos_core.logging import configure_logging, get_logger

log = get_logger(__name__)

CHUNK = 500


def _iter_raw(table: str, since: datetime | None):
    """Stream rows from a raw_whoop_* table, payload first.

    Filters by the JSONB payload's `start` field if --since given. We use
    `start` (the activity's actual start) rather than fetched_at, since
    payloads can be re-fetched without their underlying activity moving."""
    q = f"SELECT payload FROM {table}"
    params: list = []
    if since is not None:
        q += " WHERE (payload->>'start')::timestamptz >= %s"
        params.append(since)
    q += " ORDER BY (payload->>'start')::timestamptz"

    with tx() as c, c.cursor() as cur:
        cur.execute(q, params)
        for row in cur:
            yield row["payload"]


def _backfill(name: str, table: str, projector: Callable[[dict], dict | None],
              since: datetime | None) -> int:
    written = 0
    batch: list[dict] = []
    skipped = 0
    for payload in _iter_raw(table, since):
        ev = projector(payload)
        if ev is None:
            skipped += 1
            continue
        batch.append(ev)
        if len(batch) >= CHUNK:
            written += upsert_events(batch)
            batch = []
    if batch:
        written += upsert_events(batch)
    log.info("backfill.done", source=name, written=written, skipped=skipped)
    return written


def run(*, sources: list[str], since: datetime | None) -> dict:
    plan: dict[str, tuple[str, Callable[[dict], dict | None]]] = {
        "sleep":   ("raw_whoop_sleep",   transforms.to_sleep_event),
        "workout": ("raw_whoop_workout", transforms.to_workout_event),
    }
    results: dict[str, int] = {}
    for src in sources:
        if src not in plan:
            log.warning("backfill.unknown_source", src=src, valid=list(plan.keys()))
            continue
        table, projector = plan[src]
        results[src] = _backfill(src, table, projector, since)
    return results


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="backfill_events")
    p.add_argument(
        "--source",
        action="append",
        choices=["sleep", "workout"],
        help="Restrict to one source. Repeat for multiple. Default: all.",
    )
    p.add_argument(
        "--since",
        type=lambda s: datetime.fromisoformat(s),
        default=None,
        help="ISO date/timestamp; only payloads with start >= this are projected.",
    )
    args = p.parse_args(argv)
    sources = args.source or ["sleep", "workout"]
    results = run(sources=sources, since=args.since)
    print(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
