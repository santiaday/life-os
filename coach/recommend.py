"""Load recommender: programmed movement → suggested weight in kg.

Inputs (per programmed movement):
  - exercise_template_id (or analog_template_id for novel exercises)
  - prescribed_load_kg / prescribed_load_pct (parser output)
  - prescribed_reps  (parser output, may be a string like '21-15-9')
  - class_date (for the recovery lookup)

Strategy (in order):
  1. If prescribed_load_kg set → use it. Recommendation = exact prescribed.
  2. If prescribed_load_pct set → multiply by 1RM (or estimated from 3RM/5RM).
  3. Otherwise → infer % from rep count via the RPE table.

Then apply two adjustments:
  - Recovery: if today's recovery_score < 50 → multiply by 0.92.
  - Last-RPE: if the most recent attempt at the same exercise + similar
    rep range was logged with rpe ≥ 9 → no bump (hold load); rpe ≤ 7 →
    +2.5 kg vs that load.

Round to nearest 2.5 kg. Write a human-readable reasoning string explaining
the math so the user (and future Claude conversations) can audit the call.

Novel-exercise path: read pushpress_analog_ratio for (analog → base) ratio.
If no row, fall back to ratio=1.0 with a low-confidence note.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date

from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)


# ---- RPE table (rep count → conservative % of 1RM) ------------------------
# Blended Epley/Brzycki, conservative side. Hits the rep counts we actually
# see in CrossFit programming. Linear interp for in-between values.
RPE_TABLE: dict[int, float] = {
    1: 0.95, 2: 0.92, 3: 0.90, 4: 0.87, 5: 0.85,
    6: 0.82, 7: 0.79, 8: 0.75, 9: 0.72, 10: 0.70,
    12: 0.65, 15: 0.60, 20: 0.55, 30: 0.45, 50: 0.35,
}


def reps_to_pct(reps: int) -> float:
    """Map a rep count to a conservative % of 1RM. Handles arbitrary values
    via linear interpolation between the anchors. Floors at 0.30 (any
    higher-rep work past 50 reps is metabolic; load is governed by recovery)."""
    if reps <= 0:
        return 0.85
    if reps in RPE_TABLE:
        return RPE_TABLE[reps]
    # Find the bracket and interp.
    keys = sorted(RPE_TABLE.keys())
    if reps < keys[0]:
        return RPE_TABLE[keys[0]]
    if reps > keys[-1]:
        return 0.30
    for lo, hi in itertools.pairwise(keys):
        if lo <= reps <= hi:
            t = (reps - lo) / (hi - lo)
            return RPE_TABLE[lo] + t * (RPE_TABLE[hi] - RPE_TABLE[lo])
    return 0.50


# ---- types -----------------------------------------------------------------
@dataclass
class Recommendation:
    recommended_load_kg: float | None
    confidence: float                 # 0-1
    reasoning: str                    # human-readable explanation


# ---- helpers ---------------------------------------------------------------
def round_load(value: float, *, increment: float | None = None) -> float:
    inc = increment or settings.COACH_LOAD_ROUNDING_KG
    if inc <= 0:
        return round(value, 1)
    return round(value / inc) * inc


def _parse_rep_count(prescribed_reps: str | None) -> int | None:
    """Pull a representative rep count from the parser's freeform string.

    For descending schemes ('21-15-9') we use the FIRST set's reps — that's
    the highest-load demand. For 'AMRAP' or 'EMOM' we return None (rep
    count varies; load picked by feel)."""
    if prescribed_reps is None:
        return None
    s = prescribed_reps.strip()
    if not s:
        return None
    if any(tok in s.lower() for tok in ("amrap", "emom", "max", "—", "open")):
        return None
    # First number we see wins. Handles "21-15-9", "5", "5x5", "5×5".
    digits = ""
    for ch in s:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    if digits:
        try:
            return int(digits)
        except ValueError:
            return None
    return None


# ---- 1RM estimation --------------------------------------------------------
def _best_known_1rm(template_id: str) -> tuple[float, int, date] | None:
    """Best 1RM estimate for the exercise. Auto-progresses: picks the higher
    of (direct 1RM, max Epley estimate from any working set):
        1RM ≈ weight × (1 + reps/30)

    Returns (estimated_1rm_kg, anchor_reps, anchor_day) or None if no
    history. Anchor_reps=1 when the direct 1RM wins, else the rep count of
    the working set whose Epley estimate beats it.

    Why the max() rather than just the direct 1RM: as the user trains, their
    sub-maximal working sets often imply a higher 1RM than their last
    direct attempt. Without this, a synthetic seeded 1RM stays frozen even
    after the user crushes a 5×5 at a weight that would Epley-estimate to
    a higher 1RM. With it, every PR-quality working set bumps the baseline
    automatically."""
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT max_weight_kg AS w, last_hit_day AS day
              FROM vw_exercise_rep_max
             WHERE exercise_template_id = %s AND rep_count = 1
             ORDER BY max_weight_kg DESC
             LIMIT 1
            """,
            [template_id],
        )
        direct = cur.fetchone()
        cur.execute(
            """
            SELECT weight_kg AS w, reps AS r, day
              FROM fact_strength_set
             WHERE exercise_template_id = %s
               AND set_type IN ('normal', 'failure')
               AND weight_kg IS NOT NULL
               AND reps > 0
             ORDER BY (weight_kg * (1 + reps::float / 30.0)) DESC
             LIMIT 1
            """,
            [template_id],
        )
        epley = cur.fetchone()

    direct_kg = float(direct["w"]) if direct and direct["w"] is not None else None
    epley_kg = (
        float(epley["w"]) * (1.0 + int(epley["r"]) / 30.0)
        if epley and epley["w"] is not None else None
    )

    if direct_kg is None and epley_kg is None:
        return None
    if epley_kg is None or (direct_kg is not None and direct_kg >= epley_kg):
        return float(direct_kg), 1, direct["day"]
    return float(epley_kg), int(epley["r"]), epley["day"]


