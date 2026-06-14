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

## ❌ What capture #1 did NOT contain (the blocker)

- **The food diary / history read.** No endpoint returns the list of logged meals
  by date. There was **no `firestore.googleapis.com` traffic at all.** The diary is
  almost certainly stored in **Firestore** (cloud-synced, references the storage
  photos) and was served from the on-device offline cache during the capture — so
  the collection path + document schema are unknown.
- **A login** (no `identitytoolkit`/`securetoken` calls), so the **Firebase Web API
  key** and a **refresh token** weren't captured.

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

## 📸 Capture #2 — exactly what to record (≈3 min)

Run mitmproxy (`mitmweb --listen-port 8080`, iPhone proxy → your Mac IP:8080, cert
trusted, iCloud Private Relay/VPN OFF — see the chat for the full setup). Then, in
the Cal AI app:

1. **Sign out and sign back in.** This captures the login → I get the **Firebase Web
   API key** (the `?key=` param on the `identitytoolkit.googleapis.com` / `securetoken`
   request) and a **refresh token** (in the login response).
2. **Open the diary / today view, pull-to-refresh, and scroll back several weeks**
   to days you haven't viewed recently. Signing out clears the offline cache, so the
   first diary load *must* hit the network — watch for **`firestore.googleapis.com`**
   flows (gRPC/`RunQuery`/`Listen`). That reveals the collection path + document schema.
3. Open one logged meal's detail.

Then **File → Save** the flows (filter to `firestore.googleapis.com` + `calai` +
`googleapis` hosts if you want to trim) to `~/Documents/LifeOS/calai_flows2` and tell
me the path.

> If `firestore.googleapis.com` still doesn't appear after a fresh login + diary
> refresh, the SDK may be using a gRPC channel mitmproxy can't see — tell me and
> I'll switch the capture approach (force Firestore REST/long-poll, or read the
> diary via the app's gRPC-Web endpoint).

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
