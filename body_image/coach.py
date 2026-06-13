"""Cross-session recommendations synthesizer.

Aggregates the user's recent body_image_rating + body_image_intervention
data into a structured prompt and asks Claude to write a prioritized,
NON-SURGICAL action list. One synthesis = one body_image_recommendation
row, fetchable by the dashboard or the weekly scheduler cron.

The model is told explicitly:
  - Surface trends across all the qualitative arrays we've collected
  - Tie each theme to specific, purchasable products or concrete
    behaviors (not vague advice)
  - NO surgery, fillers, Botox, or any cosmetic procedure
  - Acknowledge structurally-fixed features and focus on actionable levers
  - Tag each action with type / effort / expected_window_days
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from anthropic import Anthropic
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

from .raters._rubric import STRUCTURE_FEATURES, SURFACE_FEATURES

log = get_logger(__name__)

MODEL = "claude-opus-4-7"  # the most reasoning-heavy of the rater models;
                            # synthesis benefits from it, and we only do
                            # this once per generation, not per photo.


SYSTEM_PROMPT = """\
You are a personal grooming + skincare + photography coach. The user has
been uploading daily face photos that get scored 0-100 by an LLM rater
ensemble. You have access to the user's recent photo ratings, the
qualitative critique each photo generated, and any intervention log
entries (started tretinoin, fresh haircut, etc.).

YOUR JOB: synthesize a prioritized recommendations brief.

