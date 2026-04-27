"""Migration runner.

Reads every `db/migrations/NNNN_*.sql` in numeric order, tracks applied
filenames in a `schema_migrations` table, and skips ones already applied.
Substitutes `${VAR}` placeholders from the environment before each file is
sent (used for `${MCP_DB_PASSWORD}` in 0006_views.sql).

Usage:
    python -m db.apply               # apply all pending
    python -m db.apply --dry-run     # show what would run
    python -m db.apply --to 0003     # apply up through 0003

Always uses SUPABASE_DB_URL_DIRECT (port 5432) — the pooled connection
mangles role/transaction-level statements that 0006 needs.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import psycopg

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
PLACEHOLDER = re.compile(r"\$\{(\w+)\}")


def _direct_url() -> str:
    """Migrations require a direct connection (Supabase port 5432). The pooled
    connection (6543) doesn't support role/extension statements reliably."""
    direct = settings.SUPABASE_DB_URL_DIRECT
    if direct:
        return direct
    log.warning(
        "migrate.using_pooled_url",
        message="SUPABASE_DB_URL_DIRECT not set; falling back to pooled URL. "
                "Migrations 0001 (CREATE EXTENSION) and 0006 (CREATE ROLE) may fail.",
    )
    return settings.SUPABASE_DB_URL


def _ensure_migrations_table(c: psycopg.Connection) -> None:
    with c.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              filename TEXT PRIMARY KEY,
              applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    c.commit()


def _applied(c: psycopg.Connection) -> set[str]:
    with c.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def _migrations() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def _substitute(sql_text: str, filename: str) -> str:
    """Replace ${VAR} with os.environ[VAR]. Raise if a referenced var is unset."""
    missing: list[str] = []

    def repl(m: re.Match) -> str:
        name = m.group(1)
        val = os.environ.get(name)
        if val is None:
            missing.append(name)
            return m.group(0)
        return val

    out = PLACEHOLDER.sub(repl, sql_text)
    if missing:
        raise RuntimeError(
            f"{filename}: missing env vars referenced as placeholders: "
            f"{sorted(set(missing))}"
        )
    return out


def _record(c: psycopg.Connection, filename: str) -> None:
    with c.cursor() as cur:
        cur.execute(
            "INSERT INTO schema_migrations (filename) VALUES (%s)", [filename]
        )
    c.commit()


def apply(dry_run: bool = False, up_to: str | None = None) -> int:
    """Apply pending migrations. Returns count applied."""
    files = _migrations()
    if not files:
        log.warning("migrate.no_files", dir=str(MIGRATIONS_DIR))
        return 0

    if up_to:
        files = [f for f in files if f.stem.split("_", 1)[0] <= up_to]

    with psycopg.connect(_direct_url(), autocommit=False) as c:
        _ensure_migrations_table(c)
        already = _applied(c)

        pending = [f for f in files if f.name not in already]
        if not pending:
            log.info("migrate.up_to_date", count=len(already))
            return 0

        log.info("migrate.pending", files=[f.name for f in pending])
        if dry_run:
            return 0

        applied_count = 0
        for f in pending:
            sql_text = _substitute(f.read_text(), f.name)
            log.info("migrate.apply", file=f.name, bytes=len(sql_text))
            try:
                with c.cursor() as cur:
                    cur.execute(sql_text)
                c.commit()
            except Exception as e:
                c.rollback()
                log.error("migrate.failed", file=f.name, error=str(e))
                raise
            _record(c, f.name)
            applied_count += 1

        log.info("migrate.done", applied=applied_count)
        return applied_count


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Apply life-os DB migrations.")
    p.add_argument("--dry-run", action="store_true", help="Show pending without running.")
    p.add_argument("--to", metavar="NNNN", help="Apply up through this prefix.")
    args = p.parse_args(argv)

    try:
        apply(dry_run=args.dry_run, up_to=args.to)
        return 0
    except Exception as e:
        log.error("migrate.error", error=str(e), error_type=type(e).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
