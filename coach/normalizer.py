"""Movement-name normalizer: raw_text → dim_hevy_exercise.exercise_template_id.

Three-tier matcher:

  1. Exact normalized lookup against pushpress_movement_alias.pattern.
     Cheap, ~0ms. The alias table is auto-grown — every successful match
     writes the normalized form back so the next encounter is a hit.
  2. ILIKE substring match against dim_hevy_exercise.title (and the alias
     table). Catches obvious cases ('Back Squat' → 'Squat (Barbell)').
  3. LLM-assisted match using Haiku. Send the raw_text + a shortlist of
     plausible candidates (filtered by movement-pattern keyword) and ask
     for the best match plus a confidence score. Threshold: ≥ 0.85.

Tier 3 misses → mark `novel_exercise=true`, set `analog_exercise_template_id`
to the closest match anyway, log to the review queue. Routine creation does
NOT block on review — we use the analog suggestion.

After every match (any tier), the normalized form is written back to
pushpress_movement_alias so the next time we see the same string it's a
free exact lookup.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable

import anthropic

from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)


# ---- types -----------------------------------------------------------------
@dataclass
class MatchResult:
    """One movement → exercise resolution."""
    exercise_template_id: str | None
    exercise_title: str | None
    confidence: float
    method: str            # 'alias_exact' | 'title_ilike' | 'llm' | 'unmatched'
    novel_exercise: bool
    analog_template_id: str | None
    analog_title: str | None


# ---- normalization ---------------------------------------------------------
# Multi-pass: each regex peels off one layer of load/rep noise. Order matters
# — the M/F-split pattern has to fire BEFORE the simple weight pattern, or
# we strip "95" first and leave a stray "/65".
_RE_CROSSFIT_SPLIT = re.compile(
    r"(?:@\s*)?\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?\s*(?:kg|lb|lbs)?",
    re.IGNORECASE,
)
_RE_RPE = re.compile(r"@\s*rpe\s*\d+(?:\.\d+)?", re.IGNORECASE)
_RE_PCT = re.compile(r"@?\s*\d+(?:\.\d+)?\s*%(?:\s*1rm)?", re.IGNORECASE)
_RE_LOAD_UNIT = re.compile(
    r"(?:@\s*)?\d+(?:\.\d+)?\s*(?:kg|lb|lbs|#|cal)\b",
    re.IGNORECASE,
)
_RE_INCHES = re.compile(r'\d+(?:\.\d+)?\s*"')
_RE_SETS_X_REPS = re.compile(r"\b\d+\s*[x×]\s*\d+\b", re.IGNORECASE)
_RE_LEADING_NUM = re.compile(r"^\s*\d+(?:\.\d+)?\s*")
_RE_BARE_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_RE_PUNCT = re.compile(r"[^\w\s]")


_RE_PARSER_FLAGS = re.compile(r"\((superset|partner|complex)\)", re.IGNORECASE)


def normalize_pattern(text: str) -> str:
    """Lower-case, strip load/rep notation + parser flags, collapse whitespace.

    Multi-pass cleanup that's aggressive about dropping numeric noise AND
    parser-added structural flags ("(superset)", "(partner)") so the
    surviving content is JUST the movement name. Examples:
        'KB swings 53#'              → 'kb swings'
        'Thruster 95/65'             → 'thruster'
        'Power Clean @ 60% 1RM'      → 'power clean'
        '50 Box Step-ups @ 20"'      → 'box step ups'
        '5x5 Back Squat'             → 'back squat'
        'Z-Press (superset)'         → 'z press'    ← was leaking into aliases
        'Power Clean (partner)'      → 'power clean'

    The parser flags removal is critical: without it, the alias cache ends
    up with separate entries like 'z press' and 'z press superset' that
    DON'T match the same pattern on future parses. Strip first."""
    s = text.strip().lower()
    s = _RE_PARSER_FLAGS.sub(" ", s)    # "(superset)", "(partner)", "(complex)"
    s = _RE_CROSSFIT_SPLIT.sub(" ", s)
    s = _RE_RPE.sub(" ", s)
    s = _RE_PCT.sub(" ", s)
    s = _RE_LOAD_UNIT.sub(" ", s)
    s = _RE_INCHES.sub(" ", s)
    s = _RE_SETS_X_REPS.sub(" ", s)
    s = _RE_LEADING_NUM.sub(" ", s)
    s = _RE_BARE_NUM.sub(" ", s)
    s = _RE_PUNCT.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---- DB helpers ------------------------------------------------------------