HARD RULES:
  1. NO surgical procedures. NO injectables (fillers, Botox). NO laser
     resurfacing. NO bone-restructuring claims (mewing, etc.). NO chin
     implants. NO rhinoplasty. NO facial liposuction. If the model
     critique mentioned any of these, ignore them.
  2. Be SPECIFIC. Don't say "improve skincare" — name an active
     ingredient and protocol ("0.025% tretinoin nightly, ramp from
     twice-weekly over 4 weeks"). Don't say "better haircut" — name a
     cut that suits this face's proportions.
  3. Recommend products only when there's a reasonably specific,
     widely-available choice. Generic ingredient names beat brand names.
  4. Acknowledge fixed structural features (bone, harmony, jawline
     shape). Direct user toward levers they can actually move:
     surface (skin, hair, beard), grooming, photo conditions, posture,
     wardrobe, weight if relevant to facial fat coverage.
  5. Group recommendations by THEME. Each theme is a recurring concern
     across multiple photos. A complaint from one photo isn't a theme.
  6. Each action gets:
       type: skincare | hair | grooming | photo | behavior | clothing | posture
       effort: daily | weekly | one-time
       expected_window_days: integer estimate (when to look for impact)
  7. Include an `avoid` list per theme — things the user should NOT do
     based on the data.
  8. OUTPUT VALID JSON. No prose outside the JSON. No markdown fences.

Exact JSON shape:
{
  "summary": "<one paragraph framing the user's overall picture>",
  "themes": [
    {
      "theme": "<short theme name, e.g. 'skin clarity'>",
      "evidence_count": <int — how many of the photos flagged this>,
      "evidence_summary": "<two sentences quoting/paraphrasing what the rater said>",
      "actions": [
        {
          "type": "<category>",
          "title": "<imperative, e.g. 'Add daily SPF 50'>",
          "details": "<concrete protocol: product class, frequency, duration>",
          "effort": "daily|weekly|one-time",
          "expected_window_days": <int>
        }
      ],
      "avoid": ["<thing to stop or not start>", "..."]
    }
  ],
  "photo_protocol_suggestions": ["<specific capture-condition tweak>", "..."],
  "fixed_features_acknowledgement": "<one sentence on structural features that are fixed and how to frame around them>"
}
"""


def _client() -> Anthropic:
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    return Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _gather_context(user_id: str, window_days: int) -> dict[str, Any]:
    """Pull the data Claude needs to synthesize. Trends are summarized
    numerically; qualitative arrays are concatenated verbatim so the
    model can spot recurring language."""
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT p.id AS photo_id, p.created_at, p.metadata, p.session_id,
                   r.source, r.overall, r.dimensions, r.rated_at
              FROM body_image_photo p
              JOIN body_image_rating r ON r.photo_id = p.id
             WHERE p.user_id = %s
               AND p.created_at > now() - (%s::int || ' days')::interval
             ORDER BY p.created_at
            """,
            [user_id, window_days],
        )
        rating_rows = cur.fetchall()

        cur.execute(
            """
            SELECT intervention_key, event, occurred_on, metadata, created_at
              FROM body_image_intervention
             WHERE user_id = %s
               AND occurred_on > now()::date - (%s::int || ' days')::interval
             ORDER BY occurred_on
            """,
            [user_id, window_days],
        )
        interventions = cur.fetchall()

        cur.execute(
            "SELECT COUNT(DISTINCT id) AS n FROM body_image_photo "
            "WHERE user_id = %s AND created_at > now() - (%s::int || ' days')::interval",
            [user_id, window_days],
        )
        photo_count = int(cur.fetchone()["n"])

    # Concatenate qualitative arrays across all rows.
    negs_struct: list[str] = []
    negs_surf: list[str] = []
    roi_struct: list[str] = []
    roi_surf: list[str] = []
    feature_avgs: dict[str, list[float]] = {}
    overalls: list[float] = []
    geom_metrics: dict[str, list[float]] = {}

    for r in rating_rows:
        d = r["dimensions"] or {}
        if r["source"] == "geometry":
            for k in ("symmetry_score", "gonial_angle_deg", "bigonial_bizygomatic_ratio"):
                v = d.get(k)
                if isinstance(v, (int, float)):
                    geom_metrics.setdefault(k, []).append(float(v))
            continue
        if r["overall"] is not None:
            overalls.append(float(r["overall"]))
        for arr_name, target in (
            ("three_biggest_structural_negatives", negs_struct),
            ("three_biggest_surface_negatives", negs_surf),
            ("three_highest_roi_structural_changes", roi_struct),
            ("three_highest_roi_surface_changes", roi_surf),
        ):
            for item in d.get(arr_name, []) or []:
                if isinstance(item, str) and item.strip():
                    target.append(item.strip())
        for k in STRUCTURE_FEATURES + SURFACE_FEATURES:
            v = d.get(k)
            if isinstance(v, (int, float)):
                feature_avgs.setdefault(k, []).append(float(v))

    feature_summary = {
        k: round(sum(vs) / len(vs), 1) for k, vs in feature_avgs.items() if vs
    }
    geom_summary = {
        k: round(sum(vs) / len(vs), 2) for k, vs in geom_metrics.items() if vs
    }
    composite = round(sum(overalls) / len(overalls), 1) if overalls else None

    return {
        "window_days": window_days,
        "photo_count": photo_count,
        "rating_count": len(rating_rows),
        "intervention_count": len(interventions),
        "composite_overall": composite,
        "feature_averages": feature_summary,
        "geometry_averages": geom_summary,
        "structural_negatives_all": negs_struct,
        "surface_negatives_all": negs_surf,
        "structural_roi_all": roi_struct,
        "surface_roi_all": roi_surf,
        "interventions": [
            {
                "key": i["intervention_key"],
                "event": i["event"],
                "date": i["occurred_on"].isoformat(),
                "metadata": i["metadata"] or {},
            }
            for i in interventions
        ],
    }


