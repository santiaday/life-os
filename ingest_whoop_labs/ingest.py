"""Whoop Advanced Labs ingestion: file path → raw → fact + dim.

There's no public API for Whoop Advanced Labs results yet, so this module
ingests from a JSON dump captured from the mobile app's network tab. Re-run
it whenever a new panel comes back; existing tests are upserted by test_id.

Pipelines:
  ingest_biomarker_catalog()
      Seeds dim_lab_biomarker from biomarkers.BIOMARKERS — the curated
      reference data (description, optimal/sufficient ranges, what high/low
      means, etc.). Idempotent.

  ingest_lab_panel(path)
      Parse a Whoop labs JSON dump and write:
        - raw_whoop_labs: one row per (test_id, payload).
        - fact_lab_result: one row per (test_id, biomarker_id) with the
          measured value, status, range meter geometry.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from psycopg.types.json import Jsonb

from ingest_whoop_labs import biomarkers as cat
from ingest_whoop_labs import transforms
from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run
from lifeos_core.upsert import upsert_rows

log = get_logger(__name__)


# ---- biomarker catalog -----------------------------------------------------
def ingest_biomarker_catalog() -> int:
    """Push biomarkers.BIOMARKERS into dim_lab_biomarker. Idempotent."""
    with ingestion_run("whoop_labs", "biomarker_catalog") as run:
        rows: list[dict] = []
        now = datetime.now(UTC)
        for biomarker_id, meta in cat.BIOMARKERS.items():
            rows.append({
                "biomarker_id": biomarker_id,
                "title": meta["title"],
                "category": meta["category"],
                "unit": meta.get("unit"),
                "description": meta["description"],
                "optimal_low": meta.get("optimal_low"),
                "optimal_high": meta.get("optimal_high"),
                "sufficient_low": meta.get("sufficient_low"),
                "sufficient_high": meta.get("sufficient_high"),
                "what_high_means": meta.get("what_high_means"),
                "what_low_means": meta.get("what_low_means"),
                "influenced_by": meta.get("influenced_by"),
                "notes": meta.get("notes"),
                "status": "active",
                "updated_at": now,
            })
        run.fetched(len(rows))

        with tx() as c:
            upsert_rows(
                "dim_lab_biomarker",
                rows,
                conflict_cols=["biomarker_id"],
                connection=c,
            )
        run.upserted(len(rows))
        return len(rows)


# ---- panel ingest ----------------------------------------------------------
def ingest_lab_panel(path: str | Path) -> dict:
    """Ingest a Whoop labs JSON dump. Returns counts."""
    path = Path(path)
    with ingestion_run("whoop_labs", "panel", source_path=str(path)) as run:
        payload = json.loads(path.read_text())

        meta = transforms.extract_test_meta(payload)
        if not meta.get("test_id"):
            raise RuntimeError(
                f"Couldn't find test_id in payload at {path}. Payload may "
                "have a different structure than expected."
            )

        # ---- raw upsert ----------------------------------------------------
        with tx() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO raw_whoop_labs (test_id, test_name, test_date, payload, fetched_at)
                VALUES (%s, %s, %s, %s::jsonb, now())
                ON CONFLICT (test_id) DO UPDATE SET
                  test_name = EXCLUDED.test_name,
                  test_date = EXCLUDED.test_date,
                  payload = EXCLUDED.payload,
                  fetched_at = now()
                RETURNING id
                """,
                [
                    meta["test_id"],
                    meta["test_name"],
                    meta["test_date"],
                    json.dumps(payload, default=str),
                ],
            )
            raw_id = cur.fetchone()["id"]

        # ---- biomarker rows ------------------------------------------------
        biomarker_rows = transforms.extract_biomarkers(payload)
        log.info(
            "whoop_labs.parsed",
            test_id=meta["test_id"],
            test_name=meta["test_name"],
            biomarker_count=len(biomarker_rows),
        )

        fact_rows: list[dict] = []
        unknown_ids: list[str] = []
        for raw in biomarker_rows:
            row = transforms.transform_biomarker_row(
                raw,
                test_id=meta["test_id"],
                test_date=meta["test_date"],
                raw_id=raw_id,
            )
            if row is None:
                continue
            if row["biomarker_id"] not in cat.BIOMARKERS:
                unknown_ids.append(row["biomarker_id"])
            row["range_meter"] = Jsonb(row["range_meter"] or {})
            row["updated_at"] = datetime.now(UTC)
            fact_rows.append(row)

        if unknown_ids:
            # Surfacing this in run metadata so we notice if Whoop adds new
            # biomarkers we haven't catalogued yet. The FK will reject the
            # insert otherwise.
            log.warning(
                "whoop_labs.unknown_biomarkers",
                ids=unknown_ids,
                count=len(unknown_ids),
            )
            run.add_metadata(unknown_biomarkers=unknown_ids)

        if fact_rows:
            with tx() as c:
                upsert_rows(
                    "fact_lab_result",
                    fact_rows,
                    conflict_cols=["test_id", "biomarker_id"],
                    update_cols=[
                        "raw_id", "test_date", "value_text", "value_numeric",
                        "unit", "status_type", "trend", "trend_display",
                        "range_meter", "indicator_percent", "source_row_hash",
                        "updated_at",
                    ],
                    connection=c,
                )

        run.fetched(len(biomarker_rows))
        run.upserted(len(fact_rows))
        run.add_metadata(
            test_id=meta["test_id"],
            test_name=meta["test_name"],
            test_date=str(meta["test_date"]) if meta["test_date"] else None,
        )

        return {
            "test_id": meta["test_id"],
            "test_name": meta["test_name"],
            "test_date": str(meta["test_date"]) if meta["test_date"] else None,
            "biomarkers_written": len(fact_rows),
            "unknown_biomarkers": unknown_ids,
        }


def run_all(path: str | Path | None = None) -> dict:
    """Catalog + (optionally) panel. Mirrors the shape of other ingester run_all()s."""
    out: dict = {}
    try:
        out["biomarker_catalog"] = ingest_biomarker_catalog()
    except Exception as e:
        log.exception("whoop_labs.catalog_failed")
        out["biomarker_catalog"] = f"FAILED: {type(e).__name__}: {e}"

    if path is not None:
        try:
            out["panel"] = ingest_lab_panel(path)
        except Exception as e:
            log.exception("whoop_labs.panel_failed")
            out["panel"] = f"FAILED: {type(e).__name__}: {e}"
    return out