def _alias_lookup(pattern: str) -> dict | None:
    """Tier 1: exact match against pushpress_movement_alias."""
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT a.exercise_template_id, e.title
              FROM pushpress_movement_alias a
              JOIN dim_hevy_exercise e
                ON e.exercise_template_id = a.exercise_template_id
             WHERE a.pattern = %s
            """,
            [pattern],
        )
        return cur.fetchone()


def _bump_alias_hit(pattern: str) -> None:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            UPDATE pushpress_movement_alias
               SET hit_count = hit_count + 1, last_seen_at = now()
             WHERE pattern = %s
            """,
            [pattern],
        )


def _learn_alias(
    pattern: str,
    exercise_template_id: str,
    *,
    source: str = "auto",
) -> None:
    """Persist a new alias. ON CONFLICT updates the template_id (so an
    auto-custom-create supersedes a previous low-confidence analog) and
    bumps hit_count."""
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pushpress_movement_alias
              (pattern, exercise_template_id, source, hit_count, last_seen_at)
            VALUES (%s, %s, %s, 1, now())
            ON CONFLICT (pattern) DO UPDATE SET
              exercise_template_id = EXCLUDED.exercise_template_id,
              source = EXCLUDED.source,
              hit_count = pushpress_movement_alias.hit_count + 1,
              last_seen_at = now()
            """,
            [pattern, exercise_template_id, source],
        )


def _stem(word: str) -> str:
    """Tiny suffix-stripper. Hevy uses singular forms ('Deadlift') but
    coaches usually write plurals ('Deadlifts', 'Snatches'). Just enough
    morphology to land the obvious cases."""
    w = word.lower()
    if len(w) < 5:
        return w
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith("ses") or w.endswith("xes") or w.endswith("ches") or w.endswith("shes"):
        return w[:-2]
    if w.endswith("s"):
        return w[:-1]
    return w


def _candidate_pool(pattern: str, limit: int = 12) -> list[dict]:
    """Pull plausible matches from dim_hevy_exercise via ILIKE. Tries each
    content word (longest first) and accumulates results. Stemmed forms
    are tried first so 'deadlifts' lands 'Deadlift (Barbell)'."""
    if not pattern:
        return []
    words = sorted(
        (w for w in pattern.split() if len(w) >= 3),
        key=len,
        reverse=True,
    )
    if not words:
        return []

    seen: set[str] = set()
    pool: list[dict] = []
    with tx() as c, c.cursor() as cur:
        for word in words:
            for needle in (_stem(word), word) if _stem(word) != word else (word,):
                cur.execute(
                    """
                    SELECT exercise_template_id, title, primary_muscle_group, equipment
                      FROM dim_hevy_exercise
                     WHERE LOWER(title) ILIKE %s
                     ORDER BY length(title), title
                     LIMIT %s
                    """,
                    [f"%{needle}%", limit],
                )
                for row in cur.fetchall():
                    tid = row["exercise_template_id"]
                    if tid not in seen:
                        seen.add(tid)
                        pool.append(row)
                if len(pool) >= limit:
                    return pool[:limit]
    return pool[:limit]


def _ilike_best(pattern: str, candidates: list[dict]) -> dict | None:
    """Tier 2: pick a candidate whose title contains the same content words.

    Conservative — only returns a match when overlap is strong enough that
    we don't need the LLM."""
    if not candidates:
        return None
    pattern_words = {_stem(w) for w in pattern.split() if len(w) >= 3}
    if not pattern_words:
        return None
    scored: list[tuple[float, dict]] = []
    for c in candidates:
        title_words = {_stem(w) for w in normalize_pattern(c["title"]).split()
                       if len(w) >= 3}
        if not title_words:
            continue
        overlap = pattern_words & title_words
        if not overlap:
            continue
        # Score that rewards full pattern coverage. The candidate title can
        # have extra qualifier words ('(Barbell)', 'Machine') and still be
        # a confident match — what we care about is that every content word
        # in the COACH'S text shows up in the title.
        coverage = len(overlap) / len(pattern_words)
        # Tiebreak on Jaccard so a tighter title wins over a vague one.
        jaccard = len(overlap) / len(pattern_words | title_words)
        score = coverage * 0.7 + jaccard * 0.3
        scored.append((score, c))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]
    # Coverage 1.0 (every coach word in title) → score >= 0.7. That's our
    # floor for a confident SQL-tier match — anything less hands off to the
    # LLM tier.
    if best_score >= 0.7:
        return {**best, "_ilike_score": best_score}
    return None


