# body-image runbook

Daily face/headshot rating pipeline. iOS Shortcut takes a 3-photo
session (front, ¾ left, ¾ right) → FastAPI route → photo in Supabase
Storage + parallel fan-out of Claude + GPT-4o + Gemini vision (each
split into Structure + Surface specialist calls) + MediaPipe geometry
→ two DB tables → mart_daily join → dashboard.

## What's where

| Piece                | Path                                                  |
|----------------------|-------------------------------------------------------|
| Migrations           | `db/migrations/0022_body_image.sql`, `0023_body_image_optimizations.sql` |
| Route + dashboard    | `body_image/routes.py`                                |
| Orchestration        | `body_image/service.py`                               |
| Raters               | `body_image/raters/{claude,gpt4v,gemini,geometry}.py` |
| Rubric (Structure/Surface) | `body_image/raters/_rubric.py`                  |
| Calibration anchors  | `body_image/calibration/`                             |
| Reference validation | `body_image/validation.py` + Sunday 5am cron          |
| Interventions CRUD   | `body_image/interventions.py`                         |
| Storage helper       | `body_image/storage.py`                               |
| Geometry sidecar     | `face_geometry/{main.py,Dockerfile}`                  |
| Dashboard HTML       | `body_image/templates/dashboard.html`                 |
| Mart wiring          | `mart_refresh/sql.py` (INSERT_MART_BODY_IMAGE_DAILY)  |

## One-time setup

### Env vars (`.env`)

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-proj-...       # optional; enables GPT-4o rater
GEMINI_API_KEY=AIza...           # optional; free tier covers daily cadence

SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SERVICE_KEY=eyJhbGc...   # SERVICE role, not anon
BODY_IMAGE_BUCKET=body-image      # default; create the bucket manually

# Tunables — these are the optimization knobs from the brief.
BODY_IMAGE_RATING_TEMPERATURE=0.0           # T1.2 — deterministic
BODY_IMAGE_RUNS_PER_RATER=1                 # T1.2 — bump to 3 to quantify noise floor
BODY_IMAGE_USE_SPECIALIST_CALLS=true        # T2.6 — Structure + Surface split
BODY_IMAGE_USE_CALIBRATION_ANCHORS=false    # T2.4 — flip on once anchors exist
BODY_IMAGE_GEOMETRY_BASELINE_DAYS=30        # T8 — when σ-deviation kicks in
```

### Storage

Supabase dashboard → Storage → **New bucket** named `body-image`,
**Public: off**.

### Migrate

```bash
uv run python -m db.apply        # applies 0023_body_image_optimizations
```

### Build + deploy

```bash
docker compose up -d --build mcp face-geometry scheduler
```

`scheduler` is rebuilt so the Sunday validation cron picks up.

## Capture protocol (T1.1 — the biggest single lever)

Standardize as much as you can. Camera position and lighting drive
more variance than the models do.

- **Same spot in your apartment**, facing your window. Soft side
  light — never overhead.
- **Same time of day.** Cortisol-driven AM puffiness is real and
  visible; an AM-vs-PM trend pollutes everything else.
- **Phone at eye level.** From above compresses jaw; from below puffs
  the neck.
- **Neutral expression**, lips closed, gaze straight at lens.
- **Three frames per session**: front, ¾ left, ¾ right. The Shortcut
  groups them via `session_id`.

## iOS Shortcut (3-photo session)

1. **Generate UUID** (Get Contents of URL `https://www.uuidgenerator.net/api/version4`,
   trim, store as variable `session_id`). Or use the Shortcuts
   built-in `Text` action with `${UUID}` magic variable if available
   on your iOS version.
2. Repeat the following block **three times**, varying `angle`:
   - Variant 1: `angle = front`
   - Variant 2: `angle = three_quarter_left`
   - Variant 3: `angle = three_quarter_right`

   Inside the block:
   - **Take Photo** (front camera)
   - **Ask for Input** (caption — only on the first photo if you want)
   - **Get Contents of URL**
     - URL: `https://lifeos.ledion.io/body-image/upload`
     - Method: `POST`
     - Headers: `Authorization` → `Bearer <LIFELOG_API_TOKEN>`
     - Request Body: **Form**
       - `photo` → File → the Photo from step
       - `session_id` → Text → `session_id` variable
       - `angle` → Text → the variant value
       - `caption` → Text → from "Ask for Input" (optional)
