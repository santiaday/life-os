# Possession inventory — runbook

A photo-backed record of every physical thing owned. Three jobs: decide what to
purge, know which container a thing is in, and build a trip packing list with
gap analysis.

Clothing, footwear, bags, tech, toiletries and gear share **one** table. A
charger and a t-shirt both need "where is it" and "did I take it", and
splitting them would mean two of every query downstream to buy nothing.

---

## Where things run

| Piece | Runs where | Why |
|---|---|---|
| `python -m possessions ingest` | Your Mac | Needs `rembg` (~180 MB model + onnxruntime) and the original photos. |
| MCP tools + `/possessions/app` | The droplet | Reads already-cut-out PNGs from Supabase Storage; never imports `rembg`. |
| Postgres + Storage | Supabase | Shared with the rest of LifeOS. |

The droplet has 1 GB of RAM. Keeping segmentation off it is deliberate — it is
a one-time job on 200–300 items, and paying for it in the API container's
memory footprint forever would be a bad trade.

## One-time setup

```bash
# 1. Migrations (shared Supabase DB — run locally, same DB the droplet uses)
python -m db.apply

# 2. Storage credentials. Already set on the droplet; needed locally for ingest.
#    SUPABASE_URL is https://<project-ref>.supabase.co
grep -E '^(SUPABASE_URL|SUPABASE_SERVICE_KEY)=' .env || echo "add these to .env"

# 3. Create the private bucket
python -m possessions bucket

# 4. Segmentation (optional but strongly recommended — the cutout grid is the product)
pip install "rembg[cpu]"
```

## Phase 0 — the physical purge, before any code

Do the purge first, then photograph the survivors. Photographing everything and
purging in software means paying for ~40% more vision calls and reviewing ~40%
more items to reach the same inventory.

Capture rules that actually change the output:

- One consistent neutral backdrop; poster board is fine.
- **Shoot white and cream items on grey or black.** White-on-white is the worst
  case for segmentation and produces cutouts with chunks missing.
- One item per frame, flat or on a hanger, filling ~70% of the frame.
- Include the size/care label in frame when it's legible — `size` and
  `material` are the two fields the tagger can only get from a label.

## Phase 1 — ingest

```bash
# See what would happen; nothing is written
python -m possessions ingest ~/Pictures/inventory --dry-run

# The real run
python -m possessions ingest ~/Pictures/inventory --location "Closet rod"

# A themed batch tags better when you say so
python -m possessions ingest ~/Pictures/toiletries --hint "all toiletries, dopp kit contents"
```

Per photo: sha256 → skip if seen → background removal → thumbnail → upload
original/cutout/thumb → vision tag → insert with `reviewed = false`.

**Re-running is always safe** and is how you resume. `photo_hash` is unique on
`dim_possession`, so a batch that dies at item 180 is resumed by running the
identical command; already-ingested photos are skipped before any work.

Useful flags:

| Flag | When |
|---|---|
| `--no-cutout` | rembg unavailable, or an item it mangles (mesh, lace, translucent shells). Tags the original instead. |
| `--no-tag` | Get the photos in now, tag later. Rows land as `other`/`multicolor` placeholders. |
| `--workers N` | Default 4. Mostly network wait; segmentation stays serialised behind a lock regardless. |
| `--limit N` | Try the pipeline on five items before committing to 300. |

Cost: one vision call per item at ~1024px. On `claude-opus-5` a 300-item batch
is roughly $6–10. `POSSESSION_TAGGER_MODEL=claude-sonnet-5` roughly halves it
if you expect to re-tag repeatedly.

## Phase 2 — review

Nothing is trusted until a human confirms it. `reviewed` defaults to false and
the grid dims unreviewed items.

Open `https://lifeos.ledion.io/possessions/app?token=<LIFELOG_API_TOKEN>`. The
review queue is the first screen and is keyboard-driven:

- <kbd>Enter</kbd> accepts and advances
- <kbd>Tab</kbd> visits **only** the fields the tagger flagged as low-confidence
- <kbd>Esc</kbd> skips
- <kbd>⌘⌫</kbd> accepts and flags the item to purge

