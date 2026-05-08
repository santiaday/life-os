"""Hevy routine writer: pushpress_part_movement → Hevy routine via POST/PUT.

Builds a Hevy routine payload from one fact_pushpress_session and its
associated parts/movements + recommended loads. Calls the existing
mcp_server.hevy_write_tools.create_routine / update_routine helpers so we
share the same client + mirror-into-raw_hevy_routine logic the MCP tools
already use.

Idempotency strategy:
  - First sync of a workout: POST → store hevy_routine_id on
    fact_pushpress_session.
  - Subsequent syncs (load was recomputed): PUT to the same id.
  - If POST fails because the routine somehow exists already (rare race),
    we don't try to recover — surface the error and let the user dedup.

Rest seconds, set type, and rep_range are derived from the parsed envelope:
  - strength → rest 120s, type=normal, fixed reps from prescribed_reps
  - amrap/rft/for_time → rest 0s, type=normal, single set per movement
  - emom → rest 0s, type=normal, sets=rounds
  - skill → rest 0s, type=normal, single set, no weight
"""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)


# ---- routine payload shape ------------------------------------------------
def _set_for_movement(
    *,
    workout_format: str,
    movement: dict,
) -> dict:
    """Build one Hevy set dict for a programmed movement. movement is the
    dict shape returned by the load-then-load pipeline:
      {raw_text, exercise_template_id, recommended_load_kg, prescribed_reps,
       prescribed_sets, prescribed_distance_m, prescribed_duration_s, ...}
    """
    weight = movement.get("recommended_load_kg")
    reps_str = movement.get("prescribed_reps")

    # Pull a representative rep count out of the freeform string. For
    # descending schemes ("21-15-9") we use the FIRST number — Hevy's
    # routine UI shows that as the prescription and the user adjusts during
    # the session.
    reps_int = _first_int(reps_str) if reps_str else None

    return {
        "type": "normal",
        "weight_kg": float(weight) if weight is not None else None,
        "reps": int(reps_int) if reps_int is not None else None,
        # Hevy validates distance_meters as INTEGER. Coach can program "500m"
        # or "0.5km" — round to the nearest meter on write.
        "distance_meters": (
            int(round(float(movement["prescribed_distance_m"])))
            if movement.get("prescribed_distance_m") else None
        ),
        "duration_seconds": (
            int(movement["prescribed_duration_s"])
            if movement.get("prescribed_duration_s") else None
        ),
        "custom_metric": None,
        "rep_range": None,
    }


def _set_count_for_format(workout_format: str, movement: dict, rounds: int | None) -> int:
    """How many sets to write to the Hevy routine. We don't try to model the
    full WOD logic in routine sets — that's a session-time concern. We just
    create enough sets that the user can record what they did."""
    explicit = movement.get("prescribed_sets")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    if workout_format in ("strength", "emom") and rounds:
        return int(rounds)
    if workout_format in ("amrap", "for_time", "rft", "chipper", "skill"):
        return 1
    return 1


def _rest_seconds_for_format(workout_format: str) -> int | None:
    if workout_format == "strength":
        return 120
    return 0


def _first_int(s: str) -> int | None:
    digits = ""
    for ch in s:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    if digits:
        try:
            return int(digits)
        except ValueError:
            return None
    return None


def _build_routine_payload(
    *,
    title: str,
    notes: str,
    workout_format: str,
    rounds: int | None,
    movements: list[dict],
) -> tuple[str, list[dict], str]:
    """Group movements into Hevy exercises (one per template_id sequence,
    preserving coach order). Returns (title, exercises[], notes_str).

    A movement with no template_id (truly unmatched) is dropped here — the
    user can review it in the queue and add it manually if needed.

    Superset wiring: the parser flags paired movements by appending
    '(superset)' to the raw_text. Consecutive movements that both carry
    that flag share a superset_id (auto-incremented per group) so Hevy
    renders them as a paired set in the routine UI."""
    exercises: list[dict] = []
    rest = _rest_seconds_for_format(workout_format)
    superset_counter = 0
    last_was_superset = False
    current_superset_id: int | None = None

    for m in movements:
        tpl = m.get("exercise_template_id") or m.get("analog_exercise_template_id")
        if not tpl:
            continue

        is_superset = "(superset)" in (m.get("raw_text") or "").lower()
        if is_superset:
            if not last_was_superset:
                superset_counter += 1
                current_superset_id = superset_counter
            ssid = current_superset_id
        else:
            ssid = None
        last_was_superset = is_superset

        n_sets = _set_count_for_format(workout_format, m, rounds)
        sets = [_set_for_movement(workout_format=workout_format, movement=m)
                for _ in range(n_sets)]
        ex_notes = (m.get("recommendation_reasoning") or "").strip()
        if m.get("novel_exercise"):
            ex_notes = (
                f"[analog: {m.get('analog_exercise_title') or 'no exact match'}] "
                + ex_notes
            )
        # Hevy's PUT validator rejects empty-string notes ("...notes is not
        # allowed to be empty"). Fall back to the raw_text as a default note
        # for movements with no recommendation reasoning (cardio, bodyweight).
        if not ex_notes:
            ex_notes = m.get("raw_text") or "—"
        exercises.append({
            "exercise_template_id": tpl,
            "superset_id": ssid,
            "rest_seconds": rest,
            "notes": ex_notes[:500],
            "sets": sets,
        })
    return title, exercises, notes


