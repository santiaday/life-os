"""Packing lists and wardrobe gap analysis.

The shape of the problem: a trip implies a set of *slots* ("three warm tops",
"one waterproof shell", "a charger"), and the inventory either fills them or
it doesn't. Selection is the easy half. The half that earns its keep is the
gap list — an unfilled slot is a thing to buy or borrow before you leave, and
it is invisible if you only ever look at what you own.

Two rules the design follows:

  * Requirements union across cities, selection doesn't. Tokyo warm and rainy
    plus Sapporo cold means you need both a shell and a warm layer, not the
    average of the two. Averaging destinations is how people arrive in the
    cold city with the light jacket.
  * A slot that can't be filled is reported, never silently dropped. Returning
    six of the eight things you need, with no note about the other two, is
    worse than returning nothing.

Scoring is deliberately legible rather than clever: every selected item comes
back with the reasons it scored, so a wrong pick is diagnosable instead of
mysterious.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from lifeos_core.db import conn
from lifeos_core.logging import get_logger

from . import service, vocab

log = get_logger(__name__)


# --------------------------------------------------------------- profile ----

@dataclass
class Profile:
    """The unioned weather/formality demand of a whole trip."""
    nights: int
    weather_tags: set[str] = field(default_factory=set)
    low_c: float | None = None
    high_c: float | None = None
    min_formality: int = 1
    max_formality: int = 3
    cities: list[str] = field(default_factory=list)
    international: bool = False

    def as_dict(self) -> dict:
        return {
            "nights": self.nights,
            "weather_tags": sorted(self.weather_tags),
            "low_c": self.low_c,
            "high_c": self.high_c,
            "formality_range": [self.min_formality, self.max_formality],
            "cities": self.cities,
            "international": self.international,
        }


def _merge_destination(profile: Profile, dest: dict) -> None:
    tags = dest.get("weather_tags") or []
    profile.weather_tags.update(t for t in tags if t in vocab.WEATHER_TAGS)

    low = dest.get("low_c")
    if low is not None:
        profile.low_c = low if profile.low_c is None else min(profile.low_c, float(low))
    high = dest.get("high_c")
    if high is not None:
        profile.high_c = high if profile.high_c is None else max(profile.high_c, float(high))

    formality = dest.get("formality")
    if isinstance(formality, (list, tuple)) and len(formality) == 2:
        profile.min_formality = min(profile.min_formality, int(formality[0]))
        profile.max_formality = max(profile.max_formality, int(formality[1]))
    elif isinstance(formality, int):
        profile.max_formality = max(profile.max_formality, formality)

    if dest.get("city"):
        profile.cities.append(str(dest["city"]))
    if dest.get("international"):
        profile.international = True


def build_profile(trip: dict, weather_profile: Any = None) -> Profile:
    """Union the trip's own destinations with any caller-supplied override.

    `weather_profile` accepts a single dict (applies to the whole trip) or a
    list of per-city dicts. Both are merged on top of dim_trip.destinations
    rather than replacing it, so passing "it'll rain in Kyoto" doesn't lose
    the temperatures already stored on the trip.
    """
    nights = max(1, (trip["end_date"] - trip["start_date"]).days + 1)
    profile = Profile(nights=nights)

    for dest in trip.get("destinations") or []:
        if isinstance(dest, dict):
            _merge_destination(profile, dest)

    if isinstance(weather_profile, dict):
        _merge_destination(profile, weather_profile)
    elif isinstance(weather_profile, list):
        for dest in weather_profile:
            if isinstance(dest, dict):
                _merge_destination(profile, dest)

    # Temperature implies tags the caller may not have spelled out. Below 12°C
    # you need a warm layer whether or not anyone typed "cold".
    if profile.low_c is not None and profile.low_c <= 12:
        profile.weather_tags.add("cold")
    if profile.high_c is not None and profile.high_c >= 27:
        profile.weather_tags.add("heat")

    profile.cities = list(dict.fromkeys(profile.cities))
    return profile


# ----------------------------------------------------------------- slots ----

@dataclass
class Slot:
    """One requirement. `essential=False` means an unfilled slot is a note,
    not a gap — nobody needs to buy a second pair of sunglasses before a
    flight."""
    key: str
    label: str
    kind: str
    qty: int
    subcategories: tuple[str, ...] = ()
    require_weather: tuple[str, ...] = ()
    min_warmth: int | None = None
    max_warmth: int | None = None
    min_formality: int | None = None
    essential: bool = True
    suggestion: str = ""


# Per-night rates for the clothing kinds. Tuned for "wash nothing on a short
# trip, do one load on a long one" — which is why the caps exist: nine tops
# for a twelve-night trip is a laundry decision, not a packing one.
_CLOTHING_RATES: dict[str, tuple[float, int, int]] = {
    # kind: (per-night rate, minimum, maximum)
    "top":    (0.8,  2, 8),
    "bottom": (0.34, 1, 4),
}

# Toiletries and tech that travel every time, addressed by subcategory. These
# are the things actually forgotten — nobody leaves without trousers.
_ALWAYS_SUBCATEGORIES: tuple[tuple[str, str, str], ...] = (
    # (kind, subcategory, suggestion when missing)
    ("tech", "charger", "a wall charger for the phone"),
    ("tech", "cable", "a charging cable"),
    ("toiletry", "toothbrush", "a travel toothbrush"),
    ("toiletry", "toothpaste", "travel-size toothpaste"),
    ("toiletry", "deodorant", "a travel-size deodorant"),
    ("toiletry", "razor", "a razor"),
)


def build_slots(profile: Profile) -> list[Slot]:
    """Turn a profile into the concrete list of things the trip requires."""
    nights = profile.nights
    tags = profile.weather_tags
    slots: list[Slot] = []

    for kind, (rate, floor, cap) in _CLOTHING_RATES.items():
        qty = max(floor, min(cap, math.ceil(nights * rate)))
        slots.append(Slot(
            key=f"{kind}.base",
            label=f"{qty} × {kind}",
            kind=kind,
            qty=qty,
            # In heat, exclude the heavy end: a merino midweight is a
            # technically-valid top that you will not wear in Bangkok.
            max_warmth=3 if "heat" in tags and "cold" not in tags else None,
            suggestion=f"more {kind}s in rotation",
        ))

    # Footwear: one pair worn, one packed, from three nights up.
    shoe_qty = 2 if nights >= 3 else 1
    slots.append(Slot(key="footwear.base", label=f"{shoe_qty} × footwear",
                      kind="footwear", qty=shoe_qty,
                      suggestion="a second pair of shoes"))

    # --- weather-driven -----------------------------------------------------
    if "rain" in tags:
        slots.append(Slot(
            key="outerwear.rain", label="waterproof outer layer",
            kind="outerwear", qty=1, require_weather=("rain",),
            suggestion="a packable rain shell — this is the most common real gap",
        ))
    if "cold" in tags or (profile.low_c is not None and profile.low_c <= 12):
        deep = profile.low_c is not None and profile.low_c <= 5
        slots.append(Slot(
            key="outerwear.warm",
            label="warm outer layer" + (" (sub-5°C)" if deep else ""),
            kind="outerwear", qty=1, min_warmth=4 if deep else 3,
            suggestion="an insulated jacket rated for the low temperature",
        ))
        slots.append(Slot(
            key="top.midlayer", label="warm mid layer", kind="top", qty=1,
            min_warmth=3, suggestion="a merino or fleece mid layer",
        ))
        slots.append(Slot(
            key="accessory.cold", label="hat / gloves / scarf", kind="accessory",
            qty=1, min_warmth=3, essential=deep,
            suggestion="a beanie and gloves",
        ))
    if "wind" in tags and "rain" not in tags:
        slots.append(Slot(key="outerwear.wind", label="wind layer",
                          kind="outerwear", qty=1, require_weather=("wind",),
                          essential=False, suggestion="a wind shell"))
    if "heat" in tags:
        slots.append(Slot(key="headwear.sun", label="sun protection",
                          kind="headwear", qty=1, essential=False,
                          suggestion="a cap or sun hat"))

    # --- formality ----------------------------------------------------------
    if profile.max_formality >= 4:
        for kind in ("top", "bottom", "footwear"):
            slots.append(Slot(
                key=f"{kind}.formal", label=f"smart {kind}", kind=kind, qty=1,
                min_formality=4,
                suggestion=f"a {kind} at business formality or above",
            ))

    # --- bags ---------------------------------------------------------------
    slots.append(Slot(key="bag.main", label="main bag", kind="bag", qty=1,
                      subcategories=("duffel", "carry-on suitcase",
                                     "checked suitcase", "backpack"),
                      suggestion="a carry-on or duffel"))
    if nights >= 3:
        slots.append(Slot(key="bag.day", label="daypack", kind="bag", qty=1,
                          subcategories=("daypack", "sling", "tote", "backpack"),
                          essential=False, suggestion="a packable daypack"))

    # --- tech and toiletries ------------------------------------------------
    for kind, subcategory, suggestion in _ALWAYS_SUBCATEGORIES:
        slots.append(Slot(key=f"{kind}.{subcategory}", label=subcategory,
                          kind=kind, qty=1, subcategories=(subcategory,),
                          suggestion=suggestion))
    if profile.international:
        slots.append(Slot(key="tech.adapter", label="travel adapter", kind="tech",
                          qty=1, subcategories=("travel adapter",),
                          suggestion="a plug adapter for the destination"))
    slots.append(Slot(key="tech.headphones", label="headphones", kind="tech", qty=1,
                      subcategories=("headphones", "earbuds"), essential=False,
                      suggestion="headphones for the flight"))
    slots.append(Slot(key="toiletry.sunscreen", label="sunscreen", kind="toiletry",
                      qty=1, subcategories=("sunscreen",),
                      essential="heat" in tags, suggestion="sunscreen"))

    return slots


# ------------------------------------------------------------- selection ----

def _candidates() -> list[dict]:
    """The pool: active, non-purge items with their packing-relevant columns
    and enough wear history to prefer things actually used."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.name, p.kind::text AS kind, p.subcategory, p.brand,
                   p.primary_color, p.formality, p.warmth, p.weather_tags,
                   p.packed_volume_l, p.weight_g, p.condition, p.reviewed,
                   p.photo_thumb,
                   COALESCE(s.wear_count_365, 0) AS wear_count_365,
                   s.days_since_worn
              FROM dim_possession p
              JOIN mart_possession_summary s ON s.id = p.id
             WHERE p.status = 'active'
            """
        )
        return cur.fetchall()


def _eligible(item: dict, slot: Slot) -> bool:
    if item["kind"] != slot.kind:
        return False
    if slot.subcategories and item.get("subcategory") not in slot.subcategories:
        return False
    tags = set(item.get("weather_tags") or [])
    if slot.require_weather and not set(slot.require_weather).issubset(tags):
        return False
    warmth = item.get("warmth")
    if slot.min_warmth is not None and (warmth or 0) < slot.min_warmth:
        return False
    if slot.max_warmth is not None and warmth is not None and warmth > slot.max_warmth:
        return False
    formality = item.get("formality")
    return not (slot.min_formality is not None
                and (formality or 0) < slot.min_formality)


def _score(item: dict, slot: Slot, profile: Profile) -> tuple[float, list[str]]:
    """Rank eligible items. Returns (score, human-readable reasons)."""
    score = 0.0
    reasons: list[str] = []

    overlap = set(item.get("weather_tags") or []) & profile.weather_tags
    if overlap:
        score += 2.0 * len(overlap)
        reasons.append("suits " + "/".join(sorted(overlap)))

    warmth = item.get("warmth")
    if warmth is not None and profile.low_c is not None:
        # Map the trip's coldest temperature onto the 1-5 warmth scale and
        # prefer items near it. Over-warm is penalised at half the rate of
        # under-warm: being too hot is uncomfortable, too cold is a problem.
        wanted = 5 if profile.low_c <= 0 else 4 if profile.low_c <= 8 else \
                 3 if profile.low_c <= 16 else 2 if profile.low_c <= 24 else 1
        delta = warmth - wanted
        score -= (delta * 0.5) if delta > 0 else (abs(delta) * 1.0)
        if abs(delta) <= 1:
            reasons.append("warmth fits the forecast")

    formality = item.get("formality")
    if formality is not None:
        if profile.min_formality <= formality <= profile.max_formality:
            score += 1.5
            reasons.append("right formality")
        else:
            score -= min(2.0, abs(formality - profile.max_formality) * 0.75)

    worn = item.get("wear_count_365") or 0
    if worn:
        # Diminishing: something worn 40 times isn't four times better than
        # something worn 10 times, it's just clearly in rotation.
        score += min(2.0, math.log1p(worn))
        reasons.append(f"worn {worn}× this year")
    else:
        score -= 1.0
        reasons.append("never worn this year")

    condition = item.get("condition")
    if condition is not None:
        score += (condition - 3) * 0.5
        if condition <= 2:
            reasons.append("poor condition")

    # Packability. Only applied when the data exists; most items won't have it
    # and shouldn't be penalised for that.
    volume = item.get("packed_volume_l")
    if volume is not None:
        score -= min(1.5, float(volume) * 0.15)
    weight = item.get("weight_g")
    if weight is not None:
        score -= min(1.5, float(weight) / 1000.0 * 0.3)

    if not item.get("reviewed"):
        # Auto-tags are provisional; prefer a confirmed item where there's a
        # choice, but never exclude one — on a fresh inventory nothing is
        # reviewed yet and the list would come back empty.
        score -= 0.75
        reasons.append("tags unreviewed")

    return score, reasons


def build_packing_list(trip_id: str | UUID, weather_profile: Any = None,
                       persist: bool = False) -> dict:
    """Select items for a trip and report what's missing.

    `persist=True` writes the selections into bridge_trip_packing as
    candidates (audited), which is what the Packing screen calls so the
    checkboxes have rows to hang off. The default is read-only — generating a
    list to look at shouldn't mutate anything.
    """
    trip = service.get_trip(trip_id)
    if trip is None:
        raise service.PossessionError(f"no trip {trip_id}")

    profile = build_profile(trip, weather_profile)
    slots = build_slots(profile)
    pool = _candidates()

    selected: dict[str, dict] = {}   # possession_id -> selection record
    gaps: list[dict] = []
    per_slot: list[dict] = []

    for slot in slots:
        eligible = [i for i in pool if _eligible(i, slot)]
        ranked = sorted(
            ((*_score(i, slot, profile), i) for i in eligible),
            key=lambda t: t[0],
            reverse=True,
        )
        # An item already chosen for another slot still counts as filling this
        # one — the smart trousers are also a pair of trousers. Double-counting
        # here would have you packing four bottoms for a three-night trip.
        picks: list[dict] = []
        already = 0
        for score, reasons, item in ranked:
            if len(picks) + already >= slot.qty:
                break
            key = str(item["id"])
            if key in selected:
                already += 1
                selected[key]["slots"].append(slot.key)
                continue
            picks.append({
                "id": item["id"], "name": item["name"], "kind": item["kind"],
                "subcategory": item.get("subcategory"), "brand": item.get("brand"),
                "primary_color": item.get("primary_color"),
                "photo_thumb": item.get("photo_thumb"),
                "weather_tags": item.get("weather_tags") or [],
                "warmth": item.get("warmth"), "formality": item.get("formality"),
                "packed_volume_l": item.get("packed_volume_l"),
                "weight_g": item.get("weight_g"),
                "score": round(score, 2), "reasons": reasons,
                "slots": [slot.key],
            })
        for pick in picks:
            selected[str(pick["id"])] = pick

        found = len(picks) + already
        per_slot.append({"slot": slot.key, "label": slot.label, "kind": slot.kind,
                         "needed": slot.qty, "found": found,
                         "essential": slot.essential})
        if found < slot.qty:
            gaps.append({
                "slot": slot.key,
                "label": slot.label,
                "kind": slot.kind,
                "needed": slot.qty,
                "found": found,
                "missing": slot.qty - found,
                "essential": slot.essential,
                "reason": _gap_reason(slot, profile),
                "suggestion": slot.suggestion,
            })

    items = sorted(selected.values(), key=lambda i: (i["kind"], i["name"]))
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item["kind"], []).append(item)

    if persist:
        service.save_packing_candidates(
            trip["id"],
            [{"id": i["id"], "slot": i["slots"][0]} for i in items],
        )

    return {
        "trip": {"id": trip["id"], "name": trip["name"],
                 "start_date": trip["start_date"], "end_date": trip["end_date"],
                 "destinations": trip.get("destinations")},
        "profile": profile.as_dict(),
        "by_kind": grouped,
        "items": items,
        "slots": per_slot,
        "gaps": gaps,
        "totals": {
            "item_count": len(items),
            "weight_g": round(sum(float(i["weight_g"]) for i in items
                                  if i.get("weight_g") is not None), 1) or None,
            "packed_volume_l": round(sum(float(i["packed_volume_l"]) for i in items
                                         if i.get("packed_volume_l") is not None), 1)
                               or None,
            "essential_gaps": sum(1 for g in gaps if g["essential"]),
        },
        "persisted": bool(persist),
    }


def _gap_reason(slot: Slot, profile: Profile) -> str:
    """Say why the slot exists, not just that it's empty. 'No waterproof
    outer layer' is a fact; 'rain expected in Kyoto and nothing you own is
    tagged waterproof' is actionable."""
    bits: list[str] = []
    if slot.require_weather:
        bits.append("nothing tagged " + "/".join(slot.require_weather))
    if slot.min_warmth is not None:
        bits.append(f"nothing at warmth {slot.min_warmth}+")
    if slot.min_formality is not None:
        bits.append(f"nothing at formality {slot.min_formality}+")
    if slot.subcategories:
        bits.append("no " + " or ".join(slot.subcategories))
    if not bits:
        bits.append(f"fewer than {slot.qty} active {slot.kind} items")
    where = f" for {', '.join(profile.cities)}" if profile.cities else ""
    return "; ".join(bits) + where


