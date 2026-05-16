"""Calibration anchor loader.

When BODY_IMAGE_USE_CALIBRATION_ANCHORS is true, every LLM rating call
prepends N anchor images with their known scores. This pins the model's
0-100 scale to external ground truth instead of letting it drift between
sessions.

Anchor scheme (N-anchor, list-shaped):
  body_image/calibration/anchor_scores.json
    {
      "anchors": [
        {"file": "anchor_01.jpg", "overall": 40, ...},
        {"file": "anchor_02.jpg", "overall": 55, ...},
        ...
      ]
    }
  body_image/calibration/anchor_01.jpg, anchor_02.jpg, ...

`load_anchor_pairs()` returns [(jpeg_bytes, score_int), ...] sorted by
score. Each LLM rater iterates that list to build its image-message.

Setup walkthrough lives in body_image/calibration/README.md.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CALIBRATION_DIR = Path(__file__).parent


@lru_cache(maxsize=1)
def _scores_payload() -> dict:
    p = CALIBRATION_DIR / "anchor_scores.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def _anchor_specs() -> list[dict]:
    """Return the list of {file, overall} dicts in score order."""
    payload = _scores_payload()
    items = payload.get("anchors") or []
    return sorted(
        (a for a in items if "file" in a and "overall" in a),
        key=lambda a: a["overall"],
    )


def anchors_available() -> bool:
    """True when anchor_scores.json lists ≥1 anchor and every referenced
    file exists on disk. Validation cron uses this to decide whether
    to inject anchors during reference scoring."""
    specs = _anchor_specs()
    if not specs:
        return False
    return all((CALIBRATION_DIR / a["file"]).exists() for a in specs)


@lru_cache(maxsize=16)
def _read_bytes(path_str: str) -> bytes | None:
    p = Path(path_str)
    return p.read_bytes() if p.exists() else None


def load_anchor_pairs() -> list[tuple[bytes, int]]:
    """Return [(jpeg_bytes, score), ...] sorted ascending by score. Empty
    list when anchors are missing or unparseable."""
    out: list[tuple[bytes, int]] = []
    for spec in _anchor_specs():
        path = CALIBRATION_DIR / spec["file"]
        data = _read_bytes(str(path))
        if data is None:
            continue
        out.append((data, int(spec["overall"])))
    return out
