"""Read-side MCP tools for the possession inventory.

Thin wrappers over possessions.service / possessions.packing — the logic lives
there so the HTTP surface and the CLI answer identically. What these add is
the standard envelope and the error translation: a PossessionError is a
caller-fixable mistake (unknown location, colour outside the vocabulary) and
comes back as a message naming the legal values, not a stack trace.
"""

from __future__ import annotations

from typing import Any

from lifeos_core.logging import get_logger
from mcp_server.tools import _err, _ok, _serialize
from possessions import packing, service, vocab

log = get_logger(__name__)


def _guard(tool: str, fn, *args, **kwargs) -> dict:
    try:
        return fn(*args, **kwargs)
    except service.PossessionError as e:
        return _err(tool, e)
    except Exception as e:  # unexpected; surfaced rather than swallowed
        log.exception("possessions.tool_failed", tool=tool)
        return _err(tool, e)


def get_possessions(kind: str | None = None, status: str | None = "active",
                    location: str | None = None, primary_color: str | None = None,
                    unworn_since: int | None = None, reviewed: bool | None = None,
                    search: str | None = None, weather_tags: list[str] | None = None,
                    limit: int = 100) -> dict:
    def run() -> dict:
        rows = service.get_possessions(
            kind=kind, status=status, location=location,
            primary_color=primary_color, unworn_since=unworn_since,
            reviewed=reviewed, search=search, weather_tags=weather_tags,
            limit=limit,
        )
        warnings = []
        unreviewed = sum(1 for r in rows if not r["reviewed"])
        if unreviewed:
            warnings.append(
                f"{unreviewed} of {len(rows)} rows are unreviewed — their brand, "
                f"colour and material came from the vision tagger and have not "
                f"been confirmed. Treat those fields as provisional."
            )
        return _ok("get_possessions", _serialize(rows), warnings=warnings)
    return _guard("get_possessions", run)


def get_possession(possession_id: str) -> dict:
    def run() -> dict:
        item = service.get_possession(possession_id)
        if item is None:
            return _ok("get_possession", [], warnings=[f"no possession {possession_id}"])
        return _ok("get_possession", _serialize([item]))
    return _guard("get_possession", run)


def get_storage_contents(location_name: str, include_nested: bool = True) -> dict:
    def run() -> dict:
        result = service.get_storage_contents(location_name, include_nested)
        return _ok("get_storage_contents", _serialize(result["items"]),
                   extra={"location": _serialize([result["location"]])[0],
                          "children": _serialize(result["children"]),
                          "include_nested": include_nested})
    return _guard("get_storage_contents", run)


def get_locations() -> dict:
    return _guard(
        "get_locations",
        lambda: _ok("get_locations", _serialize(service.list_locations())),
    )


def get_purge_candidates(min_days_unworn: int = 180, limit: int = 50) -> dict:
    def run() -> dict:
        rows = service.get_purge_candidates(min_days_unworn, limit)
        return _ok("get_purge_candidates", _serialize(rows), warnings=[
            "Ordered by days idle, then cost per wear. Items with no acquisition "
            "date are included — an unknown purchase date is not evidence the "
            "item is new."
        ])
    return _guard("get_purge_candidates", run)


def get_outfits(formality: int | None = None,
                weather_tags: list[str] | None = None) -> dict:
    return _guard(
        "get_outfits",
        lambda: _ok("get_outfits", _serialize(service.get_outfits(formality, weather_tags))),
    )


def get_trips(upcoming_only: bool = False) -> dict:
    return _guard(
        "get_trips",
        lambda: _ok("get_trips", _serialize(service.list_trips(upcoming_only))),
    )


def build_packing_list(trip_id: str, weather_profile: Any = None,
                       persist: bool = False) -> dict:
    """Selected items grouped by kind, plus the gaps."""
    def run() -> dict:
        result = packing.build_packing_list(trip_id, weather_profile, persist=persist)
        warnings = []
        essential = [g for g in result["gaps"] if g["essential"]]
        if essential:
            warnings.append(
                "Unfilled essential slots: "
                + "; ".join(f"{g['label']} ({g['reason']})" for g in essential)
                + ". These are things to buy or borrow before the trip, not "
                  "search-harder problems."
            )
        if not result["items"]:
            warnings.append(
                "Nothing selected. Either the inventory is empty or every item "
                "is filtered out by status — only 'active' items are eligible."
            )
        return _ok("build_packing_list", _serialize(result["items"]),
                   warnings=warnings,
                   extra={"trip": _serialize([result["trip"]])[0],
                          "profile": result["profile"],
                          "by_kind": {k: _serialize(v)
                                      for k, v in result["by_kind"].items()},
                          "slots": result["slots"],
                          "gaps": result["gaps"],
                          "totals": result["totals"],
                          "persisted": result["persisted"]})
    return _guard("build_packing_list", run)


def find_wardrobe_gaps(weather_tags: list[str] | None = None,
                       formality: int | None = None, nights: int = 5) -> dict:
    def run() -> dict:
        result = packing.find_wardrobe_gaps(weather_tags, formality, nights)
        return _ok("find_wardrobe_gaps", result["gaps"],
                   extra={"profile": result["profile"],
                          "covered": result["covered"],
                          "essential_gaps": result["essential_gaps"]})
    return _guard("find_wardrobe_gaps", run)


def get_possession_vocab() -> dict:
    """The legal values for every controlled field. Call this before writing a
    possession — a colour or subcategory outside the vocabulary is rejected by
    a foreign key, not coerced."""
    def run() -> dict:
        return _ok("get_possession_vocab", [{
            "kinds": list(vocab.KINDS),
            "statuses": list(vocab.STATUSES),
            "weather_tags": list(vocab.WEATHER_TAGS),
            "patterns": list(vocab.PATTERNS),
            "wear_contexts": list(vocab.WEAR_CONTEXTS),
            "packing_states": list(vocab.PACKING_STATES),
            "colors": vocab.colors(),
            "subcategories": vocab.subcategories(),
        }])
    return _guard("get_possession_vocab", run)


def get_possession_stats() -> dict:
    return _guard(
        "get_possession_stats",
        lambda: _ok("get_possession_stats", _serialize([service.stats()])),
    )
