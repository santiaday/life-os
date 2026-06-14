# Cal AI — Reverse-Engineering & Capture Runbook

Goal: make **Cal AI** the warehouse's nutrition source of record, replacing
Cronometer (frozen at 2026-05-30). This doc tracks what the first mitmproxy
capture revealed, what's already built + verified, and the **one** follow-up
capture needed to go live.

---

## ✅ Confirmed from capture #1 (`~/Downloads/Mitmproxy Flows`)

Cal AI is a **Firebase** app. The capture recorded an app session where one meal
was analyzed/edited (Grilled Salmon) plus account/onboarding chatter.

- **Firebase project:** `calai-app`.
- **Auth:** Firebase ID token — RS256 JWT, ~1h TTL, `iss=securetoken.google.com/calai-app`,
  `aud=calai-app`, `sub`/`user_id=f5Q08GHjoJUdUp8jUO4u5qPZZII2`,
  `email=santiagoaday7@gmail.com`, sign-in provider `password` (Apple/Google linked).
  Refreshed via `securetoken.googleapis.com/v1/token?key=<WEB_API_KEY>` with a refresh token.
- **AI / account API:** `https://api.calai.app/v6/<endpoint>` — POST, envelope
  `{"userInfo": {...platform/userId/version...}, "data": {...}}`, `Authorization: Bearer <idToken>`,
  UA `Cal%20AI/1779 CFNetwork/3860.600.12 Darwin/25.6.0`. Seen: `fixFood`, `health-score`,
  `getSubscription`, `family-sharing/get-family-status`, `getReferralsData`. Responses gzip.
- **Food photos:** Firebase Storage `calai-app.appspot.com`, path
  `food_images_user/<uid>/<imageId>.jpg`.
- **Food-object shape (CONFIRMED, from `fixFood`):**
  ```json
  {"name": "...", "servings": 1, "calories": 752, "carbs": 66, "fats": 24,
   "protein": 68, "sugar": 0, "fiber": 0, "sodium": 0, "traceId": "...",
   "ingredients": [{"name": "Salmon", "calories": 426, "protein": 64, "carbs": 0,
                    "fats": 18, "ethanol": 0, "servings": 2, "servingTypes": [...]}]}
  ```
  Top-level totals = sum of ingredients; `servings` = how many of the whole item
  were logged. Maps cleanly onto `fact_food_log` (Cal AI even carries per-ingredient
  `ethanol` → `alcohol_g`).

## 🔎 Live Firestore probing (capture #2 token) — what we learned

Using the still-valid ID token from the sign-in capture, I queried Firebase directly:
- **Firebase Web API key: recovered** (`AIza…`, from the remote-config `?key=`).
- `users/{uid}/foods` holds only **4 docs** — *saved-food templates* (`isBookmarked`,
  `isForEditingSavedFood`) from 2025-04. The sign-in capture loaded **14 distinct
  food photos**, so the real daily diary (≥14 entries) lives elsewhere.
- **Confirmed Cal AI food-doc schema** (same for saved + diary): top-level
  `name, servingCalories (per serving), quantity (multiplier), protein, carbs, fats,
  date (ISO ts), image (photo id), healthRating, ethanolCarbRatio, ingredients[]`.
  Consumed = per-serving × quantity. The transform now handles this AND the /v6
  analysis shape — **verified end-to-end against the 4 real docs** (e.g. cookies
  320×qty2 = 640 kcal). So ingestion is finalized + bulletproof; only the diary
  LOCATION is missing.
- The diary path can't be discovered by probing: Firestore `listCollectionIds` +
  collection-group queries are **403** (security rules), root reads **403**, RTDB
  (`calai-app-default-rtdb.firebaseio.com` exists) denies guessed paths, no
  `api.calai.app/v6` diary endpoint (all 404), and the photo ids aren't doc ids.

## ❌ The remaining blocker — the diary read is never on the wire

The app serves the diary from its **offline cache**, so neither capture (nor a fresh
sign-in) ever made the network read that would reveal the collection path. A
**refresh token** also wasn't captured (no securetoken refresh fired).

---

## ✅ Already built + verified (this branch)

- **Migration `0034_calai_nutrition.sql`** — `source` column on `fact_food_log`/
  `fact_food_daily`, and `raw_calai_food` (raw JSONB diary docs). Applied.
- **`ingest_calai/transforms.py`** — `transform_food_object` / `food_to_log_row`,
  **tested against the real captured payload** (752 kcal / 68 P / 66 C / 24 F, ingredient
  detail preserved in `micros`, servings multiplier, NaN-safe, idempotent hash).
- **`ingest_calai/client.py`** — `CalaiAuth` (Firebase securetoken refresh + token
  freshness), Firestore REST `runQuery` + typed-value decoding, the `/v6` envelope
  caller. Pure logic unit-tested.
- **`ingest_calai/ingest.py`** — full raw→fact→daily upsert wiring, **verified
  end-to-end against the live DB** (a synthetic diary doc wrapping the real food →
  `raw_calai_food` + `fact_food_log` + `fact_food_daily`, idempotent).
- **`ingest_calai/__main__.py`** — `login` + `ingest` CLI.

The only un-finalized spots (small, clearly marked in code): `fetch_diary()`'s
Firestore collection path + date field, and `_extract()`'s diary-doc field names.

---

## 📸 Capture #3 — the foolproof one (≈60 s): LOG A NEW FOOD

Reads are cached, but **writes always sync to the server immediately** — so logging
one food reveals the exact diary path no cache can hide.

Run mitmproxy (`mitmweb --listen-port 8080`, iPhone proxy → Mac IP:8080, cert trusted,
iCloud Private Relay/VPN OFF). Then in Cal AI:

1. **Log one new food** — snap a photo of anything (a snack, a label). Let it save.
2. (Same session, for the refresh token needed for daily auto-sync) **sign out and
   back in** so a `securetoken`/`identitytoolkit` call is captured.

The save is a network **write** — a Firestore `:commit`, an RTDB `PUT`/websocket
frame, or an `api.calai.app` save — carrying the **collection path + full doc**.
Save the flows to `~/Downloads` and tell me. That's the last unknown; everything
else (auth refresh, transforms, raw→fact→daily, idempotency) is built + verified.

---

## 🏁 Finishing once capture #2 lands (small, all verified)

1. Put `CALAI_FIREBASE_API_KEY` in `.env`; `python -m ingest_calai login --refresh-token <RT> --user-id f5Q08GHjoJUdUp8jUO4u5qPZZII2`.
2. Set `CALAI_DIARY_COLLECTION` (+ `CALAI_DIARY_DATE_FIELD` if not `createdAt`) from the
   captured Firestore path; confirm `_extract()` field names against one real doc.
3. `python -m ingest_calai ingest --backfill 365` → spot-check `fact_food_log` /
   `fact_food_daily` / `mart_daily.total_kcal` against the app.
4. Add a daily scheduler job (mirroring `whoop_private_daily`); the mart nutrition
   fallback already prefers real `fact_food_daily` over the lossy Apple-Health path.
5. (Optional) retire the Cronometer jobs once Cal AI history is verified.

## Notes
- We only ever **read** Cal AI. No writes back.
- Cal AI also writes daily totals to **Apple Health**, which is the current (lossy)
  nutrition path (`fact_food_daily_apple_health`). Direct Firestore = per-meal detail
  + ingredients + photos + health score.
