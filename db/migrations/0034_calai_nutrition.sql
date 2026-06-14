-- 0034_calai_nutrition.sql
-- Cal AI nutrition source. Reverse-engineered from a mitmproxy capture:
--   * Cal AI is Firebase-backed (project `calai-app`). Auth = Firebase ID token
--     (1h, RS256, iss securetoken.google.com/calai-app), refreshed from a stored
--     refresh token via securetoken.googleapis.com.
--   * api.calai.app/v6/* is the AI/account API (fixFood, health-score,
--     getSubscription, …) with envelope {userInfo, data}.
--   * Food photos: Firebase Storage calai-app.appspot.com,
--     food_images_user/{uid}/{imageId}.jpg.
--   * The food DIARY itself is stored in Firestore (cloud-synced; references the
--     storage images) and is read via the Firestore REST API with the ID token.
--
-- Each logged food maps onto the existing Cronometer-shaped fact_food_log /
-- fact_food_daily (Cal AI even carries per-ingredient `ethanol` -> alcohol_g), so
-- mart_refresh, mart_daily, mart_meal and every nutrition MCP tool keep working
-- unchanged — rows are just tagged with their source.

-- Source attribution on the shared nutrition facts. Cronometer was the implicit
-- writer, so default to that; Cal AI rows write source='calai', Apple-Health
-- already has its own table.
ALTER TABLE fact_food_log   ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'cronometer';
ALTER TABLE fact_food_daily ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'cronometer';

-- Raw Cal AI diary entries — one row per logged food/meal. entry_id is Cal AI's
-- stable id for the logged item (Firestore document id); payload is the full
-- Firestore document so nothing is lost and re-parsing never needs a re-fetch.
CREATE TABLE IF NOT EXISTS raw_calai_food (
  entry_id   TEXT PRIMARY KEY,
  user_id    TEXT,
  logged_at  TIMESTAMPTZ,
  day        DATE,
  payload    JSONB NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_raw_calai_food_day  ON raw_calai_food (day);
CREATE INDEX IF NOT EXISTS ix_raw_calai_food_user ON raw_calai_food (user_id, day);

GRANT SELECT ON raw_calai_food TO lifeos_reader;
