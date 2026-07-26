-- 0044_activity_suppression.sql
--
-- Tombstones for activities that should not appear in the warehouse.
--
-- Deleting local rows alone accomplishes nothing here: ingest_whoop_private
-- re-pulls lifts every two hours, so anything removed comes straight back on
-- the next tick. A delete only sticks if the ingester is told to stop
-- re-importing it.
--
-- Whoop's private API exposes no DELETE for strength workouts on any route I
-- could find (GET on the workout resource returns 200; DELETE on the same path
-- returns 405, and eleven other candidate routes 404). So the remote copy
-- stays and this is the honest boundary: the warehouse forgets it, Whoop
-- doesn't.
--
-- Reversible by design — deleting the tombstone lets the next sync restore the
-- workout, which is why this is a separate table rather than a flag mutated in
-- place.

BEGIN;

CREATE TABLE IF NOT EXISTS activity_suppression (
  source        TEXT NOT NULL,          -- 'whoop_lift', 'whoop', 'garmin', ...
  source_id     TEXT NOT NULL,          -- the vendor's activity id
  reason        TEXT,
  suppressed_by TEXT NOT NULL DEFAULT 'user',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source, source_id)
);

COMMENT ON TABLE activity_suppression IS
  'Activities the ingesters must not re-import. Consulted by '
  'ingest_whoop_private.ingest_lifts and by unify.sources.*. Removing a row '
  'here restores the activity on the next sync.';

CREATE INDEX IF NOT EXISTS activity_suppression_source_idx
  ON activity_suppression (source);

COMMIT;