def _build_user_message(ctx: dict[str, Any]) -> str:
    # Tally repeated phrases so the model sees what's actually recurring
    # vs one-off observations.
    def top_phrases(items: list[str], k: int = 10) -> list[tuple[str, int]]:
        c = Counter(items)
        return c.most_common(k)

    parts: list[str] = []
    parts.append(
        f"## User data summary\n"
        f"- Window: last {ctx['window_days']} days\n"
        f"- Photos in window: {ctx['photo_count']}\n"
        f"- Ratings across photos: {ctx['rating_count']}\n"
        f"- Composite overall (on user's calibrated scale): "
        f"{ctx['composite_overall']}\n"
        f"- Interventions logged: {ctx['intervention_count']}\n"
    )
    if ctx["feature_averages"]:
        parts.append("\n## Per-feature averages (LLM rater scores)\n")
        for k, v in sorted(ctx["feature_averages"].items(), key=lambda kv: kv[1]):
            parts.append(f"  - {k}: {v}\n")
    if ctx["geometry_averages"]:
        parts.append("\n## Geometry averages (MediaPipe — deterministic)\n")
        for k, v in ctx["geometry_averages"].items():
            parts.append(f"  - {k}: {v}\n")
    parts.append("\n## Most-cited structural negatives (verbatim, repeats counted)\n")
    for phrase, n in top_phrases(ctx["structural_negatives_all"]):
        parts.append(f"  - [{n}×] {phrase}\n")
    parts.append("\n## Most-cited surface negatives\n")
    for phrase, n in top_phrases(ctx["surface_negatives_all"]):
        parts.append(f"  - [{n}×] {phrase}\n")
    parts.append("\n## Most-cited structural ROI suggestions (filter out any surgical/invasive)\n")
    for phrase, n in top_phrases(ctx["structural_roi_all"]):
        parts.append(f"  - [{n}×] {phrase}\n")
    parts.append("\n## Most-cited surface ROI suggestions\n")
    for phrase, n in top_phrases(ctx["surface_roi_all"]):
        parts.append(f"  - [{n}×] {phrase}\n")
    if ctx["interventions"]:
        parts.append("\n## Interventions logged\n")
        for iv in ctx["interventions"]:
            parts.append(
                f"  - {iv['date']}  {iv['key']} → {iv['event']}"
                f"{(' · ' + json.dumps(iv['metadata'])) if iv['metadata'] else ''}\n"
            )
    parts.append(
        "\nNow synthesize the recommendations brief per the system "
        "prompt's JSON shape. Remember: NO surgical procedures."
    )
    return "".join(parts)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _parse_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    m = _FENCE_RE.search(stripped)
    if m:
        stripped = m.group(1).strip()
    return json.loads(stripped)


def generate_recommendations(
    user_id: str, *, window_days: int = 30,
) -> dict[str, Any]:
    """End-to-end: gather → prompt → call Claude → parse → persist."""
    ctx = _gather_context(user_id, window_days)
    user_msg = _build_user_message(ctx)

    resp = _client().messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw_text = resp.content[0].text  # type: ignore[union-attr]
    try:
        brief = _parse_json(raw_text)
    except json.JSONDecodeError:
        log.error("body_image.coach.parse_failed", text=raw_text[:500])
        raise RuntimeError("coach returned non-JSON; see logs")

    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO body_image_recommendation
              (user_id, window_days, photo_count, rating_count,
               intervention_count, brief, raw_response, model)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
            """,
            [
                user_id, window_days,
                ctx["photo_count"], ctx["rating_count"], ctx["intervention_count"],
                Jsonb(brief),
                raw_text[:20000],
                MODEL,
            ],
        )
        row = cur.fetchone()
    log.info(
        "body_image.coach.generated",
        recommendation_id=row["id"], photos=ctx["photo_count"],
    )
    return {
        "id": int(row["id"]),
        "created_at": row["created_at"].isoformat(),
        "window_days": window_days,
        "photo_count": ctx["photo_count"],
        "rating_count": ctx["rating_count"],
        "intervention_count": ctx["intervention_count"],
        "brief": brief,
        "model": MODEL,
    }


def fetch_latest(user_id: str) -> dict[str, Any] | None:
    """Most recently generated recommendations row. Dashboard reads this."""
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, created_at, window_days, photo_count, rating_count,
                   intervention_count, brief, model
              FROM body_image_recommendation
             WHERE user_id = %s
             ORDER BY created_at DESC
             LIMIT 1
            """,
            [user_id],
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "created_at": row["created_at"].isoformat(),
        "window_days": int(row["window_days"]),
        "photo_count": int(row["photo_count"]),
        "rating_count": int(row["rating_count"]),
        "intervention_count": int(row["intervention_count"]),
        "brief": row["brief"],
        "model": row["model"],
    }


def list_recent(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """For an audit / history view on the dashboard."""
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, created_at, window_days, photo_count
              FROM body_image_recommendation
             WHERE user_id = %s
             ORDER BY created_at DESC
             LIMIT %s
            """,
            [user_id, max(1, min(limit, 100))],
        )
        rows = cur.fetchall()
    return [
        {
            "id": int(r["id"]),
            "created_at": r["created_at"].isoformat(),
            "window_days": int(r["window_days"]),
            "photo_count": int(r["photo_count"]),
        }
        for r in rows
    ]