def _direct_rep_max(template_id: str, rep_target: int, *, tolerance: int = 1) -> dict | None:
    """Best weight ever done at exactly rep_target reps (within ±tolerance).
    Returns {weight_kg, rep_count, last_hit_day} or None.

    Used by the recommender to prefer direct evidence over a 1RM-derived
    formula when the user has actually trained at that rep range."""
    lo, hi = max(1, rep_target - tolerance), rep_target + tolerance
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT max_weight_kg AS weight_kg, rep_count, last_hit_day
              FROM vw_exercise_rep_max
             WHERE exercise_template_id = %s
               AND rep_count BETWEEN %s AND %s
             ORDER BY max_weight_kg DESC
             LIMIT 1
            """,
            [template_id, lo, hi],
        )
        return cur.fetchone()


def _last_attempt(
    template_id: str,
    *,
    rep_target: int | None,
    rep_tolerance: int = 2,
) -> dict | None:
    """Most recent working set on this exercise within ±tolerance of the
    target rep count. Used for the last-RPE adjustment. Returns the raw
    row dict (weight_kg, reps, rpe, day) or None."""
    if rep_target is None:
        return None
    lo, hi = max(1, rep_target - rep_tolerance), rep_target + rep_tolerance
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT weight_kg, reps, rpe, day
              FROM fact_strength_set
             WHERE exercise_template_id = %s
               AND set_type IN ('normal', 'failure')
               AND weight_kg IS NOT NULL
               AND reps BETWEEN %s AND %s
             ORDER BY day DESC, weight_kg DESC
             LIMIT 1
            """,
            [template_id, lo, hi],
        )
        return cur.fetchone()


def _recovery_score(class_date: date) -> int | None:
    """Today's Whoop recovery score from mart_daily, if available. Returns
    None for future-date recommendations (we don't have tomorrow's recovery
    yet — the orchestrator re-runs the day-of)."""
    with tx() as c, c.cursor() as cur:
        cur.execute(
            "SELECT recovery_score FROM mart_daily WHERE day = %s",
            [class_date],
        )
        row = cur.fetchone()
    if not row or row["recovery_score"] is None:
        return None
    return int(row["recovery_score"])


