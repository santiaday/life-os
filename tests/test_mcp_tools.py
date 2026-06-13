"""Tests for the MCP server.

Pure-function coverage:
  - sql_safety: validate() rejects forbidden keywords + multi-statement; doesn't
    get fooled by literals/comments; ensure_limit() appends a LIMIT only when
    appropriate.
  - schema_docs: docs_for() returns the right shape, errors helpfully on
    unknown tables.
  - tools.call(): unknown-tool path produces error envelope; date string
    coercion works.

Tools that hit the DB are exercised via the integration test stub (skipped
without LIFEOS_TEST_DB_URL).
"""

from __future__ import annotations

import pytest

from mcp_server import tools
from mcp_server.schema_docs import docs_for
from mcp_server.sql_safety import UnsafeQueryError, ensure_limit, validate


# ---- sql_safety -----------------------------------------------------------
def test_validate_accepts_select():
    validate("SELECT * FROM mart_daily WHERE day > CURRENT_DATE - 7")


def test_validate_accepts_with_cte():
    validate("WITH x AS (SELECT 1) SELECT * FROM x")


def test_validate_rejects_insert():
    with pytest.raises(UnsafeQueryError, match="INSERT"):
        validate("INSERT INTO mart_daily VALUES (...)")


@pytest.mark.parametrize("kw", ["UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER",
                                "CREATE", "GRANT", "REVOKE", "COPY"])
def test_validate_rejects_each_forbidden_keyword(kw):
    with pytest.raises(UnsafeQueryError):
        validate(f"{kw} mart_daily")


def test_validate_rejects_multiple_statements():
    with pytest.raises(UnsafeQueryError, match="Multiple"):
        validate("SELECT 1; SELECT 2")


def test_validate_allows_trailing_semicolon():
    validate("SELECT 1;")


def test_validate_ignores_keyword_inside_string_literal():
    """A user could legitimately have 'DELETE' as a search term."""
    validate("SELECT * FROM fact_calendar_event WHERE title ILIKE '%DELETE%'")


def test_validate_ignores_keyword_inside_comment():
    validate("SELECT 1 -- DROP TABLE\nFROM mart_daily")
    validate("SELECT 1 /* INSERT INTO foo */ FROM mart_daily")


def test_ensure_limit_appends_to_select_without_limit():
    out = ensure_limit("SELECT * FROM mart_daily", 50)
    assert "LIMIT 50" in out


def test_ensure_limit_does_not_append_when_present():
    out = ensure_limit("SELECT * FROM mart_daily LIMIT 10", 50)
    assert out.count("LIMIT") == 1


def test_ensure_limit_handles_trailing_semicolon():
    out = ensure_limit("SELECT 1;", 5)
    assert "LIMIT 5" in out


# ---- schema_docs ----------------------------------------------------------
def test_docs_for_returns_full_blob_when_table_omitted():
    blob = docs_for(None)
    assert "tables" in blob
    assert "mart_daily" in blob["tables"]
    assert "conventions" in blob


def test_docs_for_specific_table():
    blob = docs_for("mart_daily")
    assert blob["table"] == "mart_daily"
    assert "purpose" in blob
    assert "columns" in blob


def test_docs_for_unknown_table_returns_helpful_error():
    blob = docs_for("does_not_exist")
    assert "error" in blob
    assert "available" in blob and "mart_daily" in blob["available"]


# ---- tools.call ------------------------------------------------------------
def test_call_unknown_tool():
    out = tools.call("nope", {})
    assert out["ok"] is False
    assert out["error_type"] == "ValueError"


def test_get_schema_docs_envelope():
    out = tools.get_schema_docs()
    assert out["ok"] is True
    assert out["tool"] == "get_schema_docs"
    assert out["row_count"] == 1


def test_correlate_metrics_rejects_off_allowlist_metric():
    out = tools.correlate_metrics(
        metric_a="recovery_score",
        metric_b="dropped_table; --",
        start_date=None,  # type: ignore[arg-type]
        end_date=None,    # type: ignore[arg-type]
    )
    assert out["ok"] is False
    assert "allowlist" in out["error"]


