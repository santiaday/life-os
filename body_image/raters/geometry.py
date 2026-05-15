"""MediaPipe geometry rater.

MediaPipe is Python-only and brings a chunky native build (~150MB),
so it runs as a separate `face-geometry` container on the compose
network. We just HTTP-call it here.

The objective layer of the rating stack — same photo always produces the
same numbers (unlike LLMs). Symmetry score, gonial angle, jaw/cheekbone
ratio.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from lifeos_core.logging import get_logger

log = get_logger(__name__)

# Default points at the docker-compose service name. Override with
# FACE_GEOMETRY_URL for local dev (e.g. http://localhost:8001).
DEFAULT_URL = "http://face-geometry:8000/analyze"


def rate_geometry(jpeg_bytes: bytes) -> dict[str, Any]:
    url = os.environ.get("FACE_GEOMETRY_URL", DEFAULT_URL)
    files = {"file": ("photo.jpg", jpeg_bytes, "image/jpeg")}
    resp = httpx.post(url, files=files, timeout=20)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        # No face detected, blurry, etc. — return null overall so the
        # service layer still inserts the row (useful for diagnostics).
        return {"source": "geometry", "overall": None, "dimensions": body}
    return {"source": "geometry", "overall": None, "dimensions": body}
