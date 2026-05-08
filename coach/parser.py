"""WOD parser: PushPress description plaintext → structured movements.

Anthropic Sonnet 4.5 with tool-use for structured output. Prompt is anchored
on CrossFit programming notation with 8 few-shot examples covering the
common formats (AMRAP, RFT, FT, EMOM, strength, chipper, skill).

Each `fact_pushpress_part` row's description maps to ONE parser call. The
parser returns a workout-format envelope plus a list of movements; we pick
the workout-format envelope from whichever part has it (the WORKOUT OF THE
DAY part typically) and use the movements list per-part to populate
`pushpress_part_movement` rows.

Output is structured: workout_format ∈ {amrap, rft, for_time, emom,
strength, chipper, skill, mixed}, plus per-movement raw_text, reps, sets,
load_kg, load_pct_1rm, distance_m. The downstream normalizer maps raw_text
→ exercise_template_id; the downstream recommender turns reps + load_pct →
recommended_load_kg.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

# ---- prompt assets ---------------------------------------------------------
SYSTEM_PROMPT = """\
You parse CrossFit / barbell-club workout-of-the-day text into structured \
JSON. The text is plain English from a coach: rep schemes, loads, and \
movements separated by line breaks.

Conventions you should know:
  - "AMRAP <minutes>" = as many rounds + reps as possible in that time
  - "RFT" or "<N> rounds for time" = repeat N rounds, score = total time
  - "For Time:" = single block of work, score = total time
  - "EMOM <minutes>" = every minute on the minute, one task per minute
  - "21-15-9" / "30-20-10" / "5-5-3-3-1" = descending rep schemes
  - "@ <weight>" or "<num>/<num>" = M/F load split (left = men's, right = women's)
  - "@ RPE 8" = work to ~8/10 perceived exertion
  - "5x5", "3x3", "5 sets of 3" = sets × reps notation
  - "Build to a heavy single in 15:00" = strength session, top single
  - "Cal" = calories on a machine; "m" = meters; '"' = inches (for box height)
  - "WB" = wall ball; "T2B" = toes-to-bar; "HSPU" = handstand pushup
  - "DU" = double-unders; "DB" = dumbbell; "KB" = kettlebell; "BB" = barbell
  - Divisions: F (fitness, scaled), P (performance, prescribed), E (elite/RX+)
  - "1RM" / "5RM" = one-rep max / five-rep max
  - "Teams of 2" / "Partners" / "P1 / P2" = partner WOD; reps below are TEAM totals OR per-person depending on phrasing

LOAD EXTRACTION (be aggressive — coaches always specify in pounds):
  Always pull EXACT loads when present. Convert lb → kg (× 0.4536, round to 1 decimal). Choose the MEN'S side of split notation:
  - "95/65" or "95/65 lb" → load_kg = 95 × 0.4536 ≈ 43.1
  - "@ 53#" → load_kg = 24.0
  - "DBs @ 50s/35s" → load_kg = 22.7 (two dumbbells, 50 lb each)
  - "@ 60% 1RM" → load_pct_1rm = 0.60 (NOT load_kg)
  - "Heavy" / "AHAP" / unspecified → both null; recommender will suggest
  Sled-style "5 x 45s / 3 x 45s" means N plates of 45 lb per side. Compute:
  load_kg = N × 45 × 2 × 0.4536 (e.g. 5 x 45s → 5×45×2×0.4536 = 204.1 kg)

DISTANCE EXTRACTION (mandatory for cardio movements):
  - "500m Ski Erg" / "1,000m Run" / "2km Bike" → distance_m
  - "100'" or "100 ft" → distance_m = 100 × 0.3048 = 30.5
  - Always populate distance_m for Run, Ski Erg, Row, Bike-with-distance, \
Sled Push (with feet), Farmers Walk (with feet), etc.
  - For "Cal Bike" / "Cal Row" the unit is calories, leave distance_m null \
and put the calorie count in reps.