def find_wardrobe_gaps(weather_tags: list[str] | None = None,
                       formality: int | None = None,
                       nights: int = 5) -> dict:
    """The same analysis without a trip: "if I went somewhere cold and rainy
    for five nights, what would I be missing?" Used both standalone and to
    sanity-check the wardrobe before booking anything."""
    bad = [t for t in (weather_tags or []) if t not in vocab.WEATHER_TAGS]
    if bad:
        raise service.PossessionError(
            f"unknown weather tag(s) {bad}; one of {', '.join(vocab.WEATHER_TAGS)}")

    profile = Profile(nights=max(1, int(nights)),
                      weather_tags=set(weather_tags or []))
    if formality is not None:
        profile.max_formality = max(profile.max_formality, int(formality))
    if "cold" in profile.weather_tags:
        profile.low_c = 4.0
    if "heat" in profile.weather_tags:
        profile.high_c = 30.0

    pool = _candidates()
    gaps, covered = [], []
    for slot in build_slots(profile):
        found = sum(1 for i in pool if _eligible(i, slot))
        entry = {"slot": slot.key, "label": slot.label, "kind": slot.kind,
                 "needed": slot.qty, "owned": found, "essential": slot.essential}
        if found < slot.qty:
            gaps.append(entry | {"missing": slot.qty - found,
                                 "reason": _gap_reason(slot, profile),
                                 "suggestion": slot.suggestion})
        else:
            covered.append(entry)
    return {"profile": profile.as_dict(), "gaps": gaps, "covered": covered,
            "essential_gaps": sum(1 for g in gaps if g["essential"])}