def _analog_ratio(analog_id: str, base_id: str) -> float | None:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT ratio FROM pushpress_analog_ratio
             WHERE analog_template_id = %s AND base_template_id = %s
            """,
            [analog_id, base_id],
        )
        row = cur.fetchone()
    if not row:
        return None
    return float(row["ratio"])


# ---- public API ------------------------------------------------------------
def recommend(
    *,
    template_id: str | None,
    analog_template_id: str | None,
    prescribed_load_kg: float | None,
    prescribed_load_pct: float | None,
    prescribed_reps: str | None,
    class_date: date,
) -> Recommendation:
    """Compute a recommended load + reasoning for one programmed movement.

    Always returns a Recommendation. When we have no signal at all (no
    history, no prescribed values), recommended_load_kg=None and the
    reasoning string says why."""
    target_id = template_id or analog_template_id
    is_analog = template_id is None and analog_template_id is not None

    # ---- need a 1RM estimate to proceed ----------------------------------
    # NOTE: prescribed_load_kg from the WOD (e.g. "Thrusters 95/65") is
    # GUIDANCE for the average athlete, NOT a personal prescription. We
    # mention it in the reasoning string but compute the recommended load
    # from the user's own history. prescribed_load_pct (e.g. "80% of 1RM")
    # IS personal because it scales with the user's actual 1RM, so we
    # honor that one. Per user feedback 2026-05-08: "I often can't do the
    # prescribed weight."
    if not target_id:
        return Recommendation(
            recommended_load_kg=None,
            confidence=0.0,
            reasoning="No exercise match (novel + no analog). Pick a load by feel.",
        )

    one_rm = _best_known_1rm(target_id)
    rep_target = _parse_rep_count(prescribed_reps)

    if one_rm is None:
        rx_hint = (
            f" WOD Rx is {prescribed_load_kg:g} kg — start there if it feels manageable."
            if prescribed_load_kg is not None and prescribed_load_kg > 0 else ""
        )
        return Recommendation(
            recommended_load_kg=None,
            confidence=0.1,
            reasoning=(
                f"No prior working sets for this exercise in fact_strength_set."
                f"{rx_hint} Once you log a set, future runs will dial the load in."
            ),
        )

    one_rm_kg, anchor_reps, anchor_day = one_rm
    one_rm_note = (
        f"1RM = {one_rm_kg:.1f} kg (direct, hit {anchor_day.isoformat()})"
        if anchor_reps == 1
        else f"1RM ≈ {one_rm_kg:.1f} kg (Epley est. from {anchor_reps}-rep set on {anchor_day.isoformat()})"
    )

    # Pick the % to use.
    if prescribed_load_pct is not None and prescribed_load_pct > 0:
        pct = float(prescribed_load_pct)
        pct_note = f"coach prescribed {pct*100:.0f}% of 1RM"
    elif rep_target is not None:
        pct = reps_to_pct(rep_target)
        pct_note = f"reps={rep_target} → {pct*100:.0f}% via RPE table"
    else:
        # No load, no reps — no signal. Default to 70% as a moderate suggestion.
        pct = 0.70
        pct_note = "no rep count parsed → default 70%"

    base_load = one_rm_kg * pct
    notes: list[str] = [one_rm_note, pct_note, f"base = {base_load:.1f} kg"]

    # If the WOD wrote an Rx weight, note it for context but don't enforce.
    if prescribed_load_kg is not None and prescribed_load_kg > 0:
        notes.append(
            f"WOD Rx is {prescribed_load_kg:g} kg (guidance, not enforced)"
        )

    # If the user has direct evidence at this rep range and that weight is
    # higher than the formula suggests, trust the direct number. The user
    # has done it before, so they can do it again. Capped further down by
    # the last-RPE check to avoid recommending a grindy weight twice.
    if (
        rep_target is not None
        and prescribed_load_kg is None
        and prescribed_load_pct is None
        and not is_analog
    ):
        direct = _direct_rep_max(target_id, rep_target)
        if direct and direct.get("weight_kg") is not None:
            direct_kg = float(direct["weight_kg"])
            if direct_kg > base_load:
                notes.append(
                    f"direct {direct['rep_count']}-rep PR = {direct_kg:.1f} kg "
                    f"on {direct['last_hit_day']} > formula → use direct"
                )
                base_load = direct_kg

    # METCON BLEND: if the WOD has a Rx weight AND we're in metcon territory
    # (high reps, format ∈ amrap/rft/for_time/chipper), blend the Rx with
    # the formula. Metcons are programmed assuming the athlete CAN handle
    # the Rx load — typically at 50-65% of 1RM, which is where they're
    # selecting weights anyway. Pure formula gives suspicious lowballs
    # (e.g. 20kg for a 70kg Rx Squat Clean) because the RPE table is
    # tuned for strength sessions, not metcon sustainability.
    is_metcon_high_rep = (
        prescribed_load_kg is not None
        and rep_target is not None
        and rep_target >= 8
    )
    if is_metcon_high_rep:
        blend_kg = max(base_load, float(prescribed_load_kg) * 0.7)
        # Cap at the Rx — never recommend HEAVIER than the WOD prescription
        # for metcon volume.
        blend_kg = min(blend_kg, float(prescribed_load_kg))
        if abs(blend_kg - base_load) > 0.5:
            notes.append(
                f"metcon high-rep blend: 70% of Rx ({prescribed_load_kg:g} kg) "
                f"= {prescribed_load_kg * 0.7:.1f} kg, vs formula {base_load:.1f} kg "
                f"→ {blend_kg:.1f} kg"
            )
            base_load = blend_kg

    # Analog ratio (novel-exercise path).
    if is_analog:
        # Conservative: if no explicit ratio entry, use 0.85 as a generic
        # "different exercise, expect to lift less" haircut. Surfaces in the
        # reasoning so the user knows it's a guess.
        ratio = _analog_ratio(analog_template_id, analog_template_id) or 0.85
        base_load *= ratio
        notes.append(
            f"analog adjustment ×{ratio:g} (no direct history for prescribed exercise)"
        )

    # ---- recovery adjustment --------------------------------------------
    rec = _recovery_score(class_date)
    if rec is not None and rec < settings.COACH_RECOVERY_DELOAD_THRESHOLD:
        m = settings.COACH_RECOVERY_DELOAD_MULTIPLIER
        base_load *= m
        notes.append(
            f"recovery {rec} < {settings.COACH_RECOVERY_DELOAD_THRESHOLD} "
            f"→ deload ×{m:g}"
        )
    elif rec is not None:
        notes.append(f"recovery {rec} OK, no deload")
    else:
        notes.append("recovery unknown (future date or no Whoop data)")

    rounded = round_load(base_load)

    # ---- last-RPE microadjustment ---------------------------------------
    last = _last_attempt(target_id, rep_target=rep_target)
    if last and last.get("rpe") is not None:
        last_rpe = float(last["rpe"])
        last_w = float(last["weight_kg"])
        if last_rpe >= 9.0:
            # Last attempt was already grindy at this rep range. Cap the
            # bump: never recommend MORE than what the user just struggled
            # with.
            if rounded > last_w:
                notes.append(
                    f"last attempt {last_w:g} kg × {last['reps']} @ RPE {last_rpe} "
                    f"was grindy → cap at {last_w:g} kg"
                )
                rounded = round_load(last_w)
        elif last_rpe <= 7.0:
            bumped = round_load(last_w + settings.COACH_LOAD_ROUNDING_KG)
            if bumped > rounded:
                notes.append(
                    f"last attempt {last_w:g} kg × {last['reps']} @ RPE {last_rpe} "
                    f"left room → bump to {bumped:g} kg"
                )
                rounded = bumped

    confidence = 0.85 if not is_analog else 0.55
    if rec is None:
        confidence -= 0.1

    return Recommendation(
        recommended_load_kg=float(rounded),
        confidence=round(confidence, 2),
        reasoning="; ".join(notes),
    )
