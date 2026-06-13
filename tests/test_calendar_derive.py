"""Tests for ingest_calendar.transforms.

The classification decision tree (SPEC.md §4.2) is the most failure-prone
part of this ingester — these tests pin down each branch with synthetic
events and verify attendee internal/external counting.
"""

from __future__ import annotations

from ingest_calendar import transforms
from ingest_calendar.transforms import classify


# ---- classification decision tree ------------------------------------------
def test_classify_all_day_wins():
    assert classify(is_all_day=True, response_status="declined", attendee_count_including_self=5,
                    title="Big meeting") == "all_day"


def test_classify_declined_beats_meeting():
    assert classify(is_all_day=False, response_status="declined", attendee_count_including_self=4,
                    title="Standup") == "declined"


def test_classify_solo_with_focus_title():
    assert classify(is_all_day=False, response_status="accepted", attendee_count_including_self=1,
                    title="Deep Work") == "focus"
    assert classify(is_all_day=False, response_status=None, attendee_count_including_self=0,
                    title="DNS — writing") == "focus"
    # Case-insensitive
    assert classify(is_all_day=False, response_status=None, attendee_count_including_self=1,
                    title="focus block") == "focus"


def test_classify_meeting_when_multiple_attendees():
    assert classify(is_all_day=False, response_status="accepted", attendee_count_including_self=3,
                    title="Sprint planning") == "meeting"


def test_classify_personal_solo_no_focus_title():
    assert classify(is_all_day=False, response_status=None, attendee_count_including_self=1,
                    title="Lunch") == "personal"


def test_classify_solo_focus_title_but_with_attendees_is_meeting():
    """Don't let the focus regex hijack a real meeting."""
    assert classify(is_all_day=False, response_status="accepted", attendee_count_including_self=3,
                    title="Deep Work session") == "meeting"


def test_classify_no_title_solo_is_personal():
    assert classify(is_all_day=False, response_status=None, attendee_count_including_self=1,
                    title=None) == "personal"


# ---- attendee internal/external counting -----------------------------------
def _event(**kw) -> dict:
    """Build a minimal Google Calendar event payload + the synthetic
    `_calendar_id` field the ingester adds."""
    base = {
        "id": "evt_1",
        "_calendar_id": "primary",
        "summary": "Standup",
        "status": "confirmed",
        "start": {"dateTime": "2025-04-01T09:00:00-04:00"},
        "end": {"dateTime": "2025-04-01T09:30:00-04:00"},
    }
    base.update(kw)
    return base


def test_internal_external_attendee_counts():
    e = _event(attendees=[
        {"email": "santi@doorloop.com", "self": True, "responseStatus": "accepted"},
        {"email": "alice@doorloop.com"},
        {"email": "bob@doorloop.com"},
        {"email": "external@partnerco.com"},
    ])
    row = transforms.transform_event(e, internal_domains=["doorloop.com"])
    # Self excluded from internal/external split, but counted in attendee_count
    assert row["attendee_count"] == 4
    assert row["attendee_internal_count"] == 2
    assert row["attendee_external_count"] == 1
    assert row["classification"] == "meeting"


def test_response_status_extracted_from_self_attendee():
    e = _event(attendees=[
        {"email": "santi@doorloop.com", "self": True, "responseStatus": "declined"},
        {"email": "alice@doorloop.com"},
    ])
    row = transforms.transform_event(e, internal_domains=["doorloop.com"])
    assert row["response_status"] == "declined"
    assert row["classification"] == "declined"  # declined takes priority over meeting


# ---- video link detection --------------------------------------------------
def test_video_link_via_hangoutLink():
    e = _event(hangoutLink="https://meet.google.com/abc-defg-hij")
    row = transforms.transform_event(e, internal_domains=[])
    assert row["has_video_link"] is True


def test_video_link_via_conferenceData_entrypoint():
    e = _event(conferenceData={"entryPoints": [{"entryPointType": "video"}]})
    row = transforms.transform_event(e, internal_domains=[])
    assert row["has_video_link"] is True


def test_no_video_link():
    e = _event()
    row = transforms.transform_event(e, internal_domains=[])
    assert row["has_video_link"] is False


# ---- all-day events --------------------------------------------------------
def test_all_day_event_uses_local_midnight():
    e = _event(start={"date": "2025-04-01"}, end={"date": "2025-04-02"})
    row = transforms.transform_event(e, internal_domains=[])
    assert row["is_all_day"] is True
    assert row["classification"] == "all_day"
    # 2025-04-01 00:00 EDT == 2025-04-01 04:00 UTC
    assert row["start_ts"].isoformat() == "2025-04-01T04:00:00+00:00"


# ---- recurring events ------------------------------------------------------
def test_recurring_event_marked():
    e = _event(recurringEventId="recur_abc123")
    row = transforms.transform_event(e, internal_domains=[])
    assert row["is_recurring"] is True
    assert row["recurring_event_id"] == "recur_abc123"


# ---- skipped events --------------------------------------------------------
def test_event_without_start_returns_none():
    """Cancelled occurrences sometimes have no start/end — skip them."""
    e = {"id": "x", "_calendar_id": "primary", "status": "cancelled"}
    assert transforms.transform_event(e, internal_domains=[]) is None
