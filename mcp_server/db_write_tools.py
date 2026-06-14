"""Generic database write + introspection tools for the MCP.

`ask_sql` already gives Claude unrestricted READ access (read-only DB role +
keyword guard). This module is the WRITE counterpart: a small, safe-by-
construction set of tools that let Claude insert / update / delete / upsert
rows, run arbitrary DML/DDL, and introspect the live schema — so it can
"100% write to the DB and query it" without a bespoke tool per table.

Design / safety model (see also sql_safety.validate_write):

  * Writes run on the ADMIN pool (service role) — the same one ingesters use.
    The guards here prevent *accidental* damage, not unauthorized access
    (this is a single-user personal server).
  * Every statement runs inside a transaction. We execute, read the affected
    row count, then DECIDE whether to COMMIT:
        - dry_run=True            -> always ROLLBACK (preview only)
        - affected > max_affected -> ROLLBACK unless confirm_large=True
        - otherwise               -> COMMIT
    So a runaway whole-table write is caught by the real row count, not just
    by string heuristics.
  * UPDATE/DELETE without a WHERE is refused unless allow_no_where=True.
  * DROP / TRUNCATE / ALTER...DROP / REVOKE require confirm_destructive=True.
  * DROP DATABASE/SCHEMA/ROLE and anything touching mcp_write_audit /
    schema_migrations are refused outright.
  * Every call — dry-run, committed, blocked, or failed — is recorded in
    mcp_write_audit with the exact SQL, params, row count, and outcome.

Structured tools (db_insert/db_update/db_delete/db_upsert) build parameterized
SQL via psycopg.sql so identifiers and values can't be injection vectors, and
validate table/column names against the live catalog for clear errors. The
raw execute_sql tool is the escape hatch for anything the structured tools
don't cover (DDL, multi-table CTEs, ON CONFLICT, window updates, ...).
"""

from __future__ import annotations

import json
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from lifeos_core.db import conn
from lifeos_core.logging import get_logger
from mcp_server.sql_safety import (
    WriteSafetyError,
    classify_statement,
    extract_target_table,
    validate_write,
)
from mcp_server.tools import _err, _ok, _serialize

log = get_logger(__name__)

# Default ceiling on rows a single write may touch before we demand
# confirm_large=True. Generous enough for normal edits, low enough that a
# botched WHERE doesn't silently rewrite the whole table.
DEFAULT_MAX_AFFECTED = 1000
WRITE_TIMEOUT_MS = 30_000          # per-statement statement_timeout for writes
RETURNING_SAMPLE = 50              # cap RETURNING rows echoed back / audited


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _coerce_param(v: Any) -> Any:
    """JSON-wrap dict/list params so JSONB columns accept them naturally."""
    if isinstance(v, (dict, list)):
        return Jsonb(v)
    return v


def _params_summary(params: Any) -> Any:
    """Trim params for the audit log so we don't store megabytes."""
    if params is None:
        return None
    try:
        s = json.dumps(params, default=str)
    except (TypeError, ValueError):
        s = str(params)
    return (s[:4000] + "…") if len(s) > 4000 else s


