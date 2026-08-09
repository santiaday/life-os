"""Write-side MCP tools for the possession inventory.

Every function here mutates through possessions.service, which records the
statement and params in `mcp_write_audit` before returning — so `get_write_audit`
shows a wear logged from chat exactly as it shows a generic db_update.

Validation happens before SQL, on purpose. The database has the constraints
that matter (colour FK, subcategory composite FK, wear natural key), but a
constraint violation surfaces as a Postgres error naming an index, which tells
the caller nothing about what it should have sent. The service layer fails
first with the legal values in the message.
"""

from __future__ import annotations

from typing import Any

from lifeos_core.logging import get_logger
from mcp_server.tools import _err, _ok, _serialize
from possessions import service

log = get_logger(__name__)


def _guard(tool: str, fn, *args, **kwargs) -> dict:
    try:
        return fn(*args, **kwargs)
    except service.PossessionError as e:
        return _err(tool, e)
    except Exception as e:
        log.exception("possessions.write_tool_failed", tool=tool)
        return _err(tool, e)


def log_wear(possession_ids: list[str], worn_on: str | None = None,
             context: str | None = None, city: str | None = None) -> dict:
    """Record that these items were worn. Idempotent per (item, day, context)."""
    def run() -> dict:
        result = service.log_wear(possession_ids, worn_on, context, city)
        warnings = []
        if result["already_logged"]:
            warnings.append(
                f"{result['already_logged']} of {len(possession_ids)} were already "
                f"logged for that day and context; nothing was duplicated."
            )
        return _ok("log_wear", [result], warnings=warnings)
    return _guard("log_wear", run)


def log_possession(name: str, kind: str, primary_color: str,
                   subcategory: str | None = None, brand: str | None = None,
                   colorway: str | None = None, secondary_color: str | None = None,
                   pattern: str | None = None, material: str | None = None,
                   size: str | None = None, formality: int | None = None,
                   warmth: int | None = None, weather_tags: list[str] | None = None,
                   packed_volume_l: float | None = None, weight_g: float | None = None,
                   acquired_on: str | None = None, cost_cents: int | None = None,
                   retailer: str | None = None, condition: int | None = None,
                   status: str = "active", location: str | None = None,
                   notes: str | None = None, reviewed: bool = True) -> dict:
    """Add an item by hand — something photographed later, or never at all.

    `reviewed` defaults to True here: a human typed these values, so there is
    nothing for the review queue to confirm. The ingest pipeline is the path
    that writes reviewed=False.
    """
    def run() -> dict:
        fields: dict[str, Any] = {
            "name": name, "kind": kind, "primary_color": primary_color,
            "subcategory": subcategory, "brand": brand, "colorway": colorway,
            "secondary_color": secondary_color, "pattern": pattern,
            "material": material, "size": size, "formality": formality,
            "warmth": warmth, "weather_tags": weather_tags,
            "packed_volume_l": packed_volume_l, "weight_g": weight_g,
            "acquired_on": acquired_on, "cost_cents": cost_cents,
            "retailer": retailer, "condition": condition, "status": status,
            "notes": notes, "reviewed": reviewed,
        }
        if location:
            fields["location"] = location
        row = service.log_possession(**{k: v for k, v in fields.items() if v is not None})
        return _ok("log_possession", _serialize([row]))
    return _guard("log_possession", run)


def update_possession(possession_id: str, patch: dict) -> dict:
    """Edit one item. `patch` is a field map; `location` may be a name."""
    return _guard(
        "update_possession",
        lambda: _ok("update_possession",
                    _serialize([service.update_possession(possession_id, dict(patch))])),
    )


def set_possession_status(possession_ids: list[str], status: str) -> dict:
    """Mark items purge_candidate / listed / donated / sold / retired / active."""
    def run() -> dict:
        result = service.set_possession_status(possession_ids, status)
        return _ok("set_possession_status", _serialize(result["items"]),
                   extra={"updated": result["updated"], "status": result["status"]})
    return _guard("set_possession_status", run)


def move_possession(possession_ids: list[str], location_name: str) -> dict:
    """Move items into a storage container, addressed by name."""
    def run() -> dict:
        result = service.move_possession(possession_ids, location_name)
        return _ok("move_possession", _serialize(result["items"]),
                   extra={"moved": result["moved"], "location": result["location"]})
    return _guard("move_possession", run)


def create_storage_location(name: str, kind: str, parent: str | None = None,
                            notes: str | None = None) -> dict:
    """Create a container. `parent` nests it: 'Under bed' -> 'Bin A'."""
    return _guard(
        "create_storage_location",
        lambda: _ok("create_storage_location",
                    _serialize([service.create_location(name, kind, parent, notes)])),
    )


def create_outfit(name: str, possession_ids: list[str],
                  formality: int | None = None,
                  weather_tags: list[str] | None = None,
                  notes: str | None = None) -> dict:
    """Save a set of items as a named outfit. Re-running with the same name
    replaces its contents."""
    return _guard(
        "create_outfit",
        lambda: _ok("create_outfit", [service.create_outfit(
            name, possession_ids, formality, weather_tags, notes)]),
    )


def create_trip(name: str, start_date: str, end_date: str,
                destinations: list[dict] | None = None,
                notes: str | None = None) -> dict:
    """Create a trip. Each destination may carry city, from, to, weather_tags,
    low_c, high_c, formality [min,max] and international — build_packing_list
    unions them all into one requirement set."""
    return _guard(
        "create_trip",
        lambda: _ok("create_trip", _serialize([service.create_trip(
            name, start_date, end_date, destinations, notes)])),
    )


def set_packing_state(trip_id: str, possession_id: str, state: str,
                      qty: int = 1) -> dict:
    """Mark one item candidate / packed / worn / unused for a trip."""
    return _guard(
        "set_packing_state",
        lambda: _ok("set_packing_state", _serialize([service.set_packing_state(
            trip_id, possession_id, state, qty)])),
    )


def add_possession_vocab(color: str | None = None, color_family: str | None = None,
                         kind: str | None = None, subcategory: str | None = None,
                         notes: str | None = None) -> dict:
    """Extend the controlled vocabulary.

    The right move when the tagger keeps reaching for a value that isn't
    there. Pass color + color_family to add a colour, or kind + subcategory to
    add a subcategory.
    """
    def run() -> dict:
        added = []
        if color:
            if not color_family:
                raise service.PossessionError(
                    "color_family is required when adding a colour "
                    "(neutral | blue | green | red | brown | orange | yellow | "
                    "purple | metal | other)")
            added.append(service.add_color(color, color_family))
        if subcategory:
            if not kind:
                raise service.PossessionError(
                    "kind is required when adding a subcategory")
            added.append(service.add_subcategory(kind, subcategory, notes))
        if not added:
            raise service.PossessionError(
                "nothing to add — pass color + color_family, or kind + subcategory")
        return _ok("add_possession_vocab", _serialize(added))
    return _guard("add_possession_vocab", run)