WORKOUT-LEVEL ENVELOPE:
  - If single section (e.g. "5x5 back squat") → workout_format=strength.
  - If a clear AMRAP/RFT/For-Time block → that drives the envelope.
  - Mixed strength + metcon → workout_format=mixed.
  - Time caps ("TC: 15:00") → duration_seconds.
  - Score type: 'time' for For Time/RFT, 'rounds_reps' for AMRAP, \
'total_weight' for max-load strength sessions, 'none' for skill/EMOM.

SUPERSETS / CIRCUITS — STRENGTH SESSIONS ONLY:
  When 2-4 movements are alternated within "X sets:" or "X rounds:" \
notation IN A STRENGTH OR SKILL SESSION, mark ALL of them with "(superset)" \
in their raw_text. They share a superset group.

  Examples that ARE supersets/circuits:
    "5 sets:                          → mark BOTH '(superset)':
       2 Z-Press @ RPE 10                Z-Press (superset)
       4-8 Strict Pull-ups               Strict Pull-up (superset)"

    "3 sets:                          → mark ALL FOUR '(superset)':
       3 Hip Thrusts                     Hip Thrust (superset)
       10 Bulgarian Split Squats         Bulgarian Split Squat (superset)
       3 RDLs                            Romanian Deadlift (superset)
       10 DB Step-ups"                   DB Step-up (superset)

    "5 x 5 Strict Pull-ups            → mark both '(superset)':
       *10 Push-ups after each set"      Strict Pull-up (superset)
                                         Push-up (superset)

  CRITICAL: AMRAPs, RFTs, For Time, EMOMs, chippers are NOT supersets. \
The movements there are a sequence done in order each round; do NOT add \
"(superset)" to those. The workout_format field already captures the \
structure for those.

  When in doubt: if there's a TIME element ("AMRAP 12", "For Time", \
"21-15-9 For Time", "Cap 15:00"), it's a metcon → no supersets. If it \
says "5 sets:" or "5 rounds:" with no time element and the movements \
are strength work → supersets.

COMPLEXES (single performed unit):
  When the coach groups movements as one barbell unit \
("1 Power Clean + 2 Front Squats", "Snatch pull + drop"), emit ONE \
movement with raw_text='Power Clean + 2 Front Squats Complex' (capitalize \
"Complex"). Reps = number of complex repeats. Sets = number of complex \
sets. Don't fragment.

PARTNER WORKOUTS:
  "Teams of 2", "Partners", "P1: X / P2: Y" indicate partner WODs. The \
reps you emit should reflect what THIS user does, not the team total. \
When you see "P1: 9 BMU / P2: 12 HSPU *swap when done", and a note says \
"each athlete completes 9 BMU and 12 HSPU per round", emit BOTH movements \
at their per-person rep count. Append " (partner)" to one of the \
movement raw_texts to flag it for the downstream.

HERO / NAMED WODS:
  Recognize benchmark/named workouts ("Fran", "Murph", "Diane", "Helen", \
etc.) and named gym WODs (in quotes like "Sammy's WOD Bash", "sPECtacular"). \
Treat them as normal but the title preserves the name in your title field if \
asked. Confidence stays high.