def _audit(
    *, tool: str, operation: str | None, target_table: str | None,
    statement: str, params: Any, affected_rows: int | None,
    dry_run: bool, committed: bool, ok: bool, error: str | None,
    result_sample: list | None,
) -> None:
    """Record a write attempt. Never raises — audit failure must not break the
    tool. Runs in its own transaction so it persists even when the main
    statement rolled back."""
    try:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mcp_write_audit
                      (tool, operation, target_table, statement, params,
                       affected_rows, dry_run, committed, ok, error, result_sample)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb)
                    """,
                    [
                        tool, operation, target_table, statement,
                        # Always valid JSON (a truncated raw string would fail ::jsonb).
                        json.dumps({"params": _params_summary(params)}),
                        affected_rows, dry_run, committed, ok, error,
                        json.dumps(result_sample, default=str) if result_sample is not None else None,
                    ],
                )
            c.commit()
    except Exception as e:
        log.warning("db_write.audit_failed", error=str(e), tool=tool)


def _render(statement: Any, c) -> str:
    """Render a psycopg.sql.Composed (or str) to text for the audit log."""
    if isinstance(statement, str):
        return statement
    try:
        return statement.as_string(c)
    except Exception:
        return str(statement)


def _execute_core(
    *, tool: str, statement: Any, params: list | None, operation: str | None,
    target_table: str | None, dry_run: bool, max_affected: int,
    confirm_large: bool, timeout_ms: int,
) -> dict:
    """Run one write statement inside a transaction and decide commit vs
    rollback. Shared by execute_sql and the structured db_* tools. `statement`
    may be a raw SQL string or a psycopg.sql.Composed."""
    timeout = max(500, min(int(timeout_ms), 120_000))
    bound = [_coerce_param(p) for p in params] if params else None

    affected: int | None = None
    returning: list | None = None
    committed = False
    rendered = statement if isinstance(statement, str) else None

    try:
        with conn() as c:
            rendered = _render(statement, c)
            try:
                with c.cursor() as cur:
                    cur.execute(f"SET LOCAL statement_timeout = {timeout}")
                    cur.execute(statement, bound)
                    affected = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else None
                    if cur.description is not None:
                        rows = _serialize(cur.fetchall())
                        returning = rows[:RETURNING_SAMPLE]

                # Decide: dry-run and over-budget both ROLL BACK.
                over_budget = (
                    affected is not None and affected > max_affected and not confirm_large
                )
                if dry_run or over_budget:
                    c.rollback()
                    committed = False
                else:
                    c.commit()
                    committed = True
            except Exception:
                c.rollback()
                raise
    except Exception as e:
        _audit(tool=tool, operation=operation, target_table=target_table,
               statement=rendered or str(statement), params=params,
               affected_rows=affected, dry_run=dry_run, committed=False,
               ok=False, error=str(e), result_sample=None)
        return _err(tool, e)

    _audit(tool=tool, operation=operation, target_table=target_table,
           statement=rendered or "", params=params, affected_rows=affected,
           dry_run=dry_run, committed=committed, ok=True, error=None,
           result_sample=returning)

    over_budget = (
        affected is not None and affected > max_affected and not confirm_large
    )
    extra: dict = {
        "operation": operation,
        "target_table": target_table,
        "affected_rows": affected,
        "committed": committed,
        "dry_run": dry_run,
        "statement": rendered or "",
    }
    if returning is not None:
        extra["returning"] = returning
    if over_budget:
        err = _err(tool, RuntimeError(
            f"refused: would affect {affected} row(s), over the max_affected limit "
            f"of {max_affected}. Rolled back, nothing changed. Re-run with "
            f"confirm_large=true (or a higher max_affected), or tighten the WHERE."
        ))
        err.update({"affected_rows": affected, "committed": False, "operation": operation})
        return err
    warnings: list[str] = []
    if dry_run:
        warnings.append(
            f"DRY RUN — would affect {affected} row(s); rolled back, nothing committed. "
            "Re-run with dry_run=false to apply."
        )
    else:
        warnings.append(f"committed — {affected} row(s) affected.")
    return _ok(tool, returning or [], warnings=warnings, extra=extra)


# ---------------------------------------------------------------------------
# catalog introspection
# ---------------------------------------------------------------------------
def _split_table(table: str) -> tuple[str, str]:
    """('schema', 'name') from 'schema.name' or 'name' (default schema public)."""
    if "." in table:
        schema, name = table.split(".", 1)
        return schema.strip('"'), name.strip('"')
    return "public", table.strip('"')


def _table_exists(cur, table: str) -> bool:
    schema, name = _split_table(table)
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        UNION ALL
        SELECT 1 FROM information_schema.views
        WHERE table_schema = %s AND table_name = %s
        LIMIT 1
        """,
        [schema, name, schema, name],
    )
    return cur.fetchone() is not None


