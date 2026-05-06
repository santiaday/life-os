"""Static activity-type catalog.

Loaded once at process start from activity_types.json next to this file.
Reload with `reload_activity_types()` (the iOS app exposes a manual refresh
in Settings, but in practice editing the JSON requires a restart).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .schemas import ActivityType

_JSON_PATH = Path(__file__).with_name("activity_types.json")


@lru_cache(maxsize=1)
def _load() -> list[ActivityType]:
    raw = json.loads(_JSON_PATH.read_text())
    items = [ActivityType.model_validate(r) for r in raw]
    items.sort(key=lambda a: a.sort_order)
    return items


def list_activity_types() -> list[ActivityType]:
    return _load()


def get_activity_type(activity_id: str) -> ActivityType | None:
    for a in _load():
        if a.id == activity_id:
            return a
    return None


def reload_activity_types() -> int:
    """Drop the cached catalog. Returns the count after reload."""
    _load.cache_clear()
    return len(_load())
