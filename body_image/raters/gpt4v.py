"""GPT-4o vision rater. Same rubric, different model.

OpenAI's vision API takes a data: URL in the message content; the rest
is identical to a normal chat completion.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from openai import OpenAI

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

from ._rubric import RUBRIC

log = get_logger(__name__)

MODEL = "gpt-4o"


def rate_gpt4v(jpeg_bytes: bytes) -> dict[str, Any]:
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    data_url = (
        "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode("ascii")
    )
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=1500,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": RUBRIC},
                ],
            }
        ],
    )
    text = resp.choices[0].message.content or ""
    dims = _parse_json(text)
    overall = dims.get("overall")
    if not isinstance(overall, (int, float)):
        overall = None
    return {"source": "gpt4v", "overall": overall, "dimensions": dims}


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _parse_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    m = _FENCE_RE.search(stripped)
    if m:
        stripped = m.group(1).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as e:
        log.error("body_image.gpt4v_parse_failed", text=text[:400])
        raise RuntimeError(f"GPT-4o returned non-JSON: {e}") from e
