"""Claude vision auto-tagger.

One call per item against the cutout, returning strict JSON plus a parallel
per-field confidence object. Structured outputs (`output_config.format`) does
the schema enforcement, so there is no JSON-repair path here and no retry loop
around a parse — the API will not return a response that fails the schema.

Everything the tagger writes lands with `reviewed = false`. The confidences
exist to drive the review queue's Tab order: a human walks only the fields the
model was unsure about, which is the difference between reviewing 200 items in
one sitting and abandoning it at item 30.

Prompt rules that matter, in rough order of how much damage they prevent:

  * brand: only from a legible logo or label, never inferred from silhouette.
    A model guessing "Patagonia" from a fleece shape produces an inventory
    that looks authoritative and is fiction.
  * primary_color: constrained to dim_color by the schema's enum, not by
    asking nicely.
  * subcategory: the legal set is per-kind and can't be expressed in one JSON
    Schema, so it's in the prompt and re-checked here after the fact.
  * material: a guess is fine and expected — it just has to come back with a
    low confidence so review catches it.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from anthropic import Anthropic, APIError

from lifeos_core.logging import get_logger
from lifeos_core.settings import settings

from . import vocab

log = get_logger(__name__)

# Fields the model fills and that carry a confidence. `name` is excluded on
# purpose: it's a label for humans, always present, and "how confident are you
# in this name" isn't a question with a useful answer.
CONFIDENCE_FIELDS: tuple[str, ...] = (
    "kind", "subcategory", "brand", "colorway", "primary_color",
    "secondary_color", "pattern", "material", "size", "formality",
    "warmth", "weather_tags",
)


class TaggingError(RuntimeError):
    pass


def _client() -> Anthropic:
    if not settings.ANTHROPIC_API_KEY:
        raise TaggingError("ANTHROPIC_API_KEY is not set.")
    return Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def build_schema(colors: list[str]) -> dict:
    """JSON Schema for the tagger. Numeric ranges aren't supported by
    structured outputs, so 1-5 scales are expressed as integer enums."""
    color_enum = {"type": "string", "enum": colors}
    confidence_props = {
        f: {"type": "number", "description": "0 = guess, 1 = certain"}
        for f in CONFIDENCE_FIELDS
    }
    return {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short human label, e.g. 'navy merino crewneck'. "
                               "No brand unless the brand is legible.",
            },
            "kind": {"type": "string", "enum": list(vocab.KINDS)},
            "subcategory": _nullable({"type": "string"}),
            "brand": _nullable({"type": "string"}),
            "colorway": _nullable({"type": "string"}),
            "primary_color": color_enum,
            "secondary_color": _nullable(color_enum),
            "pattern": {"type": "string", "enum": list(vocab.PATTERNS)},
            "material": _nullable({"type": "string"}),
            "size": _nullable({"type": "string"}),
            "formality": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "warmth": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "weather_tags": {
                "type": "array",
                "items": {"type": "string", "enum": list(vocab.WEATHER_TAGS)},
            },
            "confidence": {
                "type": "object",
                "properties": confidence_props,
                "required": list(CONFIDENCE_FIELDS),
                "additionalProperties": False,
            },
        },
        "required": [
            "name", "kind", "subcategory", "brand", "colorway", "primary_color",
            "secondary_color", "pattern", "material", "size", "formality",
            "warmth", "weather_tags", "confidence",
        ],
        "additionalProperties": False,
    }


def build_prompt(colors: list[str], subcats: dict[str, list[str]]) -> str:
    subcat_block = "\n".join(
        f"  {kind}: {', '.join(names) if names else '(none defined)'}"
        for kind, names in subcats.items()
    )
    return f"""You are cataloguing one physical possession from a photo, for a personal \
inventory that drives purge decisions and travel packing lists. The subject has been \
cut out and placed on a flat grey background.

Return one record describing the single item shown.

Field rules:

brand — only if a logo, wordmark or label is legibly visible in the photo. Never infer \
a brand from silhouette, colourway, or construction, however distinctive. If you cannot \
read it, return null. A wrong brand is worse than no brand.

primary_color / secondary_color — must come from this list, exactly:
  {', '.join(colors)}
Pick the closest member for the dominant colour. secondary_color is null unless a second \
colour covers a real portion of the item; trim and small logos do not count.

colorway — free text, the manufacturer's name for the colour if it is printed on a label \
(e.g. "Alloy/Ivory"). Null otherwise. This is not a second guess at primary_color.

subcategory — must come from the list for the kind you chose:
{subcat_block}
If nothing fits, return null rather than inventing one.

size — only if printed on a visible label or tag. Null otherwise.

material — a reasoned guess from visible texture and drape is acceptable and expected. \
Report it with a confidence below 0.5 unless a fibre-content label is legible.

