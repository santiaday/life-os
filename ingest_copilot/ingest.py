"""Copilot ingestion: GraphQL → raw_* → fact/dim_*.

Three pipelines:
  - transactions: 35-day rolling window (last 1825 on --backfill).
  - categories: full refresh (small table).
  - accounts: full refresh.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from ingest_copilot import transforms
from ingest_copilot.graphql import GraphQLClient, schema_version
from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run
from lifeos_core.upsert import upsert_rows

log = get_logger(__name__)

DEFAULT_INCREMENTAL_DAYS = 35
DEFAULT_BACKFILL_DAYS = 1825  # ~5 years


def ingest_transactions(*, backfill_days: int | None = None) -> int:
    end = date.today()
    start = end - timedelta(days=backfill_days if backfill_days is not None else DEFAULT_INCREMENTAL_DAYS)

    with ingestion_run(
        "copilot",
        "transactions",
        start=str(start),
        end=str(end),
        schema_version=schema_version(),
    ) as run:
        with GraphQLClient() as client:
            records = client.transactions(start, end)
        run.fetched(len(records))
        if not records:
            return 0

        raw_rows = [
            {"transaction_id": r["id"], "payload": Jsonb(r)} for r in records
        ]
        with tx() as c:
            upsert_rows(
                "raw_copilot_transaction",
                raw_rows,
                conflict_cols=["transaction_id"],
                update_cols=["payload", "fetched_at"],
                connection=c,
            )

            id_map = _id_map(c, "raw_copilot_transaction", "transaction_id",
                             [r["transaction_id"] for r in raw_rows])
            fact_rows = []
            for r in records:
                row = transforms.transform_transaction(r)
                row["raw_id"] = id_map.get(row["transaction_id"])
                row["updated_at"] = datetime.now(timezone.utc)
                fact_rows.append(row)

            upsert_rows(
                "fact_transaction",
                fact_rows,
                conflict_cols=["transaction_id"],
                connection=c,
            )

        run.upserted(len(fact_rows))
        return len(fact_rows)


def ingest_categories() -> int:
    with ingestion_run("copilot", "categories", schema_version=schema_version()) as run:
        with GraphQLClient() as client:
            records = client.categories()
        run.fetched(len(records))
        if not records:
            return 0

        raw_rows = [
            {"category_id": r["id"], "payload": Jsonb(r)} for r in records
        ]
        with tx() as c:
            upsert_rows(
                "raw_copilot_category",
                raw_rows,
                conflict_cols=["category_id"],
                update_cols=["payload", "fetched_at"],
                connection=c,
            )

            # First pass: insert without parent FK so child-first ordering
            # doesn't break FK constraints.
            dim_rows = [transforms.transform_category(r) for r in records]
            base_rows = [{**r, "parent_category_id": None} for r in dim_rows]
            for r in base_rows:
                r["updated_at"] = datetime.now(timezone.utc)
            upsert_rows("dim_category", base_rows, conflict_cols=["category_id"], connection=c)

            # Second pass: backfill parent_category_id now that all rows exist.
            with c.cursor() as cur:
                for r in dim_rows:
                    if r.get("parent_category_id"):
                        cur.execute(
                            "UPDATE dim_category SET parent_category_id = %s WHERE category_id = %s",
                            [r["parent_category_id"], r["category_id"]],
                        )

        run.upserted(len(dim_rows))
        return len(dim_rows)


def ingest_accounts() -> int:
    with ingestion_run("copilot", "accounts", schema_version=schema_version()) as run:
        with GraphQLClient() as client:
            records = client.accounts()
        run.fetched(len(records))
        if not records:
            return 0

        raw_rows = [
            {"account_id": r["id"], "payload": Jsonb(r)} for r in records
        ]
        with tx() as c:
            upsert_rows(
                "raw_copilot_account",
                raw_rows,
                conflict_cols=["account_id"],
                update_cols=["payload", "fetched_at"],
                connection=c,
            )
            dim_rows = [transforms.transform_account(r) for r in records]
            for r in dim_rows:
                r["updated_at"] = datetime.now(timezone.utc)
            upsert_rows("dim_account", dim_rows, conflict_cols=["account_id"], connection=c)

        run.upserted(len(dim_rows))
        return len(dim_rows)


def run_all(*, backfill_days: int | None = None) -> dict:
    """Order matters slightly: categories + accounts first so the FK on
    fact_transaction.category_id/account_id is satisfied."""
    out: dict[str, int | str] = {}
    for name, fn in [
        ("accounts", lambda: ingest_accounts()),
        ("categories", lambda: ingest_categories()),
        ("transactions", lambda: ingest_transactions(backfill_days=backfill_days)),
    ]:
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            log.exception("copilot.pipeline.failed", pipeline=name)
            out[name] = f"FAILED: {type(e).__name__}: {e}"
    return out


def upsert_transaction_from_api(api: dict) -> dict:
    """Take a Copilot transaction API payload and upsert it into raw + fact.
    Used after mutations: editTransaction returns the post-mutation shape,
    so we can update locally without a separate fetch."""
    raw_row = {"transaction_id": api["id"], "payload": Jsonb(api)}
    with tx() as c:
        upsert_rows(
            "raw_copilot_transaction",
            [raw_row],
            conflict_cols=["transaction_id"],
            update_cols=["payload", "fetched_at"],
            connection=c,
        )
        id_map = _id_map(c, "raw_copilot_transaction", "transaction_id", [api["id"]])
        fact_row = transforms.transform_transaction(api)
        fact_row["raw_id"] = id_map.get(api["id"])
        fact_row["updated_at"] = datetime.now(timezone.utc)
        upsert_rows("fact_transaction", [fact_row], conflict_cols=["transaction_id"], connection=c)
    return fact_row


def refresh_one_transaction(transaction_id: str) -> dict | None:
    """Backwards-compat shim. Most callers should pass the API payload they
    already have to `upsert_transaction_from_api()` instead — it avoids a
    Copilot round-trip whose schema for single-txn-by-id we don't fully
    know. Returns None — local fact won't refresh without an API source."""
    log.warning(
        "copilot.refresh_one_deprecated",
        transaction_id=transaction_id,
        hint="Pass the editTransaction response to upsert_transaction_from_api instead.",
    )
    return None


def _id_map(connection, table: str, key_col: str, keys: list) -> dict:
    if not keys:
        return {}
    with connection.cursor() as cur:
        cur.execute(
            f"SELECT {key_col}, id FROM {table} WHERE {key_col} = ANY(%s)",
            [keys],
        )
        return {row[key_col]: row["id"] for row in cur.fetchall()}
