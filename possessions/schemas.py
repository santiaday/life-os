"""Request bodies for the possession HTTP surface.

Response shapes are plain dicts straight out of service.py — pinning them in
Pydantic would mean maintaining a second copy of a schema that already exists
in Postgres, and the UI is the only consumer. Request bodies are modelled
because they cross a trust boundary and benefit from being rejected early.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class PatchBody(BaseModel):
    """Partial edit. Unknown fields are rejected by service._coerce_writes
    with the writable list, so a typo'd column name is a clear 400 rather
    than a silently dropped edit."""
    patch: dict[str, Any]


class ReviewBody(BaseModel):
    """The review queue's Enter key: apply any corrections and confirm."""
    patch: dict[str, Any] = Field(default_factory=dict)
    reviewed: bool = True


class StatusBody(BaseModel):
    possession_ids: list[str]
    status: str


class MoveBody(BaseModel):
    possession_ids: list[str]
    location_name: str


class WearBody(BaseModel):
    possession_ids: list[str]
    worn_on: date | None = None
    context: str | None = None
    city: str | None = None


class LocationBody(BaseModel):
    name: str
    kind: str
    parent: str | None = None
    notes: str | None = None


class TripBody(BaseModel):
    name: str
    start_date: date
    end_date: date
    destinations: list[dict[str, Any]] = Field(default_factory=list)
    notes: str | None = None


class PackingStateBody(BaseModel):
    state: str
    qty: int = 1


class OutfitBody(BaseModel):
    name: str
    possession_ids: list[str]
    formality: int | None = None
    weather_tags: list[str] = Field(default_factory=list)
    notes: str | None = None
