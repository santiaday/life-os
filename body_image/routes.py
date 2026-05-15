"""FastAPI router for the /body-image surface.

Mounted by mcp_server.server.build_app alongside /lifelog. Shares the
LIFELOG_API_TOKEN bearer + LIFELOG_USER_ID resolution from lifelog_api.auth,
since the iOS Shortcut already has that token in Keychain.

Routes:
  POST /body-image/upload        — iOS Shortcut entry point (multipart)
  GET  /body-image/dashboard     — HTML page (Chart.js trends)
  GET  /body-image/api/latest    — most recent photo + ratings (JSON)
  GET  /body-image/api/trends    — 90-day per-feature averages (JSON)
  GET  /body-image/api/photos/{id}/image  — signed Storage URL (redirect)
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse

from lifelog_api.auth import current_user_id, require_token
from lifeos_core.logging import get_logger

from . import service, storage
from .schemas import PhotoRow, TrendResponse, UploadResponse

log = get_logger(__name__)

router = APIRouter(
    prefix="/body-image",
    tags=["body-image"],
)

_DASHBOARD_HTML = (Path(__file__).parent / "templates" / "dashboard.html").read_text(
    encoding="utf-8"
)


# ─── upload ──────────────────────────────────────────────────────────────


@router.post(
    "/upload",
    response_model=UploadResponse,
    dependencies=[Depends(require_token)],
)
async def upload(
    photo: UploadFile = File(...),
    caption: str | None = Form(default=None),
    device: str | None = Form(default=None),
    user_id: str = Depends(current_user_id),
) -> UploadResponse:
    raw = await photo.read()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty photo upload",
        )
    return service.process_upload(
        user_id, photo_bytes=raw, caption=caption, device=device
    )


# ─── dashboard (HTML) ────────────────────────────────────────────────────


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(token: str = Query(default="")) -> HTMLResponse:
    """Browser-facing page. Auth via a `?token=<LIFELOG_API_TOKEN>` query
    param because <img src> and <script> tags can't carry Authorization
    headers. The token is validated here and the page renders inline JS
    that re-passes the same token to the JSON endpoints.

    Not a real auth boundary — anyone with the URL has read access. The
    underlying API endpoints below DO enforce the Bearer header, so this
    is just the convenience surface for the operator (you)."""
    import hmac
    import os

    expected = os.environ.get("LIFELOG_API_TOKEN") or ""
    if not expected or not hmac.compare_digest(token, expected):
        return HTMLResponse(
            "<h1>401</h1><p>Pass ?token=&lt;LIFELOG_API_TOKEN&gt; on the URL.</p>",
            status_code=401,
        )
    # The template interpolates the token into its <script> so the JS
    # fetches can attach Authorization headers. Single-substitution is
    # safe because the token came from our own env, not user input.
    return HTMLResponse(_DASHBOARD_HTML.replace("__LIFELOG_TOKEN__", token))


# ─── JSON API (Bearer-auth) ──────────────────────────────────────────────


@router.get(
    "/api/latest",
    response_model=PhotoRow | None,
    dependencies=[Depends(require_token)],
)
def api_latest(user_id: str = Depends(current_user_id)) -> PhotoRow | None:
    return service.fetch_latest(user_id)


@router.get(
    "/api/trends",
    response_model=TrendResponse,
    dependencies=[Depends(require_token)],
)
def api_trends(
    days: int = Query(default=90, ge=1, le=730),
    user_id: str = Depends(current_user_id),
) -> TrendResponse:
    return service.fetch_trends(user_id, days=days)


@router.get(
    "/api/photos/{photo_id}/image",
    dependencies=[Depends(require_token)],
)
def api_photo_image(
    photo_id: UUID,
    user_id: str = Depends(current_user_id),
) -> RedirectResponse:
    """Redirect to a short-lived Supabase Storage signed URL.

    Why redirect instead of streaming bytes through us: Supabase Storage
    can serve the image directly via CDN once the signed URL is in hand,
    and the redirect keeps this FastAPI process out of the image-bytes
    path. The signed URL expires in 1h."""
    row = service.get_photo(user_id, photo_id)
    if row is None:
        raise HTTPException(status_code=404, detail="photo not found")
    return RedirectResponse(url=storage.signed_url(row.storage_path))
