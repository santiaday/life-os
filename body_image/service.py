"""Body-image orchestration.

Fans out every configured LLM rater × specialist × run_index plus the
geometry sidecar in parallel. Saves one body_image_rating row per
(photo, source, run_index). Aggregates per-photo and per-day stats for
the dashboard.

Session bundling: the iOS Shortcut takes 3 photos (front, ¾ left, ¾
right) in <30 seconds. Each POST can carry a `session_id` field; if
omitted, we auto-bundle by reusing the most recent session_id from the
same user within SESSION_BUNDLE_WINDOW_MINUTES.
"""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from PIL import Image, ImageOps
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from lifeos_core.db import tx
from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

from . import storage
from .raters import (
    available_llm_raters,
    load_anchors,
    rate_geometry,
)
from .raters._rubric import (
    ALL_LLM_FEATURES,
    STRUCTURE_FEATURES,
    SURFACE_FEATURES,
)
from .schemas import (
    PhotoRow, RatingRow, SessionRow, TrendPoint, TrendResponse, UploadResponse,
)

log = get_logger(__name__)

# Resize cap before LLM calls (vision models down-sample aggressively
# anyway). Once-per-upload so every rater sees identical bytes.
MAX_LLM_DIMENSION = 1568

# Auto-bundle window. If a new photo arrives within this many minutes
# of the most recent one for this user AND no explicit session_id was
# sent, attach to that session.
SESSION_BUNDLE_WINDOW_MINUTES = 10


# ─── normalization ──────────────────────────────────────────────────


def _normalize_for_llm(raw: bytes) -> bytes:
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
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


# ─── fan-out ────────────────────────────────────────────────────────


def _build_job_list(
    jpeg_bytes: bytes, *, angle: str | None = None
) -> list[tuple[str, Any]]:
    """Build a list of (job_name, callable) for every rater × specialist
    × run_index. Geometry runs once (deterministic) AND only on frontal
    photos — the symmetry math assumes a frontal pose, so a ¾ angle
    would return symmetry_score=0 garbage that pollutes trend lines.

    `angle` comes from the iOS Shortcut's per-photo form field
    ('front' | 'three_quarter_left' | 'three_quarter_right'). When
    unset (legacy uploads predating session support), we assume frontal
    and run geometry — pre-multi-angle photos were always frontal."""
    anchor_pairs = load_anchors()
    use_specialist = settings.BODY_IMAGE_USE_SPECIALIST_CALLS
    runs = max(1, settings.BODY_IMAGE_RUNS_PER_RATER)

    jobs: list[tuple[str, Any]] = []
    for name, struct_fn, surf_fn in available_llm_raters():
        for run_index in range(1, runs + 1):
            # Closure capture: bind run_index now, not at call time.
            def _wrap(fn, idx=run_index, label=name):
                def _go():
                    # gpt4v accepts a seed kwarg for variance estimation;
                    # claude and gemini ignore it (no SDK-exposed seed).
                    if label == "gpt4v":
                        result = fn(jpeg_bytes, anchor_pairs, seed=idx)
                    else:
                        result = fn(jpeg_bytes, anchor_pairs)
                    result["run_index"] = idx
                    return result
                return _go
            if use_specialist:
                jobs.append((f"{name}_structure_run{run_index}", _wrap(struct_fn)))
                jobs.append((f"{name}_surface_run{run_index}", _wrap(surf_fn)))
            else:
                # When specialist calls are disabled, use the structure
                # function only (it returns the full holistic JSON in
                # that mode by convention). The rubric for structure is
                # still narrower than the original combined rubric, so
                # this is mostly a cost-saving fallback.
                jobs.append((f"{name}_run{run_index}", _wrap(struct_fn)))

    # Geometry — deterministic, single call, no run_index. Front-only:
    # the MediaPipe symmetry math is meaningless on ¾ poses.
    if angle is None or angle == "front":
        def _geom():
            r = rate_geometry(jpeg_bytes)
            r["run_index"] = 1
            return r
        jobs.append(("geometry", _geom))

    return jobs


