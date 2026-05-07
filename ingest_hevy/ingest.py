"""Hevy ingestion: API → raw_hevy_workout → fact_strength_set + fact_strength_workout.

Default flow on the daily cron:

  1. Compute `since` from last_successful_run('hevy', 'workout') minus a
     small overlap (Hevy can edit older workouts when the band syncs).
  2. Try GET /workouts/events?since=<ts>. For each event:
       - 'updated' / 'created':  fetch full workout, upsert raw, derive sets+rollup
       - 'deleted':              mark raw_hevy_workout.deleted = true (cascades
                                 to fact_* via ON DELETE CASCADE? — no, deleted is a
                                 soft tombstone; we hard-delete fact rows so reads
                                 stay clean)
  3. If /workouts/events returns nothing on a fresh sync (or a fresh
     install with no prior cursor), fall back to paginating /workouts and
     stop once we hit a workout older than `since`.
  4. Recompute fact_strength_workout rollups for affected workouts.
  5. Link to fact_workout (Whoop) by ±10/15min start/end window match.
  6. Once a week, refresh dim_hevy_exercise from /exercise_templates.

run_all() is what the scheduler / refresh_data tool calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from psycopg.types.json import Jsonb

from ingest_hevy import transforms
from ingest_hevy.client import HevyClient
from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run, last_successful_run
from lifeos_core.upsert import upsert_rows

log = get_logger(__name__)


# How far before last-success to re-pull, to catch late edits / per-set tweaks
# Hevy users sometimes make on the phone hours after the session.
DEFAULT_INCREMENTAL_LOOKBACK_DAYS = 2
DEFAULT_BACKFILL_DAYS = 30
DEFAULT_FIRST_RUN_DAYS = 30          # if no prior successful run

# ±N-minute tolerance for the Whoop ↔ Hevy time-window match. Whoop starts
# the workout when the user taps the band; Hevy starts when the user opens
# the workout in-app — usually a few minutes apart.
WHOOP_LINK_START_TOLERANCE = timedelta(minutes=10)
WHOOP_LINK_END_TOLERANCE = timedelta(minutes=15)


# ---- top-level orchestration -----------------------------------------------
def run_all(
    *,
    backfill_days: int | None = None,
    refresh_catalog: bool = False,
    refresh_routines: bool = True,
) -> dict:
    """Run the full Hevy pipeline. Each phase is its own ingestion_runs row
    so a failure in one doesn't mask the others.

    Routines + folders are pulled by default — they're cheap (small payloads,
    one paginated list each) and they keep the local routine catalog warm
    so MCP tool calls don't need to round-trip Hevy."""
    out: dict[str, int | str] = {}
    with HevyClient() as client:
        try:
            out["workout"] = ingest_workouts(client, backfill_days=backfill_days)
        except Exception as e:  # noqa: BLE001
            log.exception("hevy.workout.failed")
            out["workout"] = f"FAILED: {type(e).__name__}: {e}"

        if refresh_routines:
            try:
                out["routine_folders"] = ingest_routine_folders(client)
            except Exception as e:  # noqa: BLE001
                log.exception("hevy.folders.failed")
                out["routine_folders"] = f"FAILED: {type(e).__name__}: {e}"
            try:
                out["routines"] = ingest_routines(client)
            except Exception as e:  # noqa: BLE001
                log.exception("hevy.routines.failed")
                out["routines"] = f"FAILED: {type(e).__name__}: {e}"

        if refresh_catalog:
            try:
                out["exercise_templates"] = ingest_exercise_templates(client)
            except Exception as e:  # noqa: BLE001
                log.exception("hevy.catalog.failed")
                out["exercise_templates"] = f"FAILED: {type(e).__name__}: {e}"
    return out


# ---- routines + folders ---------------------------------------------------
def ingest_routines(client: HevyClient) -> int:
    """Full re-pull of all routines. Routines change rarely; full sync is
    fine."""
    with ingestion_run("hevy", "routines") as run:
        rows: list[dict] = []
        for r in client.routines():
            updated = transforms.parse_ts(r.get("updated_at"))
            rows.append({
                "hevy_routine_id": r["id"],
                "payload": Jsonb(r),
                "updated_at_src": updated,
                "title": r.get("title"),
                "folder_id": r.get("folder_id"),
                "deleted": False,
            })
        run.fetched(len(rows))
        if not rows:
            return 0
        upsert_rows(
            "raw_hevy_routine",
            rows,
            conflict_cols=["hevy_routine_id"],
            update_cols=[
                "payload", "updated_at_src", "title", "folder_id",
                "deleted", "fetched_at",
            ],
        )
        run.upserted(len(rows))
        return len(rows)


