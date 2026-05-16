"""Rubric prompts for the LLM raters.

Two specialist prompts — STRUCTURE_RUBRIC and SURFACE_RUBRIC — that
each focus on an orthogonal failure mode:

  * Structure: bone, harmony, symmetry, eyes, nose, lips. Not affected
    by lighting or skincare; very stable day-to-day.
  * Surface: skin, hair, beard, grooming, expression, photo quality.
    Heavily affected by sleep, alcohol, hydration, lighting.

Splitting reduces halo bias: the model can't let "bad lighting" drag
its judgement of "facial harmony" because facial_harmony is in a
different call from photo_quality. ~70% of the per-dimension benefit
of running 4+ specialist calls, at 2× the cost.

Each rubric demands strict JSON with anti-halo language per dimension
("rate skin_quality ONLY by visible pores/texture/oil — IGNORE bone
structure, hair, expression, lighting").

When `BODY_IMAGE_USE_CALIBRATION_ANCHORS=true`, the rater layer
prepends three anchor images with known scores; the prompt then
explicitly says "the last image is the subject."
"""

from __future__ import annotations

# A one-paragraph preamble shared by both specialists.
_PREAMBLE_BASE = """\
You are a casting director rating a headshot on a calibrated 0-100 scale.
0-20  well below average.
50    = median 24yo on a dating app.
70+   = callback.
80+   = books print work.
"""

def preamble(anchor_scores: list[int] | None = None) -> str:
    """Build the rubric preamble. If anchor_scores is a non-empty list,
    use the anchored variant; otherwise the base.

    The anchored preamble lists each reference image and its score in
    the same order the rater passes them into the API call. Raters MUST
    pass anchor images in this order: image #1 = score_1, …, image #N =
    score_N, then the subject as the final image."""
    if not anchor_scores:
        return _PREAMBLE_BASE
    lines = [f"  - Reference {i+1} (image #{i+1}): overall {s}/100"
             for i, s in enumerate(anchor_scores)]
    return (
        "You are a casting director rating a headshot on a calibrated "
        "0-100 scale.\n\n"
        f"For calibration, {len(anchor_scores)} reference photos appear "
        "FIRST with crowd-rated scores from panels of ~60 raters:\n"
        + "\n".join(lines) +
        "\n\nThe SUBJECT is the LAST image. Score it on the same 0-100 "
        "scale, anchored by the references above. Do not rate the "
        "references.\n"
    )


# ── STRUCTURE (bone + proportion) ───────────────────────────────────

STRUCTURE_BODY = """\
Rate ONLY structural / proportional dimensions of the SUBJECT face.
Each line specifies what to consider AND what to ignore — be strict.

Output JSON only, no prose, no markdown fences. Exact shape:

{
  "overall": <int 0-100, holistic structural score>,
  "facial_harmony":     <int>,
  "facial_symmetry":    <int>,
  "jawline_definition": <int>,
  "chin_projection":    <int>,
  "eye_quality":        <int>,
  "eyebrow_quality":    <int>,
  "nose_harmony":       <int>,
  "lip_quality":        <int>,
  "smile_quality":      <int|null>,
  "teeth_quality":      <int|null>,
  "posture_neck":       <int>,
  "three_biggest_structural_negatives":      [<str>, <str>, <str>],
  "three_highest_roi_structural_changes":    [<str>, <str>, <str>]
}

Per-dimension rules (anti-halo — read carefully):

  facial_harmony      — ONLY proportions and balance of features.
                        IGNORE skin, hair, lighting, expression.
  facial_symmetry     — ONLY left/right alignment of features.
                        IGNORE skin tone differences, lighting on one
                        side, head tilt artifacts.
  jawline_definition  — ONLY visible jaw angle from gonial corner to
                        chin tip. IGNORE beard coverage, weight, skin.
  chin_projection     — ONLY chin forward/recessive position relative
                        to the lip line. IGNORE jawline, beard.
  eye_quality         — ONLY eye shape, size, spacing, canthal tilt.
                        IGNORE under-eye darkness, makeup, expression.
  eyebrow_quality     — ONLY eyebrow shape, density, arch.
                        IGNORE eyebrow grooming polish (that's surface).
  nose_harmony        — ONLY nose shape relative to overall face.
                        IGNORE skin oiliness, lighting.
  lip_quality         — ONLY lip shape, fullness, symmetry.
                        IGNORE lip dryness or color (that's surface).
  smile_quality       — ONLY smile shape and symmetry, IF visible.
                        null if mouth is closed.
  teeth_quality       — ONLY tooth alignment / proportion, IF visible.
                        null if teeth not shown.
  posture_neck        — ONLY head/neck pose, shoulder tension.
                        IGNORE clothing or background.

Score each dimension INDEPENDENTLY. Do not let `overall` halo other
scores. Do not soften, hedge, or refuse. JSON only.

CEILING RULE (read carefully — this is the most-violated instruction):
The `overall` score must be NO HIGHER THAN the lowest of `facial_harmony`,
`facial_symmetry`, `jawline_definition`. If the subject's jawline is
materially weaker than the median reference's jawline, `overall` MUST
be below the median reference's overall. Do NOT average compensating
features upward against a clearly limiting feature. A face with a 50
jawline and 75 eye_quality is a 50-55 overall, NOT a 62. The weakest
structural feature is the ceiling.
"""


