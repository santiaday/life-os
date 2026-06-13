"""Hand-curated alias seed for the coach's movement normalizer.

Every entry here forces a specific raw-text pattern → exact Hevy
exercise_template_id. Tier-1 in the normalizer hits this table FIRST, so a
seeded alias bypasses the ILIKE-jaccard scorer and the LLM tier entirely.

Why this is important:
  - Hevy's catalog uses qualifier suffixes ('(Barbell)', '(Cable)', etc.)
    while CrossFit programmers write bare terms ('Bench Press', 'Deadlift').
    Without a hand-curated mapping the bare term picks an arbitrary variant.
  - CrossFit-specific movements (Wall Walk, Synchro burpees, Squat Clean)
    don't exist as their own Hevy templates — they map to closest analogs.
  - User feedback 2026-05-08: "Walking" matched against "Wall Walks" (wrong).
    The seed below maps "wall walk" → "Wall Sit" template (kept under the
    same id; the WOD notes still mention the actual movement so the user
    knows what to do — Hevy is just the tracking shell).

Re-runnable: ON CONFLICT (pattern) DO UPDATE bumps to the seeded id even if
auto-learning got it wrong before. Treat this file as the canonical source.

Run:
    .venv/bin/python -m coach.seed_aliases
"""

from __future__ import annotations

import psycopg

from coach.normalizer import normalize_pattern
from lifeos_core.settings import settings