# ---- LLM-assisted match ----------------------------------------------------
_LLM_SYSTEM = """\
You match a CrossFit/gym movement name to one entry in a Hevy exercise \
catalog. The catalog is bodybuilding-shaped (Back Squat (Barbell), Bench \
Press (Cable)) and incomplete on CrossFit-specific movements (Wall Walk, \
Z-Press, Devil Press, GHD, Rope Climb, Sandbag Carry are NOT in it). \
The input is whatever the coach wrote — slangy, abbreviated, or with kit \
specs.

Decision logic:

1. If a candidate IS the same exercise (just differently named or with a \
qualifier the input didn't include) → pick it, confidence ≥0.85, \
is_novel=false. Examples that ARE the same exercise:
  - 'thruster' / 'Thruster (Barbell)'
  - 'OHS' / 'Overhead Squat'
  - 'pull-up' / 'Pull Up'
  - 'echo bike' / 'cal bike' / 'Air Bike'
  - 'KB swing' / 'Kettlebell Swing'
  - 'HSPU' / 'Handstand Push Up'
  - 'T2B' / 'Toes to Bar'
  - 'Strict Pull-up' / 'Pull Up' (the strict modifier is style, same exercise)

2. If NO candidate is the same exercise (the movement genuinely doesn't \
exist in the catalog) → set is_novel=true AND populate the custom_* \
fields so we can auto-create a clean template. Pick the closest analog \
in exercise_template_id (used as fallback if create fails). Examples \
that ARE novel and need custom creation:
  - 'Wall Walk' (full_body, bodyweight_reps, none)
  - 'Z Press' / 'Z-Press' (shoulders, weight_reps, dumbbell or barbell — pick what coach implied)
  - 'Devil Press' (full_body, weight_reps, dumbbell)
  - 'GHD Sit-Up' (abdominals, bodyweight_reps, machine)
  - 'Rope Climb' (lats, bodyweight_reps, none)
  - 'Sandbag Carry' (full_body, weight_duration, other)
  - 'Synchro Burpee Over Bar' → just 'Burpee Over the Bar' if that exists, else novel
  - 'Box Step-up @ 20"' → 'Step Up' is fine (the height qualifier doesn't change the exercise)

3. For COMPLEXES (e.g. '1 Power Clean + 2 Front Squats'), treat as one \
novel exercise with custom_title='Power Clean + 2 Front Squats Complex' \
(weight_reps, barbell, full_body). Don't fragment.

Always call the match_movement tool exactly once. When is_novel=true, \
ALL of custom_title, custom_exercise_type, custom_equipment, and \
custom_muscle_group are required.\
"""

