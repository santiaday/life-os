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
