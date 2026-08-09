"""FastAPI router for /possessions.

Mounted by mcp_server.server.build_app. Shares the LIFELOG_API_TOKEN bearer
with the other personal surfaces; the HTML app takes the same token on the
query string and hands it to fetch().

Photo URLs are signed here, in bulk, and returned inline with each row. The
alternative — one redirect endpoint per image — turns a 300-item grid into
300 round trips before the first cutout appears, which is the whole product.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from lifelog_api.auth import require_token
from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

from . import packing, service, storage
from .schemas import (
    LocationBody,
    MoveBody,
    OutfitBody,
    PackingStateBody,
    PatchBody,
    ReviewBody,
    StatusBody,
    TripBody,
    WearBody,
)

log = get_logger(__name__)

router = APIRouter(prefix="/possessions", tags=["possessions"])

_APP_HTML = (Path(__file__).parent / "templates" / "app.html").read_text(
    encoding="utf-8"
)

# Long enough that a review sitting doesn't expire mid-queue, short enough
# that a leaked URL stops working the same day.
_SIGNED_URL_TTL = 8 * 3600


def _handle(fn, *args, **kwargs):
    """Translate a caller-fixable PossessionError into a 400 with its message.

    These messages carry the legal values ("not in the colour vocabulary;
    legal values: ..."), which is the only thing that makes the failure
    fixable without reading the source.
    """
    try:
        return fn(*args, **kwargs)
    except service.PossessionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _attach_photos(rows: list[dict]) -> list[dict]:
    """Sign every thumb/cutout referenced by these rows in one call."""
    paths: list[str] = []
    for row in rows:
        for key in ("photo_thumb", "photo_cutout", "photo_original"):
            if row.get(key):
                paths.append(row[key])
    signed = storage.signed_urls(paths, _SIGNED_URL_TTL) if paths else {}
    for row in rows:
        row["thumb_url"] = signed.get(row.get("photo_thumb") or "")
        row["cutout_url"] = signed.get(row.get("photo_cutout") or "")
        row["original_url"] = signed.get(row.get("photo_original") or "")
    return rows


# ------------------------------------------------------------------- app ----

@router.get("/app", response_class=HTMLResponse, include_in_schema=False)
def app_page(token: str = Query(default="")) -> HTMLResponse:
    expected = os.environ.get("LIFELOG_API_TOKEN") or ""
    if not expected or not hmac.compare_digest(token, expected):
        return HTMLResponse(
            "<h1>401</h1><p>Add ?token=&lt;LIFELOG_API_TOKEN&gt; to the URL.</p>",
            status_code=401,
        )
    return HTMLResponse(_APP_HTML.replace("__LIFELOG_TOKEN__", token))


# ----------------------------------------------------------------- items ----

@router.get("/api/items", dependencies=[Depends(require_token)])
def api_items(
    kind: str | None = None,
    status: str | None = "active",
    location: str | None = None,
    primary_color: str | None = None,
    unworn_since: int | None = None,
    reviewed: bool | None = None,
    search: str | None = None,
    limit: int = Query(default=300, ge=1, le=500),
) -> dict:
    rows = _handle(service.get_possessions, kind=kind, status=status,
                   location=location, primary_color=primary_color,
                   unworn_since=unworn_since, reviewed=reviewed,
                   search=search, limit=limit)
    return {"items": _attach_photos(rows), "count": len(rows)}


@router.get("/api/items/{possession_id}", dependencies=[Depends(require_token)])
def api_item(possession_id: UUID) -> dict:
    item = _handle(service.get_possession, possession_id)
    if item is None:
        raise HTTPException(status_code=404, detail="possession not found")
    return _attach_photos([item])[0]


@router.patch("/api/items/{possession_id}", dependencies=[Depends(require_token)])
def api_patch_item(possession_id: UUID, body: PatchBody) -> dict:
    return _handle(service.update_possession, possession_id, dict(body.patch))


@router.post("/api/items/{possession_id}/review", dependencies=[Depends(require_token)])
def api_review_item(possession_id: UUID, body: ReviewBody) -> dict:
    """Accept an item out of the review queue, applying any corrections in the
    same write so a half-saved review can't exist."""
    patch = dict(body.patch)
    patch["reviewed"] = body.reviewed
    return _handle(service.update_possession, possession_id, patch)


@router.get("/api/review-queue", dependencies=[Depends(require_token)])
def api_review_queue(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    rows = _handle(service.review_queue, limit=limit)
    return {"items": _attach_photos(rows), "count": len(rows)}


@router.post("/api/items/status", dependencies=[Depends(require_token)])
def api_set_status(body: StatusBody) -> dict:
    return _handle(service.set_possession_status, body.possession_ids, body.status)


@router.post("/api/items/move", dependencies=[Depends(require_token)])
def api_move(body: MoveBody) -> dict:
    return _handle(service.move_possession, body.possession_ids, body.location_name)


@router.post("/api/wear", dependencies=[Depends(require_token)])
def api_log_wear(body: WearBody) -> dict:
    return _handle(service.log_wear, body.possession_ids, body.worn_on,
                   body.context, body.city)


@router.get("/api/items/{possession_id}/photo", dependencies=[Depends(require_token)])
def api_photo(possession_id: UUID, variant: str = "cutout") -> RedirectResponse:
    """Single-photo redirect, for deep links and the detail view's full-size
    original. The grid uses the inline signed URLs instead."""
    item = _handle(service.get_possession, possession_id)
    if item is None:
        raise HTTPException(status_code=404, detail="possession not found")
    path = item.get(f"photo_{variant}")
    if not path:
        raise HTTPException(status_code=404, detail=f"no {variant} photo for this item")
    return RedirectResponse(url=storage.signed_url(path, _SIGNED_URL_TTL))


# ------------------------------------------------------------- locations ----

@router.get("/api/locations", dependencies=[Depends(require_token)])
def api_locations() -> dict:
    return {"locations": _handle(service.list_locations)}


@router.post("/api/locations", dependencies=[Depends(require_token)])
def api_create_location(body: LocationBody) -> dict:
    return _handle(service.create_location, body.name, body.kind,
                   body.parent, body.notes)


@router.get("/api/locations/{location_name}/contents",
            dependencies=[Depends(require_token)])
def api_location_contents(location_name: str, include_nested: bool = True) -> dict:
    result = _handle(service.get_storage_contents, location_name, include_nested)
    result["items"] = _attach_photos(result["items"])
    return result


# ----------------------------------------------------------------- purge ----

@router.get("/api/purge", dependencies=[Depends(require_token)])
def api_purge(min_days_unworn: int = Query(default=180, ge=0),
              limit: int = Query(default=200, ge=1, le=500)) -> dict:
    rows = _handle(service.get_purge_candidates, min_days_unworn, limit)
    return {"items": _attach_photos(rows), "count": len(rows)}


# ----------------------------------------------------------------- trips ----

@router.get("/api/trips", dependencies=[Depends(require_token)])
def api_trips(upcoming_only: bool = False) -> dict:
    return {"trips": _handle(service.list_trips, upcoming_only)}


@router.post("/api/trips", dependencies=[Depends(require_token)])
def api_create_trip(body: TripBody) -> dict:
    return _handle(service.create_trip, body.name, body.start_date,
                   body.end_date, body.destinations, body.notes)


@router.get("/api/trips/{trip_id}/packing", dependencies=[Depends(require_token)])
def api_packing(trip_id: UUID, persist: bool = False,
                weather: list[str] = Query(default=[]),
                low_c: float | None = None, high_c: float | None = None,
                formality: int | None = None) -> dict:
    profile: dict = {}
    if weather:
        profile["weather_tags"] = weather
    if low_c is not None:
        profile["low_c"] = low_c
    if high_c is not None:
        profile["high_c"] = high_c
    if formality is not None:
        profile["formality"] = [1, formality]

    result = _handle(packing.build_packing_list, trip_id,
                     profile or None, persist=persist)
    result["items"] = _attach_photos(result["items"])
    by_id = {str(i["id"]): i for i in result["items"]}
    result["by_kind"] = {
        kind: [by_id.get(str(i["id"]), i) for i in items]
        for kind, items in result["by_kind"].items()
    }
    return result


@router.post("/api/trips/{trip_id}/packing/{possession_id}",
             dependencies=[Depends(require_token)])
def api_set_packing_state(trip_id: UUID, possession_id: UUID,
                          body: PackingStateBody) -> dict:
    return _handle(service.set_packing_state, trip_id, possession_id,
                   body.state, body.qty)


@router.get("/api/gaps", dependencies=[Depends(require_token)])
def api_gaps(weather: list[str] = Query(default=[]),
             formality: int | None = None,
             nights: int = Query(default=5, ge=1, le=90)) -> dict:
    return _handle(packing.find_wardrobe_gaps, weather, formality, nights)


# ---------------------------------------------------------------- outfits ----

@router.get("/api/outfits", dependencies=[Depends(require_token)])
def api_outfits(formality: int | None = None) -> dict:
    return {"outfits": _handle(service.get_outfits, formality, None)}


@router.post("/api/outfits", dependencies=[Depends(require_token)])
def api_create_outfit(body: OutfitBody) -> dict:
    return _handle(service.create_outfit, body.name, body.possession_ids,
                   body.formality, body.weather_tags, body.notes)


# ------------------------------------------------------------------ meta ----

@router.get("/api/vocab", dependencies=[Depends(require_token)])
def api_vocab() -> dict:
    from . import vocab

    return {
        "kinds": list(vocab.KINDS),
        "statuses": list(vocab.STATUSES),
        "weather_tags": list(vocab.WEATHER_TAGS),
        "patterns": list(vocab.PATTERNS),
        "wear_contexts": list(vocab.WEAR_CONTEXTS),
        "packing_states": list(vocab.PACKING_STATES),
        "colors": vocab.color_rows(),
        "subcategories": vocab.subcategories(),
        "review_threshold": settings.POSSESSION_REVIEW_CONFIDENCE_THRESHOLD,
    }


@router.get("/api/stats", dependencies=[Depends(require_token)])
def api_stats() -> dict:
    return _handle(service.stats)
