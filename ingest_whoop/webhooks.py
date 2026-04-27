"""Whoop webhook receiver.

Mounts on the MCP server's FastAPI app at /webhooks/whoop. On a verified
event, queues a small fetch of just that record so the data is fresh within
seconds instead of waiting for the next hourly cron.

Reference: https://developer.whoop.com/docs/developing/webhooks
Whoop signs each delivery with HMAC-SHA256 over `<timestamp>.<raw_body>`,
returned in `X-Whoop-Signature` (base64). Reject deliveries older than 5 min.

Set WHOOP_WEBHOOK_SECRET to enable. This module is a no-op without it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_AGE_SEC = 300  # reject anything older than 5 min — replay protection


def _verify_signature(raw_body: bytes, timestamp: str, signature: str) -> bool:
    """Constant-time signature verification."""
    if not settings.WHOOP_WEBHOOK_SECRET:
        return False
    try:
        ts_int = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts_int) > MAX_AGE_SEC:
        return False

    msg = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(
        settings.WHOOP_WEBHOOK_SECRET.encode(), msg, hashlib.sha256
    ).digest()
    expected_b64 = base64.b64encode(expected).decode()
    return hmac.compare_digest(expected_b64, signature)


def _dispatch(event: dict[str, Any]) -> None:
    """Pull just the affected record from Whoop. Falls back to a small
    incremental ingest on unknown event types."""
    from ingest_whoop import ingest as whoop_ingest

    event_type = event.get("type") or ""
    log.info("whoop.webhook.dispatch", event_type=event_type, id=event.get("id"))

    try:
        # Webhook payloads we know about (per Whoop docs):
        #   recovery.updated, sleep.updated, workout.updated, cycle.updated,
        #   profile.updated, body_measurement.updated.
        # Cheapest path: re-run the corresponding pipeline with backfill_days=1.
        # That re-fetches the affected window and upserts; idempotent + safe.
        pipeline_map = {
            "recovery": whoop_ingest.ingest_recoveries,
            "sleep": whoop_ingest.ingest_sleep,
            "workout": whoop_ingest.ingest_workouts,
            "cycle": whoop_ingest.ingest_cycles,
        }
        prefix = event_type.split(".", 1)[0]
        fn = pipeline_map.get(prefix)
        from ingest_whoop.client import WhoopClient

        with WhoopClient() as client:
            if fn is not None:
                fn(client, backfill_days=1)
            else:
                log.info("whoop.webhook.unknown_type_fallback", event_type=event_type)
                # Safe fallback: thin re-fetch across the board.
                whoop_ingest.run_all(backfill_days=1)
    except Exception:
        log.exception("whoop.webhook.dispatch_failed", event_type=event_type)


@router.post("/whoop")
async def whoop_event(request: Request, bg: BackgroundTasks) -> dict:
    if not settings.WHOOP_WEBHOOK_SECRET:
        # Don't 401 — pretend the endpoint isn't there if not configured.
        raise HTTPException(status_code=404, detail="not configured")

    raw = await request.body()
    sig = request.headers.get("x-whoop-signature", "")
    ts = request.headers.get("x-whoop-signature-timestamp", "")
    if not _verify_signature(raw, ts, sig):
        log.warning("whoop.webhook.bad_signature", sig_present=bool(sig), ts=ts)
        raise HTTPException(status_code=401, detail="bad signature")

    try:
        event = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json") from None

    bg.add_task(_dispatch, event)
    return {"ok": True}
