"""GPT-4o vision rater. Two specialist calls (Structure + Surface),
temp=0, seed rotated per run_index so we can quantify model-internal
variance.

OpenAI's Chat Completions API supports `seed`; we pass it through so a
3-run sample produces *different* sequences at temp=0 (each seed
explores a different deterministic path). Variance across the three
runs becomes the model's noise floor.
"""

from __future__ import annotations

import base64
from typing import Any

from openai import OpenAI

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

from . import _common
from . import _rubric
from ._parse import parse_json_lenient

log = get_logger(__name__)

MODEL = "gpt-4o"


def _client() -> OpenAI:
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def _img_content(jpeg: bytes) -> dict[str, Any]:
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    return {"type": "image_url", "image_url": {"url": data_url}}


def _call(
    prompt_text: str,
    target_jpeg: bytes,
    anchors: list[bytes],
    *,
    seed: int,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [_img_content(a) for a in anchors]
    content.append(_img_content(target_jpeg))
    content.append({"type": "text", "text": prompt_text})

    resp = _client().chat.completions.create(
        model=MODEL,
        max_tokens=1500,
        temperature=settings.BODY_IMAGE_RATING_TEMPERATURE,
        seed=seed,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": content}],
    )
    text = resp.choices[0].message.content or ""
    return parse_json_lenient(text, source="gpt4v")


def rate_gpt4v_structure(
    jpeg_bytes: bytes, anchors: list[bytes],
    anchor_scores: dict[str, int], *, seed: int = 1,
) -> dict[str, Any]:
    prompt = _rubric.structure_prompt(**_common.anchor_kwargs(anchor_scores))
    dims = _call(prompt, jpeg_bytes, anchors, seed=seed)
    return _common.shape_result("gpt4v_structure", dims)


def rate_gpt4v_surface(
    jpeg_bytes: bytes, anchors: list[bytes],
    anchor_scores: dict[str, int], *, seed: int = 1,
) -> dict[str, Any]:
    prompt = _rubric.surface_prompt(**_common.anchor_kwargs(anchor_scores))
    dims = _call(prompt, jpeg_bytes, anchors, seed=seed)
    return _common.shape_result("gpt4v_surface", dims)
