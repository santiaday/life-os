"""Lenient JSON parsing for LLM responses.

Centralized so every rater has the same tolerance for ```json fences
and stray whitespace. Failures log the offending text once so we can
see what the model did wrong without exposing the full response in
HTTP errors.
"""

from __future__ import annotations

import json
import re
from typing import Any

from lifeos_core.logging import get_logger

log = get_logger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_json_lenient(text: str, *, source: str) -> dict[str, Any]:
    stripped = text.strip()
    m = _FENCE_RE.search(stripped)
    if m:
        stripped = m.group(1).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as e:
        log.error(f"body_image.{source}_parse_failed", text=text[:400])
        raise RuntimeError(f"{source} returned non-JSON: {e}") from e
