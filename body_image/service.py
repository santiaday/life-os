"""Body-image orchestration: photo save + parallel rater fan-out + DB write.

The whole point of this module is `process_upload`. Everything else is
helpers for the dashboard.

Failure isolation:
  - Storage upload must succeed (otherwise we have no photo to rate).
  - Photo row insert must succeed.
  - Rater calls are independent. Each is run on a thread (so the three
    blocking HTTP/SDK calls happen concurrently); failures are captured
    individually and the route still returns a 200 with the success list.
"""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from PIL import Image, ImageOps
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from lifeos_core.db import tx
from lifeos_core.logging import get_logger

from . import storage
from .raters import rate_claude, rate_geometry
from .schemas import PhotoRow, RatingRow, TrendPoint, TrendResponse, UploadResponse

log = get_logger(__name__)

# Resize cap before sending to LLM raters. Cuts API cost and per-call
# latency; LLM vision models down-sample aggressively anyway. Keep wide
# enough that fine features (skin texture, hairline) remain legible.
MAX_LLM_DIMENSION = 1568


def _normalize_for_llm(raw: bytes) -> bytes:
    """Apply EXIF rotation and downscale to <= MAX_LLM_DIMENSION on the
    long edge. Re-encode as JPEG quality 90.

    Done once per upload (not per rater) so all three LLM calls see the
    exact same bytes — keeps ratings comparable."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)  # honor iPhone orientation tags
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    longest = max(w, h)
    if longest > MAX_LLM_DIMENSION:
        ratio = MAX_LLM_DIMENSION / longest
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, optimize=True)
    return buf.getvalue()


def _run_raters_parallel(jpeg_bytes: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """Run raters concurrently on a thread pool.

    Why threads (not asyncio): the Anthropic SDK is sync and geometry
    is a blocking httpx.post. Threads cost nothing here (all I/O-bound)
    and keep the rater code flat — no async client setup, no event-loop
    juggling.
    """
    raters = [
        ("claude", lambda: rate_claude(jpeg_bytes)),
        ("geometry", lambda: rate_geometry(jpeg_bytes)),
        # gpt4v intentionally omitted — re-enable here when OPENAI_API_KEY
        # is set. Rater module stays imported in raters/__init__.py.
    ]
    successes: list[dict[str, Any]] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=len(raters)) as pool:
        futures = {pool.submit(fn): name for name, fn in raters}
        for fut, name in futures.items():
            try:
                result = fut.result(timeout=60)
                if result is None:
                    failures.append(f"{name}: returned None")
                    continue
                successes.append(result)
            except Exception as e:  # noqa: BLE001
                log.warning("body_image.rater_failed", source=name, error=str(e))
                failures.append(f"{name}: {e}")
    return successes, failures


def process_upload(
    user_id: str,
    *,
    photo_bytes: bytes,
    caption: str | None,
    device: str | None,
) -> UploadResponse:
    """End-to-end: normalize → upload to Storage → insert photo →
    fan-out raters → insert ratings.

    Storage failure short-circuits (no photo, no point in rating).
    Rater failures are collected and returned to the caller.
    """
    normalized = _normalize_for_llm(photo_bytes)
    photo_id = uuid4()
    day = datetime.now(UTC).date().isoformat()
    storage_path = f"raw/{day}/{photo_id}.jpg"

    # 1. Upload — must succeed or we abort.
    storage.upload(storage_path, normalized, content_type="image/jpeg")

    # 2. Photo row.
    metadata: dict[str, Any] = {}
    if device:
        metadata["device"] = device
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO body_image_photo (id, user_id, storage_path, caption, metadata)
            VALUES (%s, %s, %s, %s, %s)
            """,
            [str(photo_id), user_id, storage_path, caption, Jsonb(metadata)],
        )

    # 3. Rater fan-out.
    successes, failures = _run_raters_parallel(normalized)

    # 4. Insert ratings.
    if successes:
        with tx() as c, c.cursor() as cur:
            for r in successes:
                overall = r.get("overall")
                cur.execute(
                    """
                    INSERT INTO body_image_rating
                      (photo_id, source, overall, dimensions)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (photo_id, source) DO UPDATE
                      SET overall    = EXCLUDED.overall,
                          dimensions = EXCLUDED.dimensions,
                          rated_at   = now()
                    """,
                    [
                        str(photo_id),
                        r["source"],
                        overall,
                        Jsonb(r.get("dimensions") or {}),
                    ],
                )

    return UploadResponse(
        photo_id=photo_id,
        storage_path=storage_path,
        ratings_saved=len(successes),
        sources=[r["source"] for r in successes],
        failures=failures,
    )


# ─── reads (for the dashboard) ────────────────────────────────────────────