def test_correlate_metrics_rejects_bad_method():
    out = tools.correlate_metrics(
        metric_a="recovery_score",
        metric_b="strain",
        start_date=None,  # type: ignore[arg-type]
        end_date=None,    # type: ignore[arg-type]
        method="kendall",
    )
    assert out["ok"] is False
    assert "pearson" in out["error"]


def test_get_spending_rejects_bad_group_by():
    out = tools.get_spending(start_date=None, end_date=None, group_by="quarter")  # type: ignore[arg-type]
    assert out["ok"] is False
    assert "group_by" in out["error"]


def test_get_transactions_rejects_bad_limit():
    out = tools.get_transactions(start_date=None, end_date=None, limit=99999)  # type: ignore[arg-type]
    assert out["ok"] is False
    assert "limit" in out["error"]


def test_escape_like_handles_special_chars():
    """The 'Bars & Nightlife' bug: pre-fix, % and _ in user-supplied substrings
    matched anything. Post-fix, they're treated literally."""
    assert tools._escape_like("100% sure") == r"100\% sure"
    assert tools._escape_like("foo_bar") == r"foo\_bar"
    assert tools._escape_like(r"a\b") == r"a\\b"
    # & is not an ILIKE meta-char, so it's untouched — the original transcript
    # bug was the surrounding pattern logic, not the ampersand itself.
    assert tools._escape_like("Bars & Nightlife") == "Bars & Nightlife"


# ---- correlate_metrics lag_range -------------------------------------------
def test_correlate_metrics_rejects_bad_lag_range():
    out = tools.correlate_metrics(
        metric_a="recovery_score",
        metric_b="strain",
        start_date=None,  # type: ignore[arg-type]
        end_date=None,    # type: ignore[arg-type]
        lag_range=[5, 1],
    )
    assert out["ok"] is False
    assert "lag_range" in out["error"]


def test_correlate_metrics_rejects_oversized_sweep():
    out = tools.correlate_metrics(
        metric_a="recovery_score",
        metric_b="strain",
        start_date=None,  # type: ignore[arg-type]
        end_date=None,    # type: ignore[arg-type]
        lag_range=[-30, 30],
    )
    assert out["ok"] is False
    assert "21" in out["error"]


# ---- telemetry --------------------------------------------------------------
def test_telemetry_summarize_truncates_long_strings():
    from mcp_server.telemetry import _summarize_value

    long_sql = "SELECT " + "x," * 200
    summary = _summarize_value(long_sql)
    assert isinstance(summary, dict)
    assert summary["_truncated"] is True
    assert summary["len"] == len(long_sql)


def test_telemetry_summarize_preserves_short_args():
    from datetime import date

    from mcp_server.telemetry import _summarize_args

    out = _summarize_args(
        (), {"start_date": date(2026, 4, 1), "category": "Bars & Nightlife", "limit": 50},
    )
    assert out["start_date"] == "2026-04-01"
    assert out["category"] == "Bars & Nightlife"
    assert out["limit"] == 50


def test_telemetry_summarize_caps_long_lists():
    from mcp_server.telemetry import _summarize_value

    out = _summarize_value(list(range(50)))
    assert isinstance(out, dict)
    assert out["_list_len"] == 50
    assert len(out["head"]) == 5


# ---- compute_couple_owed validation ----------------------------------------
def test_compute_couple_owed_requires_account_filter():
    from mcp_server import write_tools

    out = write_tools.compute_couple_owed(
        start_date=None, end_date=None,  # type: ignore[arg-type]
    )
    assert out["ok"] is False
    assert "account" in out["error"]


def test_compute_couple_owed_validates_split_sums_to_one():
    from mcp_server import write_tools

    out = write_tools.compute_couple_owed(
        start_date=None, end_date=None,  # type: ignore[arg-type]
        account_names=["Chase"],
        split_me=0.7, split_partner=0.7,
    )
    assert out["ok"] is False
    assert "1.0" in out["error"]