# (raw_pattern, exercise_template_id, exercise_title_for_log)
# Patterns are passed through normalize_pattern() so 'Bench Press' and
# 'bench press' produce the same key. Variants of the same canonical
# movement get separate rows so the alias-cache is exhaustive.
ALIASES: list[tuple[str, str, str]] = [
    # ---- Compound barbell — bare terms map to (Barbell) variants ----------
    ("Bench Press",                   "79D0BB3A", "Bench Press (Barbell)"),
    ("Barbell Bench Press",           "79D0BB3A", "Bench Press (Barbell)"),
    ("Flat Bench Press",              "79D0BB3A", "Bench Press (Barbell)"),
    ("Deadlift",                      "C6272009", "Deadlift (Barbell)"),
    ("Barbell Deadlift",              "C6272009", "Deadlift (Barbell)"),
    ("Conventional Deadlift",         "C6272009", "Deadlift (Barbell)"),
    ("Squat",                         "D04AC939", "Squat (Barbell)"),
    ("Back Squat",                    "D04AC939", "Squat (Barbell)"),
    ("Barbell Back Squat",            "D04AC939", "Squat (Barbell)"),
    ("Front Squat",                   "5046D0A9", "Front Squat"),
    ("Barbell Front Squat",           "5046D0A9", "Front Squat"),
    ("Overhead Squat",                "5046D0A9", "Front Squat"),  # closest analog
    ("OHS",                           "5046D0A9", "Front Squat"),

    # ---- Olympic lifts ---------------------------------------------------
    ("Clean",                         "ABB00838", "Clean"),
    ("Squat Clean",                   "ABB00838", "Clean"),
    ("Power Clean",                   "C628D768", "Power Clean"),
    ("Hang Clean",                    "BD4E7E53", "Hang Clean"),
    ("Hang Power Clean",              "BD4E7E53", "Hang Clean"),
    ("Clean and Jerk",                "ABB00838", "Clean"),  # log as Clean; jerk implied
    ("Clean High Pull",               "C628D768", "Power Clean"),
    ("Snatch",                        "FB09C938", "Snatch"),
    ("Squat Snatch",                  "FB09C938", "Snatch"),
    ("Power Snatch",                  "E22F9358", "Power Snatch"),
    ("Hang Snatch",                   "F4E77594", "Hang Snatch"),
    ("Hang Power Snatch",             "F4E77594", "Hang Snatch"),
    ("Muscle Snatch",                 "FB09C938", "Snatch"),

    # ---- Pressing --------------------------------------------------------
    ("Push Press",                    "542F3CD5", "Push Press"),
    ("Strict Press",                  "7B8D84E8", "Overhead Press (Barbell)"),
    ("Overhead Press",                "7B8D84E8", "Overhead Press (Barbell)"),
    ("Z Press",                       "7B8D84E8", "Overhead Press (Barbell)"),
    ("Z-Press",                       "7B8D84E8", "Overhead Press (Barbell)"),
    ("Strict Handstand Push-up",      "90B04F96", "Handstand Push Up"),
    ("HSPU",                          "90B04F96", "Handstand Push Up"),
    ("Kipping Handstand Push-up",     "90B04F96", "Handstand Push Up"),

    # ---- Pulling / pull-ups ---------------------------------------------
    ("Pull-up",                       "1B2B1E7C", "Pull Up"),
    ("Pull Up",                       "1B2B1E7C", "Pull Up"),
    ("Strict Pull-up",                "1B2B1E7C", "Pull Up"),
    ("Strict Pull Up",                "1B2B1E7C", "Pull Up"),
    ("Kipping Pull-up",               "A91838C0", "Kipping Pull Up"),
    ("Kipping Pull Up",               "A91838C0", "Kipping Pull Up"),
    ("Chest to Bar",                  "A91838C0", "Kipping Pull Up"),  # closest analog
    ("C2B Pull-up",                   "A91838C0", "Kipping Pull Up"),
    ("Toes-to-Bar",                   "B94E35E1", "Toes to Bar"),
    ("T2B",                           "B94E35E1", "Toes to Bar"),
    ("Knees-to-Elbows",               "B94E35E1", "Toes to Bar"),  # closest analog
    ("Bar Muscle-up",                 "9F9C164B", "Muscle Up"),
    ("Ring Muscle-up",                "9F9C164B", "Muscle Up"),
    ("Muscle-up",                     "9F9C164B", "Muscle Up"),

    # ---- CrossFit gymnastics + odd ---------------------------------------
    # Wall Walk / Wall Climbs INTENTIONALLY OMITTED — let the normalizer's
    # auto-custom path mint a real Hevy custom template the first time the
    # parser sees them. Mapping to Wall Sit (the previous behavior) was a
    # category mistake — Wall Sit is a static hold, Wall Walk is a dynamic
    # gymnastics movement. User feedback 2026-05-08.
    ("Wall Ball",                     "A1F47ACC", "Wall Ball"),
    ("Wall Balls",                    "A1F47ACC", "Wall Ball"),
    ("WB",                            "A1F47ACC", "Wall Ball"),
    ("Box Jump",                      "56092DD1", "Box Jump"),
    ("Box Jump Over",                 "56092DD1", "Box Jump"),
    ("Lateral Box Jump",              "56092DD1", "Box Jump"),
    ("Box Step-up",                   "128A2381", "Step Up"),
    ("Box Step Up",                   "128A2381", "Step Up"),
    ("Step-up",                       "128A2381", "Step Up"),
    ("Dumbbell Step-up",              "BF6ECE89", "Dumbbell Step Up"),
    ("Dumbbell Step Up",              "BF6ECE89", "Dumbbell Step Up"),
    ("Burpee",                        "BB792A36", "Burpee"),
    ("Burpees",                       "BB792A36", "Burpee"),
    ("Burpee Over Bar",               "86B00DDE", "Burpee Over the Bar"),
    ("Burpee Over the Bar",           "86B00DDE", "Burpee Over the Bar"),
    ("Burpee Broad Jump",             "115CC72C", "Burpee Broad Jumps"),
    ("Burpee To Target",              "BB792A36", "Burpee"),
    ("Synchro Burpee",                "BB792A36", "Burpee"),

    # ---- Cardio / monostructural ----------------------------------------
    ("Run",                           "AC1BB830", "Running"),
    ("Running",                       "AC1BB830", "Running"),
    ("Row",                           "AC1BB830", "Running"),  # NOTE: no Rowing template; Running is wrong but closest cardio
    ("Echo Bike",                     "43573BB8", "Air Bike"),
    ("Cal Bike",                      "43573BB8", "Air Bike"),
    ("Air Bike",                      "43573BB8", "Air Bike"),
    ("Bike",                          "43573BB8", "Air Bike"),
    ("Bike (C2)",                     "43573BB8", "Air Bike"),
    ("BikeErg",                       "43573BB8", "Air Bike"),
    ("Ski Erg",                       "5D99A2FA", "Ski Erg"),
    ("Ski",                           "5D99A2FA", "Ski Erg"),
    ("Sled Push",                     "7757171F", "Sled Push"),
    ("Sled Pull",                     "7757171F", "Sled Push"),

    # ---- Specific lifts that recur in programming -----------------------
    ("Hip Thrust",                    "D57C2EC7", "Hip Thrust (Barbell)"),
    ("Barbell Hip Thrust",            "D57C2EC7", "Hip Thrust (Barbell)"),
    ("Romanian Deadlift",             "2B4B7310", "Romanian Deadlift (Barbell)"),
    ("Barbell Romanian Deadlift",     "2B4B7310", "Romanian Deadlift (Barbell)"),
    ("RDL",                           "2B4B7310", "Romanian Deadlift (Barbell)"),
    ("Bulgarian Split Squat",         "B5D3A742", "Bulgarian Split Squat"),
    ("Zercher Bulgarian Split Squat", "B5D3A742", "Bulgarian Split Squat"),
    ("RFE Split Squat",               "B5D3A742", "Bulgarian Split Squat"),
    ("Rear Foot Elevated Split Squat","B5D3A742", "Bulgarian Split Squat"),
    ("Thruster",                      "90E506D5", "Thruster (Barbell)"),
    ("Thrusters",                     "90E506D5", "Thruster (Barbell)"),
    ("Kettlebell Swing",              "F8A0FCCA", "Kettlebell Swing"),
    ("KB Swing",                      "F8A0FCCA", "Kettlebell Swing"),
    ("KB Swings",                     "F8A0FCCA", "Kettlebell Swing"),
    ("American Swing",                "F8A0FCCA", "Kettlebell Swing"),
    ("Russian Swing",                 "F8A0FCCA", "Kettlebell Swing"),

    # ---- Push-ups + bodyweight ------------------------------------------
    ("Push-up",                       "392887AA", "Push Up"),
    ("Push-ups",                      "392887AA", "Push Up"),
    ("Push Up",                       "392887AA", "Push Up"),
    ("Push Ups",                      "392887AA", "Push Up"),
    ("Air Squat",                     "D04AC939", "Squat (Barbell)"),  # use barbell template; weight=0 for bodyweight

    # ---- Dumbbell movements ---------------------------------------------
    ("Dumbbell Bench Press",          "3601968B", "Bench Press (Dumbbell)"),
    ("DB Bench Press",                "3601968B", "Bench Press (Dumbbell)"),
    ("DB Bench",                      "3601968B", "Bench Press (Dumbbell)"),
]


