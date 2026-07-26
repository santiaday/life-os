"""Unit tests for the data-quality rules.

The two artifacts that motivated this layer are encoded as regression tests:
a 239.4 lb weight reading among eight 176-179 lb readings, and a body-fat
delta that implied losing 4.94 kg of fat while gaining 1.87 kg of lean in
30 days.
"""

from __future__ import annotations

from datetime import date

from unify import quality


class _FakeCursor:
    def __init__(self, rows_by_query: dict[str, list[dict]]):
        self._rows_by_query = rows_by_query
        self._current: list[dict] = []

    def execute(self, sql, params=None):
        for needle, rows in self._rows_by_query.items():
            if needle in sql:
                self._current = rows
                return
        self._current = []

    def fetchall(self):
        return self._current

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, rows_by_query):
        self._rows_by_query = rows_by_query

    def cursor(self):
        return _FakeCursor(self._rows_by_query)


def _body_row(uid, day, weight=None, bf=None, lean=None, method="scale"):
    return {"measurement_uid": uid, "source": "test", "method": method,
            "day": day, "weight_kg": weight, "body_fat_pct": bf,
            "lean_mass_kg": lean}


def test_out_of_range_weight_is_rejected():
    rows = [_body_row("a", date(2024, 9, 30), weight=250.0)]
    flags = quality.check_body_composition(_FakeConn({"fact_body_composition": rows}))
    assert any(f["rule"] == "plausibility" and f["metric"] == "weight_kg"
               and f["severity"] == "reject" for f in flags)


def test_the_239_lb_reading_is_caught_by_dispersion():
    # Eight readings on one day: seven around 80 kg (176-179 lb) and one at
    # 108.6 kg (239.4 lb). All are inside the absolute plausibility bounds, so
    # only the same-day dispersion rule can catch it.
    day = date(2024, 9, 30)
    rows = [_body_row(f"ok{i}", day, weight=w)
            for i, w in enumerate([79.8, 80.1, 80.3, 80.0, 81.2, 80.6, 80.9])]
    rows.append(_body_row("bad", day, weight=108.59))
    flags = quality.check_body_composition(_FakeConn({"fact_body_composition": rows}))
    dispersion = [f for f in flags if f["rule"] == "dispersion"]
    assert len(dispersion) == 1
    assert dispersion[0]["row_key"] == "bad"
    assert dispersion[0]["severity"] == "reject"


def test_normal_daily_variation_is_not_flagged():
    day = date(2026, 7, 20)
    rows = [_body_row(f"n{i}", day, weight=w)
            for i, w in enumerate([79.8, 80.4, 80.1, 79.9, 80.6])]
    flags = quality.check_body_composition(_FakeConn({"fact_body_composition": rows}))
    assert [f for f in flags if f["rule"] == "dispersion"] == []


def test_impossible_body_fat_rate_of_change_is_flagged():
    # -4.94 kg fat in 30 days is ~0.6 percentage points/day of body fat, which
    # is under the limit; the Sep 2024 artifact was a single-day jump.
    rows = [
        _body_row("d1", date(2024, 9, 1), weight=80.0, bf=20.0),
        _body_row("d2", date(2024, 9, 2), weight=80.0, bf=25.85),
    ]
    flags = quality.check_body_composition(_FakeConn({"fact_body_composition": rows}))
    assert any(f["rule"] == "rate_of_change" and f["metric"] == "body_fat_pct"
               for f in flags)


def test_gradual_body_fat_change_is_not_flagged():
    rows = [
        _body_row("g1", date(2024, 9, 1), weight=80.0, bf=20.0),
        _body_row("g2", date(2024, 10, 1), weight=78.0, bf=18.0),
    ]
    flags = quality.check_body_composition(_FakeConn({"fact_body_composition": rows}))
    assert [f for f in flags if f["rule"] == "rate_of_change"] == []


def test_rejected_reading_does_not_poison_the_rate_series():
    # A single absurd reading between two good ones must not generate two
    # extra rate-of-change flags on its neighbours.
    rows = [
        _body_row("r1", date(2026, 7, 1), weight=80.0),
        _body_row("r2", date(2026, 7, 2), weight=250.0),   # rejected
        _body_row("r3", date(2026, 7, 3), weight=80.2),
    ]
    flags = quality.check_body_composition(_FakeConn({"fact_body_composition": rows}))
    rate = [f for f in flags if f["rule"] == "rate_of_change"]
    assert rate == []


def test_nutrition_macros_that_dont_add_up_are_flagged():
    rows = [{"day": date(2026, 7, 1), "source": "cal_ai", "energy_kcal": 2000,
             "protein_g": 50, "carbs_g": 50, "fat_g": 10}]   # implies 490 kcal
    flags = quality.check_nutrition(_FakeConn({"fact_nutrition_daily": rows}))
    assert any(f["rule"] == "consistency" for f in flags)


def test_nutrition_macros_within_tolerance_are_not_flagged():
    # 180*4 + 200*4 + 70*9 = 2150 vs a stated 2100 -> inside 25%.
    rows = [{"day": date(2026, 7, 1), "source": "cronometer", "energy_kcal": 2100,
             "protein_g": 180, "carbs_g": 200, "fat_g": 70}]
    flags = quality.check_nutrition(_FakeConn({"fact_nutrition_daily": rows}))
    assert [f for f in flags if f["rule"] == "consistency"] == []


def test_sleep_bounds():
    rows = [
        {"sleep_uid": "s1", "day": date(2026, 7, 1), "source": "zero",
         "asleep_s": 20 * 3600, "efficiency_pct": 95, "is_nap": False},
        {"sleep_uid": "s2", "day": date(2026, 7, 2), "source": "whoop",
         "asleep_s": 7 * 3600, "efficiency_pct": 92, "is_nap": False},
    ]
    flags = quality.check_sleep(_FakeConn({"fact_sleep_session": rows}))
    assert len(flags) == 1
    assert flags[0]["row_key"] == "s1"
