"""Batch photo ingest: folder in, tagged inventory rows out.

Per photo: hash -> dedupe -> background removal -> thumbnail -> upload all
three renditions -> vision tag -> insert with `reviewed = false`.

The hash is the whole idempotency story. sha256 of the original bytes is the
unique key on dim_possession, so a re-run over the same folder skips what
already landed — which means a batch that dies at item 180 is resumed by
running exactly the same command again, and a folder that gains ten new
photos is caught up the same way. Without it, the second run doubles the
inventory.

Concurrency: the per-item pipeline runs in a small thread pool because it is
almost entirely waiting — one vision call and three uploads per item. rembg
inference is the exception and is serialised behind a lock; ONNX sessions are
not safe to share across threads, and a second copy of the model is 180 MB
we don't need. Only `workers` originals are ever in memory at once, which is
what keeps a 300-photo folder from being a 1.5 GB resident set.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lifeos_core.db import conn
from lifeos_core.logging import get_logger

from . import segmentation, service, storage, tagging

log = get_logger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"}

# rembg's ONNX session is not thread-safe; one inference at a time.
_SEGMENT_LOCK = threading.Lock()


@dataclass
class ItemResult:
    path: str
    status: str                     # created | skipped | failed
    possession_id: str | None = None
    name: str | None = None
    photo_hash: str | None = None
    error: str | None = None
    low_confidence: list[str] = field(default_factory=list)


@dataclass
class BatchResult:
    created: int = 0
    skipped: int = 0
    failed: int = 0
    items: list[ItemResult] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "created": self.created,
            "skipped": self.skipped,
            "failed": self.failed,
            "items": [vars(i) for i in self.items],
        }


def discover(folder: str | Path) -> list[Path]:
    """Image files in `folder`, recursively, in a stable order.

    Sorted so a resumed run processes in the same sequence as the first, which
    makes "it died around item 180" a usable piece of information.
    """
    root = Path(folder).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"no such folder: {root}")
    if root.is_file():
        return [root]
    return sorted(
        p for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in IMAGE_SUFFIXES
        and not p.name.startswith(".")      # ._foo AppleDouble stubs are not images
    )


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def existing_hashes(hashes: Iterable[str]) -> set[str]:
    wanted = list(dict.fromkeys(hashes))
    if not wanted:
        return set()
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT photo_hash FROM dim_possession WHERE photo_hash = ANY(%s)",
            [wanted],
        )
        return {r["photo_hash"] for r in cur.fetchall()}


def _content_type(path: Path) -> str:
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
        ".tif": "image/tiff", ".tiff": "image/tiff",
    }.get(path.suffix.lower(), "application/octet-stream")


def process_one(
    path: Path,
    *,
    location: str | None = None,
    hint: str | None = None,
    do_cutout: bool = True,
    do_tag: bool = True,
    model: str | None = None,
) -> ItemResult:
    """The whole pipeline for one photo. Never raises — a bad frame in the
    middle of a 300-item batch should cost that frame, not the batch."""
    try:
        raw = path.read_bytes()
    except OSError as e:
        return ItemResult(path=str(path), status="failed", error=f"unreadable: {e}")

    digest = hash_bytes(raw)
    try:
        original_path = f"{storage.ORIGINAL_PREFIX}/{digest}{path.suffix.lower()}"
        storage.upload(original_path, raw, _content_type(path))

        cutout_path = thumb_path = None
        vision_jpeg = None
        if do_cutout:
            with _SEGMENT_LOCK:
                cutout = segmentation.remove_background(raw)
            thumb = segmentation.make_thumb(cutout)
            cutout_path = f"{storage.CUTOUT_PREFIX}/{digest}.png"
            thumb_path = f"{storage.THUMB_PREFIX}/{digest}.webp"
            storage.upload(cutout_path, cutout, "image/png")
            storage.upload(thumb_path, thumb, "image/webp")
            vision_jpeg = segmentation.flatten_for_vision(cutout)
        else:
            vision_jpeg = segmentation.prepare_original_for_vision(raw)

        fields: dict[str, Any] = {
            "photo_hash": digest,
            "photo_original": original_path,
            "photo_cutout": cutout_path,
            "photo_thumb": thumb_path,
        }
        low_conf: list[str] = []

        if do_tag:
            tags = tagging.tag_image(vision_jpeg, model=model, hint=hint)
            confidence = tags.pop("confidence", {})
            fields["tag_model"] = tags.pop("tag_model", None)
            fields["tag_confidence"] = confidence
            fields.update({k: v for k, v in tags.items() if v is not None or k in (
                "subcategory", "brand", "colorway", "secondary_color",
                "material", "size")})
            low_conf = tagging.low_confidence_fields(confidence)
        else:
            # Untagged rows still need the NOT NULLs. They land as obvious
            # placeholders so the review queue shows them as work to do rather
            # than as plausible-looking data.
            fields.update({"name": path.stem, "kind": "other",
                           "primary_color": "multicolor"})

        if location:
            fields["location"] = location

        row = service.log_possession(**fields)
        return ItemResult(path=str(path), status="created",
                          possession_id=str(row["id"]), name=row["name"],
                          photo_hash=digest, low_confidence=low_conf)
    except Exception as e:  # one bad frame must not kill the batch
        log.warning("possessions.ingest.item_failed", path=str(path),
                    error=str(e), error_type=type(e).__name__)
        return ItemResult(path=str(path), status="failed", photo_hash=digest,
                          error=f"{type(e).__name__}: {e}")


def ingest_folder(
    folder: str | Path,
    *,
    location: str | None = None,
    hint: str | None = None,
    do_cutout: bool = True,
    do_tag: bool = True,
    model: str | None = None,
    workers: int = 4,
    limit: int | None = None,
    on_progress=None,
) -> BatchResult:
    """Process every new image in `folder`."""
    paths = discover(folder)
    if limit:
        paths = paths[:limit]

    result = BatchResult()
    if not paths:
        return result

    storage.ensure_bucket()

    # Pre-filter on hash before doing any work. Reading 300 files to hash them
    # is seconds; segmenting and tagging 300 already-ingested files is an hour
    # and a real API bill.
    todo: list[Path] = []
    for path in paths:
        try:
            digest = hash_bytes(path.read_bytes())
        except OSError as e:
            result.failed += 1
            result.items.append(ItemResult(path=str(path), status="failed",
                                           error=f"unreadable: {e}"))
            continue
        todo.append(path)
        result.items.append(ItemResult(path=str(path), status="pending",
                                       photo_hash=digest))

    seen = existing_hashes(i.photo_hash for i in result.items if i.photo_hash)
    pending = []
    for item in result.items:
        if item.status != "pending":
            continue
        if item.photo_hash in seen:
            item.status = "skipped"
            result.skipped += 1
        else:
            pending.append(item)

    if not pending:
        return result

    index = {i.path: i for i in pending}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(process_one, Path(i.path), location=location, hint=hint,
                        do_cutout=do_cutout, do_tag=do_tag, model=model): i.path
            for i in pending
        }
        for done, future in enumerate(as_completed(futures), start=1):
            outcome = future.result()
            slot = index[futures[future]]
            slot.status = outcome.status
            slot.possession_id = outcome.possession_id
            slot.name = outcome.name
            slot.error = outcome.error
            slot.low_confidence = outcome.low_confidence
            if outcome.status == "created":
                result.created += 1
            else:
                result.failed += 1
            if on_progress:
                on_progress(done, len(pending), outcome)

    return result