def ingest_routine_folders(client: HevyClient) -> int:
    """Refresh the routine_folders catalog. Tiny (per-user count is in
    single digits typically)."""
    with ingestion_run("hevy", "routine_folders") as run:
        rows: list[dict] = []
        for f in client.routine_folders():
            rows.append({
                "folder_id": int(f["id"]),
                "title": f.get("title") or "(untitled)",
                "index": f.get("index"),
                "payload": Jsonb(f),
            })
        run.fetched(len(rows))
        if not rows:
            return 0
        upsert_rows(
            "raw_hevy_routine_folder",
            rows,
            conflict_cols=["folder_id"],
            update_cols=["title", "index", "payload", "fetched_at"],
        )
        run.upserted(len(rows))
        return len(rows)


# ---- workouts ---------------------------------------------------------------
def ingest_workouts(
    client: HevyClient,
    *,
    backfill_days: int | None = None,
) -> int:
    """Pull new/changed workouts and rebuild the derived fact rows.

    Returns the number of workouts upserted (excluding pure deletions)."""
    since = _since_cursor(backfill_days)
    end = datetime.now(timezone.utc)

    with ingestion_run(
        "hevy", "workout", start=since.isoformat(), end=end.isoformat()
    ) as run:
        # Pass 1 — events feed (preferred path).
        events_seen = 0
        upserted_ids: set[str] = set()
        deleted_ids: set[str] = set()
        # Keyed by workout_id so the fallback path can stash full payloads
        # straight from /workouts (avoiding the per-id GET round-trip).
        payloads: dict[str, dict] = {}
        try:
            for evt in client.workout_events(since=since.isoformat()):
                events_seen += 1
                evt_type = (evt.get("type") or "").lower()
                workout = evt.get("workout") or {}
                wid = workout.get("id") or evt.get("workout_id") or evt.get("id")
                if not wid:
                    continue
                if evt_type in {"deleted", "delete", "removed"}:
                    deleted_ids.add(wid)
                else:
                    # 'updated' | 'created' | unknown → treat as upsert.
                    upserted_ids.add(wid)
                    # Some Hevy events carry the full workout payload inline;
                    # if so, stash it to skip the per-id GET below.
                    if workout.get("exercises") is not None and workout.get("start_time"):
                        payloads[wid] = workout
        except Exception as e:  # noqa: BLE001
            # Events endpoint is the preferred path but not load-bearing.
            log.warning("hevy.events.failed", error=str(e))
            run.add_metadata(events_failed=str(e))

        # Pass 2 — fall back to /workouts pagination if the events feed gave
        # us nothing useful (fresh user, endpoint hiccup, or first-ever run).
        if events_seen == 0:
            log.info("hevy.events.empty_falling_back_to_pagination")
            for w in client.workouts():
                w_updated = transforms.parse_ts(w.get("updated_at")) or transforms.parse_ts(
                    w.get("start_time")
                )
                if w_updated and w_updated < since:
                    break
                wid = w["id"]
                upserted_ids.add(wid)
                payloads[wid] = w  # /workouts returns full payloads already

        # Fetch any payloads we don't already have stashed.
        for wid in upserted_ids:
            if wid in payloads:
                continue
            try:
                payloads[wid] = client.workout(wid)
            except Exception as e:  # noqa: BLE001
                log.warning("hevy.workout.fetch_failed", workout_id=wid, error=str(e))

        run.fetched(len(payloads) + len(deleted_ids))
        if not payloads and not deleted_ids:
            return 0

        # ---- write phase --------------------------------------------------
        with tx() as c:
            # Upserts: raw → fact_strength_set (DELETE+INSERT per workout) →
            # fact_strength_workout rollup. All in one transaction so a
            # later failure rolls everything back.
            raw_rows = []
            for wid, payload in payloads.items():
                updated_src = transforms.parse_ts(payload.get("updated_at"))
                raw_rows.append({
                    "hevy_workout_id": wid,
                    "payload": Jsonb(payload),
                    "updated_at_src": updated_src,
                    "deleted": False,
                })
            if raw_rows:
                upsert_rows(
                    "raw_hevy_workout",
                    raw_rows,
                    conflict_cols=["hevy_workout_id"],
                    update_cols=["payload", "updated_at_src", "deleted", "fetched_at"],
                    connection=c,
                )
                # Resolve raw_id back-references for the workout rollups.
                raw_ids = _id_map(
                    c,
                    "raw_hevy_workout",
                    "hevy_workout_id",
                    [r["hevy_workout_id"] for r in raw_rows],
                )

                # Rebuild fact_strength_set per workout.
                set_total = 0
                rollup_rows: list[dict] = []
                for wid, payload in payloads.items():
                    set_rows = transforms.explode_workout_sets(payload)
                    _replace_workout_sets(c, wid, set_rows)
                    set_total += len(set_rows)

                    rollup = transforms.rollup_workout(payload, set_rows)
                    if rollup is None:
                        log.warning("hevy.rollup.skipped_no_ts", workout_id=wid)
                        continue
                    rollup["raw_id"] = raw_ids.get(wid)
                    rollup["updated_at"] = datetime.now(timezone.utc)
                    rollup["whoop_workout_id"] = _match_whoop_workout(
                        c, rollup["start_ts"], rollup["end_ts"]
                    )
                    rollup_rows.append(rollup)

                if rollup_rows:
                    upsert_rows(
                        "fact_strength_workout",
                        rollup_rows,
                        conflict_cols=["hevy_workout_id"],
                        connection=c,
                    )

                run.add_metadata(sets_written=set_total, rollups=len(rollup_rows))

            # Deletions: mark raw rows as tombstones; cascade-clear fact rows
            # so reads stay clean.
            if deleted_ids:
                with c.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE raw_hevy_workout
                           SET deleted = TRUE, fetched_at = now()
                         WHERE hevy_workout_id = ANY(%s)
                        """,
                        [list(deleted_ids)],
                    )
                    cur.execute(
                        "DELETE FROM fact_strength_workout WHERE hevy_workout_id = ANY(%s)",
                        [list(deleted_ids)],
                    )
                    cur.execute(
                        "DELETE FROM fact_strength_set WHERE hevy_workout_id = ANY(%s)",
                        [list(deleted_ids)],
                    )
                run.add_metadata(deleted=len(deleted_ids))

        run.upserted(len(payloads))
        return len(payloads)


# ---- exercise template catalog --------------------------------------------
def ingest_exercise_templates(client: HevyClient) -> int:
    """Refresh dim_hevy_exercise. Idempotent — full table rewrite via upsert."""
    with ingestion_run("hevy", "exercise_templates") as run:
        rows = []
        for tmpl in client.exercise_templates():
            r = transforms.transform_exercise_template(tmpl)
            r["payload"] = Jsonb(tmpl)
            rows.append(r)
        run.fetched(len(rows))
        if not rows:
            return 0
        upsert_rows(
            "dim_hevy_exercise",
            rows,
            conflict_cols=["exercise_template_id"],
            update_cols=[
                "title", "exercise_type", "primary_muscle_group",
                "secondary_muscle_groups", "equipment", "is_custom",
                "payload", "fetched_at",
            ],
        )
        run.upserted(len(rows))
        return len(rows)


# ---- helpers --------------------------------------------------------------
def _since_cursor(backfill_days: int | None) -> datetime:
    """Compute the `since` timestamp for an incremental run.

    - explicit backfill_days → now - N days
    - prior successful run    → started_at - lookback overlap
    - no prior run            → now - DEFAULT_FIRST_RUN_DAYS
    """
    now = datetime.now(timezone.utc)
    if backfill_days is not None:
        return now - timedelta(days=max(0, backfill_days))

    last = last_successful_run("hevy", "workout")
    if last and last.get("started_at"):
        return last["started_at"] - timedelta(days=DEFAULT_INCREMENTAL_LOOKBACK_DAYS)
    return now - timedelta(days=DEFAULT_FIRST_RUN_DAYS)


def _id_map(connection, table: str, key_col: str, keys: Iterable) -> dict:
    keys = list(keys)
    if not keys:
        return {}
    with connection.cursor() as cur:
        cur.execute(
            f"SELECT {key_col}, id FROM {table} WHERE {key_col} = ANY(%s)",
            [keys],
        )
        return {row[key_col]: row["id"] for row in cur.fetchall()}


def _replace_workout_sets(connection, workout_id: str, set_rows: list[dict]) -> None:
    """DELETE+INSERT all sets for a workout. Cleaner than per-set upsert
    because Hevy lets users delete/reorder sets on existing workouts; an
    upsert leaves orphaned old rows."""
    with connection.cursor() as cur:
        cur.execute(
            "DELETE FROM fact_strength_set WHERE hevy_workout_id = %s",
            [workout_id],
        )
    if not set_rows:
        return
    upsert_rows(
        "fact_strength_set",
        set_rows,
        conflict_cols=["hevy_workout_id", "exercise_index", "set_index"],
        connection=connection,
    )


def _match_whoop_workout(connection, start_ts: datetime, end_ts: datetime) -> str | None:
    """Best-effort link to fact_workout (Whoop) by start/end-time window
    overlap. Returns the workout_id (UUID) or None.

    A user typically wears the Whoop band during a Hevy session, so the
    HR/strain view (Whoop) and the per-set view (Hevy) describe the same
    physical session. We don't enforce this as a hard FK because Whoop
    can be re-ingested and renumber workout_ids — soft link only."""
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT workout_id
              FROM fact_workout
             WHERE ABS(EXTRACT(EPOCH FROM (start_ts - %s))) <= %s
               AND ABS(EXTRACT(EPOCH FROM (end_ts   - %s))) <= %s
             ORDER BY ABS(EXTRACT(EPOCH FROM (start_ts - %s)))
             LIMIT 1
            """,
            [
                start_ts, WHOOP_LINK_START_TOLERANCE.total_seconds(),
                end_ts,   WHOOP_LINK_END_TOLERANCE.total_seconds(),
                start_ts,
            ],
        )
        row = cur.fetchone()
    return row["workout_id"] if row else None