def fetch_latest(user_id: str) -> PhotoRow | None:
    """Most recent photo + all its ratings."""
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, storage_path, caption, created_at
              FROM body_image_photo
             WHERE user_id = %s
             ORDER BY created_at DESC
             LIMIT 1
            """,
            [user_id],
        )
        photo = cur.fetchone()
        if photo is None:
            return None
        cur.execute(
            """
            SELECT source, overall, dimensions, rated_at
              FROM body_image_rating
             WHERE photo_id = %s
             ORDER BY source
            """,
            [str(photo["id"])],
        )
        ratings = cur.fetchall()
    return PhotoRow(
        id=photo["id"],
        storage_path=photo["storage_path"],
        caption=photo["caption"],
        created_at=photo["created_at"],
        ratings=[
            RatingRow(
                source=r["source"],
                overall=float(r["overall"]) if r["overall"] is not None else None,
                dimensions=r["dimensions"] or {},
                rated_at=r["rated_at"],
            )
            for r in ratings
        ],
    )


# Feature keys the dashboard charts. Kept in code (not derived from
# the JSONB) so the chart legend is stable even when a rater omits a
# field. Must match the rubric in raters/_rubric.py.
DASHBOARD_FEATURES: list[str] = [
    "facial_harmony",
    "facial_symmetry",
    "jawline_definition",
    "chin_projection",
    "skin_quality",
    "skin_clarity",
    "under_eye_quality",
    "eye_quality",
    "nose_harmony",
    "lip_quality",
    "hair_quality",
    "hairline_quality",
    "grooming_overall",
    "expression_appeal",
]


def fetch_trends(user_id: str, days: int = 90) -> TrendResponse:
    """Day-grain rollup of per-feature scores across LLM raters, plus the
    raw geometry series. ~150 rows max even at daily cadence."""
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        # LLM trends — average per-feature score across claude+gpt4v,
        # then per day.
        cur.execute(
            """
            WITH llm AS (
              SELECT date_trunc('day', r.rated_at)::date AS day,
                     r.dimensions,
                     r.overall
                FROM body_image_rating r
                JOIN body_image_photo p ON p.id = r.photo_id
               WHERE p.user_id = %s
                 AND r.source IN ('claude', 'gpt4v')
                 AND r.rated_at > now() - (%s::int || ' days')::interval
            )
            SELECT day,
                   AVG(overall) FILTER (WHERE overall IS NOT NULL) AS overall_avg,
                   dimensions
              FROM llm
             ORDER BY day
            """,
            [user_id, days],
        )
        rows = cur.fetchall()

        cur.execute(
            """
            SELECT date_trunc('day', r.rated_at)::date AS day,
                   r.dimensions
              FROM body_image_rating r
              JOIN body_image_photo p ON p.id = r.photo_id
             WHERE p.user_id = %s
               AND r.source = 'geometry'
               AND r.rated_at > now() - (%s::int || ' days')::interval
             ORDER BY day
            """,
            [user_id, days],
        )
        geom_rows = cur.fetchall()

    # Aggregate per-feature averages per day. Done in Python because the
    # SQL above only groups by day on the outer SELECT — features live
    # inside the JSONB column, so we pivot here.
    by_day: dict[str, dict[str, list[float]]] = {}
    by_day_overall: dict[str, list[float]] = {}
    for r in rows:
        day = r["day"].isoformat()
        dims = r["dimensions"] or {}
        bucket = by_day.setdefault(day, {})
        for key in DASHBOARD_FEATURES:
            v = dims.get(key)
            if isinstance(v, (int, float)):
                bucket.setdefault(key, []).append(float(v))
        if r["overall_avg"] is not None:
            by_day_overall.setdefault(day, []).append(float(r["overall_avg"]))

    points = [
        TrendPoint(
            day=day,
            overall_avg=(
                sum(by_day_overall[day]) / len(by_day_overall[day])
                if by_day_overall.get(day)
                else None
            ),
            dimensions={
                k: (sum(v) / len(v) if v else None)
                for k, v in by_day.get(day, {}).items()
            },
        )
        for day in sorted(by_day.keys())
    ]

    geometry = [
        {"day": r["day"].isoformat(), **(r["dimensions"] or {})}
        for r in geom_rows
    ]

    return TrendResponse(
        points=points,
        geometry=geometry,
        feature_keys=DASHBOARD_FEATURES,
    )


def get_photo(user_id: str, photo_id: UUID) -> PhotoRow | None:
    """Lookup by photo_id. Scoped to user_id so a bearer can't read
    another user's photos once we go multi-tenant."""
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, storage_path, caption, created_at
              FROM body_image_photo
             WHERE user_id = %s AND id = %s
            """,
            [user_id, str(photo_id)],
        )
        photo = cur.fetchone()
        if photo is None:
            return None
        cur.execute(
            """
            SELECT source, overall, dimensions, rated_at
              FROM body_image_rating
             WHERE photo_id = %s
             ORDER BY source
            """,
            [str(photo_id)],
        )
        ratings = cur.fetchall()
    return PhotoRow(
        id=photo["id"],
        storage_path=photo["storage_path"],
        caption=photo["caption"],
        created_at=photo["created_at"],
        ratings=[
            RatingRow(
                source=r["source"],
                overall=float(r["overall"]) if r["overall"] is not None else None,
                dimensions=r["dimensions"] or {},
                rated_at=r["rated_at"],
            )
            for r in ratings
        ],
    )
