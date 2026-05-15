-- 0022_body_image.sql
-- Body-image / face-rating capture surface.
--
-- iOS Shortcut POSTs one photo per day to /body-image/upload. The route
-- saves the photo to Supabase Storage, inserts the photo row, and fans
-- out to Claude vision + GPT-4o vision + MediaPipe geometry in parallel.
-- Each rater writes one body_image_rating row. Total = up to 3 rows per
-- photo today (extensible — `source` is just a string).
--
-- Multi-tenant from day one for consistency with the rest of LifeOS,
-- though the iOS app is single-user today (LIFELOG_USER_ID='santi').

CREATE TABLE IF NOT EXISTS body_image_photo (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       TEXT NOT NULL DEFAULT 'santi',
  -- Object key inside the Supabase Storage bucket (e.g.
  -- 'raw/2026-05-15/<uuid>.jpg'). Bucket name lives in env
  -- (BODY_IMAGE_BUCKET), not in this column, so we can rebucket later
  -- without a schema change.
  storage_path  TEXT NOT NULL,
  caption       TEXT,
  -- Optional metadata captured by the iOS Shortcut (device, lighting,
  -- ambient_lux, time-of-day tag). Keep flexible — JSON beats columns
  -- when we don't know the final schema.
  metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS body_image_photo_user_created
  ON body_image_photo (user_id, created_at DESC);


CREATE TABLE IF NOT EXISTS body_image_rating (
  id            BIGSERIAL PRIMARY KEY,
  photo_id      UUID NOT NULL REFERENCES body_image_photo(id) ON DELETE CASCADE,
  -- 'claude' | 'gpt4v' | 'geometry' (free string; add more raters later
  -- without a schema change).
  source        TEXT NOT NULL,
  -- 0-100 composite. NULL for the geometry rater (which emits raw
  -- measurements, not a subjective score).
  overall       NUMERIC,
  -- Per-feature scores from LLM raters, or raw measurements from
  -- geometry. Whatever the rater returns goes here verbatim, so the
  -- dashboard can pivot on any feature without a schema change.
  dimensions    JSONB NOT NULL,
  -- Captured at insert. Differs from photo.created_at when we re-rate
  -- an old photo (the planned /reprocess endpoint).
  rated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS body_image_rating_photo
  ON body_image_rating (photo_id);

-- Hot path for the dashboard: "give me the last 90 days of <source>".
CREATE INDEX IF NOT EXISTS body_image_rating_source_rated
  ON body_image_rating (source, rated_at DESC);

-- One rating per (photo, source). Lets /reprocess overwrite cleanly via
-- ON CONFLICT and prevents a misbehaving rater from inserting twice.
CREATE UNIQUE INDEX IF NOT EXISTS body_image_rating_photo_source
  ON body_image_rating (photo_id, source);
