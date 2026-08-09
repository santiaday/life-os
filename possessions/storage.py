"""Supabase Storage wrapper for possession photos.

Same shape as body_image/storage.py — the Storage REST API is small enough
that pulling in the Supabase SDK for it isn't worth a dependency — but this
one takes the bucket from POSSESSIONS_BUCKET and knows the three prefixes the
pipeline writes to:

    possessions/original/<hash>.<ext>   the untouched capture
    possessions/cutout/<hash>.png       RGBA, background removed
    possessions/thumb/<hash>.webp       grid-sized cutout

Originals are kept forever. Segmentation models get better roughly every year
and re-running one over the originals costs nothing; re-shooting 300 items
because only the cutouts survived costs a weekend.
"""

from __future__ import annotations

import os

import httpx

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

log = get_logger(__name__)

ORIGINAL_PREFIX = "original"
CUTOUT_PREFIX = "cutout"
THUMB_PREFIX = "thumb"

_TIMEOUT = 60.0  # originals off a phone camera are routinely 5-10 MB


# Config is read through `settings` rather than os.environ directly, unlike
# body_image/storage.py. That module only ever runs in the container, where
# docker-compose's env_file has already exported everything; the ingest CLI
# runs from a shell on a laptop where it hasn't, and reading os.environ there
# fails with "SUPABASE_URL is not set" on a machine whose .env sets it. An
# explicit environment variable still wins, so a one-off override works.
def _base_url() -> str:
    url = (os.environ.get("SUPABASE_URL") or settings.SUPABASE_URL or "").rstrip("/")
    if not url:
        raise RuntimeError(
            "SUPABASE_URL is not set. Add it to .env "
            "(e.g. https://<project-ref>.supabase.co)."
        )
    return url


def _service_key() -> str:
    key = os.environ.get("SUPABASE_SERVICE_KEY") or settings.SUPABASE_SERVICE_KEY or ""
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_KEY is not set in .env.")
    return key


def bucket() -> str:
    return os.environ.get("POSSESSIONS_BUCKET") or settings.POSSESSIONS_BUCKET


def _headers(extra: dict | None = None) -> dict:
    h = {"Authorization": f"Bearer {_service_key()}"}
    h.update(extra or {})
    return h


def ensure_bucket() -> bool:
    """Create the bucket if it isn't there. Returns True if it was created.

    Private bucket: photos of everything you own is exactly the sort of thing
    that shouldn't be world-readable behind a guessable path. The UI and MCP
    tools hand out short-lived signed URLs instead.
    """
    name = bucket()
    resp = httpx.get(f"{_base_url()}/storage/v1/bucket/{name}",
                     headers=_headers(), timeout=15)
    if resp.status_code == 200:
        return False
    resp = httpx.post(
        f"{_base_url()}/storage/v1/bucket",
        headers=_headers({"Content-Type": "application/json"}),
        json={"id": name, "name": name, "public": False},
        timeout=15,
    )
    if resp.status_code >= 300:
        # A concurrent create is fine; anything else is not.
        if resp.status_code == 409 or "already exists" in resp.text.lower():
            return False
        raise RuntimeError(f"bucket create failed: {resp.status_code} {resp.text[:200]}")
    log.info("possessions.storage.bucket_created", bucket=name)
    return True


def upload(path: str, data: bytes, content_type: str) -> str:
    """Upload to <bucket>/<path>, overwriting. Returns the path stored in PG."""
    url = f"{_base_url()}/storage/v1/object/{bucket()}/{path}"
    resp = httpx.post(
        url,
        headers=_headers({"Content-Type": content_type, "x-upsert": "true"}),
        content=data,
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 300:
        log.error("possessions.storage.upload_failed", path=path,
                  status=resp.status_code, body=resp.text[:300])
        raise RuntimeError(f"Storage upload failed: {resp.status_code} {resp.text[:200]}")
    return path


def download(path: str) -> bytes:
    url = f"{_base_url()}/storage/v1/object/{bucket()}/{path}"
    resp = httpx.get(url, headers=_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def signed_url(path: str, expires_in_seconds: int = 3600) -> str:
    """Short-lived signed URL, so the bucket stays private and the UI can
    still put the cutout in an <img src>."""
    resp = httpx.post(
        f"{_base_url()}/storage/v1/object/sign/{bucket()}/{path}",
        headers=_headers({"Content-Type": "application/json"}),
        json={"expiresIn": expires_in_seconds},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    signed = body.get("signedURL") or body.get("signedUrl") or ""
    if not signed:
        raise RuntimeError(f"Storage signed URL missing: {body}")
    return f"{_base_url()}/storage/v1{signed}"


def signed_urls(paths: list[str], expires_in_seconds: int = 3600) -> dict[str, str]:
    """Batch-sign. The grid asks for 300 at once; 300 round trips would make
    the first paint take longer than the whole rest of the page."""
    wanted = [p for p in dict.fromkeys(paths) if p]
    if not wanted:
        return {}
    resp = httpx.post(
        f"{_base_url()}/storage/v1/object/sign/{bucket()}",
        headers=_headers({"Content-Type": "application/json"}),
        json={"expiresIn": expires_in_seconds, "paths": wanted},
        timeout=30,
    )
    if resp.status_code >= 300:
        log.warning("possessions.storage.batch_sign_failed",
                    status=resp.status_code, body=resp.text[:200])
        return {}
    out: dict[str, str] = {}
    for row in resp.json():
        signed = row.get("signedURL") or row.get("signedUrl")
        path = row.get("path")
        if signed and path:
            out[path] = f"{_base_url()}/storage/v1{signed}"
    return out