def main() -> int:
    print(f"Seeding {len(ALIASES)} hand-curated aliases…")
    written = 0
    skipped_missing_template: list[tuple[str, str]] = []

    with psycopg.connect(settings.SUPABASE_DB_URL_DIRECT) as c, c.cursor() as cur:
        # Validate every template_id exists in dim_hevy_exercise.
        ids = sorted({tid for _, tid, _ in ALIASES})
        cur.execute(
            "SELECT exercise_template_id FROM dim_hevy_exercise "
            "WHERE exercise_template_id = ANY(%s)",
            [ids],
        )
        present = {row[0] for row in cur.fetchall()}
        missing = [tid for tid in ids if tid not in present]
        if missing:
            print(f"  ⚠ {len(missing)} template_id(s) not in catalog: {missing}")
            print("    Run `python -m ingest_hevy catalog` to refresh dim_hevy_exercise.")

        for raw_pattern, tid, title in ALIASES:
            if tid not in present:
                skipped_missing_template.append((raw_pattern, tid))
                continue
            pattern = normalize_pattern(raw_pattern)
            if not pattern:
                continue
            cur.execute(
                """
                INSERT INTO pushpress_movement_alias
                  (pattern, exercise_template_id, source, hit_count, last_seen_at)
                VALUES (%s, %s, 'seed', 0, now())
                ON CONFLICT (pattern) DO UPDATE SET
                  exercise_template_id = EXCLUDED.exercise_template_id,
                  source = 'seed',
                  last_seen_at = now()
                """,
                [pattern, tid],
            )
            written += 1
        c.commit()

    print(f"  ✓ {written} aliases written/updated.")
    if skipped_missing_template:
        print(f"  ⚠ skipped {len(skipped_missing_template)} due to missing templates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
