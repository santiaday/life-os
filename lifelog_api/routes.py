"""FastAPI router for the lifelog HTTP surface.

Mounted by mcp_server.server.build_app at the /lifelog prefix. The MCP path-
secret middleware exempts this prefix; per-route auth is bearer-token via
require_token.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from . import service
from .activity_types import list_activity_types, reload_activity_types
from .auth import require_token
from .schemas import (
    ActivityType,
    EndEventRequest,
    EventResponse,
    HealthResponse,
    StartEventRequest,
)


router = APIRouter(
    prefix="/lifelog",
    tags=["lifelog"],
    dependencies=[Depends(require_token)],
)


# ─── /events ────────────────────────────────────────────────────────────────


@router.post("/events/start", response_model=EventResponse)
def start_event(req: StartEventRequest) -> EventResponse:
    return service.start_event(req)


@router.post("/events/end", response_model=EventResponse)
def end_event(req: EndEventRequest) -> EventResponse:
    result = service.close_event(req)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="event not found",
        )
    return result


@router.get("/events/active", response_model=EventResponse | None)
def active_event() -> EventResponse | None:
    return service.fetch_active()


@router.get("/events/recent", response_model=list[EventResponse])
def recent_events(
    limit: int = Query(default=50, ge=1, le=200),
    before: datetime | None = None,
) -> list[EventResponse]:
    return service.fetch_recent(limit=limit, before=before)


@router.get("/events/health", response_model=HealthResponse)
def events_health() -> HealthResponse:
    return service.health()


# /events/{id} must come AFTER the literal segments above so FastAPI's
# router doesn't shadow them with this catch-all.
@router.get("/events/{event_id}", response_model=EventResponse)
def event_detail(event_id: UUID) -> EventResponse:
    result = service.fetch_by_id(event_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="event not found",
        )
    return result


# ─── /activity-types ────────────────────────────────────────────────────────


@router.get("/activity-types", response_model=list[ActivityType])
def get_activity_types() -> list[ActivityType]:
    return list_activity_types()


@router.post("/activity-types/reload", response_model=dict)
def reload_types() -> dict:
    """Drop the in-process cache and re-read activity_types.json. Returns
    the new count. Wired to a button in the iOS Settings screen."""
    return {"ok": True, "count": reload_activity_types()}
