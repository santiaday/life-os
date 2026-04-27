"""Tests for mart_refresh.

Two flavors:
  - Static checks: validate the SQL constants resolve cleanly under psycopg's
    `%`-substitution and reference every expected source table.
  - Integration: insert synthetic fact rows, run refresh, check mart_daily.
    Skipped unless LIFEOS_TEST_DB_URL is set (see tests/conftest.py).
"""

from __future__ import annotations

import re

import pytest

from mart_refresh.sql import (
    MART_DAILY_REBUILD,
    MART_MEAL_REBUILD,
    MART_WEEKLY_REBUILD,
)


def test_mart_daily_truncates_and_inserts():
    assert "TRUNCATE mart_daily" in MART_DAILY_REBUILD
    assert "INSERT INTO mart_daily" in MART_DAILY_REBUILD


def test_mart_daily_references_every_fact_source():
    """Catch refactors that drop a source by accident."""
    expected_sources = [
        "fact_recovery",
        "fact_sleep",
        "fact_cycle",
        "fact_workout",
        "fact_calendar_event",
        "fact_food_daily",
        "fact_food_log",
        "fact_transaction",
        "fact_biometric",
    ]
    for src in expected_sources:
        assert src in MART_DAILY_REBUILD, f"missing source: {src}"


def test_mart_daily_uses_tz_placeholder():
    """All AT TIME ZONE conversions go through the %(tz)s param so tz is
    configurable without SQL edits."""
    assert "%(tz)s" in MART_DAILY_REBUILD
    # No stray hard-coded TZ literals in projection clauses.
    assert "AT TIME ZONE 'America" not in MART_DAILY_REBUILD


def test_mart_daily_percent_escapes_are_correct():
    """The double-percent `%%` must outnumber single-percent uses inside
    string literals, since psycopg expects `%` to be `%%` everywhere it isn't
    a parameter. Catches ILIKE patterns written as '%%foo%%' vs '%foo%'."""
    # Every single percent should be either part of `%(name)s` or `%%`.
    # Strip those out, then assert no bare `%` remains.
    stripped = re.sub(r"%\([a-z_]+\)s", "", MART_DAILY_REBUILD)
    stripped = stripped.replace("%%", "")
    assert "%" not in stripped, (
        "MART_DAILY_REBUILD contains a bare `%` that's neither a parameter "
        "nor an escape — psycopg will choke on it."
    )


def test_mart_meal_normalizes_meal_groups():
    assert "TRUNCATE mart_meal" in MART_MEAL_REBUILD
    assert "ARRAY_AGG(food_name" in MART_MEAL_REBUILD
    # Snack collapsing
    assert "Snack%%" in MART_MEAL_REBUILD


def test_mart_weekly_groups_by_week():
    assert "TRUNCATE mart_weekly" in MART_WEEKLY_REBUILD
    assert "date_trunc('week'" in MART_WEEKLY_REBUILD
    # Should aggregate from mart_daily, not re-derive from fact (per SPEC).
    assert "FROM mart_daily" in MART_WEEKLY_REBUILD


# ---- DB integration --------------------------------------------------------
@pytest.mark.skip(reason="DB-backed test; requires LIFEOS_TEST_DB_URL and a clean test DB")
def test_mart_daily_rebuild_against_synthetic_facts(db_url):
    """End-to-end: seed fact rows, run refresh, assert mart_daily values.
    Stub for the eventual integration suite — left skipped so CI doesn't fail
    in environments without a test DB. Implementation lands when we wire
    Phase 8 hardening + CI."""
    pass