def _run_raters_parallel(
    jpeg_bytes: bytes, *, angle: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    jobs = _build_job_list(jpeg_bytes, angle=angle)
    successes: list[dict[str, Any]] = []
    failures: list[str] = []
    if not jobs:
        return successes, failures
    with ThreadPoolExecutor(max_workers=min(len(jobs), 16)) as pool:
        futures = {pool.submit(fn): name for name, fn in jobs}
        for fut, name in futures.items():
            try:
                result = fut.result(timeout=120)
                if result is None:
                    failures.append(f"{name}: returned None")
                    continue
                successes.append(result)
            except Exception as e:  # noqa: BLE001
                log.warning("body_image.rater_failed", source=name, error=str(e))
                failures.append(f"{name}: {e}")
    return successes, failures


# ─── session helpers ────────────────────────────────────────────────


def _resolve_session_id(user_id: str, explicit_session_id: UUID | None) -> UUID:
    if explicit_session_id is not None:
        return explicit_session_id
    # Auto-bundle: reuse the most recent session if within the window.
    cutoff = datetime.now(UTC) - timedelta(minutes=SESSION_BUNDLE_WINDOW_MINUTES)
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT session_id
              FROM body_image_photo
             WHERE user_id = %s
               AND session_id IS NOT NULL
               AND created_at >= %s
             ORDER BY created_at DESC
             LIMIT 1
            """,
            [user_id, cutoff],
        )
        row = cur.fetchone()
    if row and row.get("session_id"):
        return row["session_id"]
    return uuid4()


# ─── main entry point ──────────────────────────────────────────────


def process_upload(
    user_id: str,
    *,
    photo_bytes: bytes,
    caption: str | None,
    device: str | None,
    session_id: UUID | None = None,
    angle: str | None = None,
) -> UploadResponse:
    normalized = _normalize_for_llm(photo_bytes)
    photo_id = uuid4()
    resolved_session = _resolve_session_id(user_id, session_id)
    day = datetime.now(UTC).date().isoformat()
    storage_path = f"raw/{day}/{photo_id}.jpg"

    # 1. Upload — must succeed.
    storage.upload(storage_path, normalized, content_type="image/jpeg")

    # 2. Photo row.
    metadata: dict[str, Any] = {}
    if device:
        metadata["device"] = device
    if angle:
        metadata["angle"] = angle  # 'front' | 'three_quarter_left' | 'three_quarter_right'
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO body_image_photo
              (id, user_id, storage_path, caption, metadata, session_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [
                str(photo_id), user_id, storage_path, caption,
                Jsonb(metadata), str(resolved_session),
            ],
        )

    # 3. Rater fan-out (angle gates the geometry rater — see _build_job_list).
    successes, failures = _run_raters_parallel(normalized, angle=angle)

    # 4. Insert ratings (idempotent on (photo, source, run_index)).
    if successes:
        with tx() as c, c.cursor() as cur:
            for r in successes:
                cur.execute(
                    """
                    INSERT INTO body_image_rating
                      (photo_id, source, overall, dimensions, run_index)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (photo_id, source, run_index) DO UPDATE
                      SET overall    = EXCLUDED.overall,
                          dimensions = EXCLUDED.dimensions,
                          rated_at   = now()
                    """,
                    [
                        str(photo_id),
                        r["source"],
                        r.get("overall"),
                        Jsonb(r.get("dimensions") or {}),
                        int(r.get("run_index", 1)),
                    ],
                )

    return UploadResponse(
        photo_id=photo_id,
        session_id=resolved_session,
        storage_path=storage_path,
        ratings_saved=len(successes),
        sources=sorted({r["source"] for r in successes}),
        failures=failures,
    )


# ─── reads ──────────────────────────────────────────────────────────


def _row_to_rating(r: dict) -> RatingRow:
    return RatingRow(
        source=r["source"],
        run_index=int(r.get("run_index") or 1),
        overall=float(r["overall"]) if r["overall"] is not None else None,
        dimensions=r["dimensions"] or {},
        rated_at=r["rated_at"],
    )


def _row_to_photo(p: dict, ratings: list[dict]) -> PhotoRow:
    return PhotoRow(
        id=p["id"],
        session_id=p.get("session_id"),
        storage_path=p["storage_path"],
        caption=p["caption"],
        created_at=p["created_at"],
        metadata=p.get("metadata") or {},
        ratings=[_row_to_rating(r) for r in ratings],
    )


def fetch_latest(user_id: str) -> PhotoRow | None:
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, session_id, storage_path, caption, created_at, metadata
              FROM body_image_photo
             WHERE user_id = %s
             ORDER BY created_at DESC LIMIT 1
            """,
            [user_id],
        )
        photo = cur.fetchone()
        if photo is None:
            return None
        cur.execute(
            """
            SELECT source, run_index, overall, dimensions, rated_at
              FROM body_image_rating
             WHERE photo_id = %s
             ORDER BY source, run_index
            """,
            [str(photo["id"])],
        )
        ratings = cur.fetchall()
    return _row_to_photo(photo, ratings)


