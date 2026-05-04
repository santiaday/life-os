"""ActivityWatch → Supabase sync.

Runs on each laptop under launchd every 5 minutes. Pulls not-afk events
from the local ActivityWatch server, merges them into work blocks, and
upserts to the shared `events` table. The VPS-side calendar_sync then
pushes them to Google Calendar.

Why this lives in the LifeOS repo (rather than a standalone script):
- Reuses lifeos_core.events.upsert_events, settings, logging.
- Keeps the schema contract in one place — if we change `events`, we
  change all writers in one PR.
- The laptop deploy is just a venv with this repo checked out + plist.

Hostname → category mapping comes from AW_HOSTNAME_CATEGORY env. We
don't auto-detect; explicit configuration is the only way to keep work
data off the personal laptop and vice versa.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import httpx

from lifeos_core import events as events_store
from lifeos_core.db import tx
from lifeos_core.logging import configure_logging, get_logger

log = get_logger(__name__)

# Defaults — mirror the build spec. Override via env on the laptop.
DEFAULT_AW_HOST = "http://localhost:5600"
DEFAULT_IDLE_GAP_S = 600         # >10min gap → split into separate blocks
DEFAULT_MIN_BLOCK_S = 300        # drop blocks shorter than 5min
DEFAULT_LOOKBACK_HOURS = 1       # first run reaches back this far
DEFAULT_MAX_BLOCK_S = 24 * 3600  # sanity cap

# AWQL query — returns merged not-afk events. We use only AFK status
# (not window titles); categorization is by hostname only per spec.
AW_QUERY = (
    'afk_bucket = find_bucket("aw-watcher-afk_");'
    'afk_events = query_bucket(afk_bucket);'
    'not_afk = filter_keyvals(afk_events, "status", ["not-afk"]);'
    'RETURN = merge_events_by_keys(not_afk, ["status"]);'
)


def _hostname() -> str:
    """Short hostname (no domain). socket.gethostname() can return a
    fully-qualified name on some macOS configurations."""
    raw = (
        # Allow override for VMs / hostname-renaming environments.
        # Read directly from os.environ so we don't pollute Settings.
        __import__("os").environ.get("AW_HOSTNAME_OVERRIDE")
        or socket.gethostname()
    )
    return raw.split(".")[0]


def _category_for(hostname: str) -> str:
    """Look up category from AW_HOSTNAME_CATEGORY (JSON string).

    Format: {"Santiago-Aday": "DoorLoop work", "Santiago-Personal": "Personal work"}
    """
    raw = __import__("os").environ.get("AW_HOSTNAME_CATEGORY", "{}")
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"AW_HOSTNAME_CATEGORY is not valid JSON: {e}") from e
    if hostname not in mapping:
        raise RuntimeError(
            f"Hostname {hostname!r} not in AW_HOSTNAME_CATEGORY: keys are "
            f"{sorted(mapping.keys())}. Set AW_HOSTNAME_OVERRIDE if needed."
        )
    return mapping[hostname]


def _source_tag(category: str) -> str:
    """events.source value. Embeds work/personal so analytics can split
    cheaply without joining on category text."""
    if "personal" in category.lower():
        return "aw_personal"
    return "aw_work"


# ---- Window resolution ------------------------------------------------------
def _last_block_end(source: str) -> datetime | None:
    """Most recent ended_at we've stored for this hostname's source.
    Resume from there to avoid re-querying ground we already covered."""
    with tx() as c, c.cursor() as cur:
        cur.execute(
            "SELECT max(ended_at) AS last_end FROM events WHERE source = %s",
            [source],
        )
        row = cur.fetchone()
        return row["last_end"] if row and row["last_end"] else None


def _resolve_window(source: str, lookback_hours: int) -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    last = _last_block_end(source)
    if last is not None:
        # Re-query with a small overlap so a block that was still in
        # progress on the last sync gets its ended_at extended.
        start = last - timedelta(minutes=15)
    else:
        start = end - timedelta(hours=lookback_hours)
    return start, end


# ---- ActivityWatch query ----------------------------------------------------
def _query_aw(host: str, start: datetime, end: datetime) -> list[dict]:
    url = f"{host.rstrip('/')}/api/0/query/"
    body = {
        "query": [AW_QUERY],
        "timeperiods": [f"{start.isoformat()}/{end.isoformat()}"],
    }
    r = httpx.post(url, json=body, timeout=30.0)
    r.raise_for_status()
    data = r.json()
    if not data:
        return []
    # AW returns [[event, event, ...]] — one list per timeperiod.
    return data[0] or []


def _parse_aw_event(ev: dict) -> tuple[datetime, datetime]:
    """AW events have ISO timestamp and a duration in seconds."""
    ts = ev["timestamp"]
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    start = datetime.fromisoformat(ts)
    end = start + timedelta(seconds=ev["duration"])
    return start, end


# ---- Block construction -----------------------------------------------------
def build_blocks(
    aw_events: Iterable[dict],
    *,
    idle_gap_s: int,
    min_block_s: int,
    max_block_s: int,
) -> list[tuple[datetime, datetime]]:
    """Merge AW not-afk events into contiguous work blocks.

    Two events are part of the same block if the gap between them is
    less than `idle_gap_s`. Blocks shorter than `min_block_s` are dropped
    (catches micro-activity bursts). Blocks longer than `max_block_s`
    are clipped — defends against clock skew or AFK-watcher being off.
    """
    parsed = sorted((_parse_aw_event(e) for e in aw_events), key=lambda x: x[0])
    if not parsed:
        return []

    blocks: list[list[datetime]] = []
    cur_start, cur_end = parsed[0]
    for s, e in parsed[1:]:
        gap = (s - cur_end).total_seconds()
        if 0 <= gap < idle_gap_s:
            cur_end = max(cur_end, e)
        else:
            blocks.append([cur_start, cur_end])
            cur_start, cur_end = s, e
    blocks.append([cur_start, cur_end])

    out: list[tuple[datetime, datetime]] = []
    for s, e in blocks:
        dur = (e - s).total_seconds()
        if dur < min_block_s:
            continue
        if dur > max_block_s:
            log.warning("aw_sync.block_too_long_clipped",
                        seconds=dur, started_at=s.isoformat())
            e = s + timedelta(seconds=max_block_s)
        out.append((s, e))
    return out


# ---- Main sync --------------------------------------------------------------
def sync_once(
    *,
    aw_host: str = DEFAULT_AW_HOST,
    idle_gap_s: int = DEFAULT_IDLE_GAP_S,
    min_block_s: int = DEFAULT_MIN_BLOCK_S,
    max_block_s: int = DEFAULT_MAX_BLOCK_S,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> dict:
    hostname = _hostname()
    category = _category_for(hostname)
    source = _source_tag(category)

    start, end = _resolve_window(source, lookback_hours)
    log.info("aw_sync.window",
             hostname=hostname, source=source, category=category,
             start=start.isoformat(), end=end.isoformat())

    aw_events = _query_aw(aw_host, start, end)
    blocks = build_blocks(
        aw_events,
        idle_gap_s=idle_gap_s,
        min_block_s=min_block_s,
        max_block_s=max_block_s,
    )

    rows = [
        {
            "source": source,
            "source_event_id": events_store.make_aw_source_event_id(
                hostname, s.isoformat()
            ),
            "event_type": "work_block",
            "category": category,
            "title": category,
            "started_at": s,
            "ended_at": e,
            "metadata": {
                "hostname": hostname,
                "duration_seconds": int((e - s).total_seconds()),
            },
        }
        for s, e in blocks
    ]

    written = events_store.upsert_events(rows)
    summary = {
        "hostname": hostname,
        "category": category,
        "raw_aw_events": len(aw_events),
        "blocks": len(rows),
        "rows_written": written,
    }
    log.info("aw_sync.done", **summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="aw_sync")
    p.add_argument("--aw-host", default=DEFAULT_AW_HOST)
    p.add_argument("--idle-gap", type=int, default=DEFAULT_IDLE_GAP_S)
    p.add_argument("--min-block", type=int, default=DEFAULT_MIN_BLOCK_S)
    p.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    args = p.parse_args(argv)
    try:
        result = sync_once(
            aw_host=args.aw_host,
            idle_gap_s=args.idle_gap,
            min_block_s=args.min_block,
            lookback_hours=args.lookback_hours,
        )
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as e:
        log.exception("aw_sync.failed")
        print(f"FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
