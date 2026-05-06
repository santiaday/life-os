"""Pydantic request/response models for the lifelog HTTP surface.

These shapes are the iOS app's contract — keep `Codable`-friendly:
- ISO 8601 datetimes with timezone (UTC or local with offset both fine)
- snake_case keys matching the Swift `JSONDecoder.KeyDecodingStrategy
  .convertFromSnakeCase`
- Optional fields use `null` (not omitted) so the Swift side decodes
  consistently.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# ---------- shared ----------


class EventLocation(BaseModel):
    latitude: float
    longitude: float
    name: str | None = None
    address: str | None = None


class EventContact(BaseModel):
    name: str
    identifier: str  # CNContact id; opaque on the server side


# ---------- requests ----------


class StartEventRequest(BaseModel):
    activity_type: str  # ActivityType.id (e.g. "watching_tv")
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


# ---------- responses ----------


class EventResponse(BaseModel):
    id: UUID  # source_event_id (the iOS-side identity)
    activity_type: str
    title: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None
    location: EventLocation | None
    contacts: list[EventContact]
    notes: str | None
    focus_mode: str | None = None


class ActivityType(BaseModel):
    id: str
    label: str
    emoji: str
    hue: int  # OKLCH hue degrees, 0–360 (matches design system)
    color: str  # hex (no #), fallback for clients that don't do OKLCH
    color_dark: str | None = None
    focus_mode: str | None = None  # default Focus mode hint
    default_capture_location: bool = False
    default_capture_contacts: bool = False
    live_activity_show_timer: bool = True
    sort_order: int = 0


class HealthResponse(BaseModel):
    open_session_count: int
    events_today: int
    last_event_at: datetime | None
