"""Shared helpers across LLM raters: anchor injection + result shaping.

Each LLM rater module (claude.py, gpt4v.py, gemini.py) does its own
SDK/HTTP plumbing but goes through here for the prompt/result contract.
Keeping it in one place means a future "add a fourth rater" is the
same ~30 lines as the others — different client, same prompt, same
shape.
"""

from __future__ import annotations

from typing import Any


def anchor_kwargs(anchor_scores: dict[str, int]) -> dict[str, int]:
    """Translate {'overall_low': 38, ...} into the kwargs the rubric
    `structure_prompt` / `surface_prompt` builders expect."""
    if not anchor_scores:
        return {}
    return {
        "low":  anchor_scores.get("overall_low"),
        "mid":  anchor_scores.get("overall_mid"),
        "high": anchor_scores.get("overall_high"),
    }


def shape_result(source: str, dims: dict[str, Any]) -> dict[str, Any]:
    """Normalize the rater output into the per-row shape persisted to
    body_image_rating. `overall` is the specialist's holistic score for
    its own dimension subset; the per-photo overall is computed in the
    service layer as the mean across specialists."""
    overall = dims.get("overall")
    if not isinstance(overall, (int, float)):
        overall = None
    return {"source": source, "overall": overall, "dimensions": dims}
