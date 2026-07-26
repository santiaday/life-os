"""Unit tests for the canonical vocabularies and unit conversions.

These are the pieces that silently corrupt everything downstream if they're
wrong: a mis-resolved exercise splits one movement's history into two, and a
missed unit conversion puts grams into a kilogram column.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from unify import biomarkers, taxonomy
from unify.common import g_to_kg, lb_to_kg, local_day, ms_to_s, num
from unify.dedupe import _same_event

# ---------------------------------------------------------------------------
# Activity types
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source,vendor,expected", [
    ("whoop", "weightlifting_msk", "strength"),
    ("whoop", "box-fitness", "conditioning"),
    ("whoop", "hot-yoga", "mobility"),
    ("whoop", "pickleball", "racquet"),
    ("garmin", "strength_training", "strength"),
    ("garmin", "treadmill_running", "run"),
    ("garmin", "indoor_cycling", "cycle"),
    ("garmin", "tennis_v2", "racquet"),
    ("garmin", "lap_swimming", "swim"),
])
def test_known_activity_types(source, vendor, expected):
    ctype, _, _ = taxonomy.canonical_activity_type(source, vendor)
    assert ctype == expected


def test_unknown_activity_falls_back_to_substring_heuristic():
    # Not in any map, but obviously a run.
    ctype, is_res, is_cardio = taxonomy.canonical_activity_type("garmin", "ultra_running")
    assert ctype == "run"
    assert is_cardio and not is_res


def test_truly_unknown_activity_is_other_not_a_crash():
    ctype, is_res, _ = taxonomy.canonical_activity_type("garmin", "underwater_basketweaving")
    assert ctype == "other"
    assert is_res is False


def test_strength_is_the_only_resistance_type():
    for t in taxonomy.CANONICAL_ACTIVITY_TYPES:
        _, is_res, _ = taxonomy.canonical_activity_type("whoop", t)
        assert is_res == (t == "strength")


# ---------------------------------------------------------------------------
# Exercises
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,vendor_id,expected", [
    # the same movement as each vendor spells it
    ("Bench Press - Barbell", "BENCHPRESS_BARBELL", "bench_press"),
    ("Bench Press (Barbell)", "79D0BB3A", "bench_press"),
    ("BARBELL_BENCH_PRESS", "BARBELL_BENCH_PRESS", "bench_press"),
    ("Back Squat - Barbell", "BACKSQUAT_BARBELL", "back_squat"),
    ("BARBELL_BACK_SQUAT", None, "back_squat"),
    ("WEIGHTED_BACK_SQUATS", None, "back_squat"),
    ("Lat Pull Down - Front", "LATPULLDOWNFRONT_PULLEYMACHINE", "lat_pulldown"),
    ("LAT_PULLDOWN", None, "lat_pulldown"),
    ("Romanian Deadlift (Barbell)", "2B4B7310", "romanian_deadlift"),
    ("ROMANIAN_DEADLIFT", None, "romanian_deadlift"),
])
def test_exercise_aliases_converge(label, vendor_id, expected):
    key, known = taxonomy.resolve_exercise(label, vendor_id)
    assert known, f"{label!r} should resolve"
    assert key == expected


def test_garmin_mislabelled_category_uses_subcategory():
    # Garmin files leg extensions under the CRUNCH category. Trusting the
    # category would put a quad isolation movement in the core bucket.
    key, known = taxonomy.resolve_exercise(
        "WEIGHTED_LEG_EXTENSIONS", vendor_id="WEIGHTED_LEG_EXTENSIONS",
        vendor_category="CRUNCH")
    assert known
    assert key == "leg_extension"
    assert taxonomy.movement_for(key).muscle == "quads"


def test_unknown_exercise_is_derived_not_dropped():
    key, known = taxonomy.resolve_exercise("Zercher Good Morning Complex")
    assert not known
    assert key == "zercher_good_morning_complex"


def test_spinal_flexion_flags_only_loaded_lumbar_flexion():
    flexion = {"sit_up", "crunch", "bicycle_crunch", "cable_crunch",
               "russian_twist", "hanging_leg_raise", "leg_raise"}
    for m in taxonomy.MOVEMENTS:
        assert m.spinal_flexion == (m.key in flexion), m.key
    # anti-extension work is explicitly NOT flexion
    for key in ("plank", "side_plank", "dead_bug", "bird_dog", "ab_wheel"):
        assert taxonomy.movement_for(key).spinal_flexion is False


def test_every_alias_points_at_a_real_movement():
    for alias, key in taxonomy.ALIAS_TO_KEY.items():
        assert key in taxonomy.MOVEMENT_BY_KEY, f"{alias} -> unknown key {key}"


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

def test_unit_conversions():
    assert g_to_kg(43062) == pytest.approx(43.062)
    assert lb_to_kg(220.462) == pytest.approx(100.0, abs=1e-3)
    assert ms_to_s(3287876.953125) == pytest.approx(3287.876953125)
    assert g_to_kg(None) is None and lb_to_kg(None) is None and ms_to_s(None) is None


def test_num_rejects_garbage_without_raising():
    assert num("") is None
    assert num("n/a") is None
    assert num(None) is None
    assert num("12.5") == 12.5
    assert num(float("nan")) is None


def test_local_day_uses_new_york_not_utc():
    # 2026-07-27 01:30 UTC is still 2026-07-26 in New York.
    ts = datetime(2026, 7, 27, 1, 30, tzinfo=UTC)
    assert local_day(ts).isoformat() == "2026-07-26"


# ---------------------------------------------------------------------------
# Dedupe predicate
# ---------------------------------------------------------------------------

def _act(source, start, minutes, atype="strength", day=None):
    s = datetime(2026, 7, 20, start, 0, tzinfo=UTC)
    return {
        "source": source, "start_ts": s,
        "end_ts": s + timedelta(minutes=minutes),
        "activity_type": atype, "day": (day or s.date()),
    }


def test_same_session_from_two_wearables_matches():
    whoop = _act("whoop", 13, 60)
    garmin = _act("garmin", 13, 58)
    assert _same_event(whoop, garmin)


def test_back_to_back_sessions_do_not_match():
    first = _act("whoop", 13, 50)
    second = _act("whoop", 15, 50)
    assert not _same_event(first, second)


def test_long_block_does_not_swallow_a_short_session():
    # The bug this guards: a 194-minute Apple activity block absorbing a
    # 50-minute pickleball game, then chaining three games into one cluster.
    block = _act("zero", 13, 194, atype="other")
    game = _act("whoop", 13, 50, atype="racquet")
    assert not _same_event(block, game)


def test_incompatible_types_never_match():
    lift = _act("whoop", 13, 60, atype="strength")
    run = _act("garmin", 13, 60, atype="run")
    assert not _same_event(lift, run)


def test_untyped_source_is_a_wildcard():
    lift = _act("whoop", 13, 60, atype="strength")
    apple = _act("zero", 13, 58, atype="other")
    assert _same_event(lift, apple)


def test_fabricated_timestamps_match_on_day_and_type():
    # Lose It rows are all stamped 17:00 because the export has no clock time.
    loseit = _act("loseit", 17, 30, atype="run")
    whoop_same_day = _act("whoop", 7, 30, atype="run")
    whoop_other_type = _act("whoop", 7, 30, atype="strength")
    assert _same_event(whoop_same_day, loseit)
    assert not _same_event(whoop_other_type, loseit)


# ---------------------------------------------------------------------------
# Biomarkers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vendor,expected", [
    ("alt", "alt"),
    ("alanine_aminotransferase", "alt"),
    ("ast", "ast"),
    ("aspartate_aminotransferase", "ast"),
    ("alkaline_phosphatase", "alp"),
    ("alkaline_phosotase", "alp"),          # vendor typo
    ("egfr", "egfr"),
    ("estimated_glomerular_filtration_rate", "egfr"),
    ("glucose_fasting", "glucose"),
    ("vitamin_b12_cobalamin", "vitamin_b12"),
    ("urea_nitrogen_bun", "bun"),
    ("blood_urea_nitrogen", "bun"),
    ("carbon_dioxide_co2", "co2"),
    ("erythrocyte_sedimentation_rate_esr_wes", "esr"),   # truncated at 38 chars
])
def test_biomarker_aliases_converge(vendor, expected):
    key, known = biomarkers.resolve(vendor)
    assert known, f"{vendor!r} should resolve"
    assert key == expected


@pytest.mark.parametrize("vendor", [
    "vitamin_d_25_oh",       # slug of "Vitamin D, 25-OH" — the spelling a chat
    "vitamin_d_25oh",        # upload produces, which an alias list missed
    "vitamin_d_25_hydroxy",
    "VITAMIN D 25 OH",
])
def test_punctuation_variants_of_one_analyte_converge(vendor):
    key, known = biomarkers.resolve(vendor)
    assert known and key == "vitamin_d_25oh"


def test_unknown_biomarker_is_not_invented():
    key, known = biomarkers.resolve("some_novel_assay")
    assert key is None and known is False


def test_every_biomarker_alias_points_at_a_real_biomarker():
    for alias, key in biomarkers.ALIAS_TO_KEY.items():
        assert key in biomarkers.BY_KEY, f"{alias} -> unknown key {key}"


def test_status_prefers_source_reference_range():
    # Catalogue says ALT normal is 9-46. A source supplying its own range wins.
    assert biomarkers.status_for("alt", 50) == "high"
    assert biomarkers.status_for("alt", 50, ref_low=0, ref_high=60) == "normal"


def test_status_is_none_when_no_range_known():
    assert biomarkers.status_for("ana_screen", 1) is None
