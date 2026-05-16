"""Claude vision rater. Sonnet 4.6, temp=0, two specialist calls
(Structure + Surface).

Anthropic's Messages API doesn't expose a `seed` parameter, so "N runs
with rotated seed" is a no-op for Claude — at temp=0 successive calls
are near-identical (modulo GPU non-determinism). The runs still get
saved as separate body_image_rating rows so the variance across them
is your true model-internal floor.

When BODY_IMAGE_USE_CALIBRATION_ANCHORS is true, three anchor images
with known scores are prepended to every call.
"""

from __future__ import annotations

import base64
from typing import Any

from anthropic import Anthropic

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

from . import _common
from . import _rubric
from ._parse import parse_json_lenient

log = get_logger(__name__)

MODEL = "claude-sonnet-4-6"


def _client() -> Anthropic:
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    return Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _image_block(jpeg: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.b64encode(jpeg).decode("ascii"),
        },
    }


def _call(prompt_text: str, target_jpeg: bytes, anchor_images: list[bytes]) -> dict[str, Any]:
    content: list[dict[str, Any]] = [_image_block(a) for a in anchor_images]
    content.append(_image_block(target_jpeg))
    content.append({"type": "text", "text": prompt_text})

    resp = _client().messages.create(
        model=MODEL,
        max_tokens=1500,
        temperature=settings.BODY_IMAGE_RATING_TEMPERATURE,
        messages=[{"role": "user", "content": content}],
    )
    text = resp.content[0].text  # type: ignore[union-attr]
    return parse_json_lenient(text, source="claude")


def rate_claude_structure(jpeg_bytes: bytes, anchor_pairs: list[tuple[bytes, int]]) -> dict[str, Any]:
    images, scores = _common.split_anchors(anchor_pairs)
    dims = _call(_rubric.structure_prompt(scores or None), jpeg_bytes, images)
    return _common.shape_result("claude_structure", dims)


def rate_claude_surface(jpeg_bytes: bytes, anchor_pairs: list[tuple[bytes, int]]) -> dict[str, Any]:
    images, scores = _common.split_anchors(anchor_pairs)
    dims = _call(_rubric.surface_prompt(scores or None), jpeg_bytes, images)
    return _common.shape_result("claude_surface", dims)