def _columns_of(cur, table: str) -> list[dict]:
    schema, name = _split_table(table)
    cur.execute(
        """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        [schema, name],
    )
    return _serialize(cur.fetchall())


def db_list_tables(
    schema: str = "public",
    pattern: str | None = None,
    include_views: bool = True,
) -> dict:
    """List tables (and views) in the warehouse with approximate row counts and
    column counts. `pattern` is an ILIKE filter on the name (e.g. 'fact_%',
    '%whoop%'). Start here when you don't know the exact table name; then
    db_describe_table for its columns."""
    relkinds = "('r','p','v','m')" if include_views else "('r','p')"
    where = "n.nspname = %s"
    params: list = [schema]
    if pattern:
        where += " AND c.relname ILIKE %s"
        params.append(pattern)
    q = f"""
        SELECT n.nspname AS schema, c.relname AS name,
               CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'partitioned table'
                              WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview' END AS kind,
               CASE WHEN c.relkind IN ('r','p') THEN c.reltuples::bigint END AS approx_rows,
               (SELECT count(*) FROM information_schema.columns col
                 WHERE col.table_schema = n.nspname AND col.table_name = c.relname) AS columns,
               obj_description(c.oid) AS comment
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE {where} AND c.relkind IN {relkinds}
        ORDER BY c.relname
    """
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(q, params)
            rows = _serialize(cur.fetchall())
        return _ok("db_list_tables", rows)
    except Exception as e:
        return _err("db_list_tables", e)


def db_describe_table(table: str) -> dict:
    """Full structure of one table: columns (type / nullable / default), primary
    key, unique constraints, foreign keys, indexes, approximate row count, and
    comment. Call this before writing so you use real column names and respect
    NOT NULL / FK constraints."""
    schema, name = _split_table(table)
    try:
        with conn() as c, c.cursor() as cur:
            if not _table_exists(cur, table):
                return _err("db_describe_table",
                            ValueError(f"table '{table}' not found in schema '{schema}'."))
            columns = _columns_of(cur, table)
            cur.execute(
                """
                SELECT tc.constraint_type, kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON kcu.constraint_name = tc.constraint_name
                 AND kcu.table_schema = tc.table_schema
                WHERE tc.table_schema = %s AND tc.table_name = %s
                  AND tc.constraint_type IN ('PRIMARY KEY','UNIQUE')
                ORDER BY tc.constraint_type, kcu.ordinal_position
                """,
                [schema, name],
            )
            cons = _serialize(cur.fetchall())
            pk = [r["column_name"] for r in cons if r["constraint_type"] == "PRIMARY KEY"]
            uniq = [r["column_name"] for r in cons if r["constraint_type"] == "UNIQUE"]
            cur.execute(
                """
                SELECT kcu.column_name,
                       ccu.table_name AS ref_table, ccu.column_name AS ref_column
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
                JOIN information_schema.constraint_column_usage ccu
                  ON ccu.constraint_name = tc.constraint_name
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = %s AND tc.table_name = %s
                """,
                [schema, name],
            )
            fks = _serialize(cur.fetchall())
            cur.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=%s",
                [schema, name],
            )
            indexes = _serialize(cur.fetchall())
            cur.execute(
                "SELECT reltuples::bigint AS approx_rows, obj_description(oid) AS comment "
                "FROM pg_class WHERE oid = %s::regclass",
                [f"{schema}.{name}"],
            )
            meta = _serialize(cur.fetchall())
        return _ok("db_describe_table", columns, extra={
            "table": f"{schema}.{name}",
            "primary_key": pk,
            "unique_columns": uniq,
            "foreign_keys": fks,
            "indexes": indexes,
            "approx_rows": meta[0]["approx_rows"] if meta else None,
            "comment": meta[0]["comment"] if meta else None,
        })
    except Exception as e:
        return _err("db_describe_table", e)


def _validate_columns(cur, table: str, cols: list[str]) -> None:
    """Raise if any column isn't real (clear error + stops typo writes)."""
    if not _table_exists(cur, table):
        raise ValueError(f"table '{table}' does not exist. Use db_list_tables to find it.")
    real = {r["column_name"] for r in _columns_of(cur, table)}
    bad = [c for c in cols if c not in real]
    if bad:
        raise ValueError(
            f"unknown column(s) on {table}: {bad}. Real columns: {sorted(real)}"
        )


# ---------------------------------------------------------------------------
# structured CRUD
# ---------------------------------------------------------------------------
def db_insert(
    table: str,
    rows: list[dict],
    on_conflict_do_nothing: bool = False,
    returning: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Insert one or more rows into a table. `rows` is a list of column->value
    dicts (all rows must share the same columns). dict/list values are written
    as JSONB automatically. Set on_conflict_do_nothing=True to skip rows that
    violate a unique constraint. `returning` echoes back a column (e.g. 'id').
    Validates column names against the live schema first."""
    if not rows:
        return _err("db_insert", ValueError("no rows provided."))
    cols = list(rows[0].keys())
    if any(set(r.keys()) != set(cols) for r in rows):
        return _err("db_insert", ValueError("all rows must have the same columns."))
    try:
        with conn() as c, c.cursor() as cur:
            _validate_columns(cur, table, cols)
        tbl = sql.Identifier(*_split_table(table))
        col_ids = sql.SQL(", ").join(map(sql.Identifier, cols))
        one_row = sql.SQL("({})").format(sql.SQL(", ").join(sql.Placeholder() * len(cols)))
        all_rows = sql.SQL(", ").join([one_row] * len(rows))
        stmt = sql.SQL("INSERT INTO {t} ({c}) VALUES {v}").format(t=tbl, c=col_ids, v=all_rows)
        if on_conflict_do_nothing:
            stmt = sql.SQL("{q} ON CONFLICT DO NOTHING").format(q=stmt)
        if returning:
            stmt = sql.SQL("{q} RETURNING {r}").format(q=stmt, r=sql.Identifier(returning))
        params = [r[col] for r in rows for col in cols]
        return _execute_core(
            tool="db_insert", statement=stmt, params=params, operation="INSERT",
            target_table=table, dry_run=dry_run, max_affected=max(len(rows), DEFAULT_MAX_AFFECTED),
            confirm_large=True, timeout_ms=WRITE_TIMEOUT_MS,
        )
    except Exception as e:
        return _err("db_insert", e)


def db_update(
    table: str,
    set_values: dict,
    where: str,
    where_params: list | None = None,
    returning: str | None = None,
    dry_run: bool = False,
    max_affected: int = DEFAULT_MAX_AFFECTED,
    confirm_large: bool = False,
) -> dict:
    """Update rows. `set_values` is column->new-value. `where` is a SQL
    predicate WITHOUT the word WHERE (e.g. 'id = %s AND day >= %s'); pass its
    %s values as `where_params`. A WHERE is REQUIRED. If the update would touch
    more than max_affected rows it's rolled back unless confirm_large=True. Use
    dry_run=True to preview the affected count first. JSONB values may be dicts.
    """
    if not set_values:
        return _err("db_update", ValueError("set_values is empty."))
    if not where or not where.strip():
        return _err("db_update", ValueError(
            "where is required (a full-table UPDATE is refused). Pass a predicate."))
    cols = list(set_values.keys())
    try:
        with conn() as c, c.cursor() as cur:
            _validate_columns(cur, table, cols)
        tbl = sql.Identifier(*_split_table(table))
        set_clause = sql.SQL(", ").join(
            sql.SQL("{c} = %s").format(c=sql.Identifier(col)) for col in cols
        )
        stmt = sql.SQL("UPDATE {t} SET {s} WHERE {w}").format(
            t=tbl, s=set_clause, w=sql.SQL(where))
        if returning:
            stmt = sql.SQL("{q} RETURNING {r}").format(q=stmt, r=sql.Identifier(returning))
        params = [set_values[col] for col in cols] + list(where_params or [])
        return _execute_core(
            tool="db_update", statement=stmt, params=params, operation="UPDATE",
            target_table=table, dry_run=dry_run, max_affected=max_affected,
            confirm_large=confirm_large, timeout_ms=WRITE_TIMEOUT_MS,
        )
    except Exception as e:
        return _err("db_update", e)


def db_delete(
    table: str,
    where: str,
    where_params: list | None = None,
    returning: str | None = None,
    dry_run: bool = False,
    max_affected: int = DEFAULT_MAX_AFFECTED,
    confirm_large: bool = False,
) -> dict:
    """Delete rows matching `where` (a predicate WITHOUT the word WHERE; pass
    %s values as `where_params`). A WHERE is REQUIRED. Rolled back if it would
    delete more than max_affected rows unless confirm_large=True. Use
    dry_run=True (or returning='id') to see exactly what would go first."""
    if not where or not where.strip():
        return _err("db_delete", ValueError(
            "where is required (a full-table DELETE is refused). Pass a predicate."))
    try:
        with conn() as c, c.cursor() as cur:
            if not _table_exists(cur, table):
                raise ValueError(f"table '{table}' does not exist.")
        tbl = sql.Identifier(*_split_table(table))
        stmt = sql.SQL("DELETE FROM {t} WHERE {w}").format(t=tbl, w=sql.SQL(where))
        if returning:
            stmt = sql.SQL("{q} RETURNING {r}").format(q=stmt, r=sql.Identifier(returning))
        return _execute_core(
            tool="db_delete", statement=stmt, params=list(where_params or []),
            operation="DELETE", target_table=table, dry_run=dry_run,
            max_affected=max_affected, confirm_large=confirm_large,
            timeout_ms=WRITE_TIMEOUT_MS,
        )
    except Exception as e:
        return _err("db_delete", e)


def db_upsert(
    table: str,
    rows: list[dict],
    conflict_columns: list[str],
    update_columns: list[str] | None = None,
    returning: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Insert rows, updating on conflict (INSERT ... ON CONFLICT DO UPDATE).
    `conflict_columns` must form a unique index/constraint. `update_columns`
    defaults to every non-conflict column. This is the idempotent write — safe
    to re-run. dict/list values become JSONB."""
    if not rows:
        return _err("db_upsert", ValueError("no rows provided."))
    if not conflict_columns:
        return _err("db_upsert", ValueError("conflict_columns is required."))
    cols = list(rows[0].keys())
    if any(set(r.keys()) != set(cols) for r in rows):
        return _err("db_upsert", ValueError("all rows must have the same columns."))
    upd = update_columns or [c for c in cols if c not in conflict_columns]
    try:
        with conn() as c, c.cursor() as cur:
            _validate_columns(cur, table, cols + list(conflict_columns))
        tbl = sql.Identifier(*_split_table(table))
        col_ids = sql.SQL(", ").join(map(sql.Identifier, cols))
        one_row = sql.SQL("({})").format(sql.SQL(", ").join(sql.Placeholder() * len(cols)))
        all_rows = sql.SQL(", ").join([one_row] * len(rows))
        conflict = sql.SQL(", ").join(map(sql.Identifier, conflict_columns))
        if upd:
            set_clause = sql.SQL(", ").join(
                sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(col)) for col in upd
            )
            tail = sql.SQL("ON CONFLICT ({k}) DO UPDATE SET {s}").format(k=conflict, s=set_clause)
        else:
            tail = sql.SQL("ON CONFLICT ({k}) DO NOTHING").format(k=conflict)
        stmt = sql.SQL("INSERT INTO {t} ({c}) VALUES {v} {tail}").format(
            t=tbl, c=col_ids, v=all_rows, tail=tail)
        if returning:
            stmt = sql.SQL("{q} RETURNING {r}").format(q=stmt, r=sql.Identifier(returning))
        params = [r[col] for r in rows for col in cols]
        return _execute_core(
            tool="db_upsert", statement=stmt, params=params, operation="UPSERT",
            target_table=table, dry_run=dry_run,
            max_affected=max(len(rows) * 2, DEFAULT_MAX_AFFECTED),
            confirm_large=True, timeout_ms=WRITE_TIMEOUT_MS,
        )
    except Exception as e:
        return _err("db_upsert", e)


# ---------------------------------------------------------------------------
# raw escape hatch
# ---------------------------------------------------------------------------
def execute_sql(
    statement: str,
    params: list | None = None,
    dry_run: bool = False,
    confirm_destructive: bool = False,
    allow_no_where: bool = False,
    max_affected: int = DEFAULT_MAX_AFFECTED,
    confirm_large: bool = False,
    timeout_ms: int = WRITE_TIMEOUT_MS,
) -> dict:
    """Run an arbitrary write statement (INSERT/UPDATE/DELETE/DDL/...) — the
    write counterpart to ask_sql. Use the structured db_* tools for simple
    row edits; use this for DDL (CREATE/ALTER), ON CONFLICT, CTEs, or anything
    they don't cover.

    Always parameterize values with %s + `params` (never string-concat). The
    statement runs in a transaction:
      * dry_run=True            → executed then ROLLED BACK (preview the row count)
      * affected > max_affected → rolled back unless confirm_large=True
      * UPDATE/DELETE w/o WHERE  → refused unless allow_no_where=True
      * DROP/TRUNCATE/ALTER-DROP → refused unless confirm_destructive=True
      * DROP DATABASE/SCHEMA/ROLE, or touching the audit/migration tables → always refused
    Every call is logged to mcp_write_audit. One statement per call.

    NOTE: statements that cannot run inside a transaction (CREATE INDEX
    CONCURRENTLY, VACUUM, ...) will error here — those are rare; ask the user to
    run them directly. Ad-hoc DDL changes the LIVE DB immediately but is NOT
    captured as a migration file, so it won't be reproduced on a fresh rebuild —
    mention that the user should add a db/migrations/*.sql entry if it must persist.
    """
    try:
        validate_write(statement, allow_no_where=allow_no_where,
                       confirm_destructive=confirm_destructive)
    except WriteSafetyError as e:
        _audit(tool="execute_sql", operation=classify_statement(statement),
               target_table=extract_target_table(statement), statement=statement,
               params=params, affected_rows=None, dry_run=dry_run, committed=False,
               ok=False, error=str(e), result_sample=None)
        return _err("execute_sql", e)
    return _execute_core(
        tool="execute_sql", statement=statement, params=params,
        operation=classify_statement(statement),
        target_table=extract_target_table(statement), dry_run=dry_run,
        max_affected=max_affected, confirm_large=confirm_large, timeout_ms=timeout_ms,
    )


def get_write_audit(limit: int = 50, table: str | None = None) -> dict:
    """Recent DB write history from mcp_write_audit — what was written, when,
    the exact SQL, row counts, and whether it committed. Filter by target
    `table`. Use this to review or reconstruct changes."""
    where = ""
    params: list = []
    if table:
        where = "WHERE target_table = %s"
        params.append(table)
    q = f"""
        SELECT id, ts, tool, operation, target_table, statement, affected_rows,
               dry_run, committed, ok, error
        FROM mcp_write_audit {where}
        ORDER BY ts DESC LIMIT %s
    """
    params.append(max(1, min(int(limit), 500)))
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(q, params)
            return _ok("get_write_audit", _serialize(cur.fetchall()))
    except Exception as e:
        return _err("get_write_audit", e)