def _routine_title(class_date: date, class_type_name: str) -> str:
    """e.g. '5/8 — CrossFit & HIIT (coach loads)'. Short enough to read in
    the Hevy app's routine list."""
    md = class_date.strftime("%-m/%-d") if hasattr(class_date, "strftime") else str(class_date)
    return f"{md} — {class_type_name} (coach)"


def _routine_notes(parsed: dict, movements: list[dict]) -> str:
    """Top-of-routine notes block. Renders the workout envelope and the
    per-movement reasoning. Hevy lets us put a few hundred chars here."""
    parts: list[str] = []
    fmt = parsed.get("workout_format") or "?"
    rounds = parsed.get("rounds")
    dur = parsed.get("workout_duration_s")
    score = parsed.get("workout_score_type") or "?"
    if dur:
        parts.append(f"{fmt.upper()} {dur//60}:{dur%60:02d}")
    elif rounds:
        parts.append(f"{fmt.upper()} {rounds} rounds")
    else:
        parts.append(fmt.upper())
    parts.append(f"score: {score}")

    novel = sum(1 for m in movements if m.get("novel_exercise"))
    if novel:
        parts.append(f"{novel} novel movement(s) — analog loads in notes")
    return " | ".join(parts)[:400]


# ---- public API -----------------------------------------------------------
def sync_workout(workout_uid: str, *, dry_run: bool = False) -> dict:
    """Build + POST/PUT the Hevy routine for one programmed workout. Returns
    {hevy_routine_id, exercises_written, status}. Pure side-effect call;
    safe to re-run.

    Reads from fact_pushpress_session + fact_pushpress_part +
    pushpress_part_movement (joined to dim_hevy_exercise for titles).
    """
    rows = _load_movements(workout_uid)
    if not rows:
        return {"status": "no_movements", "workout_uid": workout_uid}

    head = rows[0]
    parsed = {
        "workout_format": head["workout_format"] or "mixed",
        "rounds": head["workout_rounds"],
        "workout_duration_s": head["workout_duration_s"],
        "workout_score_type": head["workout_score_type"],
    }
    title = _routine_title(head["class_date"], head["class_type_name"])
    notes = _routine_notes(parsed, rows)

    movements: list[dict] = []
    for r in rows:
        movements.append({
            # raw_text is load-bearing for the superset detection in
            # _build_routine_payload (it scans for the '(superset)' tag the
            # parser appends to paired movements). Don't drop it.
            "raw_text": r["raw_text"],
            "exercise_template_id": r["exercise_template_id"],
            "analog_exercise_template_id": r["analog_exercise_template_id"],
            "analog_exercise_title": r["analog_title"],
            "exercise_title": r["resolved_title"],
            "novel_exercise": r["novel_exercise"],
            "recommended_load_kg": (
                float(r["recommended_load_kg"])
                if r["recommended_load_kg"] is not None else None
            ),
            "recommendation_reasoning": r["recommendation_reasoning"],
            "prescribed_reps": r["prescribed_reps"],
            "prescribed_sets": r["prescribed_sets"],
            "prescribed_distance_m": (
                float(r["prescribed_distance_m"])
                if r["prescribed_distance_m"] is not None else None
            ),
            "prescribed_duration_s": r["prescribed_duration_s"],
        })

    title, exercises, notes = _build_routine_payload(
        title=title, notes=notes,
        workout_format=parsed["workout_format"],
        rounds=parsed["rounds"],
        movements=movements,
    )

    if not exercises:
        log.warning(
            "coach.hevy.no_exercises",
            workout_uid=workout_uid,
            reason="all movements unmatched",
        )
        return {"status": "no_exercises", "workout_uid": workout_uid}

    if dry_run:
        return {
            "status": "dry_run",
            "workout_uid": workout_uid,
            "title": title,
            "exercise_count": len(exercises),
            "movement_count": len(movements),
            "payload_preview": {
                "title": title,
                "notes": notes,
                "exercises": exercises,
            },
        }

    # Lazy import — avoids a hard dep on the Hevy MCP module at orchestrator
    # import time, and keeps the call path identical to the user-facing tool.
    from mcp_server import hevy_write_tools as HW

    existing_id = head.get("hevy_routine_id")
    if existing_id:
        log.info("coach.hevy.update", workout_uid=workout_uid, routine_id=existing_id)
        resp = HW.update_routine(existing_id, title, exercises, notes=notes)
        # If the user manually deleted the routine in the Hevy app, the PUT
        # 404s. Fall back to POST so the canonical routine is re-created
        # rather than dying silently. _store_routine_id below saves the new id.
        if (not resp.get("ok")) and "404" in str(resp.get("error", "")):
            log.warning(
                "coach.hevy.update_404_falling_back_to_create",
                workout_uid=workout_uid, routine_id=existing_id,
            )
            resp = HW.create_routine(
                title, exercises,
                folder_id=settings.COACH_HEVY_FOLDER_ID,
                notes=notes,
            )
    else:
        log.info("coach.hevy.create", workout_uid=workout_uid)
        resp = HW.create_routine(
            title, exercises,
            folder_id=settings.COACH_HEVY_FOLDER_ID,
            notes=notes,
        )
    if not resp.get("ok"):
        log.error(
            "coach.hevy.write_failed",
            workout_uid=workout_uid,
            error=resp.get("error"),
        )
        return {"status": "failed", "workout_uid": workout_uid,
                "error": resp.get("error")}

    # _summarize_routine emits {hevy_routine_id, title, ...}. Older code paths
    # used routine_id/id; check all three for safety.
    routine = resp["rows"][0] if resp.get("rows") else {}
    routine_id = (
        routine.get("hevy_routine_id")
        or routine.get("routine_id")
        or routine.get("id")
        or existing_id
    )
    if routine_id:
        _store_routine_id(workout_uid, routine_id)
    return {
        "status": "ok",
        "workout_uid": workout_uid,
        "hevy_routine_id": routine_id,
        "exercises_written": len(exercises),
        "title": title,
    }


