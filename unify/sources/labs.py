"""fact_lab_result / fact_imaging_study -> unified lab + imaging layer.

Panels from different reporters that describe the same draw are clustered by
(collected_on, provider) and the richer one becomes primary. Measurements
resolve to a canonical `biomarker_key`, so `alt` from the Quest report and
`alanine_aminotransferase` from Whoop Advanced Labs land on the same row of
any trend query.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg

from lifeos_core.logging import get_logger
from unify import biomarkers
from unify.common import jsonb, num, uid, upsert

log = get_logger(__name__)


def seed_biomarkers(conn: psycopg.Connection) -> dict[str, int]:
    n_b = n_a = 0
    with conn.cursor() as cur:
        for b in biomarkers.BIOMARKERS:
            cur.execute(
                """
                INSERT INTO dim_biomarker (biomarker_key, display_name, category,
                    canonical_unit, ref_low, ref_high, higher_is_better)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (biomarker_key) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    category = EXCLUDED.category,
                    canonical_unit = EXCLUDED.canonical_unit,
                    ref_low = EXCLUDED.ref_low,
                    ref_high = EXCLUDED.ref_high,
                    higher_is_better = EXCLUDED.higher_is_better,
                    updated_at = now()
                """,
                [b.key, b.name, b.category, b.unit, b.ref_low, b.ref_high,
                 b.higher_is_better],
            )
            n_b += 1
        for alias, key in biomarkers.ALIAS_TO_KEY.items():
            cur.execute(
                "INSERT INTO dim_biomarker_alias (alias, biomarker_key) "
                "VALUES (%s,%s) ON CONFLICT (alias) DO UPDATE "
                "SET biomarker_key = EXCLUDED.biomarker_key, updated_at = now()",
                [alias, key],
            )
            n_a += 1
    return {"biomarkers": n_b, "aliases": n_a}


def project_labs(conn: psycopg.Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM fact_lab_result WHERE test_date IS NOT NULL")
        rows = cur.fetchall()

    now = datetime.now(UTC)
    panels: dict[str, dict] = {}
    measurements = []
    unmapped: set[str] = set()

    for r in rows:
        panel_uid = uid(r["source"], r["test_id"])
        p = panels.setdefault(panel_uid, {
            "panel_uid": panel_uid,
            "source": r["source"],
            "source_panel_id": r["test_id"],
            "collected_on": r["test_date"],
            "reported_on": None,
            "provider": (r["lab_provider"] or r["source"]),
            "ordering_provider": None,
            "panel_name": None,
            "result_count": 0,
            "cluster_id": None,
            "is_primary": True,
            "raw_id": r["raw_id"],
            "payload": jsonb({}),
            "updated_at": now,
        })
        p["result_count"] += 1

        key, known = biomarkers.resolve(r["biomarker_id"])
        if not known:
            unmapped.add(r["biomarker_id"])

        refs = r["reference_ranges"] or {}
        ref_low = num(refs.get("low") if isinstance(refs, dict) else None)
        ref_high = num(refs.get("high") if isinstance(refs, dict) else None)
        value = num(r["value_numeric"])

        measurements.append({
            "measurement_uid": uid(r["source"], r["test_id"], r["biomarker_id"]),
            "panel_uid": panel_uid,
            "source": r["source"],
            "collected_on": r["test_date"],
            "biomarker_key": key,
            "vendor_key": r["biomarker_id"],
            "display_name": (biomarkers.BY_KEY[key].name if key else r["biomarker_id"]),
            "value_numeric": value,
            "value_text": r["value_text"],
            "unit": r["unit"] or (biomarkers.BY_KEY[key].unit if key else None),
            "ref_low": ref_low,
            "ref_high": ref_high,
            "optimal_low": None,
            "optimal_high": None,
            "status": (r["status_type"]
                       or (biomarkers.status_for(key, value, ref_low, ref_high)
                           if key else None)),
            "is_primary": True,
            "raw_id": r["raw_id"],
            "payload": jsonb({"lab_provider": r["lab_provider"],
                              "trend": r["trend"]}),
            "updated_at": now,
        })

    n_p = upsert(conn, "fact_lab_panel", list(panels.values()),
                 conflict=["panel_uid"])
    n_m = upsert(conn, "fact_lab_measurement", measurements,
                 conflict=["measurement_uid"])
    if unmapped:
        log.warning("labs.unmapped_biomarkers", count=len(unmapped),
                    sample=sorted(unmapped)[:15])
    log.info("labs.project", panels=n_p, measurements=n_m,
             unmapped=len(unmapped))
    return {"panels": n_p, "measurements": n_m, "unmapped": len(unmapped)}


def cluster_panels(conn: psycopg.Connection) -> dict[str, int]:
    """Two panels are the same draw when they share a collection date and the
    same physical lab. The one with more resolved biomarkers wins; its
    measurements stay primary and the duplicate's are demoted so trend queries
    don't count a value twice."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.panel_uid, p.collected_on, p.provider, p.source,
                   count(m.measurement_uid) FILTER (WHERE m.biomarker_key IS NOT NULL)
                     AS mapped_count
              FROM fact_lab_panel p
              LEFT JOIN fact_lab_measurement m ON m.panel_uid = p.panel_uid
             GROUP BY p.panel_uid, p.collected_on, p.provider, p.source
             ORDER BY p.collected_on
            """
        )
        panels = cur.fetchall()

    def norm_provider(p: str | None) -> str:
        s = (p or "").lower()
        for needle in ("quest", "labcorp", "whoop", "function", "biograph"):
            if needle in s:
                return needle
        return s[:20] or "unknown"

    groups: dict[tuple, list[dict]] = {}
    for p in panels:
        groups.setdefault((p["collected_on"], norm_provider(p["provider"])), []).append(p)

    updates_panel, updates_meas = [], []
    for i, ((_, _), members) in enumerate(sorted(groups.items(),
                                                 key=lambda kv: kv[0][0]), start=1):
        members.sort(key=lambda p: p["mapped_count"], reverse=True)
        primary = members[0]
        for m in members:
            is_primary = m["panel_uid"] == primary["panel_uid"]
            updates_panel.append((i, is_primary, m["panel_uid"]))
            updates_meas.append((is_primary, m["panel_uid"]))

    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE fact_lab_panel SET cluster_id = %s, is_primary = %s, "
            "updated_at = now() WHERE panel_uid = %s", updates_panel)
        cur.executemany(
            "UPDATE fact_lab_measurement SET is_primary = %s, updated_at = now() "
            "WHERE panel_uid = %s", updates_meas)

    multi = sum(1 for m in groups.values() if len(m) > 1)
    log.info("labs.cluster", groups=len(groups), duplicate_draws=multi)
    return {"draws": len(groups), "duplicate_draws": multi}


def project_imaging(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM fact_imaging_study WHERE study_date IS NOT NULL")
        rows = cur.fetchall()
    out = [{
        "study_uid": uid(r["source"], r["study_id"]),
        "source": r["source"],
        "study_date": r["study_date"],
        "modality": (r["modality"] or "OTHER").upper(),
        "body_region": r["body_region"],
        "laterality": None,
        "provider": r["provider"],
        "ordering_reason": r["ordering_reason"],
        "impression": r["impression"],
        "findings": jsonb(r["findings"] or []),
        "measurements": jsonb({}),
        "raw_text": r["raw_text"],
        "report_url": None,
        "raw_id": r["raw_id"],
        "updated_at": datetime.now(UTC),
    } for r in rows]
    n = upsert(conn, "fact_imaging", out, conflict=["study_uid"])
    log.info("labs.project_imaging", rows=n)
    return n


def project_all(conn: psycopg.Connection) -> dict:
    out = seed_biomarkers(conn)
    out.update(project_labs(conn))
    out.update(cluster_panels(conn))
    out["imaging"] = project_imaging(conn)
    return out