def fetch_history(user_id: str, limit: int = 50) -> list[PhotoRow]:
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, session_id, storage_path, caption, created_at, metadata
              FROM body_image_photo
             WHERE user_id = %s
             ORDER BY created_at DESC
             LIMIT %s
            """,
            [user_id, max(1, min(limit, 200))],
        )
        photos = cur.fetchall()
        if not photos:
            return []
        ids = [str(p["id"]) for p in photos]
        cur.execute(
            """
            SELECT photo_id, source, run_index, overall, dimensions, rated_at
              FROM body_image_rating
             WHERE photo_id = ANY(%s::uuid[])
             ORDER BY source, run_index
            """,
            [ids],
        )
        by_photo: dict[str, list[dict]] = {}
        for r in cur.fetchall():
            by_photo.setdefault(str(r["photo_id"]), []).append(r)
    return [_row_to_photo(p, by_photo.get(str(p["id"]), [])) for p in photos]


def fetch_sessions(user_id: str, limit: int = 25) -> list[SessionRow]:
    """Group photos by session_id; for each session report all photos
    + composite overall. Used by the dashboard's "Recent sessions"
    panel so 3-photo Shortcut runs render as one row."""
    photos = fetch_history(user_id, limit=limit * 5)
    sessions: dict[UUID, list[PhotoRow]] = {}
    order: list[UUID] = []
    for p in photos:
        sid = p.session_id or p.id  # solo photos: each gets its own session
        if sid not in sessions:
            sessions[sid] = []
            order.append(sid)
        sessions[sid].append(p)
        if len(sessions) >= limit:
            break
    out: list[SessionRow] = []
    for sid in order[:limit]:
        plist = sessions[sid]
        all_overalls: list[float] = []
        for p in plist:
            for r in p.ratings:
                if r.overall is not None and r.source != "geometry":
                    all_overalls.append(r.overall)
        composite = sum(all_overalls) / len(all_overalls) if all_overalls else None
        out.append(SessionRow(
            session_id=sid,
            started_at=min(p.created_at for p in plist),
            photo_count=len(plist),
            composite_overall=composite,
            photos=plist,
        ))
    return out


# ── trends ─────────────────────────────────────────────────────────


DASHBOARD_FEATURES: list[str] = ALL_LLM_FEATURES


def fetch_trends(user_id: str, days: int = 90) -> TrendResponse:
    """Daily per-feature averages across every LLM rater, every
    specialist, every run. Geometry series is separate."""
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT date_trunc('day', r.rated_at)::date AS day,
                   r.source, r.dimensions, r.overall
              FROM body_image_rating r
              JOIN body_image_photo p ON p.id = r.photo_id
             WHERE p.user_id = %s
               AND r.source NOT IN ('geometry')
               AND r.rated_at > now() - (%s::int || ' days')::interval
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

    by_day_features: dict[str, dict[str, list[float]]] = {}
    by_day_overall: dict[str, list[float]] = {}
    for r in rows:
        day = r["day"].isoformat()
        dims = r["dimensions"] or {}
        bucket = by_day_features.setdefault(day, {})
        for key in DASHBOARD_FEATURES:
            v = dims.get(key)
            if isinstance(v, (int, float)):
                bucket.setdefault(key, []).append(float(v))
        if r["overall"] is not None:
            by_day_overall.setdefault(day, []).append(float(r["overall"]))

    points = [
        TrendPoint(
            day=day,
            overall_avg=(
                sum(by_day_overall[day]) / len(by_day_overall[day])
                if by_day_overall.get(day) else None
            ),
            dimensions={
                k: (sum(v) / len(v) if v else None)
                for k, v in by_day_features.get(day, {}).items()
            },
        )
        for day in sorted(by_day_features.keys())
    ]
    geometry = [
        {"day": r["day"].isoformat(), **(r["dimensions"] or {})}
        for r in geom_rows
    ]

    return TrendResponse(
        points=points,
        geometry=geometry,
        feature_keys=DASHBOARD_FEATURES,
        structure_keys=STRUCTURE_FEATURES,
        surface_keys=SURFACE_FEATURES,
    )


def get_photo(user_id: str, photo_id: UUID) -> PhotoRow | None:
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, session_id, storage_path, caption, created_at, metadata
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
            SELECT source, run_index, overall, dimensions, rated_at
              FROM body_image_rating
             WHERE photo_id = %s
             ORDER BY source, run_index
            """,
            [str(photo_id)],
        )
        ratings = cur.fetchall()
    return _row_to_photo(photo, ratings)