_LLM_TOOL: dict = {
    "name": "match_movement",
    "description": (
        "Pick the best Hevy template_id for the given movement. If no "
        "candidate is the same exercise (is_novel=true), ALSO populate the "
        "custom_* fields so we can auto-create a Hevy custom template "
        "instead of routing to a manual review queue."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "exercise_template_id": {
                "type": "string",
                "description": "The id of your top pick from the candidate list (used as the analog if is_novel=true).",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": ">=0.85 means it's the same exercise; 0.5–0.85 means closest analog; <0.5 means weak guess.",
            },
            "is_novel": {
                "type": "boolean",
                "description": "True if no candidate IS the same exercise (a custom template should be created).",
            },
            "custom_title": {
                "type": "string",
                "description": "Required when is_novel=true. Clean canonical name suitable as a Hevy exercise title (e.g. 'Wall Walk', 'Devil Press', 'Z Press', 'GHD Sit-Up'). Capitalize Words. No load/rep notation.",
            },
            "custom_exercise_type": {
                "type": "string",
                "enum": [
                    "weight_reps", "reps_only", "bodyweight_reps",
                    "bodyweight_assisted_reps", "duration", "weight_duration",
                    "distance_duration", "short_distance_weight",
                ],
                "description": "Required when is_novel=true. weight_reps for normal loaded lifts. reps_only/bodyweight_reps for pure bodyweight. duration for static holds. distance_duration for cardio (running, biking, rowing). weight_duration for loaded carries (Farmer's Walk).",
            },
            "custom_equipment": {
                "type": "string",
                "enum": [
                    "none", "barbell", "dumbbell", "kettlebell", "machine",
                    "plate", "resistance_band", "suspension", "other",
                ],
                "description": "Required when is_novel=true. Primary equipment used.",
            },
            "custom_muscle_group": {
                "type": "string",
                "enum": [
                    "abdominals", "shoulders", "biceps", "triceps", "forearms",
                    "quadriceps", "hamstrings", "calves", "glutes", "abductors",
                    "adductors", "lats", "upper_back", "traps", "lower_back",
                    "chest", "cardio", "neck", "full_body", "other",
                ],
                "description": "Required when is_novel=true. Primary muscle worked. Use 'cardio' for monostructural / running / rowing / biking. 'full_body' for compound CrossFit movements (Wall Walk, Burpee variants).",
            },
        },
        "required": ["exercise_template_id", "confidence", "is_novel"],
    },
}


def _llm_match(raw_text: str, candidates: list[dict]) -> dict | None:
    if not settings.ANTHROPIC_API_KEY or not candidates:
        return None
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    candidate_block = "\n".join(
        f"  - id={c['exercise_template_id']}  title={c['title']!r}  "
        f"muscle={c.get('primary_muscle_group') or '?'}  "
        f"equipment={c.get('equipment') or '?'}"
        for c in candidates
    )

    user_text = (
        f"Coach wrote: {raw_text!r}\n\n"
        f"Candidate matches from the Hevy catalog:\n{candidate_block}\n\n"
        f"Pick the best id. Set is_novel=true if no candidate is the same "
        f"exercise (you're picking an analog)."
    )

    try:
        resp = client.messages.create(
            model=settings.COACH_NORMALIZER_MODEL,
            max_tokens=512,
            system=_LLM_SYSTEM,
            tools=[_LLM_TOOL],
            tool_choice={"type": "tool", "name": "match_movement"},
            messages=[{"role": "user", "content": user_text}],
        )
    except anthropic.APIError as e:
        log.warning("coach.normalizer.llm_error", error=str(e))
        return None

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            inp = block.input if isinstance(block.input, dict) else json.loads(block.input)
            return inp
    return None


