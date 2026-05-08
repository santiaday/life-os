-- 0018_pushpress_schema.sql
-- PushPress programmed-workouts ingestion (gym daily WOD across class types).
--
-- Pipeline pulls a ±7-day window per class type from PushPress's GraphQL API
-- (POST /v2/graph/graphql, query GetWorkoutOfDay) and lands:
--   raw_pushpress_workout_of_day  — full payload, dedupe key (class_type_uuid, class_date)
--   fact_pushpress_session         — one row per published programming
--   fact_pushpress_part            — exploded workout parts (POSTERIOR / WORKOUT OF THE DAY / etc.)
--
-- Descriptions are plaintext (e.g. "AMRAP 16: 50 Box Step-ups @ 20\"…") so a
-- separate downstream parser converts them into structured movements. That
-- parser writes to fact_pushpress_movement, reserved here without being
-- created — schema lands when the parser does. See ingest_pushpress/ingest.py.
--
-- Reserved for later phases (NOT created in this migration):
--   fact_pushpress_movement, dim_pushpress_movement_canonical
--   fact_pushpress_score (your logged scored result for a workout)

BEGIN;

-- ---- class type registry --------------------------------------------------
-- Three rows in production today: Barbell/Weightlifting Club, CrossFit & HIIT,
-- HYROX. Refreshed at the start of every sync.
CREATE TABLE IF NOT EXISTS dim_pushpress_class_type (
  uuid          TEXT PRIMARY KEY,         -- e.g. '51237627-edab-47b2-83fe-04a56ff781c3'
  name          TEXT NOT NULL,
  origin        TEXT,                     -- 'train' for all current types
  is_static     BOOLEAN,
  progressive   BOOLEAN,
  last_day_num  INT,
  fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---- raw payload ----------------------------------------------------------
-- Natural key = (class_type_uuid, class_date) — every fetch overwrites with
-- the latest payload. is_empty=true means "we asked and got an empty result"
-- (rest day or unprogrammed) — preserved so we don't keep refetching empties.
CREATE TABLE IF NOT EXISTS raw_pushpress_workout_of_day (
  class_type_uuid  TEXT NOT NULL REFERENCES dim_pushpress_class_type(uuid),
  class_date       DATE NOT NULL,
  workout_uid      TEXT,
  is_empty         BOOLEAN NOT NULL DEFAULT FALSE,
  payload          JSONB,
  payload_hash     TEXT,                  -- sha256 of canonical payload — drives skip-on-unchanged
  fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at_src   TIMESTAMPTZ,           -- updatedDate from API, when set
  PRIMARY KEY (class_type_uuid, class_date)
);

CREATE INDEX IF NOT EXISTS ix_raw_pp_wod_workout_uid
  ON raw_pushpress_workout_of_day(workout_uid);
CREATE INDEX IF NOT EXISTS ix_raw_pp_wod_class_date
  ON raw_pushpress_workout_of_day(class_date);

-- ---- fact: one row per programmed session ---------------------------------
-- workout_uid is the natural key. (Same workout can in principle apply to
-- multiple dates — PushPress lets coaches reuse a programming entry — so we
-- still carry class_date on the fact row for fast date-range scans, but the
-- uid is what stays stable across edits.)
CREATE TABLE IF NOT EXISTS fact_pushpress_session (
  workout_uid       TEXT PRIMARY KEY,
  class_type_uuid   TEXT NOT NULL REFERENCES dim_pushpress_class_type(uuid),
  class_type_name   TEXT,                 -- denorm for fast list views
  class_date        DATE NOT NULL,
  title             TEXT,
  workout_state     TEXT,                 -- 'PUBLISHED' | 'DRAFT' | 'SCHEDULED' (observed)
  origin            TEXT,                 -- 'train' currently
  parts_count       INT NOT NULL DEFAULT 0,
  divisions         TEXT[],               -- union of part-level divisions, e.g. {Performance,Fitness}
  published_on      TIMESTAMPTZ,
  publishing_date   TIMESTAMPTZ,
  publishing_time   TIMESTAMPTZ,
  created_date      TIMESTAMPTZ,
  updated_date      TIMESTAMPTZ,
  -- Soft link to fact_workout (Whoop) — populated when same physical session
  -- shows up on the user's Whoop band that day. Nullable, no FK so Whoop
  -- re-ingests can renumber without breaking us.
  whoop_workout_id  TEXT,
  fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_fact_pp_session_date
  ON fact_pushpress_session(class_date);
CREATE INDEX IF NOT EXISTS ix_fact_pp_session_class_type_date
  ON fact_pushpress_session(class_type_uuid, class_date);

-- ---- fact: exploded parts -------------------------------------------------
-- Per-part rows. ordinal is the order within the workout (0-indexed). Title
-- is the section label PushPress uses (POSTERIOR, WORKOUT OF THE DAY, A) Snatch).
-- workout_title is the prescribed lift / WOD name ("Deadlifts", "Get on your
-- hands'"). description is the freeform plaintext that the LLM parser later
-- decomposes into movements.
CREATE TABLE IF NOT EXISTS fact_pushpress_part (
  part_uid          TEXT PRIMARY KEY,
  workout_uid       TEXT NOT NULL REFERENCES fact_pushpress_session(workout_uid)
                                    ON DELETE CASCADE,
  class_type_uuid   TEXT NOT NULL,
  class_date        DATE NOT NULL,
  ordinal           INT NOT NULL,
  title             TEXT,
  workout_title     TEXT,
  description       TEXT,
  score_type        TEXT,                 -- 'Weight' | 'Rounds/Reps' | 'Time' | etc.
  score_count       INT,
  set_count         INT,                  -- API field is "sets"
  default_reps      INT,
  divisions         TEXT[],
  unit              TEXT,                 -- 'IMPERIAL' | 'METRIC' | NULL
  athletes_notes    TEXT,
  coaches_notes     TEXT
);

CREATE INDEX IF NOT EXISTS ix_fact_pp_part_class_date
  ON fact_pushpress_part(class_date);
CREATE INDEX IF NOT EXISTS ix_fact_pp_part_workout_ordinal
  ON fact_pushpress_part(workout_uid, ordinal);

COMMIT;
