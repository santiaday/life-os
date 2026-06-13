"""Pure transforms: Google Calendar event JSON → fact_calendar_event row.

The interesting part is `classify()` — the spec's 5-priority decision tree.
Kept as a dedicated function so the rules are testable and easy to tweak.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

from lifeos_core.tz import local_midnight_utc

# Regex for "focus", "deep work", "block", "do not schedule", "DNS"
FOCUS_TITLE_RE = re.compile(r"\b(focus|deep work|block|do not schedule|dns)\b", re.IGNORECASE)


def transform_event(api: dict, *, internal_domains: Iterable[str]) -> dict | None:
    """Return a fact_calendar_event row dict, or None if the event has no
    usable times (e.g. cancelled occurrences without start/end)."""
    start = _parse_event_time(api.get("start"))
    end = _parse_event_time(api.get("end"))
    if start is None or end is None:
        return None

    is_all_day = "date" in (api.get("start") or {})

    attendees = api.get("attendees") or []
    organizer = api.get("organizer") or {}
    organizer_email = organizer.get("email")
    organizer_self = bool(organizer.get("self"))

    domains = {d.lower() for d in internal_domains}
    self_attendee = next((a for a in attendees if a.get("self")), None)
    response_status = self_attendee.get("responseStatus") if self_attendee else None

    # Attendee counts exclude self.
    others = [a for a in attendees if not a.get("self")]
    internal = sum(1 for a in others if _email_domain(a.get("email")) in domains)
    external = len(others) - internal

    has_video = bool(api.get("hangoutLink")) or any(
        ep.get("entryPointType") == "video"
        for ep in (api.get("conferenceData") or {}).get("entryPoints", [])
    )

    title = api.get("summary")

    classification = classify(
        is_all_day=is_all_day,
        response_status=response_status,
        attendee_count_including_self=len(attendees),
        title=title,
    )

    return {
        "calendar_id": api["_calendar_id"],  # injected by caller
        "event_id": api["id"],
        "start_ts": start,
        "end_ts": end,
        "title": title,
        "status": api.get("status"),
        "organizer_email": organizer_email,
        "organizer_self": organizer_self,
        "attendee_count": len(attendees),
        "attendee_internal_count": internal,
        "attendee_external_count": external,
        "is_recurring": bool(api.get("recurringEventId")),
        "recurring_event_id": api.get("recurringEventId"),
        "is_all_day": is_all_day,
        "has_video_link": has_video,
        "location": api.get("location"),
        "visibility": api.get("visibility"),
        "response_status": response_status,
        "classification": classification,
    }


def classify(
    *,
    is_all_day: bool,
    response_status: str | None,
    attendee_count_including_self: int,
    title: str | None,
) -> str:
    """SPEC.md §4.2 priority order:
      1. all-day → 'all_day'
      2. declined → 'declined'
      3. solo (≤1 attendee) AND title matches focus regex → 'focus'
      4. ≥2 attendees → 'meeting'
      5. else → 'personal'
    """
    if is_all_day:
        return "all_day"
    if response_status == "declined":
        return "declined"
    if attendee_count_including_self <= 1 and title and FOCUS_TITLE_RE.search(title):
        return "focus"
    if attendee_count_including_self >= 2:
        return "meeting"
    return "personal"


# ---- time parsing ----------------------------------------------------------
def _parse_event_time(t: dict | None) -> datetime | None:
    """Google returns either {dateTime, timeZone} (timed event) or {date}
    (all-day). We collapse both to UTC TIMESTAMPTZ values:
      - dateTime: parse RFC 3339 directly.
      - date: treat as local-tz midnight (per SPEC.md §4.2)."""
    if not t:
        return None
    if "dateTime" in t:
        # ISO-8601 with offset, e.g. "2025-04-01T09:30:00-04:00"
        return datetime.fromisoformat(t["dateTime"].replace("Z", "+00:00"))
    if "date" in t:
        d = datetime.fromisoformat(t["date"]).date()
        return local_midnight_utc(d)
    return None


def _email_domain(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    return email.rsplit("@", 1)[-1].lower()
