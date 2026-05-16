-- 0024_body_image_recommendations.sql
--
-- Stores the output of body_image.coach.generate_recommendations:
-- a synthesis pass across the user's recent ratings + interventions
-- + trends, asking Claude for a prioritized, NON-SURGICAL action list.
-- One row per generation (the user or a weekly cron triggers it);
-- dashboard reads the most recent row.

CREATE TABLE IF NOT EXISTS body_image_recommendation (
  id              BIGSERIAL PRIMARY KEY,
  user_id         TEXT NOT NULL DEFAULT 'santi',
  -- How wide a window of body_image_rating rows fed the synthesis.
  window_days     INT NOT NULL,
  -- Counts that fed the prompt — lets the dashboard say "based on
  -- 12 photos over 18 days, 5 themes emerged" without re-deriving.
  photo_count     INT NOT NULL DEFAULT 0,
  rating_count    INT NOT NULL DEFAULT 0,
  intervention_count INT NOT NULL DEFAULT 0,
  -- Structured output from the model.
  -- Shape:
  --   {
  --     "summary": "...one paragraph...",
  --     "themes": [
  --       { "theme": "skin clarity",
  --         "evidence_count": 8,
  --         "evidence_summary": "...",
  --         "actions": [
  --           { "type": "skincare"|"hair"|"grooming"|"photo"
  --                    |"behavior"|"clothing"|"posture",
  --             "title": "...",
  --             "details": "...specific product/protocol...",
  --             "effort": "daily"|"weekly"|"one-time",
  --             "expected_window_days": <int>
  --           }
  --         ],
  --         "avoid": ["...", "..."]
  --       }
  --     ],
  --     "photo_protocol_suggestions": ["...", "..."],
  --     "fixed_features_acknowledgement": "..."
  --   }
  brief           JSONB NOT NULL,
  -- Raw model response for audit (truncated if huge).
  raw_response    TEXT,
  -- Model used so we know which prompt regression caused a drift.
  model           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS body_image_recommendation_user_recent
  ON body_image_recommendation (user_id, created_at DESC);
