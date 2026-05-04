#!/usr/bin/env python3
"""Standalone ActivityWatch → Supabase events sync.

Single-file script, stdlib only (no pip install). Drop on a laptop with:

    1. ActivityWatch installed and running (port 5600 default).
    2. Python 3.9+ available as `python3` (macOS ships this).
    3. ~/.config/aw-sync.env with the required env vars (see below).

This script does NOT need the LifeOS repo. It writes to Supabase via the
PostgREST endpoint, not direct PG, so the laptop only needs HTTPS access.

Required env (set in shell or in ~/.config/aw-sync.env):

    SUPABASE_URL              e.g. https://dqnrcarouldholuqfxbb.supabase.co
    SUPABASE_SERVICE_KEY      service_role JWT from Supabase → Settings → API
    AW_HOSTNAME_CATEGORY      JSON map: {"<host>":"DoorLoop work", ...}

Optional:

    AW_HOST                   default http://localhost:5600
    AW_HOSTNAME_OVERRIDE      override socket.gethostname() short form
    AW_IDLE_GAP_S             default 600  (split blocks past this gap)
    AW_MIN_BLOCK_S            default 300  (drop blocks shorter than this)
    AW_LOOKBACK_HOURS         default 1    (how far back the first run reaches)
    AW_LOG_FILE               optional; tee logs here (default: stderr only)

The events table on the Supabase side has the natural key
(source, source_event_id), so re-running this script over the same
window updates the same rows — no duplicates.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import urllib.error
import urllib.request
# noqa: UP017 — this script must run on macOS system python3 (3.9), which
# doesn't have `from datetime import UTC` (added in 3.11). Keep timezone.utc.
from datetime import datetime, timedelta, timezone

# ---- Config -----------------------------------------------------------------
ENV_FILE = os.path.expanduser("~/.config/aw-sync.env")


def _load_env_file(path: str) -> None:
    """Best-effort load of KEY=VALUE pairs from a dotenv-ish file. Lines
    starting with `#` are comments. Existing process env wins, so users
    can override on the command line."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


_load_env_file(ENV_FILE)


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(
            f"FATAL: {name} not set. Configure {ENV_FILE} or export it. "
            f"See script header for the full list."
        )
    return val


SUPABASE_URL = _require("SUPABASE_URL").rstrip("/")
SUPABASE_KEY = _require("SUPABASE_SERVICE_KEY")
HOSTNAME_CATEGORY = json.loads(_require("AW_HOSTNAME_CATEGORY"))

AW_HOST = os.environ.get("AW_HOST", "http://localhost:5600").rstrip("/")
IDLE_GAP_S = int(os.environ.get("AW_IDLE_GAP_S", "600"))
MIN_BLOCK_S = int(os.environ.get("AW_MIN_BLOCK_S", "300"))
LOOKBACK_HOURS = int(os.environ.get("AW_LOOKBACK_HOURS", "1"))
MAX_BLOCK_S = 24 * 3600  # sanity cap


# ---- Logging (tiny, stderr-tee'd to optional file) --------------------------
def log(event: str, **fields: object) -> None:
    fields["event"] = event
    fields["ts"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(fields, default=str)
    print(line, file=sys.stderr)
    fp = os.environ.get("AW_LOG_FILE")
    if fp:
        try:
            with open(fp, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass  # don't fail the sync over a log write


# ---- Hostname → category resolution -----------------------------------------
def hostname() -> str:
    raw = os.environ.get("AW_HOSTNAME_OVERRIDE") or socket.gethostname()
    return raw  # spec uses .local-suffixed names so don't strip dots


def category_for(host: str) -> str:
    if host not in HOSTNAME_CATEGORY:
        sys.exit(
            f"FATAL: hostname {host!r} not in AW_HOSTNAME_CATEGORY. "
            f"Known: {sorted(HOSTNAME_CATEGORY)}. Set AW_HOSTNAME_OVERRIDE if needed."
        )
    return HOSTNAME_CATEGORY[host]


def source_tag(category: str) -> str:
    return "aw_personal" if "personal" in category.lower() else "aw_work"


# ---- HTTP helpers (stdlib only) ---------------------------------------------
def _request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    body: object | None = None,
    timeout: float = 30.0,
) -> tuple[int, bytes]:
    data = None
    if body is not None:
        data = json.dumps(body, default=str).encode("utf-8")
    req = urllib.request.Request(url=url, method=method, data=data)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---- Supabase REST ----------------------------------------------------------
def _sb_headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
    }


def _parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def last_block(source: str) -> dict | None:
    """Return the most recent block's full {started_at, ended_at,
    source_event_id} for this hostname's source. Returns None on first run.

    We need the full block (not just ended_at) so that when fresh AW
    activity follows a brief AFK gap, we can fold it into the existing
    block rather than creating a new short row. AWQL's query_bucket
    drops merged events whose start is outside the query window, so
    cross-run merging has to be done DB-side."""
    url = (
        f"{SUPABASE_URL}/rest/v1/events"
        f"?source=eq.{source}"
        f"&select=started_at,ended_at,source_event_id"
        f"&order=ended_at.desc"
        f"&limit=1"
    )
    code, body = _request("GET", url, headers=_sb_headers())
    if code >= 300:
        log("aw_sync.sb_query_failed", code=code, body=body[:500].decode("utf-8", "replace"))
        return None
    rows = json.loads(body or b"[]")
    if not rows:
        return None
    return {
        "started_at": _parse_ts(rows[0]["started_at"]),
        "ended_at": _parse_ts(rows[0]["ended_at"]),
        "source_event_id": rows[0]["source_event_id"],
    }


