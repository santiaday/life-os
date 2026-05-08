"""Re-resolve existing pushpress_part_movement rows through the current
alias cache, without re-parsing.

Use after seeding new aliases (`python -m coach.seed_aliases`) or after a
manual override hits the alias table — every existing movement gets re-run
through the normalizer's tier-1 (alias exact). Tier-2 ILIKE and tier-3 LLM
are SKIPPED here so this is essentially free (pure SQL).

Re-run flow:
    python -m coach.seed_aliases     # update alias cache
    python -m coach.reresolve         # re-link existing movements
    python -m coach recompute --force # refresh load recommendations
    python -m coach sync              # PUT updated routines to Hevy
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row

from coach.normalizer import normalize_pattern
from coach.recommend import recommend
from lifeos_core.logging import configure_logging, get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)


def reresolve(
    *,
    days_past: int = 1,
    days_future: int = 14,
    only_changed: bool = True,
) -> dict:
    """For every movement in the window, look up the alias cache for its
    raw_text. If the cache hits with a different exercise_template_id than
    what's currently stored, update + re-recommend the load.

    Returns counters for the run."""
    today = date.today()
    start = today - timedelta(days=days_past)
    end = today + timedelta(days=days_future)

    out = {
        "scanned": 0, "updated": 0, "unchanged": 0,
        "no_alias_hit": 0, "loads_recomputed": 0,
    }

    with psycopg.connect(settings.SUPABASE_DB_URL_DIRECT, row_factory=dict_row) as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.raw_text, m.class_date, m.exercise_template_id,
                   m.novel_exercise, m.analog_exercise_template_id,
                   m.prescribed_reps, m.prescribed_load_kg, m.prescribed_load_pct
              FROM pushpress_part_movement m
             WHERE m.class_date BETWEEN %s AND %s
             ORDER BY m.class_date, m.sequence
            """,
            [start, end],
        )
        rows = cur.fetchall()
        out["scanned"] = len(rows)

        for r in rows:
            pattern = normalize_pattern(r["raw_text"])
            if not pattern:
                continue
            cur.execute(
                "SELECT exercise_template_id FROM pushpress_movement_alias WHERE pattern = %s",
                [pattern],
            )
            alias = cur.fetchone()
            if alias is None:
                out["no_alias_hit"] += 1
                continue
            new_tid = alias["exercise_template_id"]
            old_tid = r["exercise_template_id"] if not r["novel_exercise"] else r["analog_exercise_template_id"]

            if only_changed and new_tid == old_tid and not r["novel_exercise"]:
                out["unchanged"] += 1
                continue

            # Update template + clear novel/analog flags (alias hit = direct match).
            cur.execute(
                """
                UPDATE pushpress_part_movement
                   SET exercise_template_id = %s,
                       novel_exercise = FALSE,
                       analog_exercise_template_id = NULL,
                       computed_at = now()
                 WHERE id = %s
                """,
                [new_tid, r["id"]],
            )
            out["updated"] += 1

            # Re-run the load recommender now that the template is correct.
            rec = recommend(
                template_id=new_tid,
                analog_template_id=None,
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
            cur.execute(
                """
                UPDATE pushpress_part_movement
                   SET recommended_load_kg = %s,
                       recommendation_reasoning = %s,
                       recommendation_confidence = %s,
                       computed_at = now()
                 WHERE id = %s
                """,
                [rec.recommended_load_kg, rec.reasoning, rec.confidence, r["id"]],
            )
            out["loads_recomputed"] += 1

            # Resolve any open review-queue rows for this movement (the alias
            # is the answer now).
            cur.execute(
                """
                UPDATE pushpress_movement_review
                   SET resolved_template_id = %s, resolved_at = now(),
                       notes = COALESCE(notes, '') || ' [auto-resolved via alias seed]'
                 WHERE movement_id = %s AND resolved_at IS NULL
                """,
                [new_tid, r["id"]],
            )

        c.commit()

    return out


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="coach.reresolve")
    p.add_argument("--past", type=int, default=1)
    p.add_argument("--future", type=int, default=14)
    args = p.parse_args(argv)
    out = reresolve(days_past=args.past, days_future=args.future)
    import json
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
