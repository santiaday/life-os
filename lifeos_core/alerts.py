"""Stale-ingest detection + Pushover/email/Slack alerting.

Runs on the scheduler at a low cadence (e.g. hourly). Checks each source's
last_success time in ingestion_runs. If anything's older than the configured
threshold, fires an alert via the configured channel.

Configuration (all optional):
  PUSHOVER_USER_KEY / PUSHOVER_API_TOKEN  → Pushover delivery
  SLACK_ALERT_WEBHOOK_URL                  → Slack incoming webhook
  ALERT_EMAIL_TO + SMTP_*                  → SMTP email (TODO: not implemented)

If no channel is configured, alerts log at WARNING and are visible in
`docker compose logs scheduler` but go nowhere external.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx

from lifeos_core.db import tx
from lifeos_core.logging import get_logger

log = get_logger(__name__)

# (source, label, max_staleness_hours) — adjust per source cadence.
SOURCE_THRESHOLDS = (
    ("whoop", "Whoop", 6),         # hourly job, alert after 6h dark
    ("calendar", "Calendar", 4),    # 30-min job, alert after 4h dark
    ("cronometer", "Cronometer", 36),  # daily job, allow margin
    ("copilot", "Copilot", 12),     # 4-hourly job
    ("mart", "Mart refresh", 36),
)


def _stale_sources(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    out: list[dict] = []
    with tx() as c, c.cursor() as cur:
        for source, label, max_hours in SOURCE_THRESHOLDS:
            cur.execute(
                """
                SELECT MAX(started_at) AS last_success
                FROM ingestion_runs
                WHERE source = %s AND status = 'success'
                """,
                [source],
            )
            row = cur.fetchone()
            last = row["last_success"] if row else None
            if last is None:
                out.append({
                    "source": source, "label": label, "last_success": None,
                    "hours_stale": None, "threshold_hours": max_hours,
                })
                continue
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            hours = (now - last).total_seconds() / 3600.0
            if hours > max_hours:
                out.append({
                    "source": source, "label": label,
                    "last_success": last.isoformat(),
                    "hours_stale": round(hours, 1),
                    "threshold_hours": max_hours,
                })
    return out


def _send_pushover(title: str, message: str) -> bool:
    user = os.environ.get("PUSHOVER_USER_KEY")
    token = os.environ.get("PUSHOVER_API_TOKEN")
    if not user or not token:
        return False
    try:
        resp = httpx.post(
            "https://api.pushover.net/1/messages.json",
            data={"user": user, "token": token, "title": title, "message": message},
            timeout=10.0,
        )
        resp.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("alerts.pushover_failed", error=str(e))
        return False


def _send_slack(title: str, message: str) -> bool:
    url = os.environ.get("SLACK_ALERT_WEBHOOK_URL")
    if not url:
        return False
    try:
        resp = httpx.post(
            url, json={"text": f"*{title}*\n{message}"}, timeout=10.0
        )
        resp.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("alerts.slack_failed", error=str(e))
        return False


def check_and_alert() -> dict:
    """Survey ingest freshness. Send an alert if anything is stale.
    Returns the survey result for logging/debugging."""
    stale = _stale_sources()
    if not stale:
        return {"ok": True, "stale": []}

    title = f"life-os: {len(stale)} stale source(s)"
    body_lines = [
        f"- {s['label']}: "
        + (f"never succeeded" if s['last_success'] is None
           else f"last success {s['last_success']} ({s['hours_stale']}h ago, threshold {s['threshold_hours']}h)")
        for s in stale
    ]
    message = "\n".join(body_lines)

    delivered = False
    for sender in (_send_pushover, _send_slack):
        if sender(title, message):
            delivered = True

    if not delivered:
        log.warning("alerts.stale_no_channel", title=title, body=message)

    return {"ok": False, "stale": stale, "delivered": delivered}
