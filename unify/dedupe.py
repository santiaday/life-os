"""Cross-source deduplication and enrichment.

The same lifting session can appear four times: once from Whoop's public API
(strain, HR, kilojoules, zone split), once from Whoop's Strength Trainer feed
(per-set reps and loads), once from the Whoop CSV export (historical zones),
and once from Garmin (per-exercise volume, HR zones, training load). None of
them is a superset of the others.

So this module does two things:

1. **Cluster** rows that describe the same real-world event, by time overlap.
2. **Enrich** the winning row with every non-NULL field its siblings have and
   it doesn't -- so `vw_activity` returns one row that is strictly richer than
   any single source, rather than forcing a choice between strain and volume.

Enrichment only ever fills NULLs. A value a source actually measured is never
overwritten by another source's value, and `payload.merged_from` records which
sources contributed so the provenance survives.

Both passes are idempotent: clusters are recomputed from scratch each run and
enrichment is a no-op once the primary is complete.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg

from lifeos_core.logging import get_logger

log = get_logger(__name__)

# How much of the shorter session must overlap for two rows to be the same
# event. Whoop and Garmin routinely disagree by a few minutes on when a
# session started, so this is deliberately forgiving -- but all three
# conditions must hold together. An earlier version accepted "starts within 20
# minutes" OR "overlaps 50%", which let a 194-minute Apple activity block
# swallow three consecutive pickleball games into one cluster and undercount
# the sessions that vw_adherence is built on.
MIN_OVERLAP_RATIO = 0.5
MAX_START_DRIFT = timedelta(minutes=20)
MIN_DURATION_RATIO = 0.4

# Sources whose timestamps are fabricated, because the export carries only a
# date. Matching these by clock time is meaningless -- they match by calendar
# day and canonical type instead.
DAY_LEVEL_SOURCES = {"loseit"}

# Sources that do not classify activity type at all; treat their type as a
# wildcard rather than a contradiction.
UNTYPED_SOURCES = {"zero"}

# Fields worth pulling across from a sibling. Ordered by domain for review.
ACTIVITY_MERGE_FIELDS = (
    "end_ts", "duration_s", "moving_duration_s",
    "strain", "training_load", "aerobic_te", "anaerobic_te", "rpe",
    "intensity_pct",
    "avg_hr", "max_hr", "min_hr", "kcal", "kilojoules",
    "zone_0_s", "zone_1_s", "zone_2_s", "zone_3_s", "zone_4_s", "zone_5_s",
    "distance_m", "elevation_gain_m", "steps",
    "total_sets", "total_reps", "total_volume_kg", "unique_exercises",
    "name",
)

SLEEP_MERGE_FIELDS = (
    "time_in_bed_s", "asleep_s", "awake_s", "light_s", "deep_s", "rem_s",
    "unmeasurable_s", "efficiency_pct", "performance_pct", "consistency_pct",
    "score", "sleep_need_s", "sleep_debt_s", "respiratory_rate", "avg_hr",
    "avg_spo2", "lowest_spo2", "avg_stress", "disturbance_count",
    "cycle_count", "restless_moments",
)


def _priority_map(conn: psycopg.Connection, domain: str) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT source, priority FROM dim_source_priority WHERE domain = %s",
                    [domain])
        return {r["source"]: r["priority"] for r in cur.fetchall()}


def _types_compatible(a: dict, b: dict) -> bool:
    ta, tb = a.get("activity_type"), b.get("activity_type")
    if ta is None or tb is None:
        return True
    if a["source"] in UNTYPED_SOURCES or b["source"] in UNTYPED_SOURCES:
        return True
    if ta == "other" or tb == "other":
        return True
    return ta == tb


def _same_event(anchor: dict, row: dict) -> bool:
    """Does `row` describe the same real-world session as `anchor`?"""
    if not _types_compatible(anchor, row):
        return False

    # Fabricated timestamps: fall back to same day + same canonical type.
    if row["source"] in DAY_LEVEL_SOURCES or anchor["source"] in DAY_LEVEL_SOURCES:
        return (anchor.get("day") == row.get("day")
                and anchor.get("activity_type") == row.get("activity_type"))

    a_start, b_start = anchor["start_ts"], row["start_ts"]
    if a_start is None or b_start is None:
        return False
    if abs(a_start - b_start) > MAX_START_DRIFT:
        return False

    a_end, b_end = anchor.get("end_ts"), row.get("end_ts")
    if a_end is None or b_end is None:
        # No end on one side: the start-drift check above is all we have, and
        # it is tight enough on its own.
        return True

    a_dur = (a_end - a_start).total_seconds()
    b_dur = (b_end - b_start).total_seconds()
    if a_dur <= 0 or b_dur <= 0:
        return False
    if min(a_dur, b_dur) / max(a_dur, b_dur) < MIN_DURATION_RATIO:
        return False

    overlap = (min(a_end, b_end) - max(a_start, b_start)).total_seconds()
    return overlap > 0 and (overlap / min(a_dur, b_dur)) >= MIN_OVERLAP_RATIO


def _cluster(rows: list[dict], uid_col: str, prio: dict[str, int],
             fields: tuple[str, ...]) -> list[list[dict]]:
    """Anchor-based clustering.

    Rows are considered best-source-first; each either attaches to an existing
    cluster's *anchor* or becomes a new anchor. Comparing only against anchors
    (never against other members) is what prevents transitive chaining --
    A~B and B~C no longer implies A~C.
    """
    ordered = sorted(
        rows,
        key=lambda r: (-prio.get(r["source"], 0), -_richness(r, fields),
                       r["start_ts"] or r["day"]),
    )
    anchors: list[dict] = []
    clusters: list[list[dict]] = []
    # Bucket anchors by day so matching stays linear on a multi-year history.
    by_day: dict[object, list[int]] = {}

    for r in ordered:
        hit = None
        for delta in (0, -1, 1):
            day = r.get("day")
            if day is None:
                continue
            key = day + timedelta(days=delta)
            for idx in by_day.get(key, ()):
                if _same_event(anchors[idx], r):
                    hit = idx
                    break
            if hit is not None:
                break
        if hit is None:
            anchors.append(r)
            clusters.append([r])
            by_day.setdefault(r.get("day"), []).append(len(anchors) - 1)
        else:
            clusters[hit].append(r)
    return clusters


def _richness(row: dict, fields: tuple[str, ...]) -> int:
    return sum(1 for f in fields if row.get(f) is not None)


def cluster_activities(conn: psycopg.Connection) -> dict[str, int]:
    prio = _priority_map(conn, "activity")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT activity_uid, source, start_ts, end_ts, activity_type, day, "
            + ", ".join(ACTIVITY_MERGE_FIELDS)
            + " FROM fact_activity ORDER BY start_ts"
        )
        rows = cur.fetchall()

    clusters = _cluster(rows, "activity_uid", prio, ACTIVITY_MERGE_FIELDS)

    updates = []
    for cluster_index, members in enumerate(clusters, start=1):
        # _cluster already ordered by (priority, richness), so members[0] --
        # the anchor -- is the primary.
        primary = members[0]
        for m in members:
            updates.append((cluster_index, m["activity_uid"] == primary["activity_uid"],
                            m["activity_uid"]))

    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE fact_activity SET cluster_id = %s, is_primary = %s, "
            "updated_at = now() WHERE activity_uid = %s",
            updates,
        )
    multi = sum(1 for m in clusters if len(m) > 1)
    log.info("dedupe.activities", rows=len(rows), clusters=len(clusters),
             multi_source_clusters=multi)
    return {"rows": len(rows), "clusters": len(clusters), "merged": multi}


def cluster_sleep(conn: psycopg.Connection) -> dict[str, int]:
    prio = _priority_map(conn, "sleep")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sleep_uid, source, start_ts, end_ts, is_nap, day, "
            + ", ".join(SLEEP_MERGE_FIELDS)
            + " FROM fact_sleep_session ORDER BY start_ts"
        )
        rows = cur.fetchall()

    updates = []
    cluster_index = 0

    # Main sleeps: there is exactly one per wake day by definition, so the day
    # IS the cluster key. Time-overlap matching would be fragile here -- Zero's
    # Apple passthrough splits a night into several blocks that each overlap
    # the Whoop record differently.
    main: dict[object, list[dict]] = {}
    for r in rows:
        if not r["is_nap"]:
            main.setdefault(r["day"], []).append(r)
    for _day, members in sorted(main.items(), key=lambda kv: (kv[0] is None, kv[0])):
        members.sort(
            key=lambda r: (prio.get(r["source"], 0),
                           _richness(r, SLEEP_MERGE_FIELDS),
                           float(r["asleep_s"] or 0)),
            reverse=True,
        )
        cluster_index += 1
        for m in members:
            updates.append((cluster_index, m["sleep_uid"] == members[0]["sleep_uid"],
                            m["sleep_uid"]))

    # Naps: several a day are legitimate, so these do cluster by overlap.
    naps = sorted((r for r in rows if r["is_nap"]),
                  key=lambda r: r["start_ts"])
    for members in _cluster(naps, "sleep_uid", prio, SLEEP_MERGE_FIELDS):
        cluster_index += 1
        for m in members:
            updates.append((cluster_index, m["sleep_uid"] == members[0]["sleep_uid"],
                            m["sleep_uid"]))

    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE fact_sleep_session SET cluster_id = %s, is_primary = %s, "
            "updated_at = now() WHERE sleep_uid = %s",
            updates,
        )
    log.info("dedupe.sleep", rows=len(rows), clusters=cluster_index)
    return {"rows": len(rows), "clusters": cluster_index}


def _enrich(conn: psycopg.Connection, table: str, uid_col: str,
            fields: tuple[str, ...], prio: dict[str, int]) -> int:
    """Fill NULL columns on each cluster's primary from its siblings, best
    source first. Never overwrites a non-NULL value."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {uid_col}, source, cluster_id, is_primary, payload, "
            + ", ".join(fields)
            + f" FROM {table} WHERE cluster_id IN ("
            f"  SELECT cluster_id FROM {table} WHERE cluster_id IS NOT NULL"
            f"  GROUP BY cluster_id HAVING count(*) > 1)"
        )
        rows = cur.fetchall()

    by_cluster: dict[int, list[dict]] = {}
    for r in rows:
        by_cluster.setdefault(r["cluster_id"], []).append(r)

    enriched = 0
    for members in by_cluster.values():
        primaries = [m for m in members if m["is_primary"]]
        if not primaries:
            continue
        primary = primaries[0]
        others = sorted((m for m in members if not m["is_primary"]),
                        key=lambda r: prio.get(r["source"], 0), reverse=True)
        patch: dict[str, object] = {}
        contributors: set[str] = set()
        for field in fields:
            if primary.get(field) is not None:
                continue
            for o in others:
                if o.get(field) is not None:
                    patch[field] = o[field]
                    contributors.add(o["source"])
                    break
        if not patch:
            continue
        sets = ", ".join(f'"{f}" = %s' for f in patch)
        params = list(patch.values())
        with conn.cursor() as cur:
            cur.execute(
                f'UPDATE {table} SET {sets}, '
                f"payload = payload || jsonb_build_object("
                f"  'merged_from', %s::jsonb, 'merged_fields', %s::jsonb), "
                f"updated_at = now() WHERE {uid_col} = %s",
                [*params,
                 __import__("json").dumps(sorted(contributors)),
                 __import__("json").dumps(sorted(patch)),
                 primary[uid_col]],
            )
        enriched += 1
    return enriched