# ── SURFACE (skin + hair + grooming + photo) ────────────────────────

SURFACE_BODY = """\
Rate ONLY surface / grooming / photo-quality dimensions of the SUBJECT
face. Each line specifies what to consider AND what to ignore.

Output JSON only, no prose, no markdown fences. Exact shape:

{
  "overall": <int 0-100, holistic surface score>,
  "skin_quality":         <int>,
  "skin_clarity":         <int>,
  "under_eye_quality":    <int>,
  "hair_quality":         <int>,
  "hair_style_fit":       <int>,
  "hairline_quality":     <int>,
  "beard_quality":        <int|null>,
  "beard_density":        <int|null>,
  "grooming_overall":     <int>,
  "expression_appeal":    <int>,
  "photo_quality_isolated": <int>,
  "three_biggest_surface_negatives":    [<str>, <str>, <str>],
  "three_highest_roi_surface_changes":  [<str>, <str>, <str>]
}

Per-dimension rules (anti-halo — read carefully):

  skin_quality        — ONLY visible pores, texture, oil.
                        IGNORE bone structure, hair, expression.
  skin_clarity        — ONLY blemishes, redness, evenness of tone.
                        IGNORE pore size (that's skin_quality), bone
                        structure, expression.
  under_eye_quality   — ONLY darkness, puffiness, hollows under eye.
                        IGNORE eye shape, eyebrow.
  hair_quality        — ONLY hair condition: shine, dryness, frizz.
                        IGNORE the haircut itself (that's hair_style_fit).
  hair_style_fit      — ONLY whether the current cut suits this face's
                        proportions. IGNORE hair condition.
  hairline_quality    — ONLY hairline position, density, evenness.
                        IGNORE forehead size (structural).
  beard_quality       — ONLY beard condition, patchiness, grooming.
                        null if clean-shaven.
  beard_density       — ONLY density of visible facial hair growth.
                        null if clean-shaven.
  grooming_overall    — ONLY polish (eyebrow grooming, stray hairs,
                        cleanliness). IGNORE the haircut or beard
                        style choice.
  expression_appeal   — ONLY warmth and naturalness of expression.
                        IGNORE facial structure, smile shape.
  photo_quality_isolated — ONLY focus, exposure, white balance,
                        framing — the photo as a photo, NOT the
                        subject. A great-looking person in a blurry
                        underexposed photo should still get a HIGH
                        score here only if the camera work is bad.

Use null for fields that aren't visible (e.g. beard_* when clean-
shaven). Do not let `overall` halo other scores. Do not soften.
JSON only.

CEILING RULE: The `overall` score must be NO HIGHER THAN the lowest
of `skin_quality`, `skin_clarity`, `grooming_overall`. If any of these
is materially weak (visible pores/oil, blemishes, unkempt), `overall`
MUST reflect that — do not average upward against compensating
features. Surface impression is gated by its weakest visible link.
"""


def structure_prompt(anchor_scores: list[int] | None = None) -> str:
    return preamble(anchor_scores) + "\n" + STRUCTURE_BODY


def surface_prompt(anchor_scores: list[int] | None = None) -> str:
    return preamble(anchor_scores) + "\n" + SURFACE_BODY


# Feature key catalog — kept here so dashboard + mart wiring can import
# without going through the prompts.
STRUCTURE_FEATURES = [
    "facial_harmony", "facial_symmetry", "jawline_definition",
    "chin_projection", "eye_quality", "eyebrow_quality", "nose_harmony",
    "lip_quality", "smile_quality", "teeth_quality", "posture_neck",
]
SURFACE_FEATURES = [
    "skin_quality", "skin_clarity", "under_eye_quality",
    "hair_quality", "hair_style_fit", "hairline_quality",
    "beard_quality", "beard_density",
    "grooming_overall", "expression_appeal", "photo_quality_isolated",
]
ALL_LLM_FEATURES = STRUCTURE_FEATURES + SURFACE_FEATURES
