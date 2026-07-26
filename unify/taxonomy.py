"""Canonical vocabularies: activity types and exercise movements.

Two problems this solves (GAP-11):

1. Every vendor names the same activity differently. Whoop says
   `weightlifting_msk`, Garmin says `strength_training`, PushPress says
   `Strength`. Callers should never have to know that.

2. Every vendor names the same movement differently, and Garmin's auto-detect
   is actively wrong in places (it files `WEIGHTED_LEG_EXTENSIONS` under the
   `CRUNCH` category). Subcategory is trusted over category for that reason.

Unmapped movements are not dropped — they get an auto-derived key from their
label, land in `dim_exercise` with `movement_pattern = NULL`, and show up in
`unmapped_exercises()` so the map can be extended.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Activity types
# ---------------------------------------------------------------------------

CANONICAL_ACTIVITY_TYPES = {
    "strength",      # resistance training
    "conditioning",  # metcon, HIIT, circuit, functional fitness
    "run",
    "cycle",
    "swim",
    "racquet",
    "walk",
    "mobility",      # yoga, pilates, stretching, breathwork
    "sport",         # team/field sports
    "other",
}

RESISTANCE_TYPES = {"strength"}
CARDIO_TYPES = {"run", "cycle", "swim", "conditioning", "walk"}

# (source, vendor_type) -> canonical_type
ACTIVITY_TYPE_MAP: dict[str, dict[str, str]] = {
    "whoop": {
        "weightlifting_msk": "strength",
        "weightlifting": "strength",
        "strength trainer": "strength",
        "powerlifting": "strength",
        "box-fitness": "conditioning",
        "functional-fitness": "conditioning",
        "functional fitness": "conditioning",
        "hiit": "conditioning",
        "crossfit": "conditioning",
        "circuit training": "conditioning",
        "rowing": "conditioning",
        "running": "run",
        "walking": "walk",
        "hiking/rucking": "walk",
        "cycling": "cycle",
        "spin": "cycle",
        "swimming": "swim",
        "tennis": "racquet",
        "pickleball": "racquet",
        "padel": "racquet",
        "squash": "racquet",
        "racquetball": "racquet",
        "hot-yoga": "mobility",
        "yoga": "mobility",
        "pilates": "mobility",
        "increase_relaxation": "mobility",
        "meditation": "mobility",
        "stretching": "mobility",
        "volleyball": "sport",
        "basketball": "sport",
        "soccer": "sport",
        "golf": "sport",
        "dance": "other",
        "kayaking": "other",
        "activity": "other",
        "other": "other",
    },
    "garmin": {
        "strength_training": "strength",
        "indoor_cardio": "conditioning",
        "hiit": "conditioning",
        "stair_climbing": "conditioning",
        "running": "run",
        "treadmill_running": "run",
        "indoor_running": "run",
        "trail_running": "run",
        "cycling": "cycle",
        "indoor_cycling": "cycle",
        "road_biking": "cycle",
        "lap_swimming": "swim",
        "open_water_swimming": "swim",
        "tennis_v2": "racquet",
        "tennis": "racquet",
        "pickleball": "racquet",
        "racquetball": "racquet",
        "padel": "racquet",
        "walking": "walk",
        "hiking": "walk",
        "yoga": "mobility",
        "pilates": "mobility",
        "breathwork": "mobility",
        "volleyball": "sport",
        "golf": "sport",
        "single_gas_diving": "other",
        "fishing_v2": "other",
        "other": "other",
    },
    "hevy": {"strength": "strength", "": "strength"},
    "whoop_lift": {"strength trainer": "strength", "": "strength"},
    "pushpress": {
        "crossfit": "conditioning",
        "strength": "strength",
        "conditioning": "conditioning",
        "olympic weightlifting": "strength",
        "": "conditioning",
    },
    "loseit": {"exercise": "other", "": "other"},
    "zero": {"activity": "other", "": "other"},
}

# Whoop's public API sport_id -> vendor string, for rows that only carry the id.
WHOOP_SPORT_ID_NAMES = {
    -1: "activity", 0: "running", 1: "cycling", 16: "baseball", 17: "basketball",
    18: "rowing", 22: "golf", 24: "hockey", 27: "rugby", 28: "sailing",
    29: "skiing", 30: "soccer", 31: "softball", 32: "squash", 33: "swimming",
    34: "tennis", 35: "track & field", 36: "volleyball", 42: "boxing",
    43: "dance", 44: "pilates", 45: "yoga", 47: "weightlifting",
    48: "cross country skiing", 49: "functional fitness", 51: "hiking/rucking",
    52: "horseback riding", 55: "kayaking", 56: "martial arts", 57: "meditation",
    59: "mountain biking", 60: "powerlifting", 61: "rock climbing",
    62: "paddleboarding", 63: "triathlon", 64: "walking", 65: "surfing",
    66: "elliptical", 67: "stairmaster", 70: "meditation", 71: "other",
    73: "diving", 74: "operations - tactical", 82: "ultimate", 83: "climber",
    84: "jumping rope", 85: "australian football", 86: "skateboarding",
    87: "coaching", 88: "ice bath", 89: "commuting", 90: "gaming",
    91: "snowboarding", 92: "motocross", 93: "caddying",
    96: "obstacle course racing", 101: "gymnastics", 102: "hiit",
    103: "spin", 104: "jiu jitsu", 105: "manual labor", 106: "cricket",
    107: "pickleball", 108: "inline skating", 109: "box fitness",
    110: "spikeball", 111: "wheelchair pushing", 112: "paddle tennis",
    113: "barre", 114: "stage performance", 115: "high stress work",
    116: "parkour", 117: "gaelic football", 118: "hurling/camogie",
    119: "circus arts", 120: "massage therapy", 121: "strength trainer",
    123: "watching sports", 125: "assault bike", 126: "kickboxing",
    127: "stretching", 128: "table tennis", 230: "badminton",
    231: "padel", 132: "racquetball", 133: "squash",
}


def canonical_activity_type(source: str, vendor_type: str | None) -> tuple[str, bool, bool]:
    """(canonical_type, is_resistance, is_cardio) for a vendor activity string."""
    key = (vendor_type or "").strip().lower()
    table = ACTIVITY_TYPE_MAP.get(source, {})
    ctype = table.get(key)
    if ctype is None:
        # Fall back to substring heuristics before giving up.
        for needle, guess in (
            ("strength", "strength"), ("weight", "strength"), ("lift", "strength"),
            ("run", "run"), ("jog", "run"),
            ("cycl", "cycle"), ("bike", "cycle"), ("spin", "cycle"),
            ("swim", "swim"),
            ("tennis", "racquet"), ("pickle", "racquet"), ("padel", "racquet"),
            ("squash", "racquet"), ("racquet", "racquet"),
            ("walk", "walk"), ("hik", "walk"),
            ("yoga", "mobility"), ("pilates", "mobility"), ("stretch", "mobility"),
            ("breath", "mobility"), ("medit", "mobility"),
            ("hiit", "conditioning"), ("crossfit", "conditioning"),
            ("functional", "conditioning"), ("circuit", "conditioning"),
            ("row", "conditioning"),
        ):
            if needle in key:
                ctype = guess
                break
    ctype = ctype or "other"
    return ctype, ctype in RESISTANCE_TYPES, ctype in CARDIO_TYPES


# ---------------------------------------------------------------------------
# Exercises
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Movement:
    key: str
    display: str
    pattern: str | None = None
    muscle: str | None = None
    equipment: str | None = None
    compound: bool = False
    spinal_flexion: bool = False
    unilateral: bool = False
    secondary: tuple[str, ...] = field(default_factory=tuple)


def _m(key, display, pattern=None, muscle=None, equipment=None,
       compound=False, spinal_flexion=False, unilateral=False) -> Movement:
    return Movement(key, display, pattern, muscle, equipment,
                    compound, spinal_flexion, unilateral)


# The canonical catalogue. Everything a source can log resolves to one of these
# or to an auto-derived key.
MOVEMENTS: tuple[Movement, ...] = (
    # --- squat ------------------------------------------------------------
    _m("back_squat", "Back Squat", "squat", "quads", "barbell", True),
    _m("front_squat", "Front Squat", "squat", "quads", "barbell", True),
    _m("goblet_squat", "Goblet Squat", "squat", "quads", "dumbbell", True),
    _m("leg_press", "Leg Press", "squat", "quads", "machine", True),
    _m("bulgarian_split_squat", "Bulgarian Split Squat", "lunge", "quads", "dumbbell", True, unilateral=True),
    _m("split_squat", "Split Squat", "lunge", "quads", "barbell", True, unilateral=True),
    _m("walking_lunge", "Walking Lunge", "lunge", "quads", "dumbbell", True, unilateral=True),
    _m("reverse_lunge", "Reverse Lunge", "lunge", "quads", "dumbbell", True, unilateral=True),
    _m("side_lunge", "Side Lunge", "lunge", "adductors", "bodyweight", True, unilateral=True),
    _m("box_jump", "Box Jump", "squat", "quads", "bodyweight", True),
    _m("squat_jack", "Squat Jack", "squat", "quads", "bodyweight", True),
    _m("wall_sit", "Wall Sit", "squat", "quads", "bodyweight"),

    # --- hinge ------------------------------------------------------------
    _m("deadlift", "Deadlift", "hinge", "posterior_chain", "barbell", True),
    _m("romanian_deadlift", "Romanian Deadlift", "hinge", "hamstrings", "barbell", True),
    _m("single_leg_rdl", "Single-Leg Romanian Deadlift", "hinge", "hamstrings", "dumbbell", True, unilateral=True),
    _m("sumo_deadlift", "Sumo Deadlift", "hinge", "posterior_chain", "barbell", True),
    _m("good_morning", "Good Morning", "hinge", "hamstrings", "barbell", True),
    _m("hip_thrust", "Hip Thrust", "hinge", "glutes", "barbell", True),
    _m("glute_bridge", "Glute Bridge", "hinge", "glutes", "bodyweight"),
    _m("single_leg_glute_bridge", "Single-Leg Glute Bridge", "hinge", "glutes", "bodyweight", unilateral=True),
    _m("cable_pull_through", "Cable Pull-Through", "hinge", "glutes", "cable"),
    _m("kettlebell_swing", "Kettlebell Swing", "hinge", "glutes", "kettlebell", True),
    _m("back_extension", "Back Extension", "hinge", "erectors", "bodyweight"),

    # --- horizontal push --------------------------------------------------
    _m("bench_press", "Bench Press", "push_h", "chest", "barbell", True),
    _m("incline_bench_press", "Incline Bench Press", "push_h", "chest", "barbell", True),
    _m("decline_bench_press", "Decline Bench Press", "push_h", "chest", "barbell", True),
    _m("close_grip_bench_press", "Close-Grip Bench Press", "push_h", "triceps", "barbell", True),
    _m("dumbbell_bench_press", "Dumbbell Bench Press", "push_h", "chest", "dumbbell", True),
    _m("incline_dumbbell_bench_press", "Incline Dumbbell Bench Press", "push_h", "chest", "dumbbell", True),
    _m("push_up", "Push Up", "push_h", "chest", "bodyweight", True),
    _m("chest_fly", "Chest Fly", "push_h", "chest", "dumbbell"),
    _m("cable_chest_fly", "Cable Chest Fly", "push_h", "chest", "cable"),
    _m("dip", "Dip", "push_h", "triceps", "bodyweight", True),

    # --- vertical push ----------------------------------------------------
    _m("overhead_press", "Overhead Press", "push_v", "shoulders", "barbell", True),
    _m("dumbbell_shoulder_press", "Dumbbell Shoulder Press", "push_v", "shoulders", "dumbbell", True),
    _m("military_press", "Military Press", "push_v", "shoulders", "barbell", True),
    _m("push_press", "Push Press", "push_v", "shoulders", "barbell", True),
    _m("push_jerk", "Push Jerk", "push_v", "shoulders", "barbell", True),
    _m("jerk_dip", "Jerk Dip", "push_v", "shoulders", "barbell", True),
    _m("thruster", "Thruster", "push_v", "shoulders", "barbell", True),

    # --- vertical pull ----------------------------------------------------
    _m("pull_up", "Pull Up", "pull_v", "lats", "bodyweight", True),
    _m("weighted_pull_up", "Weighted Pull Up", "pull_v", "lats", "bodyweight", True),
    _m("band_assisted_pull_up", "Band-Assisted Pull Up", "pull_v", "lats", "band", True),
    _m("chin_up", "Chin Up", "pull_v", "lats", "bodyweight", True),
    _m("lat_pulldown", "Lat Pulldown", "pull_v", "lats", "cable", True),
    _m("close_grip_lat_pulldown", "Close-Grip Lat Pulldown", "pull_v", "lats", "cable", True),
    _m("straight_arm_pulldown", "Straight-Arm Pulldown", "pull_v", "lats", "cable"),

    # --- horizontal pull --------------------------------------------------
    _m("barbell_row", "Barbell Row", "pull_h", "back", "barbell", True),
    _m("pendlay_row", "Pendlay Row", "pull_h", "back", "barbell", True),
    _m("seated_cable_row", "Seated Cable Row", "pull_h", "back", "cable", True),
    _m("dumbbell_row", "Dumbbell Row", "pull_h", "back", "dumbbell", True, unilateral=True),
    _m("chest_supported_row", "Chest-Supported Row", "pull_h", "back", "dumbbell", True),
    _m("single_arm_cable_row", "Single-Arm Cable Row", "pull_h", "back", "cable", True, unilateral=True),
    _m("t_bar_row", "T-Bar Row", "pull_h", "back", "barbell", True),
    _m("inverted_row", "Inverted Row", "pull_h", "back", "bodyweight", True),
    _m("face_pull", "Face Pull", "pull_h", "rear_delts", "cable"),
    _m("upright_row", "Upright Row", "pull_h", "traps", "barbell"),
    _m("shrug", "Shrug", "pull_h", "traps", "barbell"),
    _m("rear_delt_fly", "Rear Delt Fly", "pull_h", "rear_delts", "dumbbell"),

    # --- arms -------------------------------------------------------------
    _m("biceps_curl", "Biceps Curl", "isolation", "biceps", "dumbbell"),
    _m("barbell_curl", "Barbell Curl", "isolation", "biceps", "barbell"),
    _m("hammer_curl", "Hammer Curl", "isolation", "biceps", "dumbbell"),
    _m("preacher_curl", "Preacher Curl", "isolation", "biceps", "barbell"),
    _m("reverse_curl", "Reverse Curl", "isolation", "forearms", "barbell"),
    _m("cable_biceps_curl", "Cable Biceps Curl", "isolation", "biceps", "cable"),
    _m("triceps_extension", "Triceps Extension", "isolation", "triceps", "dumbbell"),
    _m("triceps_pressdown", "Triceps Pressdown", "isolation", "triceps", "cable"),
    _m("overhead_triceps_extension", "Overhead Triceps Extension", "isolation", "triceps", "dumbbell"),
    _m("skull_crusher", "Skull Crusher", "isolation", "triceps", "barbell"),
    _m("triceps_kickback", "Triceps Kickback", "isolation", "triceps", "cable"),
    _m("bench_dip", "Bench Dip", "push_h", "triceps", "bodyweight"),

    # --- shoulders --------------------------------------------------------
    _m("lateral_raise", "Lateral Raise", "isolation", "side_delts", "dumbbell"),
    _m("front_raise", "Front Raise", "isolation", "front_delts", "dumbbell"),
    _m("band_external_rotation", "Band External Rotation", "isolation", "rotator_cuff", "band"),

    # --- leg isolation ----------------------------------------------------
    _m("leg_extension", "Leg Extension", "isolation", "quads", "machine"),
    _m("leg_curl", "Leg Curl", "isolation", "hamstrings", "machine"),
    _m("seated_leg_curl", "Seated Leg Curl", "isolation", "hamstrings", "machine"),
    _m("calf_raise", "Calf Raise", "isolation", "calves", "machine"),
    _m("seated_calf_raise", "Seated Calf Raise", "isolation", "calves", "machine"),
    _m("hip_abduction", "Hip Abduction", "isolation", "glutes", "machine"),
    _m("hip_adduction", "Hip Adduction", "isolation", "adductors", "machine"),

    # --- core -------------------------------------------------------------
    # spinal_flexion=True marks loaded/repeated lumbar flexion, which the
    # L4-L5 / L5-S1 findings make contraindicated.
    _m("plank", "Plank", "core", "abs", "bodyweight"),
    _m("side_plank", "Side Plank", "core", "obliques", "bodyweight", unilateral=True),
    _m("dead_bug", "Dead Bug", "core", "abs", "bodyweight"),
    _m("bird_dog", "Bird Dog", "core", "erectors", "bodyweight"),
    _m("ab_wheel", "Ab Wheel Rollout", "core", "abs", "bodyweight"),
    _m("hanging_leg_raise", "Hanging Leg Raise", "core", "abs", "bodyweight", spinal_flexion=True),
    _m("leg_raise", "Leg Raise", "core", "abs", "bodyweight", spinal_flexion=True),
    _m("sit_up", "Sit Up", "core", "abs", "bodyweight", spinal_flexion=True),
    _m("crunch", "Crunch", "core", "abs", "bodyweight", spinal_flexion=True),
    _m("bicycle_crunch", "Bicycle Crunch", "core", "abs", "bodyweight", spinal_flexion=True),
    _m("cable_crunch", "Cable Crunch", "core", "abs", "cable", spinal_flexion=True),
    _m("russian_twist", "Russian Twist", "core", "obliques", "bodyweight", spinal_flexion=True),
    _m("cable_woodchop", "Cable Woodchop", "core", "obliques", "cable"),

    # --- olympic ----------------------------------------------------------
    _m("clean", "Clean", "hinge", "posterior_chain", "barbell", True),
    _m("power_clean", "Power Clean", "hinge", "posterior_chain", "barbell", True),
    _m("hang_clean", "Hang Clean", "hinge", "posterior_chain", "barbell", True),
    _m("hang_power_clean", "Hang Power Clean", "hinge", "posterior_chain", "barbell", True),
    _m("clean_pull", "Clean Pull", "hinge", "posterior_chain", "barbell", True),
    _m("snatch", "Snatch", "hinge", "posterior_chain", "barbell", True),
    _m("power_snatch", "Power Snatch", "hinge", "posterior_chain", "barbell", True),
    _m("hang_snatch", "Hang Snatch", "hinge", "posterior_chain", "barbell", True),
    _m("hang_power_snatch", "Hang Power Snatch", "hinge", "posterior_chain", "barbell", True),
    _m("snatch_pull", "Snatch Pull", "hinge", "posterior_chain", "barbell", True),
    _m("clean_and_press", "Clean and Press", "hinge", "full_body", "barbell", True),
    _m("clean_and_jerk", "Clean and Jerk", "hinge", "full_body", "barbell", True),
    _m("clean_complex", "Clean Complex", "hinge", "full_body", "barbell", True),
    _m("snatch_complex", "Snatch Complex", "hinge", "full_body", "barbell", True),
    _m("db_hang_snatch", "Dumbbell Hang Snatch", "hinge", "full_body", "dumbbell", True),

    # --- carry / conditioning --------------------------------------------
    _m("farmers_walk", "Farmer's Walk", "carry", "traps", "dumbbell", True),
    _m("suitcase_carry", "Suitcase Carry", "carry", "obliques", "dumbbell", True, unilateral=True),
    _m("overhead_carry", "Overhead Carry", "carry", "shoulders", "dumbbell", True),
    _m("sled_push", "Sled Push", "carry", "quads", "other", True),
    _m("sled_pull", "Sled Pull", "carry", "posterior_chain", "other", True),
    _m("burpee", "Burpee", "other", "full_body", "bodyweight", True),
    _m("bar_facing_burpee", "Bar-Facing Burpee", "other", "full_body", "bodyweight", True),
    _m("rowing_erg", "Rowing (Erg)", "other", "full_body", "machine", True),
    _m("bike_erg", "Bike Erg", "other", "legs", "machine", True),
    _m("air_bike", "Air Bike", "other", "full_body", "machine", True),
    _m("med_ball_toss", "Med Ball Toss", "other", "full_body", "other", True),
    _m("jump_rope", "Jump Rope", "other", "calves", "other"),
)

MOVEMENT_BY_KEY = {m.key: m for m in MOVEMENTS}

# Explicit vendor-string -> canonical-key map. Keys are normalized (lowercase,
# non-alphanumerics collapsed to single underscores) so one entry covers
# "Bench Press - Barbell", "BENCH_PRESS", "Bench Press (Barbell)".
_ALIAS_SOURCE: dict[str, tuple[str, ...]] = {
    "back_squat": ("back_squat", "backsquat_barbell", "barbell_back_squat", "weighted_back_squats",
                   "squat_barbell", "squat", "barbell_squat", "weighted_squat"),
    "front_squat": ("front_squat", "frontsquat_barbell", "barbell_front_squat"),
    "goblet_squat": ("goblet_squat", "gobletsquat_dumbbell", "goblet_squat_dumbbell"),
    "leg_press": ("leg_press", "leg_press_machine"),
    "bulgarian_split_squat": ("bulgarian_split_squat", "dumbbell_bulgarian_split_squat",
                              "bulgariansquatleft_dumbbell", "bulgariansquatright_dumbbell",
                              "split_squat_rear_foot_elevated_l_dumbbell",
                              "split_squat_rear_foot_elevated_r_dumbbell"),
    "split_squat": ("split_squat", "splitsquatleft_barbell", "splitsquatright_barbell",
                    "split_squat_l_barbell", "split_squat_r_barbell"),
    "walking_lunge": ("walking_lunge", "walking_dumbbell_lunge", "lunge_barbell", "lunge_dumbbell", "lunge"),
    "reverse_lunge": ("reverse_lunge", "reverse_lunge_dumbbell", "db_reverse_lunge_to_stand",
                      "alternatingbackwardlunges_dumbbell", "backward_lunge_alternating_dumbbell"),
    "side_lunge": ("side_lunge",),
    "box_jump": ("box_jump", "boxjump"),
    "squat_jack": ("weighted_squat_jacks", "squat_jack"),
    "deadlift": ("deadlift", "barbell_deadlift", "deadlift_barbell"),
    "romanian_deadlift": ("romanian_deadlift", "romaniandeadlift_barbell", "romanian_deadlift_barbell"),
    "single_leg_rdl": ("single_leg_romanian_deadlift_with_dumbbell", "single_leg_rdl"),
    "sumo_deadlift": ("sumo_deadlift",),
    "good_morning": ("good_morning", "good_morning_barbell"),
    "hip_thrust": ("hip_thrust", "hipthrust_barbell", "barbell_hip_thrust_with_bench",
                   "barbell_hip_thrust_on_floor", "hip_thrust_barbell"),
    "glute_bridge": ("glute_bridge", "weighted_glute_bridge"),
    "single_leg_glute_bridge": ("single_leg_glute_bridge", "lyingsinglelegbridgeright",
                                "lyingsinglelegbridgeleft", "glute_bridge_single_leg_r",
                                "glute_bridge_single_leg_l"),
    "cable_pull_through": ("cable_pull_through", "cable_pullthrough"),
    "kettlebell_swing": ("kettlebell_swing",),
    "back_extension": ("back_extension", "hyperextension"),
    "bench_press": ("bench_press", "benchpress_barbell", "barbell_bench_press", "bench_press_barbell"),
    "incline_bench_press": ("incline_bench_press", "inclinebenchpress_barbell",
                            "incline_barbell_bench_press", "bench_press_incline_barbell",
                            "incline_bench_press_barbell"),
    "decline_bench_press": ("decline_bench_press", "decline_bench_press_barbell"),
    "close_grip_bench_press": ("close_grip_barbell_bench_press", "close_grip_bench_press"),
    "dumbbell_bench_press": ("dumbbell_bench_press", "benchpress_dumbbell", "bench_press_dumbbell"),
    "incline_dumbbell_bench_press": ("incline_dumbbell_bench_press", "inclinebenchpress_dumbbell",
                                     "bench_press_incline_dumbbell", "incline_bench_press_dumbbell"),
    "push_up": ("push_up", "pushup_classic", "pushup"),
    "chest_fly": ("chest_fly_dumbbell", "flye", "chest_fly"),
    "cable_chest_fly": ("chestfly_pulleymachine", "machine_chest_flys", "chest_fly_machine",
                        "cable_chest_fly"),
    "dip": ("body_weight_dip", "dip", "dips"),
    "overhead_press": ("overhead_press", "overheadpress_barbell", "overhead_press_barbell",
                       "overheadpress_smithmachine", "overhead_press_smith_machine"),
    "dumbbell_shoulder_press": ("dumbbell_shoulder_press", "overhead_dumbbell_press",
                                "seatedmilitarypress_dumbbell", "overhead_press_seated_dumbbell",
                                "seated_overhead_press_dumbbell"),
    "military_press": ("military_press",),
    "push_press": ("push_press", "pushpress_barbell", "push_press_barbell"),
    "push_jerk": ("push_jerk", "pushjerk_barbell", "push_jerk_barbell"),
    "jerk_dip": ("jerk_dip",),
    "thruster": ("thruster", "thruster_barbell"),
    "pull_up": ("pull_up", "overhandgrippullups", "pullup"),
    "weighted_pull_up": ("weighted_pull_up",),
    "band_assisted_pull_up": ("band_assisted_pull_up",),
    "chin_up": ("chin_up", "underhandgrippullups"),
    "lat_pulldown": ("lat_pulldown", "latpulldownfront_pulleymachine", "lat_pull_down_front",
                     "lat_pulldown_cable"),
    "close_grip_lat_pulldown": ("close_grip_lat_pulldown",),
    "straight_arm_pulldown": ("standing_cable_pullover", "rope_straight_arm_pulldown",
                              "straight_arm_pulldown"),
    "barbell_row": ("barbell_row", "bent_over_row_with_barbell", "bentoverrow_barbell",
                    "bent_over_row_barbell", "barbell_row_barbell"),
    "pendlay_row": ("pendlay_row",),
    "seated_cable_row": ("seated_cable_row", "seated_cable_row_bar_grip"),
    "dumbbell_row": ("dumbbell_row",),
    "chest_supported_row": ("chest_supported_dumbbell_row", "chest_supported_row"),
    "single_arm_cable_row": ("single_arm_cable_row",),
    "t_bar_row": ("t_bar_row", "tbarrow_barbell", "t_bar_row_barbell"),
    "inverted_row": ("trx_inverted_row", "inverted_row"),
    "face_pull": ("face_pull", "facepull_pulleymachine", "cable_face_pulls"),
    "upright_row": ("upright_row",),
    "shrug": ("shrug", "wide_grip_barbell_shrug", "shrug_dumbbell"),
    "rear_delt_fly": ("rear_delt_reverse_fly_dumbbell", "rear_delt_reverse_fly_machine",
                      "single_arm_standing_cable_reverse_flye", "rear_delt_fly"),
    "biceps_curl": ("biceps_curl", "bicepcurl_dumbbell", "bicep_curl_dumbbell",
                    "alternating_dumbbell_biceps_curl", "dumbbell_biceps_curl", "curl"),
    "barbell_curl": ("bicepcurl_barbell", "bicep_curl_barbell", "standing_ez_bar_biceps_curl",
                     "barbell_curl"),
    "hammer_curl": ("hammer_curl", "dumbbell_hammer_curl", "hammercurl_dumbbell",
                    "hammer_curl_dumbbell"),
    "preacher_curl": ("ez_bar_preacher_curl", "preacher_curl_barbell", "preacher_curl"),
    "reverse_curl": ("reverse_ez_bar_curl", "reverse_curl_barbell", "reverse_curl"),
    "cable_biceps_curl": ("cable_biceps_curl", "behind_the_back_one_arm_cable_curl",
                          "bicep_curl_cable"),
    "triceps_extension": ("triceps_extension", "dumbbell_lying_triceps_extension",
                          "triceps_extension_dumbbell",
                          "singlearmseatedtricepsextensionright_dumbbell",
                          "singlearmseatedtricepsextensionleft_dumbbell",
                          "triceps_extension_single_arm_seated_r_dumbbell",
                          "triceps_extension_single_arm_seated_l_dumbbell"),
    "triceps_pressdown": ("triceps_pressdown", "rope_pressdown", "triceps_rope_pushdown"),
    "overhead_triceps_extension": ("overhead_dumbbell_triceps_extension",
                                   "cable_overhead_triceps_extension",
                                   "overhead_triceps_extension"),
    "skull_crusher": ("skull_crusher", "skullcrusher_barbell", "lying_ez_bar_triceps_extension",
                      "skull_crusher_flat_bench_barbell", "skullcrusher_barbell_"),
    "triceps_kickback": ("cable_kickback", "triceps_kickback_cable", "triceps_kickback"),
    "bench_dip": ("weighted_bench_dip", "bench_dip"),
    "lateral_raise": ("lateral_raise", "dumbbell_lateral_raise", "machine_lateral_raise",
                      "lateral_raise_dumbbell", "lateral_raise_cable"),
    "front_raise": ("front_raise", "cable_front_raise", "dumbbell_front_raise",
                    "plate_front_raise"),
    "band_external_rotation": ("band_external_rotation",),
    "leg_extension": ("leg_extension", "weighted_leg_extensions", "seatedlegextension_pulleymachine",
                      "seated_machine_leg_extension", "single_leg_extensions"),
    "leg_curl": ("leg_curl", "weighted_leg_curl", "lying_leg_curl_machine"),
    "seated_leg_curl": ("seated_leg_curl", "seatedlegcurl_pulleymachine", "seated_machine_leg_curl"),
    "calf_raise": ("calf_raise", "weighted_standing_calf_raise", "standing_calf_raise"),
    "seated_calf_raise": ("seated_calf_raise", "weighted_seated_calf_raise",
                          "seatedcalfraise_pulleymachine", "calf_raise_seated"),
    "hip_abduction": ("hip_abduction", "leg_abduction", "hip_abduction_machine",
                      "gluteabductor_pulleymachine", "glute_abductor_machine"),
    "hip_adduction": ("hip_adduction", "leg_adduction"),
    "plank": ("plank", "frontplankelbow", "front_plank",
              "straight_arm_plank_with_shoulder_touch"),
    "side_plank": ("side_plank", "sideplankelbowleft", "sideplankelbowright",
                   "side_plank_l", "side_plank_r"),
    "dead_bug": ("dead_bug", "deadbug"),
    "bird_dog": ("bird_dog",),
    "ab_wheel": ("kneeling_ab_wheel", "ab_wheel"),
    "hanging_leg_raise": ("hanging_leg_raises", "hangingleg_raises", "hanging_leg_raise"),
    "leg_raise": ("leg_raise",),
    "sit_up": ("sit_up", "negative_sit_up"),
    "crunch": ("crunch",),
    "bicycle_crunch": ("bicycle_crunch",),
    "cable_crunch": ("cable_crunch",),
    "russian_twist": ("russian_twist",),
    "cable_woodchop": ("cable_woodchop",),
    "clean": ("clean", "clean_barbell"),
    "power_clean": ("power_clean", "powerclean_barbell", "power_clean_barbell"),
    "hang_clean": ("hang_clean", "hangclean_barbell", "hang_clean_barbell"),
    "hang_power_clean": ("hang_power_clean", "hangpowerclean_barbell", "hang_power_clean_barbell"),
    "clean_pull": ("clean_pull", "cleanpull_barbell", "clean_pull_barbell"),
    "snatch": ("snatch",),
    "power_snatch": ("power_snatch", "powersnatch_barbell", "power_snatch_barbell"),
    "hang_snatch": ("hang_snatch", "hangsnatch_barbell", "hang_snatch_barbell"),
    "hang_power_snatch": ("hang_power_snatch", "hangpowersnatch_barbell",
                          "hang_power_snatch_barbell"),
    "snatch_pull": ("snatch_pull", "snatchpull_barbell", "snatch_pull_barbell"),
    "clean_and_press": ("clean_and_press",),
    "clean_and_jerk": ("clean_and_jerk", "db_single_arm_clean_and_jerk"),
    "clean_complex": ("clean_complex_pc_hpc_front_squat", "clean_complex"),
    "snatch_complex": ("hang_snatch_complex_hps_hang_squat_snatch", "snatch_complex"),
    "db_hang_snatch": ("db_hang_snatch_alternating", "db_hang_snatch"),
    "farmers_walk": ("farmers_walk", "farmerswalk_dumbbell", "farmers_carry", "farmer_s_walk_dumbbell"),
    "suitcase_carry": ("suitcase_carry_single_arm", "suitcase_carry"),
    "overhead_carry": ("overhead_carry",),
    "sled_push": ("sled_push",),
    "sled_pull": ("sled_pull",),
    "burpee": ("burpee", "burpees"),
    "bar_facing_burpee": ("bar_facing_burpees_lateral", "burpees_over_the_bar",
                          "bar_facing_burpee"),
    "rowing_erg": ("rowing", "rows_machine", "rowing_machine", "concept2_row_erg", "rowing_erg"),
    "bike_erg": ("bike_erg", "concept2_bike_erg"),
    "air_bike": ("air_bike", "assault_bike"),
    "med_ball_toss": ("vertical_toss_med_ball", "verticaltoss_medball", "med_ball_toss"),
    "jump_rope": ("jump_rope", "double_unders"),
}

ALIAS_TO_KEY: dict[str, str] = {}
for _canon, _aliases in _ALIAS_SOURCE.items():
    ALIAS_TO_KEY[_canon] = _canon
    for _a in _aliases:
        ALIAS_TO_KEY[_a] = _canon

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Vendor suffixes that describe equipment or side, not a different movement.
_STRIP_SUFFIXES = (
    "_barbell", "_dumbbell", "_cable", "_machine", "_smith_machine",
    "_pulleymachine", "_bodyweight", "_kettlebell", "_band",
    "_l", "_r", "_left", "_right", "_alternating",
)


def normalize_label(label: str) -> str:
    """Vendor label -> lowercase snake_case slug."""
    return _NON_ALNUM.sub("_", (label or "").strip().lower()).strip("_")


def resolve_exercise(vendor_label: str, vendor_id: str | None = None,
                     vendor_category: str | None = None) -> tuple[str, bool]:
    """Map a vendor exercise to a canonical key.

    Returns (exercise_key, is_known). `is_known` is False when the key was
    auto-derived from the label rather than found in the alias map -- those
    show up in `unmapped_exercises()` for review.

    Tries, in order: the vendor id, the label, the label with equipment/side
    suffixes stripped, then the Garmin category as a coarse fallback.
    """
    candidates: list[str] = []
    if vendor_id:
        candidates.append(normalize_label(vendor_id))
    if vendor_label:
        candidates.append(normalize_label(vendor_label))

    for cand in list(candidates):
        for suffix in _STRIP_SUFFIXES:
            if cand.endswith(suffix):
                candidates.append(cand[: -len(suffix)])

    for cand in candidates:
        hit = ALIAS_TO_KEY.get(cand)
        if hit:
            return hit, True

    # Garmin category as a last resort: "CURL" with an unknown subCategory is
    # still more useful than nothing.
    if vendor_category and vendor_category.upper() not in {"UNKNOWN", "NONE", ""}:
        hit = ALIAS_TO_KEY.get(normalize_label(vendor_category))
        if hit:
            return hit, True

    base = normalize_label(vendor_label or vendor_id or vendor_category or "unknown")
    return base or "unknown", False


def movement_for(key: str) -> Movement | None:
    return MOVEMENT_BY_KEY.get(key)
