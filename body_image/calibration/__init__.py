"""Calibration anchor loader.

When BODY_IMAGE_USE_CALIBRATION_ANCHORS is true and the three anchor
images exist on disk, every LLM rating call prepends them with their
known scores. This pins the model's 0-100 scale to external ground
truth instead of letting it drift between sessions.

Setup:
  1. Source three frontal male headshots with known crowd-rated scores
     from SCUT-FBP5500 (or any dataset where scores are public). See
     RUNBOOK §"Calibration anchors" for instructions.
  2. Save them as body_image/calibration/anchor_low.jpg,
     anchor_mid.jpg, anchor_high.jpg.
  3. Edit anchor_scores.json with the dataset-published scores
     (overall_low, overall_mid, overall_high).
  4. Set BODY_IMAGE_USE_CALIBRATION_ANCHORS=true in .env.
"""

from __future__ import annotations

import base64
import json
from functools import lru_cache
from pathlib import Path

CALIBRATION_DIR = Path(__file__).parent


@lru_cache(maxsize=1)
def _scores() -> dict[str, int]:
    p = CALIBRATION_DIR / "anchor_scores.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def anchor_image_path(level: str) -> Path:
    return CALIBRATION_DIR / f"anchor_{level}.jpg"


@lru_cache(maxsize=4)
def anchor_bytes(level: str) -> bytes | None:
    """Read anchor JPEG bytes from disk. Returns None if missing.
    LRU-cached so repeated raters reuse the same buffer."""
    p = anchor_image_path(level)
    if not p.exists():
        return None
    return p.read_bytes()


def anchor_b64(level: str) -> str | None:
    b = anchor_bytes(level)
    return base64.b64encode(b).decode("ascii") if b else None


def anchors_available() -> bool:
    """True when all three anchor files + the scores file are present
    and the scores file has the three expected keys."""
    if not all(anchor_image_path(lvl).exists() for lvl in ("low", "mid", "high")):
        return False
    s = _scores()
    return all(k in s for k in ("overall_low", "overall_mid", "overall_high"))


def anchor_score(level: str) -> int | None:
    """Lookup the published score for a level ('low'|'mid'|'high')."""
    return _scores().get(f"overall_{level}")
