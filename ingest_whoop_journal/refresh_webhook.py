"""FastAPI router for the iPhone Shortcut → server token-refresh callback.

The iPhone runs REFRESH_TOKEN_AUTH against Whoop's auth-service on a schedule
(see RUNBOOK.md). On success it POSTs the fresh token bundle to
``/lifelog/whoop/refresh-callback`` so the server can persist it. The
ingester then reads the row at runtime.

Auth model: shared-secret only. The iOS Shortcut sends
``X-Shared-Secret: <WHOOP_REFRESH_WEBHOOK_SECRET>`` and we compare with
``hmac.compare_digest``. We deliberately do NOT use the lifelog bearer
token here — keeping the two auth domains separate means rotating one
doesn't cascade, and the Shortcut's "Get Contents of URL" action handles
custom headers cleanly.

This router gets mounted by ``mcp_server.server.build_app`` alongside the
lifelog router. It nests under ``/lifelog`` for log-monitoring and TLS-
config ergonomics, but with its own ``Depends(...)`` chain so the lifelog
bearer middleware doesn't apply.
"""

from __future__ import annotations

import hmac
import os
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from ingest_whoop_journal import auth
from lifeos_core.logging import get_logger

log = get_logger(__name__)

# Cognito IDs are JWT-shaped. We don't *verify* the signature (we don't have
# the JWK URL set up and we trust the iPhone), but we sanity-check the
# structure so a typo or empty string doesn't poison the table.
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


class RefreshCallbackBody(BaseModel):
    access_token: str = Field(..., min_length=20)
    refresh_token: str = Field(..., min_length=20)
    id_token: str | None = None
    # Optional. If iOS forwards the Cognito ExpiresIn we use it; otherwise
    # we default to 24h, which matches the Whoop access-token TTL.
    expires_in: int | None = Field(default=None, ge=60, le=86400 * 7)


class RefreshCallbackResponse(BaseModel):
    ok: bool
    expires_at: datetime


def _shared_secret() -> str | None:
    """Read at request time so a rotated secret takes effect without restart."""
    return os.environ.get("WHOOP_REFRESH_WEBHOOK_SECRET")


def _check_secret(provided: str | None) -> None:
    expected = _shared_secret()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WHOOP_REFRESH_WEBHOOK_SECRET is not configured on the server.",
        )
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-Shared-Secret",
        )


router = APIRouter(prefix="/lifelog/whoop", tags=["whoop_journal"])


@router.post("/refresh-callback", response_model=RefreshCallbackResponse)
def refresh_callback(
    body: RefreshCallbackBody,
    x_shared_secret: str | None = Header(default=None, alias="X-Shared-Secret"),
) -> RefreshCallbackResponse:
    """Persist a fresh Whoop token bundle from the iPhone Shortcut.

    Returns 401 when the shared secret is missing or wrong. Returns 400 when
    access_token / refresh_token aren't JWT-shaped (the Shortcut's Cognito
    response sometimes hits an HTML error page during partial outages, and
    we'd rather reject those than poison the table). Returns 200 + the
    computed expires_at on success.
    """
    _check_secret(x_shared_secret)

    if not _JWT_RE.match(body.access_token):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="access_token doesn't look like a JWT (header.payload.sig)",
        )
    if body.id_token is not None and not _JWT_RE.match(body.id_token):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="id_token doesn't look like a JWT",
        )
    # refresh_token is opaque (not JWT) — only sanity-check non-empty + minlen.

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=body.expires_in or 86400)
    auth.save_tokens(
        access_token=body.access_token,
        refresh_token=body.refresh_token,
        id_token=body.id_token,
        expires_at=expires_at,
        metadata={
            "source": "ios_shortcut_refresh",
            "received_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    log.info("whoop_journal.refresh_webhook.ok", expires_at=expires_at.isoformat())
    return RefreshCallbackResponse(ok=True, expires_at=expires_at)
