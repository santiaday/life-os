"""External lab submission.

Lets the user hand over a lab report (PDF / printout / values) that didn't come
through Whoop and port it straight into the warehouse: writes raw_external_lab +
dim_lab_biomarker stubs + fact_lab_result(source='external'). It lands in the
SAME fact_lab_result table as Whoop Advanced Labs, so get_lab_results /
get_biomarker_info / correlate_metrics see it transparently.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime

from psycopg.types.json import Jsonb

from lifeos_core.db import tx
from lifeos_core.upsert import upsert_rows


def _slug(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _status_from_ranges(value, opt_lo, opt_hi, suf_lo, suf_hi) -> str | None:
    """Derive a status when the user didn't supply one but gave ranges."""
    v = _num(value)
    if v is None:
        return None
    if opt_lo is not None and opt_hi is not None and opt_lo <= v <= opt_hi:
        return "OPTIMAL"
    if suf_lo is not None and suf_hi is not None and suf_lo <= v <= suf_hi:
        return "SUFFICIENT"
    if opt_lo is not None or opt_hi is not None or suf_lo is not None or suf_hi is not None:
        return "OUT_OF_RANGE"
    return None


def submit_lab_results(
    test_name: str,
    test_date: str,
    biomarkers: list[dict],
    provider: str | None = None,
) -> dict:
    """Port an external lab test into the warehouse.

    biomarkers: list of dicts, each with:
      biomarker_id (slug, optional — derived from name if omitted),
      name/display_name (str), value (number, required), unit (str, optional),
      status (OPTIMAL|SUFFICIENT|OUT_OF_RANGE, optional — derived from ranges),
      optimal_low/optimal_high/sufficient_low/sufficient_high (numbers, optional).
    """
    if not biomarkers:
        return {"ok": False, "error": "no biomarkers provided"}
    td = date.fromisoformat(test_date) if isinstance(test_date, str) else test_date
    test_id = f"external-{_slug(test_name)}-{td.isoformat()}"
    now = datetime.now(UTC)

    fact_rows: list[dict] = []
    stubs: list[tuple] = []
    skipped: list[str] = []
    for b in biomarkers:
        name = b.get("display_name") or b.get("name")
        bid = b.get("biomarker_id") or _slug(name)
        value = b.get("value")
        if not bid or value is None:
            skipped.append(name or bid or "?")
            continue
        opt_lo, opt_hi = _num(b.get("optimal_low")), _num(b.get("optimal_high"))
        suf_lo, suf_hi = _num(b.get("sufficient_low")), _num(b.get("sufficient_high"))
        status = b.get("status") or _status_from_ranges(value, opt_lo, opt_hi, suf_lo, suf_hi)
        ranges = {
            "optimal": {"lower_endpoint": opt_lo, "upper_endpoint": opt_hi},
            "sufficient": {"lower_endpoint": suf_lo, "upper_endpoint": suf_hi},
        }
        fact_rows.append({
            "raw_id": None,
            "test_id": test_id,
            "test_date": td,
            "biomarker_id": bid,
            "value_text": str(value),
            "value_numeric": _num(value),
            "unit": b.get("unit"),
            "status_type": status,
            "reference_ranges": Jsonb(ranges),
            "source": "external",
            "lab_provider": provider,
            "source_row_hash": hashlib.sha256(
                f"{test_id}|{bid}|{value}|{status}".encode()
            ).hexdigest(),
            "updated_at": now,
        })
        stubs.append((bid, name or bid, b.get("unit"), opt_lo, opt_hi, suf_lo, suf_hi))

    if not fact_rows:
        return {"ok": False, "error": "no valid biomarkers (need biomarker name + value)"}

    with tx() as c:
        with c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO raw_external_lab (test_id, test_name, test_date, provider, payload)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (test_id) DO UPDATE SET
                  test_name = EXCLUDED.test_name, test_date = EXCLUDED.test_date,
                  provider = EXCLUDED.provider, payload = EXCLUDED.payload, fetched_at = now()
                """,
                [test_id, test_name, td, provider,
                 json.dumps({"test_name": test_name, "biomarkers": biomarkers}, default=str)],
            )
            for bid, title, unit, olo, ohi, slo, shi in stubs:
                cur.execute(
                    """
                    INSERT INTO dim_lab_biomarker
                        (biomarker_id, title, category, unit,
                         optimal_low, optimal_high, sufficient_low, sufficient_high,
                         status, updated_at)
                    VALUES (%s, %s, 'external', %s, %s, %s, %s, %s, 'active', now())
                    ON CONFLICT (biomarker_id) DO NOTHING
                    """,
                    [bid, title, unit, olo, ohi, slo, shi],
                )
        upsert_rows(
            "fact_lab_result", fact_rows,
            conflict_cols=["test_id", "biomarker_id"],
            update_cols=["raw_id", "test_date", "value_text", "value_numeric",
                         "unit", "status_type", "reference_ranges", "source",
                         "lab_provider", "source_row_hash", "updated_at"],
            connection=c,
        )

    return {
        "ok": True,
        "test_id": test_id,
        "test_name": test_name,
        "test_date": td.isoformat(),
        "biomarkers_written": len(fact_rows),
        "skipped": skipped,
    }
