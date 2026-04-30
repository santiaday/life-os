"""Pure transforms from a Whoop Advanced Labs JSON payload to row dicts.

Whoop's payload is a UI screen description. The interesting data lives in
two places:

  sections[0].items[0].content.current_test
      → test_id, panel name, test_date label.

  sections[3].items[0].content.biomarkers[]
      → list of 75 biomarker rows: id, title, value (string), unit,
        status_type, trend, range_meter (normalized 0–1 sections + indicator),
        plus the disclaimer/destination/icon fluff we discard.

Range bounds are NOT in absolute units in the JSON — only normalized 0-1
positions on the meter. So we store the meter geometry verbatim (so the UI
or a future tool can reconstruct the picker) and rely on dim_lab_biomarker
for absolute clinical reference ranges.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any

# Format Whoop uses on the test card: "Jan 20, 2026"
_TEST_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y")


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    for fmt in _TEST_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _safe_float(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def extract_test_meta(payload: dict) -> dict:
    """Pull test_id, name, date from the navigation/header tiles."""
    sections = payload.get("sections") or []

    # Section 0 has the test selection card.
    test_id = None
    test_name = None
    test_date = None
    for sec in sections:
        for item in sec.get("items") or []:
            content = item.get("content") or {}
            ct = content.get("current_test") if isinstance(content, dict) else None
            if isinstance(ct, dict):
                # Whoop wraps the actual UUID with a "current_test_id" prefix.
                raw_id = ct.get("id") or ""
                if raw_id.startswith("current_test_id"):
                    test_id = raw_id[len("current_test_id"):]
                else:
                    test_id = raw_id or None
                test_name = ct.get("title")
                test_date = _parse_date(ct.get("subtitle"))
                break
        if test_id:
            break

    return {
        "test_id": test_id,
        "test_name": test_name,
        "test_date": test_date,
    }


def extract_biomarkers(payload: dict) -> list[dict]:
    """Pull the biomarker list out of the searchable list section."""
    for sec in payload.get("sections") or []:
        for item in sec.get("items") or []:
            if item.get("type") != "SEARCHABLE_BIOMARKERS_LIST":
                continue
            content = item.get("content") or {}
            return list(content.get("biomarkers") or [])
    return []


def transform_biomarker_row(
    raw: dict[str, Any],
    *,
    test_id: str,
    test_date: date | None,
    raw_id: int | None,
) -> dict | None:
    """Map one biomarker entry → fact_lab_result row dict."""
    biomarker_id = raw.get("id")
    if not biomarker_id:
        return None

    value_text = raw.get("value")
    value_numeric = _safe_float(value_text)
    trend = raw.get("trend") or {}
    rm = raw.get("range_meter") or {}

    # Stable hash so re-ingest is idempotent and (test_id, biomarker_id) trips
    # the unique constraint cleanly.
    hash_src = f"{test_id}|{biomarker_id}|{value_text}|{raw.get('status_type')}"
    source_row_hash = hashlib.sha256(hash_src.encode()).hexdigest()

    return {
        "raw_id": raw_id,
        "test_id": test_id,
        "test_date": test_date,
        "biomarker_id": biomarker_id,
        "value_text": value_text,
        "value_numeric": value_numeric,
        "unit": raw.get("unit"),
        "status_type": raw.get("status_type"),
        "trend": trend.get("trend"),
        "trend_display": trend.get("title_display"),
        "range_meter": rm,
        "indicator_percent": rm.get("indicator_percent"),
        "source_row_hash": source_row_hash,
    }
