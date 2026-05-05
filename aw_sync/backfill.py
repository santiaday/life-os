#!/usr/bin/env python3
"""One-shot ActivityWatch backfill (companion to lite.py).

Re-pulls the last N hours from the local AW server and upserts every
detected work block. Use this after the daemon has been broken for a
while — AW retains data locally so we can rebuild from any window.

Usage on a laptop (after lite.py is already installed there):

    BACKFILL_HOURS=24 python3 ~/.local/share/aw-sync/backfill.py

Differences from lite.py's per-tick query:
- Uses raw not-afk events (no AW-side merge_events_by_keys, which sums
  durations and projects in-progress activity into the future).
- Ignores last_block_end — always queries the full N-hour window.
- Otherwise runs the same merge / clamp / upsert logic.

Existing rows in the same window get updated where source_event_ids
match (started_at hash). Mismatched fragments are not deleted — clean
those up server-side if needed.
"""

# noqa: UP017 — runs on macOS system python3 (3.9). Keep timezone.utc.
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

ENV_FILE = os.path.expanduser("~/.config/aw-sync.env")
if os.path.isfile(ENV_FILE):
    for line in open(ENV_FILE, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _req(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"FATAL: {name} not set in {ENV_FILE}")
    return val


SUPABASE_URL = _req("SUPABASE_URL").rstrip("/")
SUPABASE_KEY = _req("SUPABASE_SERVICE_KEY")
HOST_CAT = json.loads(_req("AW_HOSTNAME_CATEGORY"))
HOSTNAME = os.environ.get("AW_HOSTNAME_OVERRIDE") or socket.gethostname()
if HOSTNAME not in HOST_CAT:
    sys.exit(f"FATAL: hostname {HOSTNAME!r} not in AW_HOSTNAME_CATEGORY")
CATEGORY = HOST_CAT[HOSTNAME]
SOURCE = "aw_personal" if "personal" in CATEGORY.lower() else "aw_work"

AW_HOST = os.environ.get("AW_HOST", "http://localhost:5600").rstrip("/")
IDLE_GAP_S = int(os.environ.get("AW_IDLE_GAP_S", "600"))
MIN_BLOCK_S = int(os.environ.get("AW_MIN_BLOCK_S", "300"))
BACKFILL_HOURS = int(os.environ.get("BACKFILL_HOURS", "24"))


def _parse(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(re.sub(r"\.\d+", "", ts))


end = datetime.now(timezone.utc)
start = end - timedelta(hours=BACKFILL_HOURS)
print(f"backfill window: {start.isoformat()} -> {end.isoformat()}")
print(f"host={HOSTNAME}  source={SOURCE}  category={CATEGORY!r}")

body = {
    "query": [
        'afk_bucket = find_bucket("aw-watcher-afk_");'
        'afk_events = query_bucket(afk_bucket);'
        'RETURN = filter_keyvals(afk_events, "status", ["not-afk"]);'
    ],
    "timeperiods": [f"{start.isoformat()}/{end.isoformat()}"],
}
req = urllib.request.Request(
    f"{AW_HOST}/api/0/query/", method="POST",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"},
)
data = json.loads(urllib.request.urlopen(req, timeout=30).read())
events = data[0] if data else []
print(f"AW returned {len(events)} raw not-afk events")

parsed = []
for ev in events:
    s = _parse(ev["timestamp"])
    e = min(s + timedelta(seconds=ev["duration"]), end)
    if e > s:
        parsed.append((s, e))
parsed.sort()

if not parsed:
    print("nothing to backfill, exiting")
    sys.exit(0)

blocks = []
cs, ce = parsed[0]
for s, e in parsed[1:]:
    gap = (s - ce).total_seconds()
    if gap < IDLE_GAP_S:
        ce = max(ce, e)
    else:
        blocks.append((cs, ce))
        cs, ce = s, e
blocks.append((cs, ce))
blocks = [(s, e) for s, e in blocks if (e - s).total_seconds() >= MIN_BLOCK_S]
print(f"built {len(blocks)} merged blocks (>= {MIN_BLOCK_S}s each):")
for s, e in blocks:
    print(f"  {s.strftime('%H:%M')} -> {e.strftime('%H:%M')}  "
          f"({int((e-s).total_seconds()/60)} min)")

rows = []
for s, e in blocks:
    sid = hashlib.sha256(f"{HOSTNAME}|{s.isoformat()}".encode()).hexdigest()[:16]
    rows.append({
        "source": SOURCE, "source_event_id": sid, "event_type": "work_block",
        "category": CATEGORY, "title": CATEGORY,
        "started_at": s.isoformat(), "ended_at": e.isoformat(),
        "metadata": {
            "hostname": HOSTNAME,
            "duration_seconds": int((e - s).total_seconds()),
            "backfilled": True,
        },
    })

url = f"{SUPABASE_URL}/rest/v1/events?on_conflict=source,source_event_id"
req = urllib.request.Request(
    url, method="POST",
    data=json.dumps(rows).encode(),
    headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    },
)
resp = urllib.request.urlopen(req, timeout=30)
print(f"upserted {len(rows)} blocks (HTTP {resp.status})")
print("\nNext calendar_sync tick (~15 min) will publish them. Or hit it sooner")
print("by SSH-ing the droplet and running:")
print("  docker exec life-os-scheduler-1 python -m calendar_sync sync")