The target is 200 items in one sitting. If Tab is walking every field, the
confidence threshold is wrong — tune `POSSESSION_REVIEW_CONFIDENCE_THRESHOLD`
(default 0.7).

## Vocabulary

`primary_color` and `(kind, subcategory)` are **foreign keys**, not hints. An
off-vocabulary value is rejected with the legal list, never coerced. This is
what stops `navy` / `Navy` / `dark blue` / `midnight` from coexisting and
making every colour filter a lie.

When the tagger keeps reaching for something that isn't there, extend the
vocabulary — don't loosen the constraint:

```
add_possession_vocab(color="sage", color_family="green")
add_possession_vocab(kind="gear", subcategory="packing cube")
```

Free-text `colorway` is where manufacturer names ("Alloy/Ivory") belong.

## Packing

```
create_trip(name="Japan Oct", start_date="2026-10-07", end_date="2026-10-18",
            destinations=[
              {"city":"Tokyo","from":"2026-10-07","to":"2026-10-11",
               "weather_tags":["rain","humid"],"low_c":16,"high_c":24,
               "international":true},
              {"city":"Sapporo","from":"2026-10-12","to":"2026-10-18",
               "weather_tags":["cold"],"low_c":2,"high_c":9}])

build_packing_list(trip_id=..., persist=true)
```

Requirements **union** across destinations: warm-and-rainy plus cold means both
a shell and a warm layer, not the average. Averaging is how people arrive in
the cold city with the light jacket.

The `gaps[]` array is the output that matters — a required slot nothing you own
can fill. Each gap carries the reason and a suggestion, and `essential: true`
means "buy or borrow before you leave".

Tuning lives in `packing.py`: `_CLOTHING_RATES` (per-night quantities and
caps), `_ALWAYS_SUBCATEGORIES` (the things actually forgotten), and
`build_slots` (weather and formality rules).

## Deploy

Standard LifeOS flow (see the droplet-deploy notes):

```bash
git push origin main
ssh root@198.199.64.234 'cd /opt/life-os && git fetch origin && git merge --ff-only origin/main'
ssh root@198.199.64.234 'cd /opt/life-os && nohup docker compose up -d --build mcp > /tmp/deploy_build.log 2>&1 &'
```

Migrations apply to the shared Supabase DB from your Mac; the droplet needs no
separate migration step. `rembg` is deliberately **not** in `pyproject.toml`
dependencies — adding it would put onnxruntime in the API image.

## Gotchas

- **Re-running ingest is safe; deleting a photo from Storage is not.** The row
  keeps its path and the grid shows a hole. Delete the row, not the object.
- **Originals are never pruned.** Segmentation models improve; re-running one
  over the originals is free, re-shooting 300 items is a weekend.
- **`cost_cents` stays null when unknown.** A fabricated price poisons
  `cost_per_wear_cents` for every item that has a real one.
- **Nest locations.** `Bedroom → Under bed → Bin A`. `get_storage_contents` is
  nested by default, so asking about "Under bed" finds what's in Bin A.
- **Nothing is deleted on purge.** `status` moves to `purge_candidate` →
  `listed` → `donated`/`sold`, and the wear history stays, which is what keeps
  cost-per-wear honest after the fact.
- **`fact_wear` is unique on (item, day, context)**, so double-logging from a
  phone with bad signal is a no-op rather than a doubled wear count.
- **Unreviewed rows are provisional.** Both the MCP read tools and the packing
  scorer treat them that way; don't present an unreviewed brand as fact.

## Files

```
possessions/
  vocab.py         controlled vocabularies, read from Postgres, TTL-cached
  storage.py       Supabase Storage (original / cutout / thumb), batch signing
  segmentation.py  rembg + thumbnails + the vision-input flatten  (local only)
  tagging.py       Claude vision, structured outputs, post-schema repair
  ingest.py        batch pipeline and the hash gate
  service.py       every DB read and write; all writes audited
  packing.py       profile -> slots -> selection -> gaps
  routes.py        /possessions/* JSON API + the app
  templates/app.html   review queue, grid, purge, packing, locations
db/migrations/0045_possessions.sql, 0046_possession_vocab_seed.sql
mcp_server/possession_tools.py, possession_write_tools.py
tests/test_possessions_{packing,tagging,ingest}.py
```
