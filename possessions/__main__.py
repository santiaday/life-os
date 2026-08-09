"""Possession inventory CLI.

Runs on the machine holding the photos — background removal needs rembg and
the originals, neither of which belongs on the API droplet.

    python -m possessions ingest ~/Pictures/inventory --location "Closet rod"
    python -m possessions ingest ~/Pictures/inventory --dry-run
    python -m possessions retag <id>
    python -m possessions stats
    python -m possessions review --limit 20
    python -m possessions locations
    python -m possessions add-location "Bin A" --kind bin --parent "Under bed"
    python -m possessions pack <trip-id> --rain --low-c 8
    python -m possessions gaps --weather rain --weather cold --formality 4

Re-running `ingest` over the same folder is safe and is how you resume a run
that died: already-ingested photos are skipped on their hash.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from lifeos_core.logging import configure_logging, get_logger

from . import ingest as ingest_mod
from . import packing, segmentation, service, storage, tagging, vocab

log = get_logger(__name__)


def _echo_json(payload) -> None:
    click.echo(json.dumps(payload, indent=2, default=str))


@click.group()
def cli() -> None:
    """Photo-backed inventory of physical possessions."""
    configure_logging()


# ------------------------------------------------------------------ ingest ---

@cli.command()
@click.argument("folder", type=click.Path(exists=True))
@click.option("--location", default=None,
              help="Storage location name to file every item in this batch under.")
@click.option("--hint", default=None,
              help="Free-text context passed to the tagger, e.g. 'all toiletries'.")
@click.option("--no-cutout", is_flag=True,
              help="Skip background removal (no rembg needed); tag the original.")
@click.option("--no-tag", is_flag=True,
              help="Upload and hash only; leave tagging for later.")
@click.option("--model", default=None, help="Override the vision model.")
@click.option("--workers", default=4, show_default=True,
              help="Concurrent items. Mostly network wait; segmentation stays serial.")
@click.option("--limit", type=int, default=None, help="Process at most N new photos.")
@click.option("--dry-run", is_flag=True,
              help="List what would be processed and what is already ingested.")
def ingest(folder, location, hint, no_cutout, no_tag, model, workers, limit, dry_run):
    """Ingest a folder of item photos."""
    paths = ingest_mod.discover(folder)
    if limit:
        paths = paths[:limit]
    if not paths:
        click.echo(f"No images found under {folder}.")
        return

    if dry_run:
        digests = {}
        for path in paths:
            try:
                digests[path] = ingest_mod.hash_bytes(path.read_bytes())
            except OSError as e:
                click.echo(f"  unreadable  {path}  ({e})")
        seen = ingest_mod.existing_hashes(digests.values())
        new = [p for p, h in digests.items() if h not in seen]
        old = [p for p, h in digests.items() if h in seen]
        click.echo(f"{len(new)} new, {len(old)} already ingested, "
                   f"{len(paths) - len(digests)} unreadable.")
        for path in new[:50]:
            click.echo(f"  new      {path.name}")
        if len(new) > 50:
            click.echo(f"  ... and {len(new) - 50} more")
        return

    if not no_cutout:
        # Fail before uploading anything rather than at item 1 of 300.
        try:
            segmentation._session(segmentation.MODEL)
        except segmentation.SegmentationUnavailable as e:
            raise click.ClickException(str(e)) from e

    def progress(done, total, outcome):
        mark = {"created": "ok", "failed": "FAIL"}.get(outcome.status, outcome.status)
        flagged = (f"  check: {', '.join(outcome.low_confidence)}"
                   if outcome.low_confidence else "")
        detail = outcome.error or outcome.name or ""
        click.echo(f"[{done}/{total}] {mark:4} {Path(outcome.path).name}  "
                   f"{detail}{flagged}")

    result = ingest_mod.ingest_folder(
        folder, location=location, hint=hint, do_cutout=not no_cutout,
        do_tag=not no_tag, model=model, workers=workers, limit=limit,
        on_progress=progress,
    )
    click.echo(f"\ncreated {result.created}  skipped {result.skipped}  "
               f"failed {result.failed}")
    if result.failed:
        click.echo("\nFailures:")
        for item in result.items:
            if item.status == "failed":
                click.echo(f"  {Path(item.path).name}: {item.error}")
        click.echo("\nRe-run the same command to retry — ingested photos are skipped.")
    if result.created:
        click.echo("\nEverything landed unreviewed. Review it at "
                   "/possessions/app (or `python -m possessions review`).")


@cli.command()
@click.argument("possession_id")
@click.option("--model", default=None, help="Override the vision model.")
@click.option("--hint", default=None, help="Extra context for the tagger.")
def retag(possession_id, model, hint):
    """Re-run the vision tagger on one already-ingested item."""
    item = service.get_possession(possession_id)
    if not item:
        raise click.ClickException(f"no possession {possession_id}")
    source = item.get("photo_cutout") or item.get("photo_original")
    if not source:
        raise click.ClickException("item has no stored photo to re-tag")

    data = storage.download(source)
    jpeg = (segmentation.flatten_for_vision(data) if source.endswith(".png")
            else segmentation.prepare_original_for_vision(data))
    tags = tagging.tag_image(jpeg, model=model, hint=hint)
    confidence = tags.pop("confidence", {})
    patch = {k: v for k, v in tags.items() if k != "tag_model"}
    patch["tag_confidence"] = confidence
    patch["tag_model"] = tags.get("tag_model")
    patch["reviewed"] = False
    service.update_possession(possession_id, patch)
    _echo_json({"id": possession_id, "tags": tags,
                "check": tagging.low_confidence_fields(confidence)})


# ------------------------------------------------------------------- reads ---

@cli.command()
def stats():
    """Inventory counts."""
    _echo_json(service.stats())


@cli.command()
@click.option("--limit", default=20, show_default=True)
def review(limit):
    """Show unreviewed items and the fields worth checking."""
    rows = service.review_queue(limit=limit)
    if not rows:
        click.echo("Nothing to review.")
        return
    for row in rows:
        flagged = tagging.low_confidence_fields(row.get("tag_confidence") or {})
        click.echo(f"{row['id']}  {row['name']}")
        click.echo(f"    {row['kind']}/{row.get('subcategory') or '-'}  "
                   f"{row.get('brand') or 'no brand'}  {row['primary_color']}")
        if flagged:
            click.echo(f"    check: {', '.join(flagged)}")


@cli.command()
def locations():
    """Storage tree with item counts."""
    for row in service.list_locations():
        indent = "  " * (row["depth"] - 1)
        click.echo(f"{indent}{row['name']}  ({row['kind']}, "
                   f"{row['active_item_count']} items)")


@cli.command("add-location")
@click.argument("name")
@click.option("--kind", required=True,
              type=click.Choice(["closet", "bin", "drawer", "bag", "rack",
                                 "shelf", "offsite"]))
@click.option("--parent", default=None, help="Parent location name.")
@click.option("--notes", default=None)
def add_location(name, kind, parent, notes):
    """Create a storage container."""
    _echo_json(service.create_location(name, kind, parent, notes))


@cli.command()
@click.option("--min-days", default=180, show_default=True)
@click.option("--limit", default=25, show_default=True)
def purge(min_days, limit):
    """Purge candidates, worst first."""
    for row in service.get_purge_candidates(min_days, limit):
        cpw = row.get("cost_per_wear_cents")
        cost = f"  ${cpw / 100:.2f}/wear" if cpw else ""
        click.echo(f"{row['days_idle'] or '?':>5}d  {row['name']}  "
                   f"({row['kind']}, worn {row['wear_count_all']}×){cost}")


# ----------------------------------------------------------------- packing ---

@cli.command()
@click.argument("trip_id")
@click.option("--rain", "tags", flag_value="rain", multiple=True)
@click.option("--cold", "tags", flag_value="cold", multiple=True)
@click.option("--heat", "tags", flag_value="heat", multiple=True)
@click.option("--wind", "tags", flag_value="wind", multiple=True)
@click.option("--humid", "tags", flag_value="humid", multiple=True)
@click.option("--low-c", type=float, default=None, help="Coldest expected °C.")
@click.option("--high-c", type=float, default=None, help="Warmest expected °C.")
@click.option("--formality", type=int, default=None,
              help="Highest formality the trip demands (1-5).")
@click.option("--save", is_flag=True,
              help="Write the selection into the trip's packing list.")
def pack(trip_id, tags, low_c, high_c, formality, save):
    """Build a packing list for a trip, with gaps."""
    profile = {"weather_tags": list(tags)}
    if low_c is not None:
        profile["low_c"] = low_c
    if high_c is not None:
        profile["high_c"] = high_c
    if formality is not None:
        profile["formality"] = [1, formality]

    result = packing.build_packing_list(trip_id, profile, persist=save)
    click.echo(f"{result['trip']['name']}  "
               f"{result['profile']['nights']} nights  "
               f"{', '.join(result['profile']['weather_tags']) or 'no weather tags'}\n")
    for kind, items in result["by_kind"].items():
        click.echo(f"{kind}:")
        for item in items:
            click.echo(f"    {item['name']}  ({', '.join(item['reasons'])})")
    if result["gaps"]:
        click.echo("\nGaps:")
        for gap in result["gaps"]:
            flag = "!" if gap["essential"] else "-"
            click.echo(f"  {flag} {gap['label']}: {gap['reason']}")
            click.echo(f"      -> {gap['suggestion']}")
    else:
        click.echo("\nNo gaps.")


@cli.command()
@click.option("--weather", "weather", multiple=True,
              type=click.Choice(list(vocab.WEATHER_TAGS)))
@click.option("--formality", type=int, default=None)
@click.option("--nights", default=5, show_default=True)
def gaps(weather, formality, nights):
    """What would be missing for a hypothetical trip."""
    result = packing.find_wardrobe_gaps(list(weather), formality, nights)
    if not result["gaps"]:
        click.echo("Nothing missing.")
        return
    for gap in result["gaps"]:
        flag = "!" if gap["essential"] else "-"
        click.echo(f"{flag} {gap['label']}: {gap['reason']}")
        click.echo(f"    -> {gap['suggestion']}")


@cli.command("add-trip")
@click.argument("name")
@click.argument("start_date")
@click.argument("end_date")
@click.option("--destinations", default=None,
              help='JSON array, e.g. \'[{"city":"Tokyo","from":"2026-10-07",'
                   '"to":"2026-10-11","weather_tags":["rain"],"low_c":14}]\'')
def add_trip(name, start_date, end_date, destinations):
    """Create a trip."""
    parsed = json.loads(destinations) if destinations else []
    _echo_json(service.create_trip(name, start_date, end_date, parsed))


@cli.command("bucket")
def bucket_cmd():
    """Create the storage bucket if it isn't there."""
    created = storage.ensure_bucket()
    click.echo(f"bucket {storage.bucket()}: {'created' if created else 'already exists'}")


def main() -> int:
    try:
        cli(standalone_mode=False)
        return 0
    except click.ClickException as e:
        click.echo(f"error: {e.format_message()}", err=True)
        return 1
    except service.PossessionError as e:
        click.echo(f"error: {e}", err=True)
        return 1
    except click.exceptions.Abort:
        return 130


if __name__ == "__main__":
    sys.exit(main())
