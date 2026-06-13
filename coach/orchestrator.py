"""Coach orchestrator: parse → normalize → recommend → push to Hevy.

Three top-level entry points for the cron + manual flows:

  parse_pending_sessions(window=N)
      Run the LLM parser on every fact_pushpress_session that hasn't been
      parsed yet (parsed_at IS NULL) within the date window. Writes
      pushpress_part_movement rows + workout-format envelope columns.

  recompute_loads(window=N, force=False)
      Re-run the recommender for every parsed movement in the window.
      Skips rows where the prescribed_load_kg dominates anyway. force=True
      ignores the "did the recommendation actually move" guard.

  sync_to_hevy(window=N, dry_run=False)
      Push every parsed workout's routine to Hevy. Skips workouts with no
      matched movements. Idempotent — uses fact_pushpress_session.hevy_routine_id
      for create-vs-update routing.

  run_all() chains the three. This is what the cron + refresh_data MCP
  tool call.

The cron in scheduler/__main__.py triggers run_all() after every PushPress
sync. Recompute is also fired from a separate hourly job to pick up new
PRs landing via the Hevy ingester.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

from coach import hevy_routines, normalizer, parser, recommend
from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run

log = get_logger(__name__)


DEFAULT_WINDOW_PAST = 1
DEFAULT_WINDOW_FUTURE = 14


# ============================================================================
# parse pending sessions
# ============================================================================
def parse_pending_sessions(
    *,
    days_past: int = DEFAULT_WINDOW_PAST,
    days_future: int = DEFAULT_WINDOW_FUTURE,
    force: bool = False,
) -> dict:
    """Find sessions in the window that don't have parsed_at set, parse
    each via Claude Sonnet, and write pushpress_part_movement rows.

    `force=True` re-parses sessions whose payload has changed since the
    last parse (compared via raw_pushpress_workout_of_day.payload_hash)."""
    today = date.today()
    start = today - timedelta(days=days_past)
    end = today + timedelta(days=days_future)

    out: dict[str, Any] = {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "candidates": 0,
        "parsed": 0,
        "skipped": 0,
        "movements_written": 0,
        "errors": 0,
    }

    with ingestion_run("coach", "parse",
                       start=start.isoformat(), end=end.isoformat()) as run:
        sessions = _candidate_sessions(start, end, force=force)
        out["candidates"] = len(sessions)
        run.fetched(len(sessions))

        for sess in sessions:
            try:
                stats = _parse_one_session(sess)
            except Exception:
                log.exception("coach.parse.failed", workout_uid=sess["workout_uid"])
                out["errors"] += 1
                continue
            out["parsed"] += 1 if stats["parsed"] else 0
            out["skipped"] += 1 if not stats["parsed"] else 0
            out["movements_written"] += stats["movements_written"]

        run.upserted(out["movements_written"])
        run.add_metadata(**{k: v for k, v in out.items()
                            if k not in ("window_start", "window_end")})
    return out


def _candidate_sessions(
    start: date, end: date, *, force: bool,
) -> list[dict]:
    where = ["s.class_date BETWEEN %s AND %s",
             "s.workout_state = 'PUBLISHED'",
             "s.parts_count > 0"]
    if not force:
        where.append("s.parsed_at IS NULL")
    q = f"""
        SELECT s.workout_uid, s.class_type_uuid, s.class_type_name, s.class_date,
               s.title
          FROM fact_pushpress_session s
         WHERE {" AND ".join(where)}
         ORDER BY s.class_date
    """
    with tx() as c, c.cursor() as cur:
        cur.execute(q, [start, end])
        return cur.fetchall()


def _parse_one_session(sess: dict) -> dict:
    """Parse every part of one session, persist movements + envelope.

    For mixed sessions (e.g. POSTERIOR strength + WORKOUT OF THE DAY metcon)
    we parse each part independently and stitch the envelope: workout_format
    = 'mixed' if the parts disagree, else the single format. duration is
    summed across parts that have one."""
    workout_uid = sess["workout_uid"]
    parts = _load_parts(workout_uid)
    if not parts:
        return {"parsed": False, "movements_written": 0}

    formats: list[str] = []
    duration_total: int | None = None
    rounds_max: int | None = None
    score_types: list[str] = []
    confidences: list[float] = []
    movements_written = 0
    skip_reasons: list[str] = []

    # Reset prior movements for this workout — re-parse is destructive on
    # purpose so a corrected description gets a clean slate.
    _delete_movements(workout_uid)

    seq_offset = 0
    for part in parts:
        title = part.get("title")
        desc = part.get("description") or ""
        if not desc.strip():
            skip_reasons.append(f"part_uid={part['part_uid']} empty description")
            continue
        try:
            parsed = parser.parse_wod(desc, title=title)
        except parser.ParseError as e:
            log.warning(
                "coach.parse.part_failed",
                workout_uid=workout_uid, part_uid=part["part_uid"], error=str(e),
            )
            continue

        formats.append(parsed["workout_format"])
        if parsed.get("duration_seconds"):
            duration_total = (duration_total or 0) + int(parsed["duration_seconds"])
        if parsed.get("rounds"):
            rounds_max = max(rounds_max or 0, int(parsed["rounds"]))
        if parsed.get("score_type"):
            score_types.append(parsed["score_type"])
        confidences.append(float(parsed.get("parser_confidence", 0.0)))

        for m in parsed.get("movements", []):
            row_id = _insert_movement(
                part_uid=part["part_uid"],
                workout_uid=workout_uid,
                class_date=sess["class_date"],
                sequence=seq_offset,
                raw_text=m.get("raw_text") or "",
                prescribed_reps=m.get("reps"),
                prescribed_sets=m.get("sets"),
                prescribed_load_kg=m.get("load_kg"),
                prescribed_load_pct=m.get("load_pct_1rm"),
                prescribed_distance_m=m.get("distance_m"),
                parser_confidence=parsed.get("parser_confidence"),
            )
            # Resolve exercise; queue review if novel.
            match = normalizer.resolve(m.get("raw_text") or "")
            _attach_match(row_id, match)
            normalizer.enqueue_review_if_needed(
                row_id, m.get("raw_text") or "", match,
            )
            # Compute load recommendation right here while we're on this
            # row — saves a second pass for the common case. recompute_loads
            # will re-run for movements that need it after new actuals.
            rec = recommend.recommend(
                template_id=match.exercise_template_id,
                analog_template_id=match.analog_template_id,
                prescribed_load_kg=m.get("load_kg"),
                prescribed_load_pct=m.get("load_pct_1rm"),
                prescribed_reps=m.get("reps"),
                class_date=sess["class_date"],
            )
            _attach_recommendation(row_id, rec)
            movements_written += 1
            seq_offset += 1

    # Stitch envelope onto the session row. Only mark parsed_at if at
    # least one part succeeded — otherwise the next run would skip this
    # session even though we never got a real parse.
    if not formats:
        log.warning(
            "coach.parse.session_no_parts_succeeded",
            workout_uid=workout_uid,
            skip_reasons=skip_reasons,
        )
        return {"parsed": False, "movements_written": 0}

    workout_format = (
        formats[0] if len(set(formats)) == 1
        else "mixed"
    )
    score_type = (
        score_types[0] if len(set(score_types)) == 1 and score_types
        else ("rounds_reps" if score_types else None)
    )
    avg_confidence = (
        sum(confidences) / len(confidences) if confidences else 0.0
    )

    _update_session_envelope(
        workout_uid,
        workout_format=workout_format,
        duration_s=duration_total,
        rounds=rounds_max,
        score_type=score_type,
        confidence=avg_confidence,
    )
    log.info(
        "coach.parse.session_done",
        workout_uid=workout_uid,
        movements=movements_written,
        format=workout_format,
        confidence=round(avg_confidence, 2),
    )
    return {"parsed": True, "movements_written": movements_written}


# ============================================================================
# recompute loads
# ============================================================================
def recompute_loads(
    *,
    days_past: int = 0,
    days_future: int = DEFAULT_WINDOW_FUTURE,
    force: bool = False,
    epsilon_kg: float = 2.5,
) -> dict:
    """Re-run the recommender for every parsed movement in the window. Only
    writes back if the new recommendation differs from the stored one by
    more than `epsilon_kg` (so a no-op refresh doesn't spam routine PUTs to
    Hevy)."""
    today = date.today()
    start = today - timedelta(days=days_past)
    end = today + timedelta(days=days_future)

    out: dict[str, Any] = {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "scanned": 0,
        "updated": 0,
        "unchanged": 0,
    }
    with ingestion_run("coach", "recompute",
                       start=start.isoformat(), end=end.isoformat()) as run:
        rows = _load_for_recompute(start, end)
        out["scanned"] = len(rows)
        run.fetched(len(rows))
        for r in rows:
            rec = recommend.recommend(
                template_id=r["exercise_template_id"],
                analog_template_id=r["analog_exercise_template_id"],
                prescribed_load_kg=(
                    float(r["prescribed_load_kg"])
                    if r["prescribed_load_kg"] is not None else None
                ),
                prescribed_load_pct=(
                    float(r["prescribed_load_pct"])
                    if r["prescribed_load_pct"] is not None else None
                ),
                prescribed_reps=r["prescribed_reps"],
                class_date=r["class_date"],
            )
            old = (
                float(r["recommended_load_kg"])
                if r["recommended_load_kg"] is not None else None
            )
            new = rec.recommended_load_kg
            moved = (
                (old is None and new is not None)
                or (old is not None and new is None)
                or (old is not None and new is not None
                    and abs((new or 0) - (old or 0)) >= epsilon_kg)
            )
            if force or moved:
                _attach_recommendation(r["id"], rec)
                out["updated"] += 1
            else:
                out["unchanged"] += 1
        run.upserted(out["updated"])
        run.add_metadata(**{k: v for k, v in out.items()
                            if k not in ("window_start", "window_end")})
    return out


# ============================================================================
# sync to Hevy
# ============================================================================
def sync_to_hevy(
    *,
    days_past: int = DEFAULT_WINDOW_PAST,
    days_future: int = DEFAULT_WINDOW_FUTURE,
    dry_run: bool = False,
) -> dict:
    """Push routines to Hevy for every parsed workout in the window. Calls
    coach.hevy_routines.sync_workout for each, which handles POST vs PUT
    based on existing hevy_routine_id."""
    today = date.today()
    start = today - timedelta(days=days_past)
    end = today + timedelta(days=days_future)

    out: dict[str, Any] = {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "candidates": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
        "dry_run": dry_run,
    }

    with ingestion_run("coach", "sync_hevy",
                       start=start.isoformat(), end=end.isoformat()) as run:
        rows = _candidates_for_hevy(start, end)
        out["candidates"] = len(rows)
        run.fetched(len(rows))
        for i, r in enumerate(rows):
            # Hevy rate-limits routine writes aggressively (~1/sec). The
            # default retry policy then retries fast and burns three
            # 429s before bailing. Pre-throttle: sleep between writes
            # except on the first iteration.
            if i > 0 and not dry_run:
                time.sleep(1.5)
            try:
                result = hevy_routines.sync_workout(
                    r["workout_uid"], dry_run=dry_run,
                )
            except Exception:
                log.exception("coach.hevy.sync_failed", workout_uid=r["workout_uid"])
                out["errors"] += 1
                continue
            status = result.get("status")
            if status == "ok":
                if r["hevy_routine_id"]:
                    out["updated"] += 1
                else:
                    out["created"] += 1
            elif status == "dry_run":
                pass
            elif status in ("no_movements", "no_exercises"):
                out["skipped"] += 1
            else:
                out["errors"] += 1
        run.upserted(out["created"] + out["updated"])
        run.add_metadata(**{k: v for k, v in out.items()
                            if k not in ("window_start", "window_end")})
    return out


# ============================================================================
# run all
# ============================================================================
def run_all(
    *,
    days_past: int = DEFAULT_WINDOW_PAST,
    days_future: int = DEFAULT_WINDOW_FUTURE,
    force_parse: bool = False,
    sync_hevy: bool = True,
) -> dict:
    """Full coach pipeline. Used by the cron + refresh_data('coach')."""
    out: dict[str, Any] = {}
    t0 = time.perf_counter()
    try:
        out["parse"] = parse_pending_sessions(
            days_past=days_past, days_future=days_future, force=force_parse,
        )
    except Exception as e:
        log.exception("coach.run_all.parse_failed")
        out["parse"] = f"FAILED: {type(e).__name__}: {e}"

    try:
        out["recompute"] = recompute_loads(
            days_future=days_future,
        )
    except Exception as e:
        log.exception("coach.run_all.recompute_failed")
        out["recompute"] = f"FAILED: {type(e).__name__}: {e}"

    if sync_hevy:
        try:
            out["sync_hevy"] = sync_to_hevy(
                days_past=days_past, days_future=days_future,
            )
        except Exception as e:
            log.exception("coach.run_all.sync_failed")
            out["sync_hevy"] = f"FAILED: {type(e).__name__}: {e}"

    out["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    return out


# ============================================================================
# DB helpers
# ============================================================================
def _load_parts(workout_uid: str) -> list[dict]:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT part_uid, ordinal, title, workout_title, description
              FROM fact_pushpress_part
             WHERE workout_uid = %s
             ORDER BY ordinal
            """,
            [workout_uid],
        )
        return cur.fetchall()


