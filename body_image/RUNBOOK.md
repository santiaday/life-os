# body-image runbook

Daily face/headshot rating pipeline. iOS Shortcut → FastAPI route → photo
in Supabase Storage + 3 raters (Claude vision, GPT-4o vision, MediaPipe
geometry) fanned out in parallel → two DB tables.

## What's where

| Piece                | Path                                            |
|----------------------|-------------------------------------------------|
| Migration            | `db/migrations/0022_body_image.sql`             |
| Route + dashboard    | `body_image/routes.py`                          |
| Orchestration        | `body_image/service.py`                         |
| Raters               | `body_image/raters/{claude,gpt4v,geometry}.py`  |
| Storage helper       | `body_image/storage.py`                         |
| Geometry sidecar     | `face_geometry/{main.py,Dockerfile}`            |
| Dashboard HTML       | `body_image/templates/dashboard.html`           |
| Compose service      | `face-geometry` in `docker-compose.yml`         |

## One-time setup

### 1. Supabase Storage bucket

In the Supabase dashboard → Storage → **Create new bucket**:
- Name: `body-image` (or set `BODY_IMAGE_BUCKET` to match)
- Public: **off** (route hands out short-lived signed URLs)

### 2. Env vars (add to `.env`)

```
# Supabase project URL + service-role key (Settings → API in dashboard).
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SERVICE_KEY=eyJhbGc...  # the SERVICE ROLE key, not anon

# Bucket name (defaults to "body-image" if omitted).
BODY_IMAGE_BUCKET=body-image

# Rater keys. ANTHROPIC_API_KEY may already be set for the coach pipeline.
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-proj-...
```

The bearer token (`LIFELOG_API_TOKEN`) is reused from the Lifelog iOS
surface — same Keychain entry, same Shortcut header.

### 3. Migrate

```bash
uv run python -m db.apply
```

### 4. Build + deploy

```bash
docker compose up -d --build mcp face-geometry
```

The mcp container picks up the new `body_image` package on rebuild.
`face-geometry` is a new service — first build is slow (~600MB of
MediaPipe + OpenCV).

## iOS Shortcut

Build once on the phone:

1. **Take Photo** (front camera, square)
2. **Ask for Input** — "Caption?" (text, optional)
3. **Get Contents of URL**
   - URL: `https://<your-host>/body-image/upload`
   - Method: `POST`
   - Headers: `Authorization: Bearer <LIFELOG_API_TOKEN>`
   - Request Body: **Form**
     - `photo` — File — the Photo from step 1
     - `caption` — Text — the input from step 2
     - `device` — Text — `iPhone` (optional, for debugging)
4. **Show Notification** — text = the URL response

Set the Shortcut to a Back Tap (Settings → Accessibility → Touch → Back
Tap → Double Tap) so a one-handed double-tap captures + uploads.

## Verifying it works

```bash
# Health check on the geometry sidecar
docker compose exec face-geometry curl -s localhost:8000/health

# Smoke test the upload from a local file
curl -X POST https://<host>/body-image/upload \
  -H "Authorization: Bearer $LIFELOG_API_TOKEN" \
  -F "photo=@/path/to/test.jpg" \
  -F "caption=runbook smoke test"
```

Expected response:
```json
{
  "photo_id": "...",
  "storage_path": "raw/2026-05-15/<uuid>.jpg",
  "ratings_saved": 3,
  "sources": ["claude", "gpt4v", "geometry"],
  "failures": []
}
```

## Dashboard

Open in any browser:
```
https://<host>/body-image/dashboard?token=<LIFELOG_API_TOKEN>
```

The token query param is a convenience — the underlying JSON endpoints
require the proper `Authorization: Bearer` header (which the page's
inline JS does for you). Anyone with the URL can read; the token is
the same one you already trust on the iOS Shortcut, so don't share.

## Failure modes

| Symptom                                | Likely cause                                                   |
|----------------------------------------|----------------------------------------------------------------|
| `ratings_saved: 0`, all in `failures`  | Both LLM keys missing / quota; geometry sidecar down           |
| `geometry: no face detected`           | Photo too dark, profile shot, or face cropped — re-shoot       |
| `Storage upload failed: 401`           | `SUPABASE_SERVICE_KEY` wrong (you used the anon key)           |
| `Storage upload failed: 404`           | Bucket doesn't exist or `BODY_IMAGE_BUCKET` typo               |
| `claude: 529`                          | Anthropic overloaded — Promise.allSettled equivalent in service catches; re-rate later via DB |
| `gpt4v: rate_limit_exceeded`           | OpenAI tier 1 limit — same; the photo + other ratings are still saved |

## Cost

Roughly $0.03 per photo, ~$0.90/month at one upload/day. Verify current
pricing before increasing cadence.

## Re-rating an old photo

There's no `/reprocess` endpoint yet. To re-rate a specific photo (e.g.
after fixing a rater), drop into `python -m mcp_server` shell or run:

```python
from body_image import service, storage
from uuid import UUID
photo = service.get_photo("santi", UUID("..."))
# fetch the raw bytes back from storage via signed URL, then:
# service._run_raters_parallel(bytes) and INSERT manually.
```

If you find yourself doing this more than twice, add a `/body-image/reprocess/{id}` route.
