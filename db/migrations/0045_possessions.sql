-- 0045_possessions.sql
--
-- Possession inventory: a photo-backed record of every physical thing owned.
--
-- Three jobs it has to do: decide what to purge, answer "which bin is that in",
-- and build a trip packing list with gap analysis. All three want the same row
-- shape, so a charger, a duffel and a t-shirt live in ONE table. Modelling
-- garments separately from gear would mean two of everything downstream --
-- two location joins, two packing queries, two purge views -- to buy nothing.
--
-- Controlled vocab from day one (dim_color, dim_possession_subcategory) with
-- real foreign keys. Free-text colour produces 'navy' / 'Navy' / 'dark blue' /
-- 'midnight' inside a week and every filter downstream is then a lie. The
-- subcategory FK is composite on (kind, subcategory), so 'running shoe' can
-- only ever attach to a footwear row.
--
-- Idempotency is photo_hash: sha256 of the ORIGINAL image bytes, unique. The
-- ingest pipeline is a batch job over a folder and will be re-run; without
-- this, re-running duplicates the entire closet.

BEGIN;

-- ---------------------------------------------------------------- enums ----

DO $$ BEGIN
  CREATE TYPE possession_kind AS ENUM (
    'top','bottom','outerwear','footwear','headwear',
    'accessory','bag','tech','toiletry','gear','other'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE possession_status AS ENUM (
    'active','purge_candidate','listed','donated','sold','retired'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ------------------------------------------------------ controlled vocab ----

CREATE TABLE IF NOT EXISTS dim_color (
  name        TEXT PRIMARY KEY,          -- lowercase canonical token
  family      TEXT NOT NULL,             -- neutral | blue | green | red | ...
  hex         TEXT,                      -- representative swatch, for the UI
  sort_order  SMALLINT NOT NULL DEFAULT 100
);

COMMENT ON TABLE dim_color IS
  'Controlled vocabulary for dim_possession.primary_color / secondary_color. '
  'The vision tagger is handed this list in its prompt and may not invent '
  'values. Add rows here rather than loosening the FK.';

CREATE TABLE IF NOT EXISTS dim_possession_subcategory (
  kind        possession_kind NOT NULL,
  name        TEXT NOT NULL,             -- 'rain shell', 'running shoe', 'crew tee'
  notes       TEXT,
  PRIMARY KEY (kind, name)
);

COMMENT ON TABLE dim_possession_subcategory IS
  'Controlled vocabulary for (kind, subcategory). Composite key so a '
  'subcategory can only attach to the kind it belongs to.';

-- ----------------------------------------------------------- locations ----

CREATE TABLE IF NOT EXISTS dim_storage_location (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL UNIQUE,      -- 'Under-bed bin A', 'Closet rod', 'TNF duffel'
  kind        TEXT NOT NULL,             -- closet | bin | drawer | bag | rack | shelf | offsite
  parent_id   UUID REFERENCES dim_storage_location(id) ON DELETE SET NULL,
  notes       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN dim_storage_location.parent_id IS
  'Nesting: Bedroom -> Under bed -> Bin A. Flattening this into one string '
  'makes "what is under my bed" unanswerable once a container moves.';

CREATE INDEX IF NOT EXISTS dim_storage_location_parent_idx
  ON dim_storage_location (parent_id);

-- --------------------------------------------------------- possessions ----

CREATE TABLE IF NOT EXISTS dim_possession (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name              TEXT NOT NULL,
  kind              possession_kind NOT NULL,
  subcategory       TEXT,
  brand             TEXT,
  colorway          TEXT,                -- as sold: 'Alloy/Ivory'. Free text on purpose.
  primary_color     TEXT NOT NULL REFERENCES dim_color(name),
  secondary_color   TEXT REFERENCES dim_color(name),
  pattern           TEXT,                -- solid | stripe | check | graphic | camo
  material          TEXT,
  size              TEXT,
  formality         SMALLINT CHECK (formality BETWEEN 1 AND 5),
  warmth            SMALLINT CHECK (warmth BETWEEN 1 AND 5),
  weather_tags      TEXT[] NOT NULL DEFAULT '{}',   -- rain | wind | cold | heat | humid
  packed_volume_l   NUMERIC,
  weight_g          NUMERIC,
  acquired_on       DATE,
  cost_cents        INTEGER,             -- nullable on purpose; see comment below
  retailer          TEXT,
  condition         SMALLINT CHECK (condition BETWEEN 1 AND 5),
  status            possession_status NOT NULL DEFAULT 'active',
  location_id       UUID REFERENCES dim_storage_location(id) ON DELETE SET NULL,
  photo_original    TEXT,                -- storage path, never bytes
  photo_cutout      TEXT,
  photo_thumb       TEXT,
  photo_hash        TEXT UNIQUE,         -- sha256 of original bytes; idempotency key
  tag_confidence    JSONB,               -- {"brand":0.42,"primary_color":0.95,...}
  tag_model         TEXT,                -- which vision model produced the tags
  reviewed          BOOLEAN NOT NULL DEFAULT FALSE,
  notes             TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (kind, subcategory)
    REFERENCES dim_possession_subcategory (kind, name)
);

COMMENT ON COLUMN dim_possession.cost_cents IS
  'Nullable and left null when unknown. Most items have no recoverable price, '
  'and a made-up one poisons cost_per_wear for every item that does.';
COMMENT ON COLUMN dim_possession.reviewed IS
  'FALSE until a human has confirmed the auto-tags. The grid dims unreviewed '
  'rows; purge and packing both treat unreviewed tags as provisional.';
COMMENT ON COLUMN dim_possession.photo_hash IS
  'sha256 of the original image bytes. UNIQUE, so re-running the batch '
  'ingest over the same folder is a no-op instead of a second closet.';

CREATE INDEX IF NOT EXISTS dim_possession_kind_idx     ON dim_possession (kind);
CREATE INDEX IF NOT EXISTS dim_possession_status_idx   ON dim_possession (status);
CREATE INDEX IF NOT EXISTS dim_possession_location_idx ON dim_possession (location_id);
CREATE INDEX IF NOT EXISTS dim_possession_color_idx    ON dim_possession (primary_color);
CREATE INDEX IF NOT EXISTS dim_possession_reviewed_idx ON dim_possession (reviewed)
  WHERE reviewed = FALSE;
CREATE INDEX IF NOT EXISTS dim_possession_weather_idx
  ON dim_possession USING GIN (weather_tags);

CREATE OR REPLACE FUNCTION possession_touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS dim_possession_touch ON dim_possession;
CREATE TRIGGER dim_possession_touch
  BEFORE UPDATE ON dim_possession
  FOR EACH ROW EXECUTE FUNCTION possession_touch_updated_at();

-- ---------------------------------------------------------------- wear ----

CREATE TABLE IF NOT EXISTS fact_wear (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  possession_id  UUID NOT NULL REFERENCES dim_possession(id) ON DELETE CASCADE,
  worn_on        DATE NOT NULL,
  context        TEXT,                   -- work | gym | travel | social | home
  city           TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (possession_id, worn_on, context)
);

COMMENT ON TABLE fact_wear IS
  'One row per (item, day, context). The unique constraint is what makes '
  'log_wear safe to call twice from a phone with bad signal.';

CREATE INDEX IF NOT EXISTS fact_wear_possession_idx ON fact_wear (possession_id, worn_on DESC);
CREATE INDEX IF NOT EXISTS fact_wear_worn_on_idx    ON fact_wear (worn_on DESC);

-- ------------------------------------------------------------- outfits ----

CREATE TABLE IF NOT EXISTS dim_outfit (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name         TEXT NOT NULL UNIQUE,
  formality    SMALLINT CHECK (formality BETWEEN 1 AND 5),
  weather_tags TEXT[] NOT NULL DEFAULT '{}',
  notes        TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bridge_outfit_possession (
  outfit_id     UUID NOT NULL REFERENCES dim_outfit(id) ON DELETE CASCADE,
  possession_id UUID NOT NULL REFERENCES dim_possession(id) ON DELETE CASCADE,
  PRIMARY KEY (outfit_id, possession_id)
);

CREATE INDEX IF NOT EXISTS bridge_outfit_possession_possession_idx
  ON bridge_outfit_possession (possession_id);

-- --------------------------------------------------------------- trips ----

CREATE TABLE IF NOT EXISTS dim_trip (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name         TEXT NOT NULL UNIQUE,
  start_date   DATE NOT NULL,
  end_date     DATE NOT NULL,
  destinations JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- [{"city":"Tokyo","from":"2026-10-07","to":"2026-10-11",
    --   "weather_tags":["rain","humid"],"low_c":14,"high_c":24,"formality":[2,4]}]
  notes        TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (end_date >= start_date)
);

COMMENT ON COLUMN dim_trip.destinations IS
  'Ordered city blocks. Each may carry its own weather_tags / low_c / high_c / '
  'formality range; build_packing_list unions them into the trip requirement '
  'set, so a single warm city still pulls a warm layer for the cold one.';

CREATE TABLE IF NOT EXISTS bridge_trip_packing (
  trip_id       UUID NOT NULL REFERENCES dim_trip(id) ON DELETE CASCADE,
  possession_id UUID NOT NULL REFERENCES dim_possession(id) ON DELETE CASCADE,
  qty           SMALLINT NOT NULL DEFAULT 1 CHECK (qty > 0),
  state         TEXT NOT NULL DEFAULT 'candidate'
                  CHECK (state IN ('candidate','packed','worn','unused')),
  slot          TEXT,                    -- which requirement slot selected it
  notes         TEXT,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (trip_id, possession_id)
);

CREATE INDEX IF NOT EXISTS bridge_trip_packing_trip_idx
  ON bridge_trip_packing (trip_id, state);

-- ---------------------------------------------------------------- views ----

-- Full container path, so "Bin A" reads as "Bedroom / Under bed / Bin A".
-- Depth is capped at 10 -- a cycle introduced by a bad parent_id update would
-- otherwise spin forever, and no real storage tree is that deep.
CREATE OR REPLACE VIEW vw_storage_location AS
WITH RECURSIVE tree AS (
  SELECT id, name, kind, parent_id, name AS full_path, 1 AS depth
    FROM dim_storage_location
   WHERE parent_id IS NULL
  UNION ALL
  SELECT c.id, c.name, c.kind, c.parent_id,
         t.full_path || ' / ' || c.name, t.depth + 1
    FROM dim_storage_location c
    JOIN tree t ON c.parent_id = t.id
   WHERE t.depth < 10
)
SELECT t.id, t.name, t.kind, t.parent_id, t.full_path, t.depth,
       (SELECT count(*) FROM dim_possession p
         WHERE p.location_id = t.id AND p.status = 'active') AS active_item_count
  FROM tree t;

-- Per-possession wear economics. A view, not a table: the whole inventory is
-- a few hundred rows, so a nightly mart refresh would buy nothing and could
-- serve stale cost-per-wear right after a wear is logged.
CREATE OR REPLACE VIEW mart_possession_summary AS
SELECT
  p.id,
  p.name,
  p.kind,
  p.subcategory,
  p.brand,
  p.primary_color,
  p.status,
  p.condition,
  p.reviewed,
  p.acquired_on,
  p.cost_cents,
  p.location_id,
  l.full_path                                        AS location_path,
  COALESCE(w.wear_count_30,  0)                      AS wear_count_30,
  COALESCE(w.wear_count_90,  0)                      AS wear_count_90,
  COALESCE(w.wear_count_365, 0)                      AS wear_count_365,
  COALESCE(w.wear_count_all, 0)                      AS wear_count_all,
  w.last_worn_on,
  CASE WHEN w.last_worn_on IS NOT NULL
       THEN (CURRENT_DATE - w.last_worn_on)::int END AS days_since_worn,
  CASE WHEN p.cost_cents IS NOT NULL AND COALESCE(w.wear_count_all, 0) > 0
       THEN ROUND(p.cost_cents::numeric / w.wear_count_all)::int END
                                                     AS cost_per_wear_cents,
  -- Stale = still owned, old enough to have been worn, and wasn't. An item
  -- acquired last week with no wears is not stale, it's new.
  (p.status = 'active'
   AND p.acquired_on IS NOT NULL
   AND p.acquired_on <= CURRENT_DATE - 180
   AND COALESCE(w.last_worn_on, p.acquired_on) <= CURRENT_DATE - 180)
                                                     AS is_stale
FROM dim_possession p
LEFT JOIN vw_storage_location l ON l.id = p.location_id
LEFT JOIN (
  SELECT possession_id,
         count(*) FILTER (WHERE worn_on >= CURRENT_DATE - 30)  AS wear_count_30,
         count(*) FILTER (WHERE worn_on >= CURRENT_DATE - 90)  AS wear_count_90,
         count(*) FILTER (WHERE worn_on >= CURRENT_DATE - 365) AS wear_count_365,
         count(*)                                              AS wear_count_all,
         max(worn_on)                                          AS last_worn_on
    FROM fact_wear
   GROUP BY possession_id
) w ON w.possession_id = p.id;

COMMENT ON VIEW mart_possession_summary IS
  'Per-possession wear economics: wear counts over 30/90/365 days, days since '
  'last worn, cost per wear, and is_stale. Read this rather than joining '
  'fact_wear by hand.';

-- Grant the read-only MCP role access alongside everything else it can read.
DO $$ BEGIN
  EXECUTE 'GRANT SELECT ON dim_color, dim_possession_subcategory, '
          'dim_storage_location, dim_possession, fact_wear, dim_outfit, '
          'bridge_outfit_possession, dim_trip, bridge_trip_packing, '
          'vw_storage_location, mart_possession_summary TO lifeos_mcp';
EXCEPTION WHEN undefined_object THEN
  RAISE NOTICE 'role lifeos_mcp not present; skipping grants';
END $$;

COMMIT;