def _delete_movements(workout_uid: str) -> None:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            "DELETE FROM pushpress_part_movement WHERE workout_uid = %s",
            [workout_uid],
        )


def _insert_movement(
    *,
    part_uid: str,
    workout_uid: str,
    class_date: date,
    sequence: int,
    raw_text: str,
    prescribed_reps: str | None,
    prescribed_sets: int | None,
    prescribed_load_kg: float | None,
    prescribed_load_pct: float | None,
    prescribed_distance_m: float | None,
    parser_confidence: float | None,
) -> int:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pushpress_part_movement
              (part_uid, workout_uid, class_date, sequence, raw_text,
               prescribed_reps, prescribed_sets,
               prescribed_load_kg, prescribed_load_pct, prescribed_distance_m,
               parser_confidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (part_uid, sequence) DO UPDATE SET
              raw_text = EXCLUDED.raw_text,
              prescribed_reps = EXCLUDED.prescribed_reps,
              prescribed_sets = EXCLUDED.prescribed_sets,
              prescribed_load_kg = EXCLUDED.prescribed_load_kg,
              prescribed_load_pct = EXCLUDED.prescribed_load_pct,
              prescribed_distance_m = EXCLUDED.prescribed_distance_m,
              parser_confidence = EXCLUDED.parser_confidence,
              computed_at = now()
            RETURNING id
            """,
            [part_uid, workout_uid, class_date, sequence, raw_text,
             prescribed_reps, prescribed_sets,
             prescribed_load_kg, prescribed_load_pct, prescribed_distance_m,
             parser_confidence],
        )
        return cur.fetchone()["id"]


def _attach_match(row_id: int, match: normalizer.MatchResult) -> None:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            UPDATE pushpress_part_movement
               SET exercise_template_id = %s,
                   novel_exercise = %s,
                   analog_exercise_template_id = %s,
                   computed_at = now()
             WHERE id = %s
            """,
            [match.exercise_template_id, match.novel_exercise,
             match.analog_template_id, row_id],
        )


