// cronometer-sync
//
// Pulls the Cronometer diary into life-os over the mobile JSON API (the same
// surface the Android app uses), replacing the droplet's two read paths: the
// cronometer-export Go binary and ingest_cronometer/history.py.
//
//   raw_import (source cronometer, entity diary)  one row per day, verbatim
//   raw_import (source cronometer, entity food)   food records, per 100 g
//   fact_nutrition_entry (source cronometer)      one row per serving
//   fact_nutrition_daily (source cronometer)      per-day rollup
//
// Row shapes match unify/sources/cronometer.py, plus `meal`, which the diary
// encodes in the high 16 bits of `order` and the Python projection dropped.
// Entries deleted in Cronometer are deleted here for every day re-read.
//
// Biometric diary entries (Apple Health / Whoop passthrough) are kept in
// raw_import but not projected: Apple Health now arrives directly through
// apple-health-ingest, and projecting the passthrough too would land the same
// weigh-in twice under two sources.
//
// Invoked by pg_cron (job cronometer-sync, hourly) with header x-cron-key.
// ?days=N re-reads the last N days (default 3). ?start=YYYY-MM-DD&end=YYYY-MM-DD
// re-reads a range; keep it under ~90 days per call, because Cronometer answers
// a throttled request with HTTP 200 and an empty diary, so days are spaced
// DAY_DELAY_MS apart.
//
// Secrets: CRONOMETER_USERNAME, CRONOMETER_PASSWORD. The session token is
// cached in oauth_tokens (service 'cronometer_mobile') because Cronometer
// rate-limits logins after about four in quick succession.

import { createClient } from "jsr:@supabase/supabase-js@2";

type Json = Record<string, any>;

const SOURCE = "cronometer";
const BASE = "https://mobile.cronometer.com";
const TZ = "America/New_York";
const DAY_DELAY_MS = 1200;
const APP_AUTH = { api: 3, os: "Android", build: "2807", flavour: "free" };

const sb = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, {
  auth: { persistSession: false },
});

// USDA nutrient ids (amounts on a food record are per 100 g) -> column names.
const NUTRIENT_IDS: Record<number, string> = {
  208: "energy_kcal", 203: "protein_g", 204: "fat_g", 205: "carbs_g", 291: "fiber_g",
  269: "sugar_g", 606: "saturated_fat_g", 605: "trans_fat_g", 645: "mono_fat_g",
  646: "poly_fat_g", 601: "cholesterol_mg", 307: "sodium_mg", 306: "potassium_mg",
  262: "caffeine_mg", 221: "alcohol_g", 301: "calcium_mg", 303: "iron_mg",
  304: "magnesium_mg", 305: "phosphorus_mg", 309: "zinc_mg", 317: "selenium_ug",
  312: "copper_mg", 315: "manganese_mg", 320: "vitamin_a_ug", 401: "vitamin_c_mg",
  323: "vitamin_e_mg", 328: "vitamin_d_ug", 430: "vitamin_k_ug", 404: "thiamine_mg",
  405: "riboflavin_mg", 406: "niacin_mg", 410: "pantothenic_acid_mg", 415: "vitamin_b6_mg",
  417: "folate_ug", 418: "vitamin_b12_ug", 421: "choline_mg", 318: "vitamin_a_iu",
};
const ENTRY_FIELDS = ["energy_kcal", "protein_g", "carbs_g", "fiber_g", "sugar_g", "fat_g",
  "saturated_fat_g", "cholesterol_mg", "sodium_mg", "potassium_mg", "caffeine_mg", "alcohol_g"];
const SUM_FIELDS = ["energy_kcal", "protein_g", "carbs_g", "net_carbs_g", "fiber_g", "sugar_g",
  "fat_g", "saturated_fat_g", "cholesterol_mg", "sodium_mg", "potassium_mg", "caffeine_mg", "alcohol_g"];
const MEALS: Record<number, string> = { 1: "breakfast", 2: "lunch", 3: "dinner", 4: "snack" };

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const num = (v: unknown) => (v === null || v === undefined || v === "" || !isFinite(Number(v)) ? null : Number(v));
const r4 = (v: number) => Math.round(v * 10_000) / 10_000;
const r2 = (v: number) => Math.round(v * 100) / 100;

