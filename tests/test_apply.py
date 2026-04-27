"""Smoke tests for db.apply that don't need a database."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from db.apply import _migrations, _substitute


def test_migrations_present_and_ordered() -> None:
    files = _migrations()
    names = [f.name for f in files]
    assert names == sorted(names), "migrations must be lexicographically ordered"
    assert names[0].startswith("0001_"), f"expected 0001_* first, got {names[:1]}"
    # All six baseline migrations should exist.
    prefixes = {n.split("_", 1)[0] for n in names}
    assert {"0001", "0002", "0003", "0004", "0005", "0006"}.issubset(prefixes)


def test_substitute_replaces_known_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DB_PASSWORD", "hunter2")
    out = _substitute("PASSWORD '${MCP_DB_PASSWORD}'", "test.sql")
    assert out == "PASSWORD 'hunter2'"


def test_substitute_leaves_dollar_dollar_untouched() -> None:
    """Postgres function bodies use $$ delimiters; placeholder regex must
    not match them."""
    body = "CREATE FUNCTION f() RETURNS TEXT AS $$ SELECT 'hi' $$ LANGUAGE SQL"
    assert _substitute(body, "test.sql") == body


def test_substitute_raises_on_missing_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEFINITELY_UNSET_VAR", raising=False)
    with pytest.raises(RuntimeError, match="DEFINITELY_UNSET_VAR"):
        _substitute("hi ${DEFINITELY_UNSET_VAR}", "test.sql")


def test_views_migration_uses_password_placeholder() -> None:
    p = Path("db/migrations/0006_views.sql")
    text = p.read_text()
    assert "${MCP_DB_PASSWORD}" in text, (
        "0006 must reference ${MCP_DB_PASSWORD} so apply.py substitutes from env"
    )


def test_no_migration_uses_unsubstituted_placeholder_for_unset_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If you add a new ${VAR}, document and surface it via this test."""
    monkeypatch.setenv("MCP_DB_PASSWORD", "test")
    for f in _migrations():
        # Should not raise.
        _substitute(f.read_text(), f.name)
