"""Pure-function tests for ingest_pushpress.transforms.

Pinned to the real-payload fingerprint we captured on 2026-05-07: PushPress's
non-ISO timestamp format ('2026-05-07 00:00:00.0'), HYROX's null-uid
placeholder behavior, and the parts-derived divisions roll-up.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from ingest_pushpress import transforms


# ---- timestamp parsing -----------------------------------------------------
def test_parse_pushpress_ts_native_format():
    # Their wire format is "YYYY-MM-DD HH:MM:SS.f" (no timezone).
    out = transforms.parse_pushpress_ts("2026-05-07 00:00:00.0")
    assert out == datetime(2026, 5, 7, 0, 0, 0, tzinfo=UTC)


def test_parse_pushpress_ts_iso_z():
    out = transforms.parse_pushpress_ts("2026-05-03T16:00:00Z")
    assert out == datetime(2026, 5, 3, 16, 0, 0, tzinfo=UTC)


def test_parse_pushpress_ts_none():
    assert transforms.parse_pushpress_ts(None) is None
    assert transforms.parse_pushpress_ts("") is None


def test_parse_pushpress_ts_garbage_returns_none():
    assert transforms.parse_pushpress_ts("not a date") is None


def test_parse_pushpress_date_pulls_calendar_day():
    assert transforms.parse_pushpress_date("2026-05-07 00:00:00.0") == date(2026, 5, 7)


# ---- payload hash ----------------------------------------------------------
def test_payload_hash_stable_across_key_order():
    a = {"foo": 1, "bar": [1, 2, 3]}
    b = {"bar": [1, 2, 3], "foo": 1}
    assert transforms.payload_hash(a) == transforms.payload_hash(b)


def test_payload_hash_changes_on_value_change():
    a = {"foo": 1}
    b = {"foo": 2}
    assert transforms.payload_hash(a) != transforms.payload_hash(b)


# ---- session row -----------------------------------------------------------
def _crossfit_payload() -> dict:
    """Trimmed real CrossFit payload from 2026-05-07."""
    return {
        "uid": "1579f1b0-f6f9-4973-9ec0-d32feb58adaa",
        "id": 8307,
        "origin": "train",
        "classTypeId": 8307,
        "workoutUid": "1579f1b0-f6f9-4973-9ec0-d32feb58adaa",
        "workoutState": "PUBLISHED",
        "title": "CrossFit & HIIT",
        "publishingDate": "2026-05-07 00:00:00.0",
        "publishedOn": "2026-05-03 16:00:00.0",
        "createdDate": "2026-05-07 00:00:00.0",
        "updatedDate": None,
        "parts": [
            {
                "workoutPartUid": "ea80525d-38f5-4153-be45-f66b32bdb228",
                "title": "POSTERIOR",
                "workoutTitle": "Deadlifts ",
                "description": "Deadlifts \n\nBuild To Heavy Single in 15:00",
                "scoreType": "Weight",
                "scoreCount": 1,
                "defaultReps": 1,
                "divisions": ["Performance"],
                "sets": 5,
                "rawUnit": "IMPERIAL",
            },
            {
                "workoutPartUid": "77e1a3a3-275c-40bd-b1b6-040ca61f15bb",
                "title": "WORKOUT OF THE DAY",
                "workoutTitle": '"Get on your hands\'"',
                "description": "AMRAP 16: 50 Box Step-ups",
                "scoreType": "Rounds/Reps",
                "scoreCount": 1,
                "defaultReps": None,
                "divisions": ["Performance", "Fitness"],
                "sets": 1,
                "rawUnit": None,
            },
        ],
    }


def test_session_row_collects_divisions_from_parts():
    p = _crossfit_payload()
    row = transforms.session_row(
        p,
        class_type_uuid="51237627-edab-47b2-83fe-04a56ff781c3",
        class_type_name="CrossFit & HIIT",
        class_date=date(2026, 5, 7),
    )
    assert row["workout_uid"] == p["workoutUid"]
    assert row["parts_count"] == 2
    assert row["divisions"] == ["Fitness", "Performance"]
    assert row["workout_state"] == "PUBLISHED"
    assert row["published_on"] == datetime(2026, 5, 3, 16, 0, 0, tzinfo=UTC)


def test_session_row_synthesizes_uid_for_hyrox_placeholder():
    """HYROX returns workoutUid=null on dates where programming is reserved
    but not yet published. We mint a stable synthetic uid so the row still
    lands and re-runs are idempotent."""
    p = {
        "uid": None,
        "workoutUid": None,
        "id": 63470,
        "title": "HYROX",
        "workoutState": "PUBLISHED",
        "parts": [
            {
                "workoutPartUid": None,
                "title": "Workout not yet available",
                "workoutTitle": None,
                "description": "Workout will be published on May 10 12:00 EDT",
                "scoreType": "No Score",
                "scoreCount": 0,
                "defaultReps": None,
                "divisions": [],
                "sets": 1,
                "rawUnit": None,
            }
        ],
    }
    row = transforms.session_row(
        p,
        class_type_uuid="fa6ae83a-46d1-4649-be5b-67482bb03772",
        class_type_name="HYROX",
        class_date=date(2026, 5, 12),
    )
    assert row["workout_uid"] == (
        "synthetic:fa6ae83a-46d1-4649-be5b-67482bb03772:2026-05-12"
    )
    # Two runs against the same payload must mint the same uid (no clock /
    # randomness in the synthesis path).
    again = transforms.session_row(
        p,
        class_type_uuid="fa6ae83a-46d1-4649-be5b-67482bb03772",
        class_type_name="HYROX",
        class_date=date(2026, 5, 12),
    )
    assert row["workout_uid"] == again["workout_uid"]


# ---- part rows -------------------------------------------------------------
def test_part_rows_preserve_ordinal_and_full_payload():
    p = _crossfit_payload()
    rows = transforms.part_rows(
        p,
        class_type_uuid="51237627-edab-47b2-83fe-04a56ff781c3",
        class_date=date(2026, 5, 7),
    )
    assert [r["ordinal"] for r in rows] == [0, 1]
    assert rows[0]["title"] == "POSTERIOR"
    assert rows[0]["workout_title"] == "Deadlifts "
    assert rows[0]["score_type"] == "Weight"
    assert rows[0]["set_count"] == 5
    assert rows[0]["unit"] == "IMPERIAL"
    assert rows[1]["divisions"] == ["Performance", "Fitness"]


def test_part_rows_synthesize_uid_when_api_returns_null():
    p = {
        "uid": None, "workoutUid": None,
        "parts": [
            {"workoutPartUid": None, "title": "x", "description": "y",
             "scoreType": None, "scoreCount": None, "defaultReps": None,
             "divisions": [], "sets": 1, "rawUnit": None,
             "workoutTitle": None, "athletesNotes": None, "coachesNotes": None},
        ],
    }
    rows = transforms.part_rows(
        p, class_type_uuid="ct-uid", class_date=date(2026, 5, 12),
    )
    assert rows[0]["part_uid"] == "synthetic:synthetic:ct-uid:2026-05-12:part-0"
