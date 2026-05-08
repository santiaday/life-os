"""Pure-function tests for coach.normalizer.normalize_pattern.

The pattern function is the alias-cache key. It's run thousands of times
across the alias auto-learning loop, so it has to be deterministic and
not surprise on the freeform load notation real coaches write.
"""

from __future__ import annotations

import pytest

from coach.normalizer import normalize_pattern


@pytest.mark.parametrize("raw,expected", [
    # Drop trailing load notation.
    ("KB swings 53#", "kb swings"),
    ("Thruster 95/65", "thruster"),
    ("Deadlifts 102/70 kg", "deadlifts"),
    ("Power Clean @ 60% 1RM", "power clean"),
    # Drop leading rep counts and inches.
    ('50 Box Step-ups @ 20"', "box step ups"),
    # "Cal" without a leading digit survives — that's fine, the alias cache
    # treats "cal echo bike" and "echo bike" as separate entries pointing
    # to the same Air Bike template.
    ("15/12 Cal Echo Bike", "cal echo bike"),
    ("21 Pull-ups", "pull ups"),
    # Sets-x-reps notation strips cleanly.
    ("5x5 Back Squat", "back squat"),
    # Punctuation collapse — hyphens become spaces, quotes drop.
    ('Box Step-up @ 20"', "box step up"),
    ("\"Get on your hands'\"", "get on your hands"),
    # Idempotent across whitespace + case.
    ("  Power   Snatch  ", "power snatch"),
    ("BACK SQUAT", "back squat"),
    # Real coach text we've seen.
    ("Wall Walk", "wall walk"),
    ("Echo Bike (calories)", "echo bike calories"),
])
def test_normalize_pattern(raw: str, expected: str) -> None:
    assert normalize_pattern(raw) == expected


def test_normalize_pattern_empty():
    assert normalize_pattern("") == ""
    assert normalize_pattern("   ") == ""