If you can't parse confidently, set parser_confidence < 0.5 and best-effort \
the rest. Don't refuse to parse — return what you can.
"""

# 8 few-shot examples — one per format, all from real PushPress data we've
# already seen on this user's account.
FEW_SHOTS: list[dict] = [
    {
        "title": "POSTERIOR — Deadlifts",
        "description": (
            "Deadlifts \n\n"
            "Build To Heavy Single in 15:00\n\n"
            "*Move the bar well.\n*Take your time."
        ),
        "expected": {
            "workout_format": "strength",
            "duration_seconds": 900,
            "rounds": None,
            "score_type": "total_weight",
            "movements": [
                {"raw_text": "Deadlift", "reps": "1", "sets": 1,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
            ],
            "parser_confidence": 0.95,
        },
    },
    {
        "title": 'WORKOUT OF THE DAY — "Get on your hands\'"',
        "description": (
            '"Get on your hands\'"\n\n'
            "AMRAP 16:\n\n"
            "50 Box Step-ups @ 20\"\n"
            "15/12 Cal Echo Bike \n"
            "5 Wall Walks\n\n"
            "*goal = move the whole time here.\n"
            "*bike = should take no more than 1:15.\n\n"
            "F: Wall Climbs.\nP&M: As Written\nE: 50' HSW "
        ),
        "expected": {
            "workout_format": "amrap",
            "duration_seconds": 960,
            "rounds": None,
            "score_type": "rounds_reps",
            "movements": [
                {"raw_text": "Box Step-up @ 20\"", "reps": "50", "sets": None,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
                {"raw_text": "Echo Bike (calories)", "reps": "15", "sets": None,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
                {"raw_text": "Wall Walk", "reps": "5", "sets": None,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
            ],
            "parser_confidence": 0.95,
        },
    },
    {
        "title": "Snatch complex",
        "description": (
            "A) Snatch\n\n"
            "Build to a heavy:\n"
            "1 Power Snatch + 1 Squat Snatch + 1 OHS\n"
            "in 12:00, then drop 10% and do 3x complex"
        ),
        "expected": {
            "workout_format": "strength",
            "duration_seconds": None,
            "rounds": 3,
            "score_type": "total_weight",
            "movements": [
                {"raw_text": "Power Snatch", "reps": "1", "sets": 3,
                 "load_kg": None, "load_pct_1rm": 0.90, "distance_m": None},
                {"raw_text": "Squat Snatch", "reps": "1", "sets": 3,
                 "load_kg": None, "load_pct_1rm": 0.90, "distance_m": None},
                {"raw_text": "Overhead Squat", "reps": "1", "sets": 3,
                 "load_kg": None, "load_pct_1rm": 0.90, "distance_m": None},
            ],
            "parser_confidence": 0.85,
        },
    },
    {
        "title": "EMOM 12",
        "description": (
            "EMOM 12:\n"
            "Min 1: 5 Power Cleans @ 60% 1RM\n"
            "Min 2: 10 Burpees Over Bar\n"
            "Min 3: 15/12 Cal Row"
        ),
        "expected": {
            "workout_format": "emom",
            "duration_seconds": 720,
            "rounds": 4,
            "score_type": "none",
            "movements": [
                {"raw_text": "Power Clean", "reps": "5", "sets": 4,
                 "load_kg": None, "load_pct_1rm": 0.60, "distance_m": None},
                {"raw_text": "Burpee Over Bar", "reps": "10", "sets": 4,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
                {"raw_text": "Row (calories)", "reps": "15", "sets": 4,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
            ],
            "parser_confidence": 0.95,
        },
    },
    {
        "title": "Diane",
        "description": (
            "\"Diane\"\n\n"
            "21-15-9 For Time:\n"
            "Deadlifts 102/70 kg\n"
            "Handstand Push-ups"
        ),
        "expected": {
            "workout_format": "for_time",
            "duration_seconds": None,
            "rounds": None,
            "score_type": "time",
            "movements": [
                {"raw_text": "Deadlift", "reps": "21-15-9", "sets": 1,
                 "load_kg": 102.0, "load_pct_1rm": None, "distance_m": None},
                {"raw_text": "Handstand Push-up", "reps": "21-15-9", "sets": 1,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
            ],
            "parser_confidence": 0.95,
        },
    },
    {
        "title": "Back Squat 5x5",
        "description": "Back Squat\n5x5 @ RPE 8\nrest 2:00",
        "expected": {
            "workout_format": "strength",
            "duration_seconds": None,
            "rounds": 5,
            "score_type": "total_weight",
            "movements": [
                {"raw_text": "Back Squat", "reps": "5", "sets": 5,
                 "load_kg": None, "load_pct_1rm": 0.80, "distance_m": None},
            ],
            "parser_confidence": 0.95,
        },
    },
    {
        "title": "Mixed (strength + WOD)",
        "description": (
            "A) Power Clean: 5x3 @ 75%\n"
            "B) AMRAP 8:\n"
            "10 Thrusters 43/29 kg\n"
            "10 Pull-ups"
        ),
        "expected": {
            "workout_format": "mixed",
            "duration_seconds": 480,
            "rounds": None,
            "score_type": "rounds_reps",
            "movements": [
                {"raw_text": "Power Clean", "reps": "3", "sets": 5,
                 "load_kg": None, "load_pct_1rm": 0.75, "distance_m": None},
                {"raw_text": "Thruster", "reps": "10", "sets": None,
                 "load_kg": 43.0, "load_pct_1rm": None, "distance_m": None},
                {"raw_text": "Pull-up", "reps": "10", "sets": None,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
            ],
            "parser_confidence": 0.90,
        },
    },
    {
        "title": "Skill: Muscle-up Progressions",
        "description": (
            "Skill: Bar Muscle-Up Progressions\n"
            "Take 15 min — drill kip swing → high pull → transition\n"
            "No score, focus on quality"
        ),
        "expected": {
            "workout_format": "skill",
            "duration_seconds": 900,
            "rounds": None,
            "score_type": "none",
            "movements": [
                {"raw_text": "Bar Muscle-up Progressions", "reps": None,
                 "sets": None, "load_kg": None, "load_pct_1rm": None,
                 "distance_m": None},
            ],
            "parser_confidence": 0.85,
        },
    },
    # ---- Superset within a strength session ----
    {
        "title": "SHOULDERS — Press & Bicep",
        "description": (
            "Press & Bicep\n"
            "5 sets:\n"
            "2 Z-Press @ RPE 10\n"
            "4-8 Strict Pull-ups\n"
            "*rest as needed.\n"
        ),
        "expected": {
            "workout_format": "strength",
            "duration_seconds": None,
            "rounds": 5,
            "score_type": "total_weight",
            "movements": [
                {"raw_text": "Z-Press (superset)", "reps": "2", "sets": 5,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
                {"raw_text": "Strict Pull-up (superset)", "reps": "4-8", "sets": 5,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
            ],
            "parser_confidence": 0.95,
        },
    },
    # ---- 4-movement circuit (all share one superset group) ----
    {
        "title": "B) Posterior Chain/Accessory",
        "description": (
            "3 sets:\n"
            "3 Barbell Hip Thrusts @ MTR\n"
            "10 Zercher Bulgarian Split Squats (5 per leg) @ 65 / 3 Barbell Romanian Deadlifts @ MTR\n"
            "10 DB Step-ups (5 per leg) @ as heavy as possible\n"
            "*all for quality!"
        ),
        "expected": {
            "workout_format": "strength",
            "duration_seconds": None,
            "rounds": 3,
            "score_type": "none",
            "movements": [
                {"raw_text": "Barbell Hip Thrust (superset)", "reps": "3", "sets": 3,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
                {"raw_text": "Zercher Bulgarian Split Squat (superset)", "reps": "10", "sets": 3,
                 "load_kg": 29.5, "load_pct_1rm": None, "distance_m": None},
                {"raw_text": "Barbell Romanian Deadlift (superset)", "reps": "3", "sets": 3,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
                {"raw_text": "DB Step-up (superset)", "reps": "10", "sets": 3,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
            ],
            "parser_confidence": 0.92,
        },
    },
    # ---- Cardio with distance + DB load extraction (the 'sPECtacular' pattern) ----
    {
        "title": 'WORKOUT OF THE DAY — "sPECtacular"',
        "description": (
            '"sPECtacular"\n'
            "For Time:\n"
            "500/425m Ski Erg\n"
            "25 Toes To Bar\n"
            "50 DB Bench Press @ 40s/30s\n"
            "25 Toes To Bar\n"
            "500/425m Ski Erg\n"
            "*TC: 15:00"
        ),
        "expected": {
            "workout_format": "for_time",
            "duration_seconds": 900,
            "rounds": None,
            "score_type": "time",
            "movements": [
                {"raw_text": "Ski Erg", "reps": None, "sets": None,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": 500},
                {"raw_text": "Toes to Bar", "reps": "25", "sets": None,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
                {"raw_text": "Dumbbell Bench Press", "reps": "50", "sets": None,
                 "load_kg": 18.1, "load_pct_1rm": None, "distance_m": None},
                {"raw_text": "Toes to Bar", "reps": "25", "sets": None,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
                {"raw_text": "Ski Erg", "reps": None, "sets": None,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": 500},
            ],
            "parser_confidence": 0.95,
        },
    },
    # ---- Sled push with imperial feet + plate notation ----
    {
        "title": 'HYROX x DTSC — "Push.Run.Push"',
        "description": (
            'For Time:\n'
            "100' Sled Push @ 5 x 45s / 3 x 45s\n"
            "1,000m Run\n"
            "150' Burpee Broad Jumps\n"
            "1,000m Run\n"
            "100' Sled Push @ 5 x 45s / 3 x 45s\n"
            "*TC: 30:00"
        ),
        "expected": {
            "workout_format": "for_time",
            "duration_seconds": 1800,
            "rounds": None,
            "score_type": "time",
            "movements": [
                {"raw_text": "Sled Push", "reps": None, "sets": None,
                 "load_kg": 204.1, "load_pct_1rm": None, "distance_m": 30.5},
                {"raw_text": "Run", "reps": None, "sets": None,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": 1000},
                {"raw_text": "Burpee Broad Jump", "reps": None, "sets": None,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": 45.7},
                {"raw_text": "Run", "reps": None, "sets": None,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": 1000},
                {"raw_text": "Sled Push", "reps": None, "sets": None,
                 "load_kg": 204.1, "load_pct_1rm": None, "distance_m": 30.5},
            ],
            "parser_confidence": 0.95,
        },
    },
    # ---- Partner WOD with per-person reps ----
    {
        "title": 'WORKOUT OF THE DAY — "Sammys WOD Bash"',
        "description": (
            "3 Rounds For Time: (Teams of 2)\n"
            "21 Power Cleans @ 155/105 *ygig\n"
            "10 Synchro Lateral Burpees Over Bar\n"
            "P1: 9 Bar Muscle-ups / P2: 12 Strict HSPU *swap when done\n"
            "*each round each athlete completes 9 BMU and 12 HSPU.\n"
            "*TC: 26:00"
        ),
        "expected": {
            "workout_format": "rft",
            "duration_seconds": 1560,
            "rounds": 3,
            "score_type": "time",
            "movements": [
                {"raw_text": "Power Clean (partner)", "reps": "21", "sets": 3,
                 "load_kg": 70.3, "load_pct_1rm": None, "distance_m": None},
                {"raw_text": "Synchro Lateral Burpee Over Bar", "reps": "10", "sets": 3,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
                {"raw_text": "Bar Muscle-up", "reps": "9", "sets": 3,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
                {"raw_text": "Strict Handstand Push-up", "reps": "12", "sets": 3,
                 "load_kg": None, "load_pct_1rm": None, "distance_m": None},
            ],
            "parser_confidence": 0.92,
        },
    },
]


PARSER_TOOL: dict = {
    "name": "parse_wod",
    "description": (
        "Emit the parsed structured representation of the workout. Always "
        "call this exactly once. Don't write a chat reply — just call the tool."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "workout_format": {
                "type": "string",
                "enum": ["amrap", "rft", "for_time", "emom",
                         "strength", "chipper", "skill", "mixed"],
                "description": "Top-level format. 'mixed' if multiple distinct blocks (e.g. strength + metcon).",
            },
            "duration_seconds": {
                "type": ["integer", "null"],
                "description": "Time cap in seconds (e.g. AMRAP 16 → 960). NULL if open-ended.",
            },
            "rounds": {
                "type": ["integer", "null"],
                "description": "Number of rounds (RFT, sets in strength). NULL otherwise.",
            },
            "score_type": {
                "type": "string",
                "enum": ["time", "rounds_reps", "total_reps",
                         "total_weight", "distance", "none"],
            },
            "movements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "raw_text": {
                            "type": "string",
                            "description": "Cleaned movement name suitable for matching to an exercise registry. Strip load/rep notation. e.g. 'Box Step-up @ 20\"' for the box height variant.",
                        },
                        "reps": {
                            "type": ["string", "null"],
                            "description": "As programmed: '50', '21-15-9', 'AMRAP', etc. String to allow descending schemes.",
                        },
                        "sets": {"type": ["integer", "null"]},
                        "load_kg": {
                            "type": ["number", "null"],
                            "description": "Absolute load in kg if explicitly programmed. Use the men's value when split (e.g. '95/65').",
                        },
                        "load_pct_1rm": {
                            "type": ["number", "null"],
                            "description": "Fraction of 1RM (0.85 for 85%) if programmed as a percent.",
                        },
                        "distance_m": {"type": ["number", "null"]},
                    },
                    "required": ["raw_text"],
                },
            },
            "parser_confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Your confidence the parse matches what the coach intended. <0.5 means human-review.",
            },
        },
        "required": ["workout_format", "movements", "parser_confidence"],
    },
}


# ---- types -----------------------------------------------------------------
class ParseError(RuntimeError):
    """Parser failed (API error, malformed tool output, etc.)."""


# ---- public API ------------------------------------------------------------
def parse_wod(description: str, *, title: str | None = None) -> dict:
    """Parse one PushPress workout description into structured form.

    Returns a dict matching the parse_wod tool schema. Raises ParseError on
    API or schema failures — caller decides whether to fall back to a
    no-movements stub or skip entirely."""
    if not description or not description.strip():
        return _empty_parse(reason="empty description")

    if not settings.ANTHROPIC_API_KEY:
        raise ParseError(
            "ANTHROPIC_API_KEY not set. Add it to .env to enable WOD parsing."
        )

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    user_text = _build_user_prompt(description, title=title)

    try:
        resp = client.messages.create(
            model=settings.COACH_PARSER_MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=[PARSER_TOOL],
            tool_choice={"type": "tool", "name": "parse_wod"},
            messages=[{"role": "user", "content": user_text}],
        )
    except anthropic.APIError as e:
        log.error("coach.parser.api_error", error=str(e))
        raise ParseError(f"Anthropic API error: {e}") from e

    parsed = _extract_tool_input(resp)
    if parsed is None:
        log.error("coach.parser.no_tool_use",
                  stop_reason=resp.stop_reason,
                  blocks=[type(b).__name__ for b in resp.content])
        raise ParseError(
            f"Model didn't call parse_wod tool (stop_reason={resp.stop_reason})"
        )

    parsed.setdefault("duration_seconds", None)
    parsed.setdefault("rounds", None)
    parsed.setdefault("score_type", "none")
    parsed.setdefault("parser_confidence", 0.5)
    parsed["movements"] = parsed.get("movements") or []
    log.info(
        "coach.parser.parsed",
        format=parsed["workout_format"],
        movements=len(parsed["movements"]),
        confidence=parsed["parser_confidence"],
    )
    return parsed


# ---- internals -------------------------------------------------------------
def _build_user_prompt(description: str, *, title: str | None) -> str:
    examples_block = "\n\n".join(
        _format_example(ex) for ex in FEW_SHOTS
    )
    title_line = f"Section title: {title}\n\n" if title else ""
    return (
        f"# Examples\n\n{examples_block}\n\n"
        f"# Now parse this workout\n\n"
        f"{title_line}"
        f"```\n{description}\n```"
    )


def _format_example(ex: dict) -> str:
    return (
        f"## {ex['title']}\n"
        f"```\n{ex['description']}\n```\n"
        f"Expected `parse_wod` input:\n"
        f"```json\n{json.dumps(ex['expected'], indent=2)}\n```"
    )


def _extract_tool_input(resp: Any) -> dict | None:
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "parse_wod":
            inp = block.input
            if isinstance(inp, dict):
                return inp
            try:
                return json.loads(inp)
            except (TypeError, json.JSONDecodeError):
                return None
    return None


def _empty_parse(*, reason: str) -> dict:
    return {
        "workout_format": "skill",
        "duration_seconds": None,
        "rounds": None,
        "score_type": "none",
        "movements": [],
        "parser_confidence": 0.0,
        "_skip_reason": reason,
    }