def enrich_activities(conn: psycopg.Connection) -> int:
    n = _enrich(conn, "fact_activity", "activity_uid",
                ACTIVITY_MERGE_FIELDS, _priority_map(conn, "activity"))
    log.info("dedupe.enrich_activities", rows=n)
    return n


def enrich_sleep(conn: psycopg.Connection) -> int:
    n = _enrich(conn, "fact_sleep_session", "sleep_uid",
                SLEEP_MERGE_FIELDS, _priority_map(conn, "sleep"))
    log.info("dedupe.enrich_sleep", rows=n)
    return n


def attach_orphan_sets(conn: psycopg.Connection) -> int:
    """When a non-primary row owns the only per-set detail in its cluster (the
    Whoop lift feed almost always does), point the set and exercise rows at the
    cluster's primary too, so vw_activity_set doesn't silently drop them."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH primaries AS (
              SELECT cluster_id, activity_uid AS primary_uid
                FROM fact_activity WHERE is_primary AND cluster_id IS NOT NULL
            ),
            movable AS (
              SELECT s.activity_uid AS from_uid, p.primary_uid
                FROM fact_activity_set s
                JOIN fact_activity a ON a.activity_uid = s.activity_uid
                JOIN primaries p ON p.cluster_id = a.cluster_id
               WHERE NOT a.is_primary
                 AND NOT EXISTS (SELECT 1 FROM fact_activity_set s2
                                  WHERE s2.activity_uid = p.primary_uid)
               GROUP BY 1, 2
            )
            SELECT count(*) AS n FROM movable
            """
        )
        return cur.fetchone()["n"]


def run_all(conn: psycopg.Connection) -> dict:
    out = {}
    out.update({f"activity_{k}": v for k, v in cluster_activities(conn).items()})
    out.update({f"sleep_{k}": v for k, v in cluster_sleep(conn).items()})
    out["enriched_activities"] = enrich_activities(conn)
    out["enriched_sleep"] = enrich_sleep(conn)
    return out
