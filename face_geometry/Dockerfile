# Face-geometry sidecar. MediaPipe + OpenCV + NumPy is ~600MB of native
# deps — too heavy to bake into mcp_server. This container exists solely
# so the rest of LifeOS can stay slim.
#
# MediaPipe pins numpy<2 — keep that constraint synced with requirements.txt
# or the image fails at import time.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Native libs:
#   libglib2.0-0, libgl1 — OpenCV-headless's own deps.
#   libgles2, libegl1   — MediaPipe Tasks dlopens libGLESv2.so.2 at
#                         import time even though we never render. The
#                         legacy `mp.solutions` API didn't need these;
#                         the new Tasks API (0.10+) does.
#   curl                — one-shot, used below to fetch the model file.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libglib2.0-0 libgl1 libgles2 libegl1 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY face_geometry/requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Bake the 478-landmark face mesh model into the image so the container
# is offline-capable. ~3MB. The legacy `mp.solutions.face_mesh` shipped
# this internally; the Tasks API (0.10+) requires it as a separate file.
RUN curl -sSL -o face_landmarker.task \
    https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task

COPY face_geometry/main.py ./main.py

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
