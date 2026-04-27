"""DB pool and transaction helpers.

Two pools:
- `pool()`             — admin/service-role, used by ingesters and mart_refresh.
- `reader_pool()`      — read-only role, used by MCP `ask_sql`.

Pools are lazily constructed on first use so importing lifeos_core.db doesn't
require credentials (handy for tests that monkey-patch).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from lifeos_core.settings import settings

_pool: ConnectionPool | None = None
_reader_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """Admin/service-role connection pool. Created on first call.

    `prepare_threshold=None` disables psycopg's server-side prepared
    statements. Required when running through Supabase's transaction pooler
    (port 6543), which multiplexes Postgres backends across clients and
    causes name collisions on prepared statement reuse."""
    global _pool
    if _pool is None:
        if not settings.SUPABASE_DB_URL:
            raise RuntimeError(
                "SUPABASE_DB_URL is not set. Configure it in .env before running "
                "any service that touches the database."
            )
        _pool = ConnectionPool(
            conninfo=settings.SUPABASE_DB_URL,
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row, "autocommit": False, "prepare_threshold": None},
            open=True,
        )
    return _pool


def reader_pool() -> ConnectionPool:
    """Read-only pool for ask_sql. Falls back to admin pool if reader URL not
    configured (tests). In production, always set LIFEOS_READER_DB_URL."""
    global _reader_pool
    if _reader_pool is None:
        url = settings.LIFEOS_READER_DB_URL or settings.SUPABASE_DB_URL
        if not url:
            raise RuntimeError(
                "Neither LIFEOS_READER_DB_URL nor SUPABASE_DB_URL is set."
            )
        _reader_pool = ConnectionPool(
            conninfo=url,
            min_size=1,
            max_size=4,
            kwargs={"row_factory": dict_row, "autocommit": True, "prepare_threshold": None},
            open=True,
        )
    return _reader_pool


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    """Borrow an admin connection. Caller is responsible for commit/rollback
    via context manager on the connection itself, or use `tx()` for auto-commit
    semantics."""
    with pool().connection() as c:
        yield c


@contextmanager
def tx() -> Iterator[psycopg.Connection]:
    """Run a unit of work in a transaction. Commits on clean exit, rolls back
    on exception."""
    with pool().connection() as c:
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


@contextmanager
def reader_conn() -> Iterator[psycopg.Connection]:
    with reader_pool().connection() as c:
        yield c


def close_pools() -> None:
    """Tear down pools. Call from process shutdown hooks."""
    global _pool, _reader_pool
    if _pool is not None:
        _pool.close()
        _pool = None
    if _reader_pool is not None:
        _reader_pool.close()
        _reader_pool = None
