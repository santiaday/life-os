"""FastAPI router for the lifelog HTTP surface.

Mounted by mcp_server.server.build_app at the /lifelog prefix. The MCP
path-secret middleware exempts this prefix; per-route auth is bearer-
token via require_token. Per-user scoping via current_user_id (today:
single hardcoded user; future: JWT subject extractor).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from . import activity_types as types_service
from . import service
from .auth import current_user_id, require_token
from .schemas import (
    ActivityType,
    AnnotateEventRequest,
    CreateActivityTypeRequest,
    EndEventRequest,
    EventResponse,
    HealthResponse,
    StartEventRequest,
    UpdateActivityTypeRequest,
    UpdateEventRequest,
)


router = APIRouter(
    prefix="/lifelog",
    tags=["lifelog"],
    dependencies=[Depends(require_token)],
)


# ─── /events ────────────────────────────────────────────────────────────────


@router.post("/events/start", response_model=EventResponse)
def start_event(
    req: StartEventRequest,
    user_id: str = Depends(current_user_id),
) -> EventResponse:
    return service.start_event(user_id, req)


@router.post("/events/end", response_model=EventResponse)
def end_event(
    req: EndEventRequest,
    user_id: str = Depends(current_user_id),
) -> EventResponse:
    result = service.close_event(user_id, req)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="event not found",
        )
    return result


@router.post("/events/annotate", response_model=EventResponse)
def annotate_event(
    req: AnnotateEventRequest,
    user_id: str = Depends(current_user_id),
) -> EventResponse:
    return service.annotate(user_id, req)


@router.get("/events/active", response_model=EventResponse | None)
def active_event(user_id: str = Depends(current_user_id)) -> EventResponse | None:
    return service.fetch_active(user_id)


@router.get("/events/recent", response_model=list[EventResponse])
def recent_events(
    limit: int = Query(default=50, ge=1, le=200),
    before: datetime | None = None,
    kind: str | None = Query(default=None, pattern="^(session|annotation)$"),
    user_id: str = Depends(current_user_id),
) -> list[EventResponse]:
    return service.fetch_recent(user_id, limit=limit, before=before, kind=kind)


@router.get("/events/health", response_model=HealthResponse)
def events_health(user_id: str = Depends(current_user_id)) -> HealthResponse:
    return service.health(user_id)


# /events/{id} must come AFTER the literal segments above so FastAPI's
# router doesn't shadow them with this catch-all.
@router.get("/events/{event_id}", response_model=EventResponse)
def event_detail(
    event_id: UUID,
    user_id: str = Depends(current_user_id),
) -> EventResponse:
    result = service.fetch_by_id(user_id, event_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="event not found",
        )
    return result


@router.patch("/events/{event_id}", response_model=EventResponse)
def update_event(
    event_id: UUID,
    req: UpdateEventRequest,
    user_id: str = Depends(current_user_id),
) -> EventResponse:
    result = service.update_event(user_id, event_id, req)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="event not found",
        )
    return result


@router.delete("/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_event(
    event_id: UUID,
    user_id: str = Depends(current_user_id),
) -> None:
    if not service.delete_event(user_id, event_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="event not found",
        )


# ─── /activity-types — CRUD ─────────────────────────────────────────────────


@router.get("/activity-types", response_model=list[ActivityType])
def get_activity_types(
    user_id: str = Depends(current_user_id),
) -> list[ActivityType]:
    return types_service.list_activity_types(user_id)


@router.post(
    "/activity-types",
    response_model=ActivityType,
    status_code=status.HTTP_201_CREATED,
)
def create_activity_type(
    req: CreateActivityTypeRequest,
    user_id: str = Depends(current_user_id),
) -> ActivityType:
    try:
        return types_service.create_activity_type(user_id, req)
    except types_service.SlugTakenError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An activity called '{e}' already exists.",
        ) from e


@router.patch("/activity-types/{slug}", response_model=ActivityType)
def update_activity_type(
    slug: str,
    req: UpdateActivityTypeRequest,
    user_id: str = Depends(current_user_id),
) -> ActivityType:
    try:
        return types_service.update_activity_type(user_id, slug, req)
    except types_service.NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="activity-type not found",
        ) from e


@router.delete(
    "/activity-types/{slug}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_activity_type(
    slug: str,
    user_id: str = Depends(current_user_id),
) -> None:
    try:
        types_service.delete_activity_type(user_id, slug)
    except types_service.NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="activity-type not found",
        ) from e


@router.post("/activity-types/reload", response_model=dict)
def reload_types(user_id: str = Depends(current_user_id)) -> dict:
    """Compatibility shim from when the catalog lived in JSON. With the
    DB-backed catalog every read is fresh; this just returns the count
    so the iOS Settings "Refresh from server" UX has something to show."""
    types_service.reload_activity_types()
    return {"ok": True, "count": len(types_service.list_activity_types(user_id))}
