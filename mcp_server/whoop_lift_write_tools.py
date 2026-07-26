"""MCP write surface for Whoop Strength Trainer: create custom exercises, save
workout templates, and log workouts — with full custom-exercise support (the
gap in the third-party Whoop MCP). Each call opens a WhoopPrivateClient on the
shared, auto-refreshing Cognito token and delegates to ingest_whoop_private.
lift_write, which builds exercise_details live from the user's full library.
"""

from __future__ import annotations

from ingest_whoop_journal.auth import WhoopAuth
from ingest_whoop_private import lift_write
from ingest_whoop_private.client import WhoopPrivateClient


def _client() -> WhoopPrivateClient:
    auth = WhoopAuth()
    auth.ensure_fresh()
    return WhoopPrivateClient(auth=auth)


def save_whoop_lift_template(
    name: str,
    exercises: list[dict],
    base_template_key: int | None = None,
    dry_run: bool = True,
) -> dict:
    with _client() as c:
        return lift_write.save_template(
            c, name, exercises, base_template_key, dry_run=dry_run
        )


def log_whoop_workout(
    exercises: list[dict],
    name: str | None = None,
    start: str | None = None,
    end: str | None = None,
    dry_run: bool = True,
) -> dict:
    with _client() as c:
        return lift_write.log_workout(c, name, exercises, start, end, dry_run=dry_run)


def create_whoop_custom_exercise(
    name: str,
    base_exercise_id: str,
    muscle_groups: list[str],
    equipment: str = "OTHER",
    movement_pattern: str = "OTHER",
    laterality: str = "BILATERAL",
    volume_input_format: str = "REPS",
    dry_run: bool = True,
) -> dict:
    with _client() as c:
        return lift_write.create_custom_exercise(
            c, name, base_exercise_id, muscle_groups,
            equipment=equipment, movement_pattern=movement_pattern,
            laterality=laterality, volume_input_format=volume_input_format,
            dry_run=dry_run,
        )


# ---------------------------------------------------------------------------
# Deleting a Strength Trainer workout
#
# Scope note, because the boundary is not obvious and matters:
#
# Whoop's private API exposes no DELETE for strength workouts. GET on
# /weightlifting-service/v2/weightlifting-workout/{id} returns the workout;
# DELETE on that same path returns 405, and eleven other candidate routes
# (v3, activity-service, user-service, a POST .../delete) all 404. So this
# removes the workout from the WAREHOUSE, and Whoop keeps its copy.
#
# That still has to suppress re-import, or the 2-hourly sync restores the rows
# within the hour and the delete looks like it silently failed.
# ---------------------------------------------------------------------------

def _lift_target(activity_id: str) -> dict:
    """What a delete would affect. Used by both dry-run and execute."""
    from lifeos_core.db import tx

    with tx() as c, c.cursor() as cur:
        cur.execute(
            "SELECT day, name, duration_minutes, strain, set_count, "
            "       exercise_count, total_volume_kg "
            "  FROM fact_whoop_lift_workout WHERE activity_id = %s",
            [activity_id],
        )
        w = cur.fetchone()
        cur.execute("SELECT count(*) AS n FROM fact_whoop_lift_set "
                    "WHERE activity_id = %s", [activity_id])
        sets = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM fact_activity "
                    "WHERE activity_uid = %s", [f"whoop_lift:{activity_id}"])
        unified = cur.fetchone()["n"]
    return {
        "activity_id": activity_id,
        "found": w is not None,
        "day": str(w["day"]) if w else None,
        "name": (w["name"] if w else None) or "(unnamed)",
        "duration_minutes": float(w["duration_minutes"])
        if w and w["duration_minutes"] is not None else None,
        "total_volume_kg": float(w["total_volume_kg"])
        if w and w["total_volume_kg"] is not None else None,
        "set_rows": sets,
        "unified_rows": unified,
    }


def delete_whoop_lift_workout(
    activity_id: str,
    reason: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict:
    """Remove a Strength Trainer workout from the warehouse and stop it being
    re-imported.

    Does NOT delete it from Whoop — their private API has no delete route for
    strength workouts (see the module comment). Delete it in the Whoop app if
    you want it gone there too; this tool keeps the warehouse consistent with
    that decision either way.

    Reversible: `restore_whoop_lift_workout(activity_id)` drops the tombstone
    and the next sync brings it back.

    Guarded twice on purpose — `dry_run=False` alone is refused; `confirm=True`
    is also required, so one flipped flag cannot destroy data.
    """
    from lifeos_core.db import tx

    if not activity_id or len(activity_id) != 36:
        return {"ok": False,
                "error": f"activity_id must be a 36-char UUID, got {activity_id!r}"}

    target = _lift_target(activity_id)

    if dry_run:
        return {"ok": True, "dry_run": True, "would_delete": target,
                "also": "writes an activity_suppression tombstone so the "
                        "2-hourly sync won't restore it",
                "not_affected": "the workout stays in Whoop — delete it in the "
                                "app if you want it gone there",
                "next": "re-run with dry_run=false AND confirm=true"}

    if not confirm:
        return {"ok": False, "error": "refusing to delete without confirm=true",
                "would_delete": target}

    deleted: dict[str, int] = {}
    with tx() as c, c.cursor() as cur:
        # Tombstone first: if the deletes below fail the row is still protected
        # from re-import, which is the safer half-state.
        cur.execute(
            """
            INSERT INTO activity_suppression (source, source_id, reason, suppressed_by)
            VALUES ('whoop_lift', %s, %s, 'mcp')
            ON CONFLICT (source, source_id) DO UPDATE
              SET reason = EXCLUDED.reason, created_at = now()
            """,
            [activity_id, reason],
        )
        for table, sql in (
            ("fact_activity",
             "DELETE FROM fact_activity WHERE activity_uid = %s"),
            ("fact_whoop_lift_set",
             "DELETE FROM fact_whoop_lift_set WHERE activity_id = %s"),
            ("fact_whoop_lift_workout",
             "DELETE FROM fact_whoop_lift_workout WHERE activity_id = %s"),
        ):
            param = (f"whoop_lift:{activity_id}" if table == "fact_activity"
                     else activity_id)
            cur.execute(sql, [param])
            deleted[table] = cur.rowcount

    return {"ok": True, "deleted": True, "was": target,
            "rows_deleted": deleted, "suppressed": True,
            "still_in_whoop": True,
            "note": "mart_daily keeps the old totals until the next rebuild — "
                    "call refresh_data('mart') if it matters now"}


def restore_whoop_lift_workout(activity_id: str) -> dict:
    """Undo a delete: drop the tombstone so the next sync re-imports it."""
    from lifeos_core.db import tx

    with tx() as c, c.cursor() as cur:
        cur.execute("DELETE FROM activity_suppression "
                    "WHERE source = 'whoop_lift' AND source_id = %s",
                    [activity_id])
        removed = cur.rowcount
    if not removed:
        return {"ok": False, "error": f"no suppression found for {activity_id}"}
    return {"ok": True, "restored": True, "activity_id": activity_id,
            "note": "the next ingest_whoop_private run (every 2h) re-imports "
                    "it; refresh_data('whoop_private') to do it now"}


def list_suppressed_activities() -> dict:
    """Every activity currently withheld from the warehouse."""
    from lifeos_core.db import tx

    with tx() as c, c.cursor() as cur:
        cur.execute("SELECT source, source_id, reason, suppressed_by, created_at "
                    "FROM activity_suppression ORDER BY created_at DESC")
        rows = cur.fetchall()
    return {"ok": True, "count": len(rows),
            "rows": [{**r, "created_at": str(r["created_at"])} for r in rows]}
