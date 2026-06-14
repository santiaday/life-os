"""Tests for the write-path safety guards (sql_safety) + db_write_tools helpers.

These are pure functions — no DB — so they pin the guard logic that decides
whether a write is refused, needs confirmation, or passes. The end-to-end
behavior against a real table is verified live, not here.
"""

from __future__ import annotations

import pytest

from mcp_server import sql_safety as S
from mcp_server.db_write_tools import _coerce_param, _split_table


# ---- classify_statement ----------------------------------------------------
@pytest.mark.parametrize("query,expected", [
    ("SELECT * FROM t", "SELECT"),
    ("  insert into t (a) values (1)", "INSERT"),
    ("UPDATE t SET a=1 WHERE id=2", "UPDATE"),
    ("DELETE FROM t WHERE id=2", "DELETE"),
    ("CREATE TABLE t (id int)", "DDL_CREATE"),
    ("ALTER TABLE t ADD COLUMN x int", "DDL_ALTER"),
    ("DROP TABLE t", "DDL_DROP"),
    ("TRUNCATE t", "TRUNCATE"),
    ("GRANT SELECT ON t TO r", "DCL"),
    # data-modifying CTE must classify as the modifying verb, not SELECT
    ("WITH d AS (DELETE FROM t WHERE id=1 RETURNING *) SELECT * FROM d", "DELETE"),
    ("WITH x AS (SELECT 1) SELECT * FROM x", "SELECT"),
])
def test_classify_statement(query, expected):
    assert S.classify_statement(query) == expected


# ---- statement_has_where ---------------------------------------------------
def test_statement_has_where():
    assert S.statement_has_where("DELETE FROM t WHERE id = 1")
    assert not S.statement_has_where("DELETE FROM t")
    # 'where' inside a string literal must not count
    assert not S.statement_has_where("INSERT INTO t (note) VALUES ('no where here')")
    # top-level WHERE with a subquery WHERE still counts
    assert S.statement_has_where("DELETE FROM t WHERE id IN (SELECT id FROM u WHERE x=1)")
    # WHERE ONLY inside a subquery does NOT count — this rewrites all of t
    assert not S.statement_has_where("UPDATE t SET x = (SELECT y FROM z WHERE z.id = 1)")


def test_subquery_only_where_is_refused():
    # A whole-table UPDATE disguised with a subquery WHERE must be blocked.
    with pytest.raises(S.WriteSafetyError, match="WHERE"):
        S.validate_write("UPDATE t SET x = (SELECT max(v) FROM z WHERE z.k = 1)")
    # ...but allowed with the explicit override
    S.validate_write("UPDATE t SET x = (SELECT max(v) FROM z WHERE z.k = 1)",
                     allow_no_where=True)


# ---- count_statements ------------------------------------------------------
def test_count_statements():
    assert S.count_statements("SELECT 1") == 1
    assert S.count_statements("SELECT 1;") == 1
    assert S.count_statements("SELECT 1; SELECT 2") == 2
    # semicolon inside a literal is not a separator
    assert S.count_statements("INSERT INTO t (s) VALUES ('a;b')") == 1


# ---- is_destructive --------------------------------------------------------
@pytest.mark.parametrize("query,destructive", [
    ("DROP TABLE t", True),
    ("TRUNCATE t", True),
    ("ALTER TABLE t DROP COLUMN x", True),
    ("REVOKE SELECT ON t FROM r", True),
    ("ALTER TABLE t ADD COLUMN x int", False),
    ("UPDATE t SET a=1 WHERE id=2", False),
    ("INSERT INTO t (a) VALUES (1)", False),
])
def test_is_destructive(query, destructive):
    assert S.is_destructive(query) is destructive


# ---- validate_write: refusals ----------------------------------------------
def test_multiple_statements_refused():
    with pytest.raises(S.WriteSafetyError, match="multiple statements"):
        S.validate_write("UPDATE t SET a=1 WHERE id=1; DELETE FROM t WHERE id=2")


@pytest.mark.parametrize("query", [
    "DROP DATABASE lifeos",
    "DROP SCHEMA public CASCADE",
    "DROP ROLE lifeos_mcp",
    "DROP TABLE mcp_write_audit",
    "TRUNCATE schema_migrations",
])
def test_catastrophic_always_refused(query):
    # even with every override, catastrophic ops are refused
    with pytest.raises(S.WriteSafetyError, match="catastrophic"):
        S.validate_write(query, allow_no_where=True, confirm_destructive=True)


def test_update_without_where_refused_then_allowed():
    with pytest.raises(S.WriteSafetyError, match="WHERE"):
        S.validate_write("UPDATE t SET a = 1")
    # explicit override passes
    S.validate_write("UPDATE t SET a = 1", allow_no_where=True)


def test_delete_without_where_refused():
    with pytest.raises(S.WriteSafetyError, match="WHERE"):
        S.validate_write("DELETE FROM t")


def test_destructive_needs_confirm():
    with pytest.raises(S.WriteSafetyError, match="destructive"):
        S.validate_write("DROP TABLE some_table")
    # confirm_destructive lets a non-catastrophic drop through
    S.validate_write("DROP TABLE some_table", confirm_destructive=True)


def test_truncate_needs_confirm():
    with pytest.raises(S.WriteSafetyError, match="destructive"):
        S.validate_write("TRUNCATE some_table")
    S.validate_write("TRUNCATE some_table", confirm_destructive=True)


# ---- validate_write: passes ------------------------------------------------
@pytest.mark.parametrize("query", [
    "INSERT INTO t (a) VALUES (%s)",
    "UPDATE t SET a = %s WHERE id = %s",
    "DELETE FROM t WHERE id = %s",
    "CREATE TABLE t (id int)",
    "ALTER TABLE t ADD COLUMN x int",
])
def test_safe_writes_pass(query):
    S.validate_write(query)  # should not raise


def test_empty_refused():
    with pytest.raises(S.WriteSafetyError, match="empty"):
        S.validate_write("   ")


# ---- extract_target_table --------------------------------------------------
@pytest.mark.parametrize("query,table", [
    ("INSERT INTO fact_biometric (a) VALUES (1)", "fact_biometric"),
    ("UPDATE public.mart_daily SET x=1 WHERE day='2026-01-01'", "public.mart_daily"),
    ("DELETE FROM raw_whoop_trend WHERE id=1", "raw_whoop_trend"),
    ("CREATE TABLE new_thing (id int)", "new_thing"),
])
def test_extract_target_table(query, table):
    assert S.extract_target_table(query) == table


# ---- db_write_tools pure helpers -------------------------------------------
def test_split_table():
    assert _split_table("fact_biometric") == ("public", "fact_biometric")
    assert _split_table("analytics.mart_daily") == ("analytics", "mart_daily")
    assert _split_table('"Weird".tbl') == ("Weird", "tbl")


def test_coerce_param_wraps_json():
    from psycopg.types.json import Jsonb
    assert isinstance(_coerce_param({"a": 1}), Jsonb)
    assert isinstance(_coerce_param([1, 2]), Jsonb)
    assert _coerce_param("plain") == "plain"
    assert _coerce_param(5) == 5
    assert _coerce_param(None) is None
