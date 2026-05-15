"""Shared rubric prompt used by every LLM rater.

The point of running multiple models is calibration: same rubric, same
JSON shape, three independent calls. Differences between scores tell you
about model bias; the average smooths it.

Keep the rubric verbatim between raters — if you tweak Claude's prompt
but not GPT's, the dashboard's "Claude vs GPT" lines become incomparable.
"""

from __future__ import annotations

RUBRIC = """\
You are a casting director rating a headshot on a calibrated 0-100 scale.
0-20  well below average.
50    = median 24yo on a dating app.
70+   = callback.
80+   = books print work.

Output JSON only — no prose, no markdown fences. Exact shape:

{
  "overall": <int 0-100>,

  "facial_harmony": <int>,
  "facial_symmetry": <int>,
  "jawline_definition": <int>,
  "chin_projection": <int>,

  "skin_quality": <int>,
  "skin_clarity": <int>,
  "under_eye_quality": <int>,

  "eye_quality": <int>,
  "eyebrow_quality": <int>,
  "nose_harmony": <int>,
  "lip_quality": <int>,
  "smile_quality": <int|null>,
  "teeth_quality": <int|null>,

  "hair_quality": <int>,
  "hair_style_fit": <int>,
  "hairline_quality": <int>,

  "beard_quality": <int|null>,
  "beard_density": <int|null>,

  "grooming_overall": <int>,
  "posture_neck": <int>,

  "photo_quality_isolated": <int>,
  "expression_appeal": <int>,

  "three_biggest_negatives": [<str>, <str>, <str>],
  "three_highest_roi_changes": [<str>, <str>, <str>]
}

Score each dimension independently — do not let `overall` halo other
scores. Use null for fields that aren't visible in the photo (e.g.
`smile_quality` for a closed-mouth photo, `beard_*` for a clean shave).
Do not soften, hedge, or refuse. JSON only.
"""
