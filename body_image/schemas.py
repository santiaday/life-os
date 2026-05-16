"""Pydantic models for the /body-image HTTP surface."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


# ── photos / ratings ───────────────────────────────────────────────


class UploadResponse(BaseModel):
    photo_id: UUID
    session_id: UUID
    storage_path: str
    ratings_saved: int
    sources: list[str]
    failures: list[str]


class RatingRow(BaseModel):
    source: str            # 'claude_structure' | 'claude_surface' | 'gpt4v_*' | 'gemini_*' | 'geometry'
    run_index: int = 1
    overall: float | None  # specialist's holistic score for its subset, or None (geometry)
    dimensions: dict[str, Any]
    rated_at: datetime


class PhotoRow(BaseModel):
    id: UUID
    session_id: UUID | None
    storage_path: str
    caption: str | None
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    ratings: list[RatingRow]


class SessionRow(BaseModel):
    session_id: UUID
    started_at: datetime
    photo_count: int
    composite_overall: float | None
    photos: list[PhotoRow]


# ── trends ─────────────────────────────────────────────────────────


class TrendPoint(BaseModel):
    day: str  # ISO date
    overall_avg: float | None
    dimensions: dict[str, float | None]


class TrendResponse(BaseModel):
    points: list[TrendPoint]
    geometry: list[dict[str, Any]]
    feature_keys: list[str]
    structure_keys: list[str]
    surface_keys: list[str]


# ── interventions ──────────────────────────────────────────────────


InterventionEvent = Literal["start", "stop", "apply", "milestone"]


class InterventionCreate(BaseModel):
    intervention_key: str = Field(min_length=1, max_length=80)
    event: InterventionEvent
    occurred_on: date
    metadata: dict[str, Any] = Field(default_factory=dict)


class Intervention(BaseModel):
    id: int
    intervention_key: str
    event: InterventionEvent
    occurred_on: date
    metadata: dict[str, Any]
    created_at: datetime
