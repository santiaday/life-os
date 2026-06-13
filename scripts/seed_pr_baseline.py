"""Seed user 1RM baseline from Whoop Strength Trainer PR screenshots.

One-shot script: reads a hand-curated list of (exercise, lbs, hevy_template_id)
tuples extracted from the user's Whoop PR view (2026-05-07 screenshots) and
writes one fact_strength_set row per PR.

Synthesizes a single raw_hevy_workout row to anchor the FK ('PR baseline
import'). Re-runnable: ON CONFLICT (PK on hevy_workout_id, exercise_index,
set_index) updates in place.

Run once:
    .venv/bin/python -m scripts.seed_pr_baseline

After running, vw_exercise_rep_max picks up these as 1RMs and the load
recommender will use them as the baseline. Once the user starts logging
real Hevy sessions, those will overwrite (max(weight_kg)) wherever they
exceed these synthetic anchors.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from lifeos_core.settings import settings

# Sentinel raw_hevy_workout id. Out-of-band uuid that's clearly synthetic
# so a glance at raw_hevy_workout shows where these rows came from.
SYNTHETIC_WORKOUT_ID = UUID("00000000-0000-0000-0000-00000000bee7")
SYNTHETIC_TS = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
LB_TO_KG = 0.4536


# (PR_label, lbs, exercise_template_id, exercise_title)
# template_ids verified by direct SQL search against dim_hevy_exercise
# on 2026-05-07. exercise_title carries the display name we want stored
# on fact_strength_set (matches dim_hevy_exercise.title for that id).
PRS: list[tuple[str, int, str, str]] = [
    # Compound barbell (the load-recommender's main inputs)
    ("Bench Press - Barbell",                           155, "79D0BB3A", "Bench Press (Barbell)"),
    ("Back Squat - Barbell",                            175, "D04AC939", "Squat (Barbell)"),
    ("Deadlift - Barbell",                              275, "C6272009", "Deadlift (Barbell)"),
    ("Bent Over Row - Barbell",                         145, "55E6546F", "Bent Over Row (Barbell)"),
    ("Overhead Press - Barbell",                        105, "7B8D84E8", "Overhead Press (Barbell)"),
    ("Front Squat - Barbell",                           125, "5046D0A9", "Front Squat"),
    ("Hang Clean - Barbell",                             95, "BD4E7E53", "Hang Clean"),
    ("Romanian Deadlift - Barbell",                     115, "2B4B7310", "Romanian Deadlift (Barbell)"),
    ("Hip Thrust - Barbell",                            130, "D57C2EC7", "Hip Thrust (Barbell)"),
    ("Bench Press - Incline - Barbell",                 135, "50DFDFAB", "Incline Bench Press (Barbell)"),
    ("Bench Press - Decline - Barbell",                 115, "DA0F0470", "Decline Bench Press (Barbell)"),
    ("Backward Lunge - Alternating - Barbell",          115, "C284D923", "Reverse Lunge"),
    ("Front Rack Lunge - Alternating - Barbell",         75, "6E6EE645", "Lunge (Barbell)"),
    ("Split Squat - Barbell",                            40, "B5D3A742", "Bulgarian Split Squat"),
    ("Power Snatch - Barbell",                          105, "FB09C938", "Snatch"),
    ("Clean High Pull - Barbell",                        75, "ABB00838", "Clean"),
    ("Bicep Curl - Barbell",                             55, "A5AC6449", "Bicep Curl (Barbell)"),
    ("Reverse Curls - Barbell",                          33, "112FC6B7", "Reverse Curl (Barbell)"),
    ("Upright Row - Barbell",                            40, "7AB9A362", "Upright Row (Barbell)"),
    ("T-Bar Row - Barbell",                              75, "08A2974E", "T Bar Row"),
    ("Skull Crusher - Flat Bench - Barbell",             60, "875F585F", "Skullcrusher (Barbell)"),
    ("Good Morning - Barbell",                           25, "4180C405", "Good Morning (Barbell)"),
    ("Overhead Press - Smith Machine",                   60, "B09A1304", "Overhead Press (Smith Machine)"),

    # Dumbbell
    ("Bench Press - Dumbbell",                          110, "3601968B", "Bench Press (Dumbbell)"),
    ("Bench Press - Incline - Dumbbell",                110, "07B38369", "Incline Bench Press (Dumbbell)"),
    ("Overhead Press - Seated - Dumbbell",              110, "9930DF71", "Seated Overhead Press (Dumbbell)"),
    ("Hammer Curl - Dumbbell",                           70, "7E3BC8B6", "Hammer Curl (Dumbbell)"),
    ("Bicep Curl - Dumbbell",                            60, "37FCC2BB", "Bicep Curl (Dumbbell)"),
    ("Lateral Shoulder Raise - Dumbbell",                30, "422B08F1", "Lateral Raise (Dumbbell)"),
    ("Front Shoulder Raise - Dumbbell",                  30, "DBF9273A", "Plate Front Raise"),
    ("Reverse Fly - Dumbbell",                           60, "E5988A0A", "Rear Delt Reverse Fly (Dumbbell)"),
    ("Bench Fly - Dumbbell",                            100, "12017185", "Chest Fly (Dumbbell)"),
    ("Triceps Extension - Single Arm - Dumbbell",        30, "3765684D", "Triceps Extension (Dumbbell)"),
    ("Standing Triceps Extension - Dumbbell",            45, "3765684D", "Triceps Extension (Dumbbell)"),
    ("Tricep Kickback - Single Arm - Dumbbell",          11, "EC3B69A3", "Triceps Kickback (Cable)"),
    ("Lunge - Alternating - Dumbbell",                   30, "B537D09F", "Lunge (Dumbbell)"),
    ("Travelling Lunge - Alternating - Dumbbell",        50, "32HKJ34K", "Walking Lunge"),
    ("Split Squat - RFE - Dumbbell",                     40, "B5D3A742", "Bulgarian Split Squat"),
    ("Single Arm Snatch - Dumbbell",                     50, "FB09C938", "Snatch"),
    ("Row - Single Arm - Dumbbell",                      50, "F1E57334", "Dumbbell Row"),
    ("Shrugs - Dumbbell",                                45, "ABEC557F", "Shrug (Dumbbell)"),
    ("Goblet Squat - Dumbbell",                          70, "3D0C7C75", "Goblet Squat"),
    ("Farmer's Walk - Dumbbell",                        100, "50C613D0", "Farmers Walk"),

    # Cable / machine / kettlebell / specialty
    ("Lat Pull Down - Front",                           148, "6A6C31A5", "Lat Pulldown (Cable)"),
    ("Lat Pull Down - Wide Grip Front Pull",            121, "6A6C31A5", "Lat Pulldown (Cable)"),
    ("Cable Face Pulls",                                 50, "BE640BA0", "Face Pull"),
    ("Lateral Raise - Cable",                            15, "BE289E45", "Lateral Raise (Cable)"),
    ("Bicep Curl - Cable",                               38, "ADA8623C", "Bicep Curl (Cable)"),
    ("Triceps Pulldown - Rope",                          44, "94B7239B", "Triceps Rope Pushdown"),
    ("Tricep Extension - Standing - Rope - Pulley",      33, "94B7239B", "Triceps Rope Pushdown"),
    ("Straight Arm Pull Down",                           44, "9273BA17", "Rope Straight Arm Pulldown"),
    ("Preacher Curl",                                    50, "4F942934", "Preacher Curl (Barbell)"),
    ("Seated Row",                                      143, "F1D60854", "Seated Cable Row - Bar Grip"),
    ("Seated Machine Leg Curl",                         180, "B8127AD1", "Lying Leg Curl (Machine)"),
    ("Seated Machine Leg Extension",                    150, "629AE73D", "Single Leg Extensions"),
    ("Machine Chest Flys",                              125, "78683336", "Chest Fly (Machine)"),
    ("Machine Shoulder Flys",                            60, "D8281C62", "Rear Delt Reverse Fly (Machine)"),
    ("Glute Abductor Machine",                          105, "F4B4C6EE", "Hip Abduction (Machine)"),
    ("Leg Press",                                       340, "C7973E0E", "Leg Press (Machine)"),
    ("Calf Raise - Seated",                              90, "062AB91A", "Seated Calf Raise"),
    ("Calf Raise - Standing",                           105, "06745E58", "Standing Calf Raise"),
    ("Swing - Kettlebell",                               25, "F8A0FCCA", "Kettlebell Swing"),
]


def main() -> None:
    print(f"Seeding {len(PRS)} PR rows from Whoop Strength Trainer screenshots…")

    with psycopg.connect(settings.SUPABASE_DB_URL_DIRECT) as c, c.cursor() as cur:
        # Sanity: every template_id must exist in dim_hevy_exercise.
        ids = sorted({tid for _, _, tid, _ in PRS})
        cur.execute(
            "SELECT exercise_template_id FROM dim_hevy_exercise "
            "WHERE exercise_template_id = ANY(%s)",
            [ids],
        )
        present = {row[0] for row in cur.fetchall()}
        missing = [tid for tid in ids if tid not in present]
        if missing:
            raise RuntimeError(
                f"{len(missing)} template_id(s) not in dim_hevy_exercise: {missing}"
            )
        print(f"  ✓ All {len(ids)} unique template_ids found in catalog.")

        # 1) Synthetic raw_hevy_workout anchor.
        payload = {
            "id": str(SYNTHETIC_WORKOUT_ID),
            "title": "PR baseline import (Whoop Strength Trainer 2026-05-07)",
            "synthetic": True,
            "source": "scripts/seed_pr_baseline.py",
        }
        cur.execute(
            """
            INSERT INTO raw_hevy_workout
              (hevy_workout_id, payload, updated_at_src, deleted, fetched_at)
            VALUES (%s, %s, %s, FALSE, now())
            ON CONFLICT (hevy_workout_id) DO UPDATE SET
              payload = EXCLUDED.payload,
              fetched_at = now()
            """,
            [SYNTHETIC_WORKOUT_ID, Jsonb(payload), SYNTHETIC_TS],
        )

        # 2) One fact_strength_set row per PR. reps=1 so this row counts as
        # a 1RM in vw_exercise_rep_max.
        for i, (label, lbs, tpl, title) in enumerate(PRS):
            kg = round(lbs * LB_TO_KG, 2)
            cur.execute(
                """
                INSERT INTO fact_strength_set
                  (hevy_workout_id, exercise_index, set_index,
                   exercise_template_id, exercise_title, set_type,
                   weight_kg, reps, rpe,
                   workout_start_ts, workout_end_ts, updated_at)
                VALUES
                  (%s, %s, 0, %s, %s, 'normal', %s, 1, NULL, %s, %s, now())
                ON CONFLICT (hevy_workout_id, exercise_index, set_index)
                DO UPDATE SET
                  exercise_template_id = EXCLUDED.exercise_template_id,
                  exercise_title = EXCLUDED.exercise_title,
                  weight_kg = EXCLUDED.weight_kg,
                  updated_at = now()
                """,
                [SYNTHETIC_WORKOUT_ID, i, tpl, title, kg,
                 SYNTHETIC_TS, SYNTHETIC_TS],
            )

        c.commit()

    print(f"  ✓ {len(PRS)} PRs written.")

    # Quick verification.
    with psycopg.connect(settings.SUPABASE_DB_URL_DIRECT) as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT exercise_title, max_weight_kg, last_hit_day
              FROM vw_exercise_rep_max
             WHERE rep_count = 1
             ORDER BY max_weight_kg DESC
             LIMIT 10
            """
        )
        print("\nTop 10 1RMs in vw_exercise_rep_max:")
        for title, kg, day in cur.fetchall():
            print(f"  {kg:>6.1f} kg  {title}  ({day})")


if __name__ == "__main__":
    main()