function must<T>(r: { data: T; error: { message: string } | null }, what: string): T {
  if (r.error) throw new Error(`${what}: ${r.error.message}`);
  return r.data;
}

// ---- dates -------------------------------------------------------------------
const nyFmt = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });
const nyToday = () => nyFmt.format(new Date());
const addDays = (day: string, n: number) => {
  const d = new Date(`${day}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
};
/** Cronometer's wire format: YYYY-M-D, no zero padding (padded dates silently match nothing). */
const wireDay = (day: string) => day.split("-").map((p) => String(Number(p))).join("-");

/** The instant at which America/New_York's wall clock reads `day time`. */
function nyInstant(day: string, time: string): string {
  const [h, m, s] = (/^\d{1,2}:\d{2}(:\d{2})?$/.test(time) ? time : "12:00:00").split(":").map(Number);
  const guess = Date.UTC(+day.slice(0, 4), +day.slice(5, 7) - 1, +day.slice(8, 10), h, m, s || 0);
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: TZ, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(new Date(guess)).map((p) => [p.type, p.value]));
  const asNy = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
  return new Date(guess - (asNy - guess)).toISOString();
}

// ---- Cronometer mobile API ---------------------------------------------------
class Cronometer {
  userId: number | null = null;
  token: string | null = null;

  async loadSession() {
    const row = must(await sb.from("oauth_tokens").select("access_token, metadata")
      .eq("service", "cronometer_mobile").maybeSingle(), "session load") as Json | null;
    if (row?.access_token && row.metadata?.user_id) {
      this.token = row.access_token;
      this.userId = Number(row.metadata.user_id);
    }
  }

  async login() {
    const email = Deno.env.get("CRONOMETER_USERNAME"), password = Deno.env.get("CRONOMETER_PASSWORD");
    if (!email || !password) throw new Error("CRONOMETER_USERNAME / CRONOMETER_PASSWORD secrets are not set");
    const res = await fetch(`${BASE}/api/v2/login`, {
      method: "POST",
      headers: { "content-type": "application/json", "user-agent": "Dart/3.9 (dart:io)" },
      body: JSON.stringify({
        email, password, timezone: TZ, userCode: null, build: "4.48.2 b2807-a",
        device: "Android 14 (SDK 34), Google Pixel 6 Pro", firebaseToken: "",
        features: { food_search_config: '{"newSearch": true, "newSpellcheck": true}', use_gpt_autofill: "true" },
        auth: { userId: null, token: null, ...APP_AUTH }, lastSeen: 0, config: { call_version: 2 },
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.sessionKey || !data.id) {
      throw new Error(`Cronometer login rejected (${res.status}): ${JSON.stringify(data).slice(0, 200)}`);
    }
    this.userId = Number(data.id);
    this.token = String(data.sessionKey);
    must(await sb.from("oauth_tokens").upsert({
      service: "cronometer_mobile", access_token: this.token, refresh_token: null,
      metadata: { user_id: this.userId }, updated_at: new Date().toISOString(),
    }, { onConflict: "service" }), "session save");
  }

  async post(endpoint: string, payload: Json, retried = false): Promise<Json> {
    if (!this.token) await this.login();
    const res = await fetch(BASE + endpoint, {
      method: "POST",
      headers: { "content-type": "application/json", "user-agent": "Dart/3.9 (dart:io)" },
      body: JSON.stringify({ ...payload, auth: { userId: this.userId, token: this.token, ...APP_AUTH }, lastSeen: 0 }),
    });
    const data = await res.json().catch(() => null);
    const failed = res.status === 401 || res.status === 403 || data?.result === "FAILURE";
    if (failed && !retried) {
      this.token = null;
      return this.post(endpoint, payload, true);
    }
    if (!res.ok || failed || data === null) {
      throw new Error(`Cronometer ${endpoint} ${res.status}: ${JSON.stringify(data).slice(0, 200)}`);
    }
    return data;
  }

  diary = (day: string) => this.post("/api/v2/get_diary", { day: wireDay(day), config: { call_version: 1 } });
  food = (id: number) => this.post("/api/v2/get_food", { id, config: { call_version: 1 } });
}

// ---- projection --------------------------------------------------------------
type Food = { name: string; nutrients: Record<string, number | null> };

function parseFood(payload: Json, id: number): Food {
  const nutrients: Record<string, number | null> = {};
  for (const n of payload.nutrients ?? []) {
    const name = NUTRIENT_IDS[n.id];
    if (name) nutrients[name] = num(n.amount);
  }
  return { name: payload.name || `food:${id}`, nutrients };
}

function entryRows(day: string, diary: Json[], foods: Map<number, Food>, now: string) {
  const rows: Json[] = [];
  const missing = new Set<number>();
  for (const e of diary) {
    if (e.type !== "Serving") continue;
    const fid = Number(e.foodId);
    const food = foods.get(fid);
    if (!food) { missing.add(fid); continue; }
    const grams = num(e.grams);
    const factor = (grams ?? 0) / 100;
    const clock = String(e.time || "12:00:00");
    const id = e.servingId ? String(e.servingId) : `s:${day}:${fid}:${clock}`;
    const row: Json = {
      entry_uid: `${SOURCE}:${id}`, source: SOURCE, source_entry_id: e.servingId ? String(e.servingId) : "",
      eaten_at: nyInstant(day, clock), day, meal: MEALS[Number(e.order ?? 0) >> 16] ?? null,
      food_name: food.name, brand: null, amount: grams, unit: "g", raw_id: null,
      payload: { foodId: fid, measureId: e.measureId ?? null },
      micros: Object.fromEntries(Object.entries(food.nutrients)
        .filter(([k, v]) => v !== null && !ENTRY_FIELDS.includes(k))
        .map(([k, v]) => [k, r4((v as number) * factor)])),
      updated_at: now,
    };
    for (const f of ENTRY_FIELDS) {
      const v = food.nutrients[f];
      row[f] = v === null || v === undefined ? null : r4(v * factor);
    }
    row.net_carbs_g = row.carbs_g === null ? null : r4(row.carbs_g - (row.fiber_g ?? 0));
    rows.push(row);
  }
  return { rows, missing };
}

/** unify.sources.nutrition._rollup: a nutrient nobody logged stays NULL, not 0. */
function rollup(day: string, rows: Json[], now: string): Json {
  const out: Json = { day, source: SOURCE, entry_count: rows.length, is_rollup: true, micros: {}, updated_at: now };
  for (const f of SUM_FIELDS) {
    const vals = rows.map((r) => r[f]).filter((v) => v !== null && v !== undefined) as number[];
    out[f] = vals.length ? r2(vals.reduce((a, b) => a + b, 0)) : null;
  }
  const times = rows.map((r) => r.eaten_at as string).sort();
  out.first_eaten_at = times[0];
  out.last_eaten_at = times.at(-1);
  return out;
}

// ---- handler -----------------------------------------------------------------
Deno.serve(async (req) => {
  const { data: ok } = await sb.rpc("check_ingest_token", { p_name: "cron", p_token: req.headers.get("x-cron-key") ?? "" });
  if (ok !== true) return new Response("unauthorized", { status: 401 });

  const url = new URL(req.url);
  const today = nyToday();
  const end = url.searchParams.get("end") ?? today;
  const start = url.searchParams.get("start") ?? addDays(end, 1 - Math.max(1, Number(url.searchParams.get("days") ?? 3)));
  if (!/^\d{4}-\d{2}-\d{2}$/.test(start) || !/^\d{4}-\d{2}-\d{2}$/.test(end) || start > end) {
    return Response.json({ ok: false, error: "start/end must be YYYY-MM-DD with start <= end" }, { status: 400 });
  }

  const now = new Date().toISOString();
  const run = must(await sb.from("ingestion_runs").insert({
    source: SOURCE, data_type: "mobile_diary", started_at: now, status: "running", metadata: { start, end },
  }).select("id").single(), "run insert") as Json;

  const api = new Cronometer();
  const counts = { days: 0, servings: 0, new_foods: 0, missing_foods: 0, entries_deleted: 0 };
  try {
    await api.loadSession();

    // Read every day first, so one throttled request can't leave half a window projected.
    const days: { day: string; diary: Json[]; summary: Json | null }[] = [];
    for (let day = start; day <= end; day = addDays(day, 1)) {
      if (days.length) await sleep(DAY_DELAY_MS);
      const d = await api.diary(day);
      days.push({ day, diary: (d.diary ?? []) as Json[], summary: d.summary ?? null });
    }
    counts.days = days.length;

    must(await sb.from("raw_import").upsert(days.map((d) => ({
      source: SOURCE, entity: "diary", natural_key: d.day, occurred_on: d.day,
      payload: { diary: d.diary, summary: d.summary }, file_name: "mobile_api", imported_at: now,
    })), { onConflict: "source,entity,natural_key" }), "raw diary upsert");

    // Food records, fetched once each and cached in raw_import.
    const ids = [...new Set(days.flatMap((d) => d.diary.filter((e) => e.type === "Serving").map((e) => Number(e.foodId))))];
    const foods = new Map<number, Food>();
    if (ids.length) {
      const cached = must(await sb.from("raw_import").select("natural_key, payload")
        .eq("source", SOURCE).eq("entity", "food").in("natural_key", ids.map(String)), "food cache") as Json[];
      for (const c of cached) foods.set(Number(c.natural_key), parseFood(c.payload, Number(c.natural_key)));
      for (const id of ids.filter((i) => !foods.has(i))) {
        const f = await api.food(id);
        foods.set(id, parseFood(f, id));
        must(await sb.from("raw_import").upsert({
          source: SOURCE, entity: "food", natural_key: String(id), occurred_on: null,
          payload: f, file_name: "mobile_api", imported_at: now,
        }, { onConflict: "source,entity,natural_key" }), "food upsert");
        counts.new_foods++;
      }
    }

    for (const d of days) {
      const { rows, missing } = entryRows(d.day, d.diary, foods, now);
      counts.servings += rows.length;
      counts.missing_foods += missing.size;
      if (rows.length) must(await sb.from("fact_nutrition_entry").upsert(rows, { onConflict: "entry_uid" }), "entry upsert");

      // Entries removed in Cronometer since the last read.
      const keep = rows.map((r) => r.entry_uid);
      let del = sb.from("fact_nutrition_entry").delete({ count: "exact" }).eq("source", SOURCE).eq("day", d.day);
      if (keep.length) del = del.not("entry_uid", "in", `(${keep.map((k) => `"${k}"`).join(",")})`);
      const gone = await del;
      if (gone.error) throw new Error(`entry delete: ${gone.error.message}`);
      counts.entries_deleted += gone.count ?? 0;

      if (rows.length) {
        must(await sb.from("fact_nutrition_daily").upsert(rollup(d.day, rows, now), { onConflict: "day,source" }), "daily upsert");
      } else {
        must(await sb.from("fact_nutrition_daily").delete().eq("source", SOURCE).eq("day", d.day), "daily delete");
      }
    }

    const lastFood = days.filter((d) => d.diary.some((e) => e.type === "Serving")).map((d) => d.day).at(-1);
    if (lastFood) {
      await sb.from("source_health").update({ last_row_day: lastFood, last_checked_at: now })
        .eq("source_key", "cronometer.nutrition").lt("last_row_day", lastFood);
    }

    await sb.from("ingestion_runs").update({
      finished_at: new Date().toISOString(), status: "success",
      rows_fetched: counts.days, rows_upserted: counts.servings, metadata: { start, end, ...counts },
    }).eq("id", run.id);
    return Response.json({ ok: true, start, end, ...counts });
  } catch (e) {
    console.error(e);
    await sb.from("ingestion_runs").update({
      finished_at: new Date().toISOString(), status: "failure", error_message: String(e).slice(0, 2000),
      metadata: { start, end, ...counts },
    }).eq("id", run.id);
    return Response.json({ ok: false, error: String(e), ...counts }, { status: 500 });
  }
});
