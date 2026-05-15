"""Supabase Storage HTTP wrapper.

Uses the service-role key — bypasses RLS, full read/write to any bucket.
Keep the key server-side only (it's in .env, never sent to the client).

The Supabase Python SDK isn't a project dependency. The Storage REST API
is tiny (PUT/GET against /storage/v1/object/<bucket>/<path>), so we just
hit it with httpx and skip the extra dep.
"""

from __future__ import annotations

import os

import httpx

from lifeos_core.logging import get_logger

log = get_logger(__name__)


def _base_url() -> str:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        raise RuntimeError(
            "SUPABASE_URL is not set. Add it to .env "
            "(e.g. https://<project-ref>.supabase.co)."
        )
    return url


def _service_key() -> str:
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_KEY is not set in .env.")
    return key


def bucket() -> str:
    return os.environ.get("BODY_IMAGE_BUCKET", "body-image")


def upload(path: str, data: bytes, content_type: str = "image/jpeg") -> None:
    """Upload `data` to <bucket>/<path>. Overwrites on duplicate.

    Raises on non-2xx. Caller is the route — it's already inside a try
    around the photo-save step so we want loud failures here.
    """
    url = f"{_base_url()}/storage/v1/object/{bucket()}/{path}"
    headers = {
        "Authorization": f"Bearer {_service_key()}",
        "Content-Type": content_type,
        # Overwrite if a duplicate path lands (re-uploads, retries).
        "x-upsert": "true",
    }
    resp = httpx.post(url, headers=headers, content=data, timeout=30)
    if resp.status_code >= 300:
        log.error(
            "body_image.storage_upload_failed",
            path=path,
            status=resp.status_code,
            body=resp.text[:300],
        )
        raise RuntimeError(f"Storage upload failed: {resp.status_code} {resp.text[:200]}")


def signed_url(path: str, expires_in_seconds: int = 3600) -> str:
    """Generate a short-lived signed URL for `<bucket>/<path>`.

    The dashboard uses these for <img> src so the bucket can stay private.
    """
    url = f"{_base_url()}/storage/v1/object/sign/{bucket()}/{path}"
    headers = {
        "Authorization": f"Bearer {_service_key()}",
        "Content-Type": "application/json",
    }
    resp = httpx.post(
        url,
        headers=headers,
        json={"expiresIn": expires_in_seconds},
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    # Response shape: {"signedURL": "/object/sign/<bucket>/<path>?token=..."}
    signed = body.get("signedURL") or body.get("signedUrl") or ""
    if not signed:
        raise RuntimeError(f"Storage signed URL missing: {body}")
    return f"{_base_url()}/storage/v1{signed}"