# ---- review queue ----------------------------------------------------------
def _enqueue_review(
    movement_id: int,
    raw_text: str,
    suggested_template_id: str | None,
    suggested_title: str | None,
) -> None:
    """One open review row per movement_id. Resolved rows can stay in place;
    a re-run only adds a new row when there isn't already an unresolved one."""
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pushpress_movement_review
              (movement_id, raw_text, suggested_template_id, suggested_title)
            SELECT %s, %s, %s, %s
            WHERE NOT EXISTS (
              SELECT 1 FROM pushpress_movement_review
               WHERE movement_id = %s AND resolved_at IS NULL
            )
            """,
            [movement_id, raw_text, suggested_template_id, suggested_title,
             movement_id],
        )


# ---- public API ------------------------------------------------------------
def resolve(raw_text: str, *, write_alias: bool = True) -> MatchResult:
    """Map a movement raw_text to an exercise. Cascades through the three
    tiers; first hit wins. Always returns a MatchResult — `novel_exercise=
    true` for the LLM-analog case, both ids None for the truly-unmatched
    case (extremely rare with a well-seeded catalog)."""
    pattern = normalize_pattern(raw_text)
    if not pattern:
        return MatchResult(None, None, 0.0, "unmatched", False, None, None)

    # Tier 1: exact alias hit.
    hit = _alias_lookup(pattern)
    if hit:
        _bump_alias_hit(pattern)
        return MatchResult(
            exercise_template_id=hit["exercise_template_id"],
            exercise_title=hit["title"],
            confidence=1.0,
            method="alias_exact",
            novel_exercise=False,
            analog_template_id=None,
            analog_title=None,
        )

    # Tier 2: ILIKE on a content word.
    candidates = _candidate_pool(pattern)
    ilike_hit = _ilike_best(pattern, candidates)
    if ilike_hit:
        if write_alias:
            _learn_alias(pattern, ilike_hit["exercise_template_id"])
        return MatchResult(
            exercise_template_id=ilike_hit["exercise_template_id"],
            exercise_title=ilike_hit["title"],
            confidence=float(ilike_hit["_ilike_score"]),
            method="title_ilike",
            novel_exercise=False,
            analog_template_id=None,
            analog_title=None,
        )

    # Tier 3: LLM. If we have no candidates at all (recall miss on the first
    # content word), broaden the pool by trying every word.
    if not candidates:
        candidates = []
        for word in (w for w in pattern.split() if len(w) >= 3):
            with tx() as c, c.cursor() as cur:
                cur.execute(
                    """
                    SELECT exercise_template_id, title, primary_muscle_group, equipment
                      FROM dim_hevy_exercise
                     WHERE LOWER(title) ILIKE %s
                     LIMIT 6
                    """,
                    [f"%{word}%"],
                )
                candidates.extend(cur.fetchall())
            if len(candidates) >= 12:
                break

    if not candidates:
        log.warning("coach.normalizer.no_candidates", pattern=pattern)
        return MatchResult(None, None, 0.0, "unmatched", True, None, None)

    llm = _llm_match(raw_text, candidates)
    if not llm:
        # LLM call failed — fall back to the highest-overlap ILIKE candidate.
        best = candidates[0]
        return MatchResult(
            exercise_template_id=None,
            exercise_title=None,
            confidence=0.0,
            method="unmatched",
            novel_exercise=True,
            analog_template_id=best["exercise_template_id"],
            analog_title=best["title"],
        )

    chosen_id = llm.get("exercise_template_id")
    confidence = float(llm.get("confidence", 0.0))
    is_novel = bool(llm.get("is_novel", False))
    chosen = next(
        (c for c in candidates if c["exercise_template_id"] == chosen_id),
        None,
    )
    if chosen is None:
        # Model hallucinated an id outside the candidate list. Pick the best
        # candidate and treat as novel.
        chosen = candidates[0]
        is_novel = True
        confidence = min(confidence, 0.4)

    if not is_novel and confidence >= 0.85:
        if write_alias:
            _learn_alias(pattern, chosen["exercise_template_id"])
        return MatchResult(
            exercise_template_id=chosen["exercise_template_id"],
            exercise_title=chosen["title"],
            confidence=confidence,
            method="llm",
            novel_exercise=False,
            analog_template_id=None,
            analog_title=None,
        )

    # Novel — auto-create a Hevy custom exercise template instead of routing
    # to a review queue with an analog. The LLM tool schema requires the
    # custom_* fields whenever is_novel=true; if they're missing we fall
    # back to the analog path (e.g. when the LLM call itself failed earlier).
    custom_title = llm.get("custom_title")
    custom_type = llm.get("custom_exercise_type")
    custom_equipment = llm.get("custom_equipment")
    custom_muscle = llm.get("custom_muscle_group")
    have_custom_fields = all([custom_title, custom_type, custom_equipment, custom_muscle])
    if is_novel and have_custom_fields:
        new_tid = _create_custom_template(
            title=custom_title,
            exercise_type=custom_type,
            equipment=custom_equipment,
            muscle_group=custom_muscle,
        )
        if new_tid:
            log.info(
                "coach.normalizer.auto_created_custom",
                pattern=pattern, raw_text=raw_text,
                template_id=new_tid, title=custom_title,
            )
            if write_alias:
                _learn_alias(pattern, new_tid, source="auto_custom")
            return MatchResult(
                exercise_template_id=new_tid,
                exercise_title=custom_title,
                confidence=0.95,
                method="auto_custom",
                novel_exercise=False,
                analog_template_id=None,
                analog_title=None,
            )

    # Couldn't auto-create (custom_* fields missing or Hevy POST failed) —
    # fall back to analog routing. Don't auto-learn the alias because the
    # analog isn't the actual movement.
    return MatchResult(
        exercise_template_id=None,
        exercise_title=None,
        confidence=confidence,
        method="llm",
        novel_exercise=True,
        analog_template_id=chosen["exercise_template_id"],
        analog_title=chosen["title"],
    )


# ---- Hevy custom-template creation -----------------------------------------
def _create_custom_template(
    *,
    title: str,
    exercise_type: str,
    equipment: str,
    muscle_group: str,
) -> str | None:
    """POST a new template to Hevy and mirror it into dim_hevy_exercise via
    the existing helper. Returns the new template_id, or None on failure.

    De-dupes by title — if a custom or built-in template with the same title
    already exists in dim_hevy_exercise, we just return its id instead of
    creating a duplicate. Important because the LLM can mint slightly
    different titles for the same movement across runs ("Wall Walk" vs
    "Wall Walks") and we want all of them to converge on one template."""
    title = title.strip()
    if not title:
        return None

    with tx() as c, c.cursor() as cur:
        cur.execute(
            "SELECT exercise_template_id FROM dim_hevy_exercise "
            "WHERE LOWER(title) = LOWER(%s) LIMIT 1",
            [title],
        )
        existing = cur.fetchone()
        if existing:
            log.info("coach.normalizer.custom_dedup",
                     title=title, template_id=existing["exercise_template_id"])
            return existing["exercise_template_id"]

    # Lazy import — keeps coach.normalizer's hot path free of mcp_server
    # dependencies in environments where it's not installed (e.g. unit tests).
    try:
        from mcp_server.hevy_write_tools import create_custom_exercise as hw_create
    except ImportError as e:  # pragma: no cover
        log.warning("coach.normalizer.hw_import_failed", error=str(e))
        return None

    resp = hw_create(
        title=title,
        exercise_type=exercise_type,
        equipment_category=equipment,
        muscle_group=muscle_group,
    )
    if not resp.get("ok"):
        log.warning(
            "coach.normalizer.custom_create_failed",
            title=title, error=resp.get("error"),
        )
        return None
    rows = resp.get("rows") or []
    if not rows:
        return None
    new_tid = rows[0].get("id")
    return new_tid


def enqueue_review_if_needed(movement_id: int, raw_text: str, match: MatchResult) -> None:
    """Push novel-exercise rows into the review queue. Idempotent: ON
    CONFLICT DO NOTHING in the underlying insert (we can re-insert by
    movement_id when the recommender re-runs)."""
    if match.novel_exercise:
        _enqueue_review(
            movement_id,
            raw_text,
            match.analog_template_id,
            match.analog_title,
        )
