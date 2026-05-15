"""Face-geometry sidecar.

Standalone FastAPI service. MediaPipe + OpenCV + NumPy bring a heavy
native build (~150MB), so they live in a separate container instead of
bloating the mcp_server image.

Single endpoint: POST /analyze (multipart file=<jpeg>) → JSON with three
deterministic measurements. Same photo always produces the same numbers
— the objective layer of the body-image rating stack.

MediaPipe 0.10+ Tasks API is the only surface (`mp.solutions` was
removed). Landmark indices are unchanged — same 478-point face mesh
model, just a different loader.
"""

from __future__ import annotations

import cv2
import mediapipe as mp
import numpy as np
from fastapi import FastAPI, UploadFile
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision

app = FastAPI(title="face-geometry")

# Path baked into the Docker image — Dockerfile downloads the .task at
# build time so the container is offline-capable.
MODEL_PATH = "/app/face_landmarker.task"

_options = vision.FaceLandmarkerOptions(
    base_options=mp_python.BaseOptions(model_asset_path=MODEL_PATH),
    running_mode=vision.RunningMode.IMAGE,
    num_faces=1,
)
# Created once at import (thread-safe for single-image detect calls).
_landmarker = vision.FaceLandmarker.create_from_options(_options)


def _angle(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
    """Angle ABC in degrees."""
    ba = a - b
    bc = c - b
    denom = np.linalg.norm(ba) * np.linalg.norm(bc)
    if denom == 0:
        return float("nan")
    cosang = np.clip(np.dot(ba, bc) / denom, -1.0, 1.0)
    return float(np.degrees(np.arccos(cosang)))


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/analyze")
async def analyze(file: UploadFile) -> dict:
    raw = await file.read()
    arr = np.frombuffer(raw, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return {"error": "invalid image"}
    h, w = img.shape[:2]
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    result = _landmarker.detect(mp_image)
    if not result.face_landmarks:
        return {"error": "no face detected"}

    landmarks = result.face_landmarks[0]
    pts = np.array([(lm.x * w, lm.y * h) for lm in landmarks])

    # Symmetry: for paired landmarks, how far each pair's midpoint sits
    # from the face's overall midline. Lower deviation = more symmetric.
    midline = (pts[10][0] + pts[152][0]) / 2  # forehead-chin x
    pairs = [(33, 263), (133, 362), (61, 291), (78, 308), (172, 397)]
    sym_dev = np.mean(
        [abs(pts[lft][0] - (2 * midline - pts[rgt][0])) for lft, rgt in pairs]
    )
    symmetry_score = float(100 - min(100, sym_dev * 2))

    # Gonial angle: jaw corner. ~120-130° = soft; ~110° = square.
    gonial = (
        _angle(pts[234], pts[172], pts[152])
        + _angle(pts[454], pts[397], pts[152])
    ) / 2

    # Bigonial (jaw width at corners) / bizygomatic (cheekbone width).
    # Lower = more tapered. >1 means jaw wider than cheekbones (uncommon).
    bigonial = float(np.linalg.norm(pts[172] - pts[397]))
    bizygomatic = float(np.linalg.norm(pts[234] - pts[454]))
    jaw_cheek_ratio = bigonial / bizygomatic if bizygomatic else None

    return {
        "symmetry_score": symmetry_score,
        "gonial_angle_deg": gonial,
        "bigonial_bizygomatic_ratio": jaw_cheek_ratio,
        "image_width": w,
        "image_height": h,
    }
