"""Pure-function tests for coach.recommend.

The recommender has heavy DB dependencies (vw_exercise_rep_max, mart_daily,
fact_strength_set), so the deterministic pieces — RPE table, rep parsing,
load rounding — are tested in isolation here. Integration tests against
real data are in the smoke-test runs.
"""

from __future__ import annotations

import pytest

from coach.recommend import (
    _parse_rep_count,
    reps_to_pct,
    round_load,
)


# ---- RPE table -------------------------------------------------------------
@pytest.mark.parametrize("reps,expected", [
    (1, 0.95), (3, 0.90), (5, 0.85), (10, 0.70),
    (15, 0.60), (20, 0.55),
])
def test_reps_to_pct_anchor_values(reps: int, expected: float) -> None:
    assert reps_to_pct(reps) == pytest.approx(expected)


def test_reps_to_pct_interpolates_between_anchors() -> None:
    # 11 reps is between 10 (0.70) and 12 (0.65). Linear interp says 0.675.
    out = reps_to_pct(11)
    assert 0.66 <= out <= 0.70


def test_reps_to_pct_below_floor() -> None:
    # < 1 reps: floor at 1RM percentage.
    assert reps_to_pct(0) == 0.85   # default for nonsense input
    assert reps_to_pct(-5) == 0.85


def test_reps_to_pct_above_anchors() -> None:
    # 50 reps still has a table value; 100 reps falls back to 0.30 floor.
    assert reps_to_pct(50) == 0.35
    assert reps_to_pct(100) == 0.30


# ---- rep parsing -----------------------------------------------------------
@pytest.mark.parametrize("prescribed,expected", [
    ("5", 5),
    ("21-15-9", 21),       # descending: highest set governs
    ("5x5", 5),            # sets×reps notation
    ("3 sets of 8", 3),
    (None, None),
    ("", None),
    ("AMRAP", None),
    ("EMOM", None),
    ("max effort", None),
])
def test_parse_rep_count(prescribed, expected) -> None:
    assert _parse_rep_count(prescribed) == expected


# ---- load rounding ---------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    (60.0, 60.0),
    (61.2, 60.0),    # nearest 2.5 down
    (61.3, 62.5),    # nearest 2.5 up
    (118.7, 117.5),  # halfway-ish
    (0.0, 0.0),
    (1.2, 0.0),      # rounds to nearest 2.5
])
def test_round_load_default_increment(raw: float, expected: float) -> None:
    assert round_load(raw) == pytest.approx(expected)


def test_round_load_custom_increment() -> None:
    # 5kg granularity (bumper-only gym). 117 → 115.
    assert round_load(117.0, increment=5.0) == 115.0
    assert round_load(118.0, increment=5.0) == 120.0