def _attach_recommendation(row_id: int, rec: recommend.Recommendation) -> None:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            UPDATE pushpress_part_movement
               SET recommended_load_kg = %s,
                   recommendation_reasoning = %s,
                   recommendation_confidence = %s,
                   computed_at = now()
             WHERE id = %s
            """,
            [rec.recommended_load_kg, rec.reasoning, rec.confidence, row_id],
        )


def _update_session_envelope(
    workout_uid: str, *,
    workout_format: str | None,
    duration_s: int | None,
    rounds: int | None,
    score_type: str | None,
    confidence: float,
) -> None:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            UPDATE fact_pushpress_session
               SET workout_format = %s,
                   workout_duration_s = %s,
                   workout_rounds = %s,
                   workout_score_type = %s,
                   parser_confidence = %s,
                   parsed_at = now()
             WHERE workout_uid = %s
            """,
            [workout_format, duration_s, rounds, score_type, confidence,
             workout_uid],
        )


def _load_for_recompute(start: date, end: date) -> list[dict]:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.workout_uid, m.class_date,
                   m.exercise_template_id, m.analog_exercise_template_id,
                   m.prescribed_reps, m.prescribed_sets,
                   m.prescribed_load_kg, m.prescribed_load_pct,
                   m.recommended_load_kg
              FROM pushpress_part_movement m
             WHERE m.class_date BETWEEN %s AND %s
             ORDER BY m.class_date, m.sequence
            """,
            [start, end],
        )
        return cur.fetchall()


def _candidates_for_hevy(start: date, end: date) -> list[dict]:
    """Sessions eligible for Hevy push.

    De-duplicates by content: when two sessions on the same date have the
    same movement signature (gym programs the SAME WOD for two class types,
    e.g. a 'CrossFit & HIIT' that's actually a Hyrox simulator on Hyrox day),
    only the first by class_type_name is pushed. The user goes to one class,
    not both — and dual routines clutter the Hevy app.
    """
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT s.workout_uid, s.class_date, s.class_type_name,
                   s.hevy_routine_id, s.parsed_at,
                   md5(string_agg(
                     COALESCE(m.exercise_template_id, '?') || '|' ||
                     COALESCE(m.prescribed_reps, '?')      || '|' ||
                     COALESCE(m.prescribed_load_kg::text, '?'),
                     ',' ORDER BY m.sequence
                   )) AS movement_signature
              FROM fact_pushpress_session s
              JOIN pushpress_part_movement m ON m.workout_uid = s.workout_uid
             WHERE s.class_date BETWEEN %s AND %s
               AND s.parsed_at IS NOT NULL
               AND m.exercise_template_id IS NOT NULL
             GROUP BY s.workout_uid, s.class_date, s.class_type_name,
                      s.hevy_routine_id, s.parsed_at
             ORDER BY s.class_date, s.class_type_name
            """,
            [start, end],
        )
        rows = cur.fetchall()

    # Drop duplicates: keep first occurrence per (class_date, signature).
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for r in rows:
        key = (r["class_date"], r["movement_signature"])
        if key in seen:
            log.info(
                "coach.hevy.dedup_skip",
                class_date=str(r["class_date"]),
                class_type_name=r["class_type_name"],
                workout_uid=r["workout_uid"],
                reason="identical movement signature to earlier session same day",
            )
            continue
        seen.add(key)
        deduped.append(r)
    return deduped