# ---- DB helpers -----------------------------------------------------------
def _load_movements(workout_uid: str) -> list[dict]:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.workout_uid, m.class_date, m.sequence, m.raw_text,
                   m.exercise_template_id,
                   m.novel_exercise,
                   m.analog_exercise_template_id,
                   m.prescribed_reps, m.prescribed_sets,
                   m.prescribed_load_kg, m.prescribed_load_pct,
                   m.prescribed_distance_m, m.prescribed_duration_s,
                   m.recommended_load_kg, m.recommendation_reasoning,
                   m.recommendation_confidence,
                   resolved.title AS resolved_title,
                   analog.title  AS analog_title,
                   s.class_type_name,
                   s.workout_format, s.workout_rounds,
                   s.workout_duration_s, s.workout_score_type,
                   s.hevy_routine_id
              FROM pushpress_part_movement m
              JOIN fact_pushpress_session s ON s.workout_uid = m.workout_uid
              LEFT JOIN dim_hevy_exercise resolved
                ON resolved.exercise_template_id = m.exercise_template_id
              LEFT JOIN dim_hevy_exercise analog
                ON analog.exercise_template_id = m.analog_exercise_template_id
             WHERE m.workout_uid = %s
             ORDER BY m.sequence
            """,
            [workout_uid],
        )
        return cur.fetchall()


def _store_routine_id(workout_uid: str, routine_id: str) -> None:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            UPDATE fact_pushpress_session
               SET hevy_routine_id = %s,
                   hevy_routine_synced_at = now()
             WHERE workout_uid = %s
            """,
            [routine_id, workout_uid],
        )
