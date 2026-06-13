"""Native Whoop Advanced Labs ingestion.

Replaces the manual JSON-capture path (ingest_whoop_labs) with the live
endpoints, which also cover external tests the user uploaded into Whoop:

  GET /advanced-labs-service/v1/biomarker-tests            -> list of tests
  GET /advanced-labs-service/v1/biomarker-tests/{id}/summary -> flat biomarkers[]

Each test's biomarkers[] carries biomarker_name (slug id), value (numeric),
units, status, and absolute optimal/sufficient/out-of-range bounds. We upsert
raw_whoop_labs + fact_lab_result, auto-stubbing dim_lab_biomarker for any marker
not already curated (Whoop's catalog is wider than biomarkers.py and external
panels carry their own markers), without clobbering the rich curated rows.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime

from psycopg.types.json import Jsonb

from ingest_whoop_private.client import WhoopPrivateClient
from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run
from lifeos_core.upsert import upsert_rows

log = get_logger(__name__)

_STATUS_REAL = {"OPTIMAL", "SUFFICIENT", "OUT_OF_RANGE"}


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def transform_labs_summary(
    payload: dict, *, source: str = "whoop", lab_provider: str | None = None
) -> tuple[dict | None, list[dict], list[dict]]:
    """One /biomarker-tests/{id}/summary payload -> (test_meta, fact_rows,
    dim_stub_rows). Skips UNAVAILABLE (untested) markers."""
    resp = payload.get("response", payload) if isinstance(payload, dict) else {}
    test_id = resp.get("biomarker_test_id")
    if not test_id:
        return None, [], []
    test_date = _parse_iso_date(resp.get("test_date"))
    meta = {
        "test_id": test_id,
        "test_name": resp.get("test_display_name"),
        "test_date": test_date,
        "source": source,
        "lab_provider": lab_provider,
    }

    fact_rows: list[dict] = []
    dim_stubs: list[dict] = []
    for b in resp.get("biomarkers") or []:
        if b.get("status") not in _STATUS_REAL:
            continue
        bid = b.get("biomarker_name")
        if not bid:
            continue
        value = b.get("value")
        opt = b.get("optimal_range") or {}
        suf = b.get("sufficient_range") or {}
        ranges = {
            "optimal": b.get("optimal_range"),
            "sufficient": b.get("sufficient_range"),
            "out_of_range": b.get("out_of_range"),
        }
        hash_src = f"{test_id}|{bid}|{value}|{b.get('status')}"
        fact_rows.append({
            "test_id": test_id,
            "test_date": test_date,
            "biomarker_id": bid,
            "value_text": str(value) if value is not None else None,
            "value_numeric": _num(value),
            "unit": b.get("units"),
            "status_type": b.get("status"),
            "reference_ranges": Jsonb(ranges),
            "source": source,
            "lab_provider": lab_provider,
            "source_row_hash": hashlib.sha256(hash_src.encode()).hexdigest(),
        })
        dim_stubs.append({
            "biomarker_id": bid,
            "title": b.get("biomarker_display_name") or bid,
            "unit": b.get("units"),
            "optimal_low": _num(opt.get("lower_endpoint")),
            "optimal_high": _num(opt.get("upper_endpoint")),
            "sufficient_low": _num(suf.get("lower_endpoint")),
            "sufficient_high": _num(suf.get("upper_endpoint")),
        })
    return meta, fact_rows, dim_stubs


def _stub_biomarkers(c, stubs: list[dict]) -> None:
    """Insert dim_lab_biomarker stubs, never overwriting curated rows."""
    if not stubs:
        return
    with c.cursor() as cur:
        for s in stubs:
            cur.execute(
                """
                INSERT INTO dim_lab_biomarker
                    (biomarker_id, title, category, unit,
                     optimal_low, optimal_high, sufficient_low, sufficient_high,
                     status, updated_at)
                VALUES (%s, %s, 'uncategorized', %s, %s, %s, %s, %s, 'active', now())
                ON CONFLICT (biomarker_id) DO NOTHING
                """,
                [s["biomarker_id"], s["title"], s["unit"],
                 s["optimal_low"], s["optimal_high"],
                 s["sufficient_low"], s["sufficient_high"]],
            )


def _upsert_test(c, meta: dict, payload: dict, fact_rows: list[dict]) -> int:
    """Upsert raw_whoop_labs + dim stubs + fact_lab_result for one test."""
    with c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_whoop_labs (test_id, test_name, test_date, payload, fetched_at)
            VALUES (%s, %s, %s, %s::jsonb, now())
            ON CONFLICT (test_id) DO UPDATE SET
              test_name = EXCLUDED.test_name, test_date = EXCLUDED.test_date,
              payload = EXCLUDED.payload, fetched_at = now()
            RETURNING id
            """,
            [meta["test_id"], meta["test_name"], meta["test_date"],
             json.dumps(payload, default=str)],
        )
        raw_id = cur.fetchone()["id"]
    now = datetime.now(UTC)
    for r in fact_rows:
        r["raw_id"] = raw_id
        r["updated_at"] = now
    if fact_rows:
        upsert_rows(
            "fact_lab_result", fact_rows,
            conflict_cols=["test_id", "biomarker_id"],
            update_cols=["raw_id", "test_date", "value_text", "value_numeric",
                         "unit", "status_type", "reference_ranges", "source",
                         "lab_provider", "source_row_hash", "updated_at"],
            connection=c,
        )
    return len(fact_rows)


def ingest_labs(client: WhoopPrivateClient) -> int:
    """Pull every Advanced Labs test + its biomarkers into the warehouse."""
    with ingestion_run("whoop_private", "labs") as run:
        listing = client.labs_tests()
        records = (listing.get("records") if isinstance(listing, dict) else None) or []
        run.fetched(len(records))
        written = 0
        tests = 0
        errors: dict[str, str] = {}
        for rec in records:
            test_id = rec.get("id")
            if not test_id:
                continue
            provider = rec.get("upload_source") or rec.get("test_source")
            try:
                summary = client.labs_summary(test_id)
            except Exception as e:
                errors[test_id] = f"{type(e).__name__}: {e}"
                continue
            meta, fact_rows, dim_stubs = transform_labs_summary(
                summary, source="whoop", lab_provider=provider
            )
            if meta is None or not fact_rows:
                continue
            with tx() as c:
                _stub_biomarkers(c, dim_stubs)
                written += _upsert_test(c, meta, summary, fact_rows)
            tests += 1
        run.upserted(written)
        run.add_metadata(tests=tests, biomarkers=written)
        if errors:
            run.add_metadata(errors=errors)
        return written
