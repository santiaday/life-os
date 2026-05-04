"""Push events table rows out to Google Calendar.

Reads `events` rows where `synced_at IS NULL` (never synced) or where
`updated_at > synced_at` (source re-emitted with new ended_at, e.g. an
ActivityWatch block extending past a sync boundary). Inserts new entries
or PATCHes existing ones, then writes calendar_id / calendar_event_id /
synced_at back so we don't re-process them.

Idempotency:
- Each events row carries a stable extended-property `lifelog_events_id`.
  If we ever lose calendar_event_id (DB restore, manual deletion), a
  follow-up sync would naively duplicate. We don't search-by-property
  yet — single-user low-volume, an occasional manual cleanup is fine.

Auth:
- Reuses the shared 'google' oauth_tokens row (same refresh token as
  ingest_calendar). Scopes were broadened in 0012-era oauth.SCOPES;
  re-run `python -m ingest_calendar oauth-init` once to upgrade the
  token. The 'calendar.events' scope is what authorizes writes.
"""

from __future__ import annotations

import json
from datetime import datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ingest_calendar import oauth as google_oauth
from lifeos_core import events as events_store
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run
from lifeos_core.settings import settings

log = get_logger(__name__)

LOCAL_TZ_NAME = settings.LOCAL_TZ


def _service():
    """Build the Google Calendar API client. Lazy so import-time errors
    (missing creds) don't break unit tests that don't actually sync."""
    creds = google_oauth.credentials()
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _to_google_body(event: dict, *, event_db_id: int) -> dict:
    """Render an events-table row as a Google Calendar API body."""
    started: datetime = event["started_at"]
    ended: datetime = event["ended_at"]
    metadata = event.get("metadata") or {}
    body = {
        "summary": event["title"],
        "start": {"dateTime": started.isoformat(), "timeZone": LOCAL_TZ_NAME},
        "end": {"dateTime": ended.isoformat(), "timeZone": LOCAL_TZ_NAME},
        "description": json.dumps(metadata, indent=2, default=str),
        "extendedProperties": {
            "private": {
                "lifelog_source": event["source"],
                "lifelog_events_id": str(event_db_id),
                "lifelog_event_type": event["event_type"],
            }
        },
    }
    if settings.LIFELOG_EVENTS_TRANSPARENT:
        body["transparency"] = "transparent"
    return body


def _insert(service, calendar_id: str, body: dict) -> str:
    resp = service.events().insert(calendarId=calendar_id, body=body).execute()
    return resp["id"]


def _patch(service, calendar_id: str, calendar_event_id: str, body: dict) -> str:
    """Patch an existing event. Falls back to insert if Google returns 404
    (manual deletion in the UI, calendar swap, etc.) so we self-heal."""
    try:
        resp = service.events().patch(
            calendarId=calendar_id,
            eventId=calendar_event_id,
            body=body,
        ).execute()
        return resp["id"]
    except HttpError as e:
        if e.resp.status in (404, 410):
            log.warning(
                "calsync.patch.gone_reinserting",
                calendar_id=calendar_id,
                calendar_event_id=calendar_event_id,
            )
            return _insert(service, calendar_id, body)
        raise


def sync_once(*, limit: int | None = None) -> dict:
    """Process one batch. Returns a summary dict suitable for logging.

    Splits the work by category so a misconfigured calendar id (one
    category) doesn't block sync of the rest."""
    cal_map = settings.lifelog_calendar_map
    if not cal_map:
        raise RuntimeError(
            "LIFELOG_CALENDAR_MAP_JSON is empty; refusing to sync. "
            "Configure category→calendar_id JSON in .env first."
        )

    batch_size = limit or settings.LIFELOG_SYNC_BATCH_SIZE
    pending = events_store.fetch_unsynced(limit=batch_size)
    if not pending:
        return {"pending": 0, "synced": 0, "skipped": 0, "errors": 0}

    service = _service()
    synced = 0
    skipped = 0
    errors: list[dict] = []

    for ev in pending:
        category = ev["category"]
        cal_id = cal_map.get(category)
        if not cal_id:
            log.warning(
                "calsync.unknown_category",
                category=category,
                events_id=ev["id"],
                hint="Add this category to LIFELOG_CALENDAR_MAP_JSON",
            )
            skipped += 1
            continue

        body = _to_google_body(ev, event_db_id=ev["id"])

        try:
            existing_cal_id = ev.get("calendar_id")
            existing_event_id = ev.get("calendar_event_id")

            if existing_event_id and existing_cal_id == cal_id:
                gcal_event_id = _patch(service, cal_id, existing_event_id, body)
            elif existing_event_id and existing_cal_id != cal_id:
                # Category remapped to a different calendar — delete from old,
                # insert in new. Best-effort delete; if it fails we orphan one
                # event but don't block sync.
                try:
                    service.events().delete(
                        calendarId=existing_cal_id, eventId=existing_event_id
                    ).execute()
                except HttpError as e:
                    log.warning(
                        "calsync.delete_old_failed",
                        calendar_id=existing_cal_id,
                        calendar_event_id=existing_event_id,
                        status=e.resp.status,
                    )
                gcal_event_id = _insert(service, cal_id, body)
            else:
                gcal_event_id = _insert(service, cal_id, body)

            events_store.mark_synced(
                ev["id"],
                calendar_id=cal_id,
                calendar_event_id=gcal_event_id,
            )
            synced += 1
        except HttpError as e:
            log.error(
                "calsync.http_error",
                events_id=ev["id"],
                category=category,
                status=e.resp.status,
                content=e.content[:500] if e.content else None,
            )
            errors.append({"id": ev["id"], "status": e.resp.status})
        except Exception as e:
            log.exception("calsync.unexpected_error", events_id=ev["id"])
            errors.append({"id": ev["id"], "error": str(e)})

    return {
        "pending": len(pending),
        "synced": synced,
        "skipped": skipped,
        "errors": len(errors),
        "error_detail": errors[:5],
    }


def run() -> dict:
    """One scheduled tick: open an ingestion_run, sync_once, log."""
    with ingestion_run("calendar_sync", "events") as run_ctx:
        result = sync_once()
        run_ctx.fetched(result["pending"])
        run_ctx.upserted(result["synced"])
        run_ctx.add_metadata(**{k: v for k, v in result.items() if k != "error_detail"})
        return result
