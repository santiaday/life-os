"""Gemini vision rater. Uses Gemini 2.5 Flash — free tier covers a
daily-cadence solo user comfortably.

Same two-call shape as Claude/GPT-4o (structure + surface), same
anti-halo rubric, same JSON output. Optional in the fan-out — disabled
silently if GEMINI_API_KEY isn't set.

Gemini's google-genai SDK isn't in the project deps yet. We use raw
HTTP to its REST endpoint so we don't have to add the SDK to a
Dockerfile that's already kitchen-sinked. The REST surface is simple
and stable for Gemini 1.5/2.0/2.5: POST to
https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=$KEY
with {contents: [{parts: [{text|inline_data}...]}], generationConfig}.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

from . import _rubric
from . import _common

log = get_logger(__name__)

MODEL = "gemini-2.5-flash"
ENDPOINT_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


def _call(prompt_text: str, target_jpeg: bytes, anchor_images: list[bytes]) -> dict[str, Any]:
    if not settings.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    parts: list[dict[str, Any]] = []
    for a in anchor_images:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(a).decode("ascii"),
            }
        })
    parts.append({
        "inline_data": {
            "mime_type": "image/jpeg",
            "data": base64.b64encode(target_jpeg).decode("ascii"),
        }
    })
    parts.append({"text": prompt_text})

    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": settings.BODY_IMAGE_RATING_TEMPERATURE,
            "topP": 1.0,
            "maxOutputTokens": 1500,
            "responseMimeType": "application/json",
        },
    }
    url = ENDPOINT_TEMPLATE.format(model=MODEL)
    resp = httpx.post(
        url,
        params={"key": settings.GEMINI_API_KEY},
        json=body,
        timeout=60,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Gemini {resp.status_code}: {resp.text[:200]}")
    payload = resp.json()
    # candidates[0].content.parts[0].text
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Gemini unexpected payload shape: {payload}") from e
    return _parse_json(text)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _parse_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    m = _FENCE_RE.search(stripped)
    if m:
        stripped = m.group(1).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as e:
        log.error("body_image.gemini_parse_failed", text=text[:400])
        raise RuntimeError(f"Gemini returned non-JSON: {e}") from e


def rate_gemini_structure(jpeg_bytes: bytes, anchor_pairs: list[tuple[bytes, int]]) -> dict[str, Any]:
    images, scores = _common.split_anchors(anchor_pairs)
    dims = _call(_rubric.structure_prompt(scores or None), jpeg_bytes, images)
    return _common.shape_result("gemini_structure", dims)


def rate_gemini_surface(jpeg_bytes: bytes, anchor_pairs: list[tuple[bytes, int]]) -> dict[str, Any]:
    images, scores = _common.split_anchors(anchor_pairs)
    dims = _call(_rubric.surface_prompt(scores or None), jpeg_bytes, images)
    return _common.shape_result("gemini_surface", dims)
