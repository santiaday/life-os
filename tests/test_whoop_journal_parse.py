"""Pure-function tests for ingest_whoop_journal.transforms + e2e round-trip.

Covers:
  - behavior catalog → dim_whoop_behavior
  - tracked_behaviors[] → fact_habit_log
  - integrations.tracker_inputs[] → fact_food_daily_apple_health (macros pivot)
  - integrations.tracker_inputs[] → fact_habit_log autofill rows
  - day-level envelope → fact_journal_day
  - End-to-end ingest_journal_day with mocked client + DB

DB calls in ingest_journal_day are stubbed via monkeypatching so this stays
a pure-function test (no LIFEOS_TEST_DB_URL dependency).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ingest_whoop_journal import transforms


@pytest.fixture
def fx() -> Path:
    return Path(__file__).parent / "fixtures" / "whoop_journal"


def _load(fx: Path, name: str) -> Any:
    return json.loads((fx / name).read_text())


# ---- behavior catalog → dim_whoop_behavior -------------------------------
def test_transform_behavior_full(fx):
    api = _load(fx, "behavior_catalog.json")[0]
    row = transforms.transform_behavior(api)

    assert row["behavior_id"] == 11
    assert row["internal_name"] == "alcohol"
    assert row["title"] == "Did you have any alcohol?"
    assert row["category"] == "DAYTIME"
    assert row["behavior_type"] == "NEGATIVE"
    assert row["magnitude_unit"] == "drinks"
    assert row["magnitude_min"] == pytest.approx(0.0)
    assert row["magnitude_max"] == pytest.approx(20.0)


def test_transform_behavior_missing_internal_name_returns_none():
    api = {"id": 99, "title": "Anonymous behavior"}
    assert transforms.transform_behavior(api) is None


def test_transform_behavior_falls_back_to_name_field():
    """Whoop sometimes returns `name` instead of `internal_name`."""
    api = {"id": 7, "name": "stretching", "title": "Did you stretch?"}
    row = transforms.transform_behavior(api)
    assert row["internal_name"] == "stretching"


def test_synthesize_dim_from_autofill():
    row = transforms.synthesize_dim_from_autofill("Calcium", 808)
    assert row["behavior_id"] == 808
    assert row["internal_name"] == "calcium"
    assert row["title"] == "Calcium"
    assert row["category"] == "AUTOFILL"


# ---- tracked_behaviors[] → fact_habit_log --------------------------------
def test_parse_tracked_behavior_full_magnitude_and_time(fx):
    payload = _load(fx, "draft.json")
    tracked = payload["journal"]["tracked_behaviors"][0]  # alcohol
    row = transforms.transform_tracked_behavior(tracked, date(2026, 5, 4))

    assert row["habit_key"] == "alcohol"
    assert row["behavior_id"] == 11
    assert row["answered_yes"] is True
    assert row["magnitude_value"] == pytest.approx(2.0)
    assert row["magnitude_unit"] == "drinks"
    assert row["time_input_value"] is not None
    assert row["time_input_value"].isoformat() == "2026-05-04T22:30:00+00:00"
    assert row["whoop_journal_entry_id"] == 1001
    assert row["whoop_cycle_id"] == 555000111
    assert row["user_reviewed"] is True
    assert row["source"] == "whoop_private_api"
    assert row["source_row_hash"]


def test_parse_tracked_behavior_null_magnitude_and_time(fx):
    """Yes/no-only behaviors emit null magnitude_value and time_input_value."""
    payload = _load(fx, "draft.json")
    tracked = payload["journal"]["tracked_behaviors"][2]  # morning_sunlight
    row = transforms.transform_tracked_behavior(tracked, date(2026, 5, 4))

    assert row["habit_key"] == "morning_sunlight"
    assert row["answered_yes"] is False
    assert row["magnitude_value"] is None
    assert row["time_input_value"] is None


def test_parse_tracked_behavior_returns_none_when_internal_name_missing():
    bad = {
        "behavior_id": 42,
        "behavior": {"id": 42, "name": None},
        "answered_yes": True,
    }
    assert transforms.transform_tracked_behavior(bad, date(2026, 5, 4)) is None


def test_parse_tracked_behavior_returns_none_when_behavior_id_missing():
    bad = {"behavior": {"internal_name": "alcohol"}, "answered_yes": True}
    assert transforms.transform_tracked_behavior(bad, date(2026, 5, 4)) is None


# ---- integrations.tracker_inputs[] → fact_food_daily_apple_health --------
def test_parse_autofill_input_full(fx):
    """Verify the macros-pivot path (transform_tracker_inputs)."""
    payload = _load(fx, "draft.json")
    row = transforms.transform_tracker_inputs(payload, date(2026, 5, 4))

    assert row is not None
    assert row["day"] == date(2026, 5, 4)
    assert row["energy_kcal"] == pytest.approx(2400.0)
    assert row["protein_g"] == pytest.approx(165.0)
    assert row["carbs_g"] == pytest.approx(220.0)
    assert row["fat_g"] == pytest.approx(95.0)
    assert row["water_servings"] == pytest.approx(8.0)
    assert row["source"] == "whoop_apple_health"
    assert isinstance(row["payload"], list)


def test_parse_autofill_input_no_integrations_returns_none():
    assert transforms.transform_tracker_inputs({}, date(2026, 5, 4)) is None
    assert transforms.transform_tracker_inputs({"integrations": {}}, date(2026, 5, 4)) is None


def test_parse_autofill_input_unknown_fields_only_returns_none():
    payload = {
        "integrations": {
            "tracker_inputs": [
                {"name": "Vitamin XYZ", "value": 1.0},
                {"name": "MysteryNutrient", "value": 2.0},
            ]
        }
    }
    assert transforms.transform_tracker_inputs(payload, date(2026, 5, 4)) is None


def test_transform_autofill_input_synthesizes_habit_key_from_source_tracking_key():
    """Behavior comes from Apple Health autofill: no internal_name, but
    source_tracking_key is present. We slugify it into habit_key."""
    entry = {
        "behavior_tracker_id": 808,
        "source_tracking_key": "Calcium",
        "value": 1200,
        "unit": "mg",
        "recorded_at": "2026-05-04T13:00:00.000Z",
    }
    row = transforms.transform_autofill_input(entry, date(2026, 5, 4))
    assert row is not None
    assert row["behavior_id"] == 808
    assert row["habit_key"] == "calcium"
    assert row["source"] == "whoop_apple_health"
    assert row["magnitude_value"] == pytest.approx(1200.0)
    assert row["magnitude_unit"] == "mg"
    assert row["answered_yes"] is True


def test_transform_autofill_input_returns_none_without_id_or_name():
    assert transforms.transform_autofill_input({"value": 100}, date(2026, 5, 4)) is None
    assert transforms.transform_autofill_input(
        {"behavior_tracker_id": 808}, date(2026, 5, 4)
    ) is None


# ---- day-level envelope → fact_journal_day -------------------------------
def test_transform_journal_day_full(fx):
    payload = _load(fx, "draft.json")
    row = transforms.transform_journal_day(payload, date(2026, 5, 4))
    assert row is not None
    assert row["day"] == date(2026, 5, 4)
    assert row["journal_entry_id"] == 9000001
    assert row["cycle_id"] == 555000111
    assert row["notes"] == "felt sluggish"
    assert row["user_reviewed"] is True


def test_transform_journal_day_empty_payload_returns_none():
    assert transforms.transform_journal_day({}, date(2026, 5, 4)) is None


# ---- end-to-end round trip with mocked client + DB ----------------------
class _StubAuth:
    def ensure_fresh(self, **_kw): return "stub-token"
    def headers(self): return {"Authorization": "Bearer stub-token"}
    def invalidate(self): pass


class _StubClient:
    def __init__(self, payload): self._payload = payload
    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def journal_draft(self, _day): return self._payload
    def behaviors_catalog(self): return []
    def user_behaviors_for_day(self, _day): return []


def _stub_db(monkeypatch, captured):
    """Wire monkeypatched tx() + upsert_rows so ingest_journal_day captures
    rows it would have written. Returns nothing — captured dict mutates."""
    from ingest_whoop_journal import ingest

    class _Cursor:
        def __init__(self, sink): self._sink = sink
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def execute(self, sql, params=None):
            self._sink["raw_sql"].append((sql, params))

    class _Conn:
        def cursor(self): return _Cursor(captured)
        def __enter__(self): return self
        def __exit__(self, *exc): pass

    class _TxCM:
        def __enter__(self): return _Conn()
        def __exit__(self, *exc): pass

    def fake_tx():
        return _TxCM()

    def fake_upsert(table, rows, **kw):
        captured.setdefault(table, []).extend(rows)
        return len(rows)

    monkeypatch.setattr(ingest, "tx", fake_tx)
    monkeypatch.setattr(ingest, "upsert_rows", fake_upsert)


def test_ingest_journal_day_round_trip(fx, monkeypatch):
    """Feed the fixture payload through ingest_journal_day. Assert each
    table receives the right shape."""
    from ingest_whoop_journal import ingest

    payload = _load(fx, "draft.json")
    captured: dict[str, list] = {"raw_sql": []}
    _stub_db(monkeypatch, captured)

    counts = ingest.ingest_journal_day(date(2026, 5, 4), client=_StubClient(payload))  # type: ignore[arg-type]

    assert counts["raw"] == 1
    assert counts["journal_day"] == 1
    assert counts["habit_log"] == 3  # alcohol, caffeine, morning_sunlight
    assert counts["food_daily_ah"] == 1

    habit_keys = sorted(r["habit_key"] for r in captured["fact_habit_log"])
    assert habit_keys == ["alcohol", "caffeine", "morning_sunlight"]

    alcohol = next(r for r in captured["fact_habit_log"] if r["habit_key"] == "alcohol")
    assert alcohol["behavior_id"] == 11
    assert alcohol["answered_yes"] is True
    assert alcohol["magnitude_value"] == pytest.approx(2.0)
    assert alcohol["source"] == "whoop_private_api"

    journal_day = captured["fact_journal_day"][0]
    assert journal_day["day"] == date(2026, 5, 4)
    assert journal_day["notes"] == "felt sluggish"

    ah = captured["fact_food_daily_apple_health"][0]
    assert ah["energy_kcal"] == pytest.approx(2400.0)


def test_ingest_journal_day_no_entry(monkeypatch):
    """Empty payload (Whoop returned 404 → {}) → no_entry=True, no DB writes."""
    captured: dict[str, list] = {"raw_sql": []}
    _stub_db(monkeypatch, captured)

    from ingest_whoop_journal import ingest
    counts = ingest.ingest_journal_day(date(2026, 5, 4), client=_StubClient({}))  # type: ignore[arg-type]

    assert counts == {
        "raw": 0, "journal_day": 0, "habit_log": 0,
        "habit_log_autofill": 0, "food_daily_ah": 0, "no_entry": True,
    }
    assert "fact_habit_log" not in captured
    assert "fact_journal_day" not in captured