3. **Show Notification** with the response of the final call.

Set the Shortcut to Back Tap → Double Tap.

Server-side auto-bundling falls back if `session_id` is omitted: any
photo arriving within 10 min of the previous one for the same user
attaches to that session. Don't rely on it — explicit is better.

## Calibration anchors (T2.4)

Three reference photos with crowd-rated scores get sent inline with
every LLM call when `BODY_IMAGE_USE_CALIBRATION_ANCHORS=true`. The
prompt tells the model "the LAST image is the subject — score it on
the same scale as the references."

See [body_image/calibration/README.md](calibration/README.md) for
SCUT-FBP5500 sourcing instructions and the file layout.

**Cost impact** (anchors on, defaults otherwise): ~$0.06/photo
instead of ~$0.015/photo. Worth it for stability.

## Weekly reference validation (T7)

Every Sunday 5am, the scheduler runs every photo in
`body_image/calibration/validation/*.jpg` through the live rating
pipeline and checks Pearson r against expected scores.

- If r ≥ 0.7: silent ok (logged to scheduler logs).
- If r < 0.7: Pushover alert with the 5 worst-offending photos.

Reference set should have 5-8 photos spanning the score range:
- 3 SCUT-FBP photos at known scores
- 1 stable male celebrity at online consensus
- 1 of you at a known baseline (the "static control" — change here
  means *something happened to you*, not the model)

Each reference is `<slug>.jpg` paired with `<slug>.score` (plaintext
file with the expected 0-100 score).

## Interventions (T14)

Discrete behavior changes that get vertical markers on every
dashboard trend chart. Add via the dashboard form or curl:

```bash
curl -X POST https://lifeos.ledion.io/body-image/api/interventions \
  -H "Authorization: Bearer $LIFELOG_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "intervention_key": "tretinoin",
    "event": "start",
    "occurred_on": "2026-06-01",
    "metadata": {"strength": "0.025%", "brand": "altreno"}
  }'
```

Suggested key conventions: `tretinoin`, `minoxidil_cheeks`,
`minoxidil_scalp`, `spf_daily`, `niacinamide`, `creatine`,
`haircut`, `clean_shave`, `red_light_therapy`, `sleep_position_back`.
Events: `start` / `stop` for continuous things, `apply` for one-offs,
`milestone` for noteworthy checkpoints.

## Lagged correlations (T13)

Body-image metrics are now columns on `mart_daily`, so the existing
MCP `correlate_metrics` tool works directly. Example queries to ask
the LifeOS connector:

> "Pearson correlate `body_image_skin_clarity` against `alcohol_g`
> with lags 0, 1, 2, 3 days."

> "Spearman correlate `body_image_under_eye` against
> `sleep_consistency_pct` with lag 1 day."

> "Correlate `body_image_overall` against `recovery_score` with lag 1."

The mart refresh runs body_image after the regular mart_daily so
`correlate_metrics` always sees the latest data.

## Cost (rough, per photo, all features on)

| Mode                                  | Per photo |
|---------------------------------------|-----------|
| Claude only (specialists, no anchors) | ~$0.03    |
| + GPT-4o                              | ~$0.06    |
| + Gemini (free tier, ≤15rpm)          | ~$0.06    |
| + anchors (4 images per call)         | ~$0.18    |
| + 3 runs/rater                        | ~$0.54    |

Defaults shipped: Claude + Gemini specialists, no anchors, 1 run.
≈ $0.03/photo, $1/month at one session/day.

## Failure modes

| Symptom                              | Likely cause                              |
|--------------------------------------|-------------------------------------------|
| `gemini_*: 429`                      | Gemini free-tier quota (15rpm). Pace down or pay. |
| `gpt4v_*: openai.RateLimitError`     | OpenAI tier-1 cap on parallel image calls. Lower `BODY_IMAGE_RUNS_PER_RATER`. |
| Scheduler logs `body_image.validation.skipped` | No reference photos sourced yet. Drop files in `body_image/calibration/validation/`. |
| Pushover: `body-image: model drift`  | Pearson r < 0.7 vs reference scores. Likely prompt drift or a model rev. Check recent commits to `_rubric.py`. |
| `Bucket not found` on upload         | `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` are for different projects. Decode both JWTs and check `ref`. |
| Dashboard shows "0 photos"           | `LIFELOG_API_TOKEN` mismatch on `?token=` query — must match `.env`. |
