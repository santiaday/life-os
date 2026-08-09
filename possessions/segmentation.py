"""Background removal and thumbnailing.

rembg with `isnet-general-use` — free, batchable, and good enough for a
one-time 200-300 item job. A paid per-image API (Photoroom, remove.bg) is
worth reaching for on the handful of items rembg mangles (mesh, lace,
translucent shells, white-on-white), not on the default path.

rembg is an OPTIONAL dependency and imported lazily. It drags in onnxruntime
plus a ~180 MB model, which has no business on a 1 GB droplet that only ever
serves already-cut-out PNGs. The ingest CLI runs on the machine holding the
photos; the API container never calls in here.

    pip install "rembg[cpu]"
"""

from __future__ import annotations

import io
import os
from functools import lru_cache

from PIL import Image

from lifeos_core.logging import get_logger

log = get_logger(__name__)

MODEL = os.environ.get("POSSESSIONS_REMBG_MODEL", "isnet-general-use")
THUMB_MAX_PX = 512
TAG_MAX_PX = 1024      # what the vision call sees; see tagging.py


class SegmentationUnavailable(RuntimeError):
    """rembg isn't installed. Raised with the fix, not just the ImportError."""


@lru_cache(maxsize=4)
def _session(model_name: str):
    try:
        from rembg import new_session
    except ImportError as e:  # pragma: no cover - depends on local install
        raise SegmentationUnavailable(
            "rembg is not installed. It is an optional dependency because the "
            "API container never needs it:\n"
            '    pip install "rembg[cpu]"\n'
            "Or run the batch with --no-cutout to ingest originals only."
        ) from e
    # One session reused across the batch: model load is ~3s, per-image
    # inference is ~0.4s. Re-creating it per photo would dominate the run.
    return new_session(model_name)


def remove_background(image_bytes: bytes, model_name: str | None = None) -> bytes:
    """Return an RGBA PNG with the background removed."""
    from rembg import remove

    out = remove(image_bytes, session=_session(model_name or MODEL))
    # rembg returns PNG bytes already, but normalize through PIL so we know
    # the mode is RGBA whatever the model handed back.
    with Image.open(io.BytesIO(out)) as im:
        im = im.convert("RGBA")
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def _fit(im: Image.Image, max_px: int) -> Image.Image:
    if max(im.size) <= max_px:
        return im
    scale = max_px / max(im.size)
    return im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                     Image.LANCZOS)


def make_thumb(cutout_png: bytes, max_px: int = THUMB_MAX_PX) -> bytes:
    """Grid-sized WebP, transparency preserved. The grid is the product; a
    512px cutout on transparent is what it renders."""
    with Image.open(io.BytesIO(cutout_png)) as im:
        im = _fit(im.convert("RGBA"), max_px)
        buf = io.BytesIO()
        im.save(buf, format="WEBP", quality=85, method=4)
        return buf.getvalue()


def flatten_for_vision(cutout_png: bytes, max_px: int = TAG_MAX_PX,
                       background: tuple[int, int, int] = (128, 128, 128)) -> bytes:
    """JPEG for the vision call: downscaled and composited onto mid-grey.

    Two reasons not to send the RGBA cutout as-is. Transparent pixels encode
    as black in JPEG, which turns every white or cream item into a silhouette
    the model reads as black. And a full-resolution image costs several times
    the tokens of a 1024px one for tags that don't depend on the extra pixels.
    Mid-grey keeps both white and black items legible against it.
    """
    with Image.open(io.BytesIO(cutout_png)) as im:
        im = _fit(im.convert("RGBA"), max_px)
        canvas = Image.new("RGB", im.size, background)
        canvas.paste(im, mask=im.split()[3])
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=88)
        return buf.getvalue()


def prepare_original_for_vision(image_bytes: bytes, max_px: int = TAG_MAX_PX) -> bytes:
    """Same downscale for the --no-cutout path, where there's no alpha."""
    with Image.open(io.BytesIO(image_bytes)) as im:
        im = _fit(im.convert("RGB"), max_px)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
        return buf.getvalue()
