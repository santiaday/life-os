"""Shared helpers across LLM raters: anchor unpacking + result shaping."""

from __future__ import annotations

from typing import Any

from lifeos_core.settings import settings


def split_anchors(anchor_pairs: list[tuple[bytes, int]]) -> tuple[list[bytes], list[int]]:
    """Decompose [(jpeg, score), ...] into parallel image and score lists,
    preserving order. Raters pass the images into the API in the same
    order they appear in `anchor_pairs`."""
    images = [pair[0] for pair in anchor_pairs]
    scores = [int(pair[1]) for pair in anchor_pairs]
    return images, scores


def _apply_personal_calibration(raw: float | None) -> tuple[float | None, float | None]:
    """Apply the user's personal slope+offset (Track B). Returns
    (calibrated, raw). When slope=1.0 and offset=0.0 (default), the
    calibrated and raw values are identical — no-op until the user
    runs the blind-rating workflow."""
    if raw is None:
        return None, None
    slope = settings.BODY_IMAGE_CALIBRATION_SLOPE
    offset = settings.BODY_IMAGE_CALIBRATION_OFFSET
    calibrated = slope * raw + offset
    # Clamp to 0-100
    calibrated = max(0.0, min(100.0, calibrated))
    return calibrated, raw


def shape_result(source: str, dims: dict[str, Any]) -> dict[str, Any]:
    """Normalize rater output. `overall` becomes the personally-calibrated
    score; `dimensions._raw_overall` preserves the model's pre-correction
    output so we can re-derive the calibration later without losing data.
    """
    raw_overall = dims.get("overall")
    if not isinstance(raw_overall, (int, float)):
        raw_overall = None
    calibrated, raw = _apply_personal_calibration(raw_overall)
    if raw is not None:
        # Stash the raw model output so future re-calibration can use it
        # without re-running the API call.
        dims = dict(dims)
        dims["_raw_overall"] = raw
    return {"source": source, "overall": calibrated, "dimensions": dims}
