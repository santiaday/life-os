"""Rater orchestration.

Exposes:
  * Per-rater specialist functions (rate_claude_structure, rate_claude_surface,
    rate_gpt4v_structure, rate_gpt4v_surface, rate_gemini_structure,
    rate_gemini_surface, rate_geometry).
  * `available_llm_raters()` — which models are configured (have API key set).
  * `run_llm_raters_once(jpeg_bytes)` — single-pass helper used by
    body_image.validation; no DB writes, no run_index iteration.
  * Calibration anchor loading via `load_anchors()`.

`body_image.service._run_raters_parallel` is the production fan-out and
lives in service.py (it needs DB + threads + per-run wiring).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

from .. import calibration
from .claude import rate_claude_structure, rate_claude_surface
from .gemini import rate_gemini_structure, rate_gemini_surface
from .geometry import rate_geometry
from .gpt4v import rate_gpt4v_structure, rate_gpt4v_surface

log = get_logger(__name__)


# ── Anchor loading ───────────────────────────────────────────────────


def load_anchors() -> list[tuple[bytes, int]]:
    """Return [(jpeg_bytes, score), ...] sorted ascending by score when
    calibration is enabled and the anchor files exist; otherwise []."""
    if not settings.BODY_IMAGE_USE_CALIBRATION_ANCHORS:
        return []
    if not calibration.anchors_available():
        log.warning("body_image.anchors.requested_but_missing")
        return []
    pairs = calibration.load_anchor_pairs()
    if not pairs:
        return []
    return pairs


# ── Available raters ─────────────────────────────────────────────────

RaterFn = Callable[..., dict[str, Any]]


def available_llm_raters() -> list[tuple[str, RaterFn, RaterFn]]:
    """Return [(name, structure_fn, surface_fn), ...] for every LLM
    rater whose API key is set. Order is stable (claude first, then
    gpt4v, then gemini). geometry is handled separately (it's a single
    sidecar call, not a structure/surface split)."""
    out: list[tuple[str, RaterFn, RaterFn]] = []
    if settings.ANTHROPIC_API_KEY:
        out.append(("claude", rate_claude_structure, rate_claude_surface))
    if settings.OPENAI_API_KEY:
        out.append(("gpt4v", rate_gpt4v_structure, rate_gpt4v_surface))
    if settings.GEMINI_API_KEY:
        out.append(("gemini", rate_gemini_structure, rate_gemini_surface))
    return out


# ── Single-pass helper for validation ────────────────────────────────


def run_llm_raters_once(jpeg_bytes: bytes) -> list[dict[str, Any]]:
    """No-DB sequential pass for the weekly validation cron. Returns
    every rater's per-specialist result (or fewer if some fail). Does
    NOT iterate run_index — validation only needs one sample per ref."""
    anchor_pairs = load_anchors()
    results: list[dict[str, Any]] = []
    for name, struct_fn, surf_fn in available_llm_raters():
        for label, fn in (("structure", struct_fn), ("surface", surf_fn)):
            try:
                results.append(fn(jpeg_bytes, anchor_pairs))
            except Exception as e:
                log.warning(
                    "body_image.validation.rater_failed",
                    rater=name, specialist=label, error=str(e),
                )
    # Geometry too — same shape (only one call, no specialist split).
    try:
        results.append(rate_geometry(jpeg_bytes))
    except Exception as e:
        log.warning("body_image.validation.geometry_failed", error=str(e))
    return results


__all__ = [
    "available_llm_raters",
    "load_anchors",
    "rate_claude_structure",
    "rate_claude_surface",
    "rate_gemini_structure",
    "rate_gemini_surface",
    "rate_geometry",
    "rate_gpt4v_structure",
    "rate_gpt4v_surface",
    "run_llm_raters_once",
]
