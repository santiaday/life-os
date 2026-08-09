"""Controlled vocabularies, read from the database.

The vocab lives in Postgres (dim_color, dim_possession_subcategory) rather
than in this file so the FKs and the tagger prompt can never disagree: the
prompt is generated from the same rows the constraint checks against. Adding
a colour is one INSERT, not an INSERT plus a code change plus a deploy.

Cached per-process with a short TTL — the tables change a few times a year,
and the ingest CLI would otherwise re-query them 300 times in a row.
"""

from __future__ import annotations

import time

from lifeos_core.db import conn

# Enum values, mirrored from 0045. These DO live in code because they're
# Postgres enums: changing them needs a migration either way.
KINDS: tuple[str, ...] = (
    "top", "bottom", "outerwear", "footwear", "headwear",
    "accessory", "bag", "tech", "toiletry", "gear", "other",
)
STATUSES: tuple[str, ...] = (
    "active", "purge_candidate", "listed", "donated", "sold", "retired",
)
WEATHER_TAGS: tuple[str, ...] = ("rain", "wind", "cold", "heat", "humid")
PATTERNS: tuple[str, ...] = (
    "solid", "stripe", "check", "graphic", "camo", "floral", "colorblock", "other",
)
WEAR_CONTEXTS: tuple[str, ...] = ("work", "gym", "travel", "social", "home")
PACKING_STATES: tuple[str, ...] = ("candidate", "packed", "worn", "unused")

_TTL_SECONDS = 300
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, loader):
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < _TTL_SECONDS:
        return hit[1]
    value = loader()
    _cache[key] = (now, value)
    return value


def invalidate() -> None:
    """Drop the cache. Called after any vocab write."""
    _cache.clear()


def colors() -> list[str]:
    def _load() -> list[str]:
        with conn() as c, c.cursor() as cur:
            cur.execute("SELECT name FROM dim_color ORDER BY sort_order, name")
            return [r["name"] for r in cur.fetchall()]
    return _cached("colors", _load)  # type: ignore[return-value]


def color_rows() -> list[dict]:
    def _load() -> list[dict]:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT name, family, hex FROM dim_color ORDER BY sort_order, name"
            )
            return cur.fetchall()
    return _cached("color_rows", _load)  # type: ignore[return-value]


def subcategories() -> dict[str, list[str]]:
    """{kind: [subcategory, ...]}. Every kind is present, even if empty."""
    def _load() -> dict[str, list[str]]:
        out: dict[str, list[str]] = {k: [] for k in KINDS}
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT kind::text AS kind, name FROM dim_possession_subcategory "
                "ORDER BY kind, name"
            )
            for row in cur.fetchall():
                out.setdefault(row["kind"], []).append(row["name"])
        return out
    return _cached("subcategories", _load)  # type: ignore[return-value]


def is_valid_subcategory(kind: str, subcategory: str | None) -> bool:
    if subcategory is None:
        return True
    return subcategory in subcategories().get(kind, [])


def normalize_color(value: str | None) -> str | None:
    """Case/whitespace-fold onto the vocabulary. Returns None if it isn't in
    it — callers decide whether that's a rejection or a fallback, because a
    silent coercion to 'black' is worse than an obvious null."""
    if not value:
        return None
    token = value.strip().lower()
    return token if token in set(colors()) else None
