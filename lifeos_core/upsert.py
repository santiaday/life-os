"""Generic upsert helpers.

Every ingester needs the same shape: take a list of dicts, write them with
ON CONFLICT (natural_key) DO UPDATE. These helpers give us that without each
service hand-rolling SQL string concatenation.
"""

from __future__ import annotations

from typing import Iterable, Sequence

import psycopg
from psycopg import sql

from lifeos_core.db import tx


def upsert_rows(
    table: str,
    rows: Sequence[dict],
    *,
    conflict_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
    returning: str | None = None,
    connection: psycopg.Connection | None = None,
) -> int:
    """Upsert `rows` into `table`. Returns the number of rows written.

    - `conflict_cols`: natural-key columns that must form a unique index.
    - `update_cols`: columns to update on conflict. Defaults to all non-conflict
      columns from the first row.
    - `returning`: optional column to RETURN; if set, returns the list of
      values; otherwise returns row count.
    - `connection`: pass an existing connection to participate in an outer
      transaction; if omitted, a new transaction is opened.
    """
    if not rows:
        return 0

    cols = list(rows[0].keys())
    if update_cols is None:
        update_cols = [c for c in cols if c not in conflict_cols]

    placeholders = sql.SQL(", ").join(sql.Placeholder() * len(cols))
    insert_cols = sql.SQL(", ").join(map(sql.Identifier, cols))
    conflict = sql.SQL(", ").join(map(sql.Identifier, conflict_cols))
    set_clause = sql.SQL(", ").join(
        sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in update_cols
    )

    base = sql.SQL(
        "INSERT INTO {tbl} ({cols}) VALUES ({vals}) "
        "ON CONFLICT ({conflict}) DO UPDATE SET {set}"
    ).format(
        tbl=sql.Identifier(table),
        cols=insert_cols,
        vals=placeholders,
        conflict=conflict,
        set=set_clause,
    )

    if returning:
        base = sql.SQL("{q} RETURNING {ret}").format(q=base, ret=sql.Identifier(returning))

    def _execute(c: psycopg.Connection) -> int:
        with c.cursor() as cur:
            count = 0
            for r in rows:
                cur.execute(base, [r[col] for col in cols])
                count += cur.rowcount
            return count

    if connection is not None:
        return _execute(connection)
    with tx() as c:
        return _execute(c)


def fetch_id_map(
    table: str, key_col: str, id_col: str, keys: Iterable
) -> dict:
    """Look up `id_col` for each `key_col` value. Returns {key: id}.

    Used to resolve raw_id foreign keys after a raw upsert: pass the natural
    keys you just wrote, get back the surrogate ids."""
    keys = list(keys)
    if not keys:
        return {}
    q = sql.SQL("SELECT {k}, {i} FROM {t} WHERE {k} = ANY(%s)").format(
        k=sql.Identifier(key_col),
        i=sql.Identifier(id_col),
        t=sql.Identifier(table),
    )
    with tx() as c, c.cursor() as cur:
        cur.execute(q, [keys])
        return {row[key_col]: row[id_col] for row in cur.fetchall()}
