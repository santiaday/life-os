"""Pydantic request/response models for the lifelog HTTP surface.

These shapes are the iOS app's contract — keep `Codable`-friendly:
- ISO 8601 datetimes with timezone (UTC or local with offset both fine)
- snake_case keys matching the Swift `JSONDecoder.KeyDecodingStrategy
  .convertFromSnakeCase` (note: iOS app uses explicit CodingKeys, no
  strategy — see ActivityType.swift for the footgun explanation)
- Optional fields use `null` (not omitted) so the Swift side decodes
  consistently.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# Two kinds of activity types, mirrored on events as event_kind.
ActivityKind = Literal["session", "annotation"]


# ---------- shared ----------


class EventLocation(BaseModel):
    latitude: float
    longitude: float
    name: str | None = None
    address: str | None = None


class EventContact(BaseModel):
    name: str
    identifier: str  # CNContact id; opaque on the server side


# ---------- requests: events ----------


class StartEventRequest(BaseModel):
    activity_type: str  # ActivityType.slug (e.g. "watching_tv")
    label: str | None = None  # override; falls back to ActivityType.label
    started_at: datetime | None = None  # default: server now()
    location: EventLocation | None = None
    contacts: list[EventContact] = Field(default_factory=list)
    notes: str | None = None
    focus_mode: str | None = None  # informational; iOS handles the toggle
    device: str | None = None  # iPhone name; useful for debugging


class EndEventRequest(BaseModel):
    event_id: UUID  # source_event_id (ios_manual UUID, NOT the BIGSERIAL id)
    ended_at: datetime | None = None  # default: server now()
    notes: str | None = None


class AnnotateEventRequest(BaseModel):
    """Log a single-moment event (annotation). Server stores with
    `ended_at = started_at` and `event_kind = 'annotation'`."""
    activity_type: str
    occurred_at: datetime | None = None  # default: server now()
    location: EventLocation | None = None
    contacts: list[EventContact] = Field(default_factory=list)
    notes: str | None = None
    device: str | None = None


class UpdateEventRequest(BaseModel):
    """All-optional patch payload for an existing event. nil = unchanged.

    Used by the iOS Event Detail "edit" sheet to fix start/end times
    after the fact (e.g. you forgot to end a Reading session before
    sleep). For annotations, only `occurred_at` and `notes` are
    meaningful — sending `ended_at` on an annotation is silently
    ignored server-side (it's pinned to == started_at)."""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    occurred_at: datetime | None = None
    notes: str | None = None
    location: EventLocation | None = None
    contacts: list[EventContact] | None = None


# ---------- requests: activity-type CRUD ----------


class CreateActivityTypeRequest(BaseModel):
    """User-created activity. Server assigns a slug from the label (lower
    snake case) and rejects collisions in the user's namespace.
    is_custom = True is forced server-side."""
    label: str = Field(min_length=1, max_length=80)
    emoji: str = Field(min_length=1, max_length=8)
    hue: int = Field(ge=0, le=359)
    kind: ActivityKind = "session"
    focus_mode: str | None = None
    default_capture_location: bool = False
    default_capture_contacts: bool = False
    live_activity_show_timer: bool = True
    is_pinned: bool = True
    sort_order: int = 100

    @field_validator("emoji")
    @classmethod
    def _strip_emoji(cls, v: str) -> str:
        # Strip whitespace; the iOS picker sometimes returns trailing
        # variation selectors that wouldn't hurt but make string compares
        # awkward downstream.
        return v.strip()


class UpdateActivityTypeRequest(BaseModel):
    """All-optional patch payload. Anything left as None is preserved.
    The server protects `is_custom = False` rows from destructive edits
    (label/kind/emoji can still be tweaked, but the slug is permanent and
    delete is rejected at the route layer)."""
    label: str | None = None
    emoji: str | None = None
    hue: int | None = Field(default=None, ge=0, le=359)
    kind: ActivityKind | None = None
    focus_mode: str | None = None
    default_capture_location: bool | None = None
    default_capture_contacts: bool | None = None
    live_activity_show_timer: bool | None = None
    is_pinned: bool | None = None
    sort_order: int | None = None


# ---------- responses ----------


class EventResponse(BaseModel):
    id: UUID  # source_event_id (the iOS-side identity)
    activity_type: str
    title: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None
    event_kind: ActivityKind
    location: EventLocation | None
    contacts: list[EventContact]
    notes: str | None
    focus_mode: str | None = None


class ActivityType(BaseModel):
    # `id` here is the slug (string) — iOS treats slug as the identity.
    # The DB-side BIGSERIAL id never crosses the wire.
    id: str
    label: str
    emoji: str
    hue: int
    color: str  # hex (no #), fallback for clients that don't do OKLCH
    color_dark: str | None = None
    kind: ActivityKind = "session"
    focus_mode: str | None = None
    default_capture_location: bool = False
    default_capture_contacts: bool = False
    live_activity_show_timer: bool = True
    is_pinned: bool = True
    is_custom: bool = False
    sort_order: int = 0


class HealthResponse(BaseModel):
    open_session_count: int
    events_today: int
    last_event_at: datetime | None