formality — 1 gym/loungewear, 2 casual, 3 smart casual, 4 business, 5 formal. For items \
where it doesn't apply (a charger, a toothbrush), return 2.

warmth — 1 hot-weather only, 3 three-season, 5 deep winter. For items with no thermal \
role, return 1.

weather_tags — only conditions the item is genuinely built for. A waterproof shell gets \
["rain","wind"]; a cotton hoodie gets []. Do not tag an item "rain" because it is dark.

confidence — one number in 0..1 per field, your actual calibrated confidence. These drive \
which fields a human is asked to check, so a confident wrong answer costs more than an \
honest low number. Use null-returning fields' confidence to express how sure you are that \
the value is genuinely absent."""


def tag_image(
    jpeg_bytes: bytes,
    *,
    model: str | None = None,
    hint: str | None = None,
) -> dict[str, Any]:
    """Tag one item. Returns the parsed record with `confidence` split out.

    `hint` is free text from the operator ("this batch is all toiletries",
    "size label is on the inner collar") appended to the user turn — a cheap
    way to lift accuracy on a batch that shares context.
    """
    colors = vocab.colors()
    subcats = vocab.subcategories()
    if not colors:
        raise TaggingError(
            "dim_color is empty — apply db/migrations/0046_possession_vocab_seed.sql "
            "before tagging, or the model has no legal colour to return."
        )

    content: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(jpeg_bytes).decode("ascii"),
            },
        },
        {"type": "text", "text": build_prompt(colors, subcats)},
    ]
    if hint:
        content.append({"type": "text", "text": f"Operator note for this batch: {hint}"})

    try:
        resp = _client().messages.create(
            model=model or settings.POSSESSION_TAGGER_MODEL,
            # Generous: on Opus 5 thinking is on by default and shares this
            # budget with the JSON. A tight cap truncates mid-record.
            max_tokens=4000,
            output_config={
                "format": {"type": "json_schema",
                           "schema": build_schema(colors)},
                "effort": settings.POSSESSION_TAGGER_EFFORT,
            },
            messages=[{"role": "user", "content": content}],
        )
    except APIError as e:
        raise TaggingError(f"vision call failed: {type(e).__name__}: {e}") from e

    if resp.stop_reason == "refusal":
        raise TaggingError("vision call was declined by safety classifiers")

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise TaggingError(f"vision call returned no text block (stop={resp.stop_reason})")

    record = json.loads(text)
    return _repair(record, model=model or settings.POSSESSION_TAGGER_MODEL)


def _clamp01(v: Any) -> float | None:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None


def _repair(record: dict, *, model: str) -> dict:
    """Post-schema validation for the things JSON Schema can't express.

    The schema pins colours and kinds. It cannot pin subcategory to the chosen
    kind (that's a conditional the format doesn't support), so an out-of-vocab
    subcategory is dropped to null here and its confidence zeroed — which puts
    the field at the front of the review queue rather than failing the item.
    """
    out = dict(record)
    confidence = {k: _clamp01(v) for k, v in (out.pop("confidence", {}) or {}).items()}

    kind = out.get("kind")
    sub = out.get("subcategory")
    if sub is not None and not vocab.is_valid_subcategory(kind, sub):
        log.info("possessions.tag.subcategory_rejected", kind=kind, subcategory=sub)
        out["subcategory"] = None
        confidence["subcategory"] = 0.0

    for field in ("primary_color", "secondary_color"):
        normalized = vocab.normalize_color(out.get(field))
        if out.get(field) and normalized is None:
            log.info("possessions.tag.color_rejected", field=field, value=out.get(field))
            confidence[field] = 0.0
        out[field] = normalized

    # primary_color is NOT NULL in the schema. If the model somehow returned
    # something unusable, park it on 'multicolor' with zero confidence so the
    # row still lands and review can fix it, rather than dropping the item.
    if not out.get("primary_color"):
        out["primary_color"] = "multicolor" if "multicolor" in vocab.colors() else vocab.colors()[0]
        confidence["primary_color"] = 0.0

    tags = [t for t in (out.get("weather_tags") or []) if t in vocab.WEATHER_TAGS]
    out["weather_tags"] = sorted(set(tags))

    out["confidence"] = confidence
    out["tag_model"] = model
    return out


def low_confidence_fields(confidence: dict[str, float | None],
                          threshold: float | None = None) -> list[str]:
    """Fields the review queue should flag and Tab through."""
    cut = settings.POSSESSION_REVIEW_CONFIDENCE_THRESHOLD if threshold is None else threshold
    return sorted(
        f for f, v in (confidence or {}).items()
        if v is None or v < cut
    )
