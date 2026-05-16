"""FastAPI router for /body-image."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import UUID

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status,
)
from fastapi.responses import HTMLResponse, RedirectResponse

from lifelog_api.auth import current_user_id, require_token
from lifeos_core.logging import get_logger

from . import interventions as interventions_service
from . import service, storage
from .schemas import (
    Intervention, InterventionCreate,
    PhotoRow, SessionRow, TrendResponse, UploadResponse,
)

log = get_logger(__name__)

router = APIRouter(prefix="/body-image", tags=["body-image"])

_DASHBOARD_HTML = (Path(__file__).parent / "templates" / "dashboard.html").read_text(
    encoding="utf-8"
)


# ─── upload ─────────────────────────────────────────────────────────


@router.post(
    "/upload",
    response_model=UploadResponse,
    dependencies=[Depends(require_token)],
)
async def upload(
    photo: UploadFile = File(...),
    caption: str | None = Form(default=None),
    device: str | None = Form(default=None),
    session_id: UUID | None = Form(default=None),
    angle: str | None = Form(default=None),
    user_id: str = Depends(current_user_id),
) -> UploadResponse:
    raw = await photo.read()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty photo upload",
        )
    return service.process_upload(
        user_id,
        photo_bytes=raw, caption=caption, device=device,
        session_id=session_id, angle=angle,
    )


# ─── dashboard ──────────────────────────────────────────────────────


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(token: str = Query(default="")) -> HTMLResponse:
    import hmac
    import os
    expected = os.environ.get("LIFELOG_API_TOKEN") or ""
    if not expected or not hmac.compare_digest(token, expected):
        return HTMLResponse(
            "<h1>401</h1><p>Pass ?token=&lt;LIFELOG_API_TOKEN&gt; on the URL.</p>",
            status_code=401,
        )
    return HTMLResponse(_DASHBOARD_HTML.replace("__LIFELOG_TOKEN__", token))


# ─── JSON API: photos + ratings ─────────────────────────────────────


@router.get(
    "/api/latest",
    response_model=PhotoRow | None,
    dependencies=[Depends(require_token)],
)
def api_latest(user_id: str = Depends(current_user_id)) -> PhotoRow | None:
    return service.fetch_latest(user_id)


@router.get(
    "/api/history",
    response_model=list[PhotoRow],
    dependencies=[Depends(require_token)],
)
def api_history(
    limit: int = Query(default=50, ge=1, le=200),
    user_id: str = Depends(current_user_id),
) -> list[PhotoRow]:
    return service.fetch_history(user_id, limit=limit)


@router.get(
    "/api/sessions",
    response_model=list[SessionRow],
    dependencies=[Depends(require_token)],
)
def api_sessions(
    limit: int = Query(default=25, ge=1, le=100),
    user_id: str = Depends(current_user_id),
) -> list[SessionRow]:
    return service.fetch_sessions(user_id, limit=limit)


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
    row = service.get_photo(user_id, photo_id)
    if row is None:
        raise HTTPException(status_code=404, detail="photo not found")
    return RedirectResponse(url=storage.signed_url(row.storage_path))


# ─── interventions ─────────────────────────────────────────────────


@router.post(
    "/api/interventions",
    response_model=Intervention,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_token)],
)
def api_create_intervention(
    req: InterventionCreate,
    user_id: str = Depends(current_user_id),
) -> Intervention:
    return interventions_service.create_intervention(user_id, req)


@router.get(
    "/api/interventions",
    response_model=list[Intervention],
    dependencies=[Depends(require_token)],
)
def api_list_interventions(
    intervention_key: str | None = Query(default=None),
    since: date | None = Query(default=None),
    user_id: str = Depends(current_user_id),
) -> list[Intervention]:
    return interventions_service.list_interventions(
        user_id, intervention_key=intervention_key, since=since,
    )


@router.delete(
    "/api/interventions/{intervention_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_token)],
)
def api_delete_intervention(
    intervention_id: int,
    user_id: str = Depends(current_user_id),
) -> None:
    if not interventions_service.delete_intervention(user_id, intervention_id):
        raise HTTPException(status_code=404, detail="intervention not found")