def upsert_events(rows: list[dict]) -> int:
    """POST to Supabase REST with on_conflict + merge-duplicates so the
    server treats it as an upsert keyed on (source, source_event_id)."""
    if not rows:
        return 0
    url = (
        f"{SUPABASE_URL}/rest/v1/events"
        f"?on_conflict=source,source_event_id"
    )
    headers = {
        **_sb_headers(),
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    code, body = _request("POST", url, headers=headers, body=rows)
    if code >= 300:
        log("aw_sync.sb_upsert_failed",
            code=code, body=body[:500].decode("utf-8", "replace"))
        sys.exit(1)
    return len(rows)


# ---- ActivityWatch ----------------------------------------------------------
AW_QUERY = (
    'afk_bucket = find_bucket("aw-watcher-afk_");'
    'afk_events = query_bucket(afk_bucket);'
    'not_afk = filter_keyvals(afk_events, "status", ["not-afk"]);'
    'RETURN = merge_events_by_keys(not_afk, ["status"]);'
)


def query_aw(start: datetime, end: datetime) -> list[dict]:
    url = f"{AW_HOST}/api/0/query/"
    body = {
        "query": [AW_QUERY],
        "timeperiods": [f"{start.isoformat()}/{end.isoformat()}"],
    }
    code, raw = _request("POST", url, body=body)
    if code >= 300:
        log("aw_sync.aw_query_failed", code=code,
            body=raw[:500].decode("utf-8", "replace"))
        sys.exit(1)
    data = json.loads(raw or b"[]")
    return data[0] if data else []


def parse_aw_event(ev: dict) -> tuple[datetime, datetime]:
    ts = ev["timestamp"]
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    s = datetime.fromisoformat(ts)
    return s, s + timedelta(seconds=ev["duration"])


def build_blocks(aw_events: list[dict]) -> list[tuple[datetime, datetime]]:
    parsed = sorted((parse_aw_event(e) for e in aw_events), key=lambda x: x[0])
    if not parsed:
        return []
    out_pre: list[list[datetime]] = []
    cs, ce = parsed[0]
    for s, e in parsed[1:]:
        gap = (s - ce).total_seconds()
        if 0 <= gap < IDLE_GAP_S:
            ce = max(ce, e)
        else:
            out_pre.append([cs, ce])
            cs, ce = s, e
    out_pre.append([cs, ce])

    out: list[tuple[datetime, datetime]] = []
    for s, e in out_pre:
        dur = (e - s).total_seconds()
        if dur < MIN_BLOCK_S:
            continue
        if dur > MAX_BLOCK_S:
            log("aw_sync.block_clipped", started_at=s.isoformat(), seconds=dur)
            e = s + timedelta(seconds=MAX_BLOCK_S)
        out.append((s, e))
    return out


def make_source_event_id(host: str, started_iso: str) -> str:
    h = hashlib.sha256(f"{host}|{started_iso}".encode()).hexdigest()
    return h[:16]


# ---- Main -------------------------------------------------------------------
def main() -> int:
    host = hostname()
    category = category_for(host)
    source = source_tag(category)

    end = datetime.now(timezone.utc)
    last = last_block(source)
    if last is not None:
        # Slight overlap so a block that was still in progress on the
        # last sync gets its ended_at extended on the next pass.
        start = last["ended_at"] - timedelta(minutes=15)
    else:
        start = end - timedelta(hours=LOOKBACK_HOURS)

    log("aw_sync.window", host=host, source=source, category=category,
        start=start.isoformat(), end=end.isoformat())

    aw_events = query_aw(start, end)
    blocks = build_blocks(aw_events)

    # Cross-run merge: if the earliest new block starts within IDLE_GAP_S
    # of the previous block's end, fold it in. We reuse the previous block's
    # source_event_id so the upsert UPDATEs the existing row (extending
    # ended_at) rather than INSERTing a new short fragment. Without this,
    # any AFK gap longer than 5 min (the launchd interval) creates a new
    # row even when activity was effectively continuous.
    merged_with_last: str | None = None
    if last is not None and blocks:
        first_start, first_end = blocks[0]
        gap = (first_start - last["ended_at"]).total_seconds()
        if 0 <= gap < IDLE_GAP_S:
            merged_start = last["started_at"]
            merged_end = max(last["ended_at"], first_end)
            blocks[0] = (merged_start, merged_end)
            merged_with_last = last["source_event_id"]

    rows = []
    for i, (s, e) in enumerate(blocks):
        # Block 0 may have inherited the previous row's identity via the
        # cross-run merge above; preserve its source_event_id so we UPDATE
        # rather than INSERT. Otherwise hash from started_at as usual.
        sid = (
            merged_with_last if (i == 0 and merged_with_last is not None)
            else make_source_event_id(host, s.isoformat())
        )
        rows.append({
            "source": source,
            "source_event_id": sid,
            "event_type": "work_block",
            "category": category,
            "title": category,
            "started_at": s.isoformat(),
            "ended_at": e.isoformat(),
            "metadata": {
                "hostname": host,
                "duration_seconds": int((e - s).total_seconds()),
            },
        })

    written = upsert_events(rows)
    log("aw_sync.done", host=host, raw_aw=len(aw_events),
        blocks=len(rows), written=written)
    print(json.dumps({
        "host": host, "category": category,
        "raw_aw": len(aw_events), "blocks": len(rows),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
