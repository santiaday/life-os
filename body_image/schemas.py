"""Pydantic models for the /body-image HTTP surface."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class UploadResponse(BaseModel):
    photo_id: UUID
    storage_path: str
    ratings_saved: int
    sources: list[str]
    failures: list[str]


class RatingRow(BaseModel):
    source: str
    overall: float | None
    dimensions: dict[str, Any]
    rated_at: datetime


class PhotoRow(BaseModel):
    id: UUID
    storage_path: str
    caption: str | None
    created_at: datetime
    ratings: list[RatingRow]


class TrendPoint(BaseModel):
    """One day of per-feature scores, averaged across LLM raters."""
    day: str  # ISO date
    overall_avg: float | None
    dimensions: dict[str, float | None]


class TrendResponse(BaseModel):
    # 90-day series of per-feature averages across Claude + GPT-4o.
    points: list[TrendPoint]
    # Geometry series — raw measurements, one entry per photo.
    geometry: list[dict[str, Any]]
    # Per-feature keys present in `points[].dimensions`. Useful for the
    # dashboard to know which chart series to render.
    feature_keys: list[str]
