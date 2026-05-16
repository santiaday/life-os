"""Shared helpers across LLM raters: anchor unpacking + result shaping."""

from __future__ import annotations

from typing import Any


def split_anchors(anchor_pairs: list[tuple[bytes, int]]) -> tuple[list[bytes], list[int]]:
    """Decompose [(jpeg, score), ...] into parallel image and score lists,
    preserving order. Raters pass the images into the API in the same
    order they appear in `anchor_pairs`."""
    images = [pair[0] for pair in anchor_pairs]
    scores = [int(pair[1]) for pair in anchor_pairs]
    return images, scores


def shape_result(source: str, dims: dict[str, Any]) -> dict[str, Any]:
    """Normalize the rater output into the per-row shape persisted to
    body_image_rating. `overall` is the specialist's holistic score for
    its own dimension subset; per-photo overall is computed in the
    service layer as the mean across specialists."""
    overall = dims.get("overall")
    if not isinstance(overall, (int, float)):
        overall = None
    return {"source": source, "overall": overall, "dimensions": dims}
