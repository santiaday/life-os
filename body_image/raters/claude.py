"""Claude vision rater. Uses Sonnet 4.6 — costs ~$0.015 per image."""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from anthropic import Anthropic

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

from ._rubric import RUBRIC

log = get_logger(__name__)

# Sonnet (mid-tier) is the right cost/quality tradeoff for this — Opus is
# overkill for a calibrated rubric, Haiku is too noisy.
MODEL = "claude-sonnet-4-6"


def rate_claude(jpeg_bytes: bytes) -> dict[str, Any]:
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(jpeg_bytes).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": RUBRIC},
                ],
            }
        ],
    )
    text = resp.content[0].text  # type: ignore[union-attr]
    dims = _parse_json(text)
    overall = dims.get("overall")
    if not isinstance(overall, (int, float)):
        overall = None
    return {"source": "claude", "overall": overall, "dimensions": dims}


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _parse_json(text: str) -> dict[str, Any]:
    """Tolerate ```json fences and leading/trailing whitespace. Re-raise
    with the offending text on failure so logs are actionable."""
    stripped = text.strip()
    m = _FENCE_RE.search(stripped)
    if m:
        stripped = m.group(1).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as e:
        log.error("body_image.claude_parse_failed", text=text[:400])
        raise RuntimeError(f"Claude returned non-JSON: {e}") from e
