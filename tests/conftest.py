"""Shared pytest fixtures.

Most unit tests are pure-function (parsing, transforms) and don't need a
database. The few that do (`test_mart_refresh.py`, `test_mcp_tools.py`) opt
into the `db` fixture which expects a `LIFEOS_TEST_DB_URL` env var pointing
at a throwaway Postgres (e.g. `postgresql://postgres@localhost:5432/lifeos_test`).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def db_url() -> str:
    url = os.environ.get("LIFEOS_TEST_DB_URL")
    if not url:
        pytest.skip("LIFEOS_TEST_DB_URL not set; skipping DB-backed test.")
    return url
