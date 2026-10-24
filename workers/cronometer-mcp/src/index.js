// cronometer-mcp: read and write the Cronometer diary over Cronometer's mobile
// JSON API (the surface the Android app talks to; unofficial, so treat every
// call as best-effort). Port of mcp_server/cronometer_write_tools.py plus a
// direct diary read.
//
// Reads that don't need to be live (trends, history, macros over time) belong
// in Supabase: the cronometer-sync edge function lands the diary there every
// 15 minutes.
//
// Bindings: KV  (session token + food cache)
// Secrets:  CRONOMETER_USERNAME, CRONOMETER_PASSWORD, MCP_TOKEN

import { DAY, ToolError, mcpHandler, num, str } from "../../lib/mcp.js";

const BASE = "https://mobile.cronometer.com";
const TZ = "America/New_York";
const APP_AUTH = { api: 3, os: "Android", build: "2807", flavour: "free" };
const SESSION_KEY = "session";
const FOOD_TTL_S = 30 * 86_400;

// diaryGroup. 0 lets the server guess from the clock, which mis-files a
// breakfast logged at 11am, so callers should name the meal.
const MEAL_GROUPS = { auto: 0, breakfast: 1, lunch: 2, dinner: 3, snack: 4, snacks: 4 };
const MEAL_NAMES = { 1: "breakfast", 2: "lunch", 3: "dinner", 4: "snack" };

// USDA nutrient ids; a food record's amounts are per 100 g.
const MACROS = { 208: "energy_kcal", 203: "protein_g", 204: "fat_g", 205: "carbs_g", 291: "fiber_g",
  269: "sugar_g", 606: "saturated_fat_g", 307: "sodium_mg", 306: "potassium_mg", 262: "caffeine_mg",
  221: "alcohol_g" };
const WRITE_IDS = { energy: 208, protein: 203, fat: 204, carbs: 205, fiber: 291, sugar: 269,
  sodium: 307, net_carbs: -1205 };

// ---- time --------------------------------------------------------------------

function nyNow() {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: TZ, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(new Date()).map((x) => [x.type, x.value]));
  return { day: `${p.year}-${p.month}-${p.day}`, time: `${+p.hour}:${+p.minute}:${+p.second}` };
}

/** Cronometer's wire format, YYYY-M-D. Zero-padded dates silently match no diary. */
const wireDay = (day) => day.split("-").map((x) => String(Number(x))).join("-");
const r1 = (v) => Math.round(v * 10) / 10;

// ---- client ------------------------------------------------------------------

class Cronometer {
  constructor(env) {
    this.env = env;
    this.userId = null;
    this.token = null;
  }

  async session() {
    if (this.token) return;
    const cached = await this.env.KV.get(SESSION_KEY, "json");
    if (cached?.token) {
      this.userId = cached.userId;
      this.token = cached.token;
      return;
    }
    await this.login();
  }

  // Cronometer locks the login endpoint after about four attempts in quick
  // succession, which is why the token lives in KV across requests.
  async login() {
    const { CRONOMETER_USERNAME: email, CRONOMETER_PASSWORD: password } = this.env;
    if (!email || !password) throw new ToolError("CRONOMETER_USERNAME / CRONOMETER_PASSWORD secrets are not set on this Worker");
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
      const msg = data.error || JSON.stringify(data).slice(0, 200);
      throw new ToolError(/too many attempts/i.test(msg)
        ? `Cronometer rate-limited logins: ${msg}. Wait several minutes before retrying.`
        : `Cronometer login rejected (${res.status}): ${msg}`);
    }
    this.userId = data.id;
    this.token = data.sessionKey;
    await this.env.KV.put(SESSION_KEY, JSON.stringify({ userId: this.userId, token: this.token }));
  }

  async reset() {
    this.token = null;
    await this.env.KV.delete(SESSION_KEY);
  }

  async post(endpoint, payload, retried = false) {
    await this.session();
    const res = await fetch(BASE + endpoint, {
      method: "POST",
      headers: { "content-type": "application/json", "user-agent": "Dart/3.9 (dart:io)" },
      body: JSON.stringify({ ...payload, auth: { userId: this.userId, token: this.token, ...APP_AUTH }, lastSeen: 0 }),
    });
    const data = await res.json().catch(() => null);
    const authFailed = res.status === 401 || res.status === 403 || data?.result === "FAILURE";
    if (authFailed && !retried) {
      await this.reset();
      return this.post(endpoint, payload, true);
    }
    if (!res.ok || authFailed || data === null) {
      throw new ToolError(`Cronometer ${endpoint} returned ${res.status}: ${JSON.stringify(data).slice(0, 300)}`);
    }
    return data;
  }

  async v3(method, path, body, retried = false) {
    await this.session();
    const res = await fetch(`${BASE}/api/v3/user/${this.userId}${path}`, {
      method,
      headers: {
        "x-crono-session": this.token, "x-crono-app-os": "android", "x-crono-app-build-number": "2807",
        "x-crono-app-version": "4.48.2", "content-type": "application/json; charset=utf-8",
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    if ((res.status === 401 || res.status === 403) && !retried) {
      await this.reset();
      return this.v3(method, path, body, true);
    }
    return res;
  }

  async food(id) {
    const key = `food:${id}`;
    const cached = await this.env.KV.get(key, "json");
    if (cached) return cached;
    const f = await this.post("/api/v2/get_food", { id, config: { call_version: 1 } });
    await this.env.KV.put(key, JSON.stringify(f), { expirationTtl: FOOD_TTL_S });
    return f;
  }

  diary = (day) => this.post("/api/v2/get_diary", { day: wireDay(day), config: { call_version: 1 } });
}

function macrosPer100(food) {
  const out = {};
  for (const n of food.nutrients ?? []) if (MACROS[n.id]) out[MACROS[n.id]] = Number(n.amount);
  return out;
}

function measures(food) {
  return (food.measures ?? []).filter((m) => !m.hidden).map((m) => ({
    measure_id: m.id, name: m.amount && m.amount !== 1 ? `${m.amount} ${m.name}` : m.name, grams: m.value,
  }));
}

// ---- tools -------------------------------------------------------------------

const tools = [
  {
    name: "search_foods",
    description: "Search Cronometer's food database by name. Returns food_id and a default measure_id to pass to log_food. Call this before log_food whenever you don't already have a food_id.",
    inputSchema: { type: "object", properties: {
      query: { type: "string", description: "Food name, e.g. 'greek yogurt 2%'." },
      limit: { type: "integer", minimum: 1, maximum: 50, default: 15 },
    }, required: ["query"] },
    annotations: { readOnlyHint: true },
    async handler(args, env) {
      const query = str(args, "query", { required: true }).trim();
      const limit = num(args, "limit", { min: 1, max: 50 }) ?? 15;
      const data = await new Cronometer(env).post("/api/v2/find_food", {
        query, tab: "ALL", sources: ["All"], config: { newSearch: true, newSpellcheck: true, call_version: 1 },
      });
      return { query, foods: (data.foods ?? []).slice(0, limit).map((f) => ({
        food_id: f.id, name: f.name, brand: f.brand ?? null, source: f.source ?? null,
        measure_id: f.measureId ?? null, measure: f.measureDisplayName ?? null,
        translation_id: f.translationId ?? 0,
      })) };
    },
  },
  {
    name: "get_food",
    description: "One food's macros per 100 g and its serving measures (measure_id, name, grams). Use it to turn 'two slices' into grams before log_food.",
    inputSchema: { type: "object", properties: { food_id: { type: "integer" } }, required: ["food_id"] },
    annotations: { readOnlyHint: true },
    async handler(args, env) {
      const id = num(args, "food_id", { required: true, min: 1 });
      const f = await new Cronometer(env).food(id);
      return { food_id: f.id, name: f.name, source: f.source ?? null, default_measure_id: f.defaultMeasureId ?? null,
        measures: measures(f), per_100g: macrosPer100(f) };
    },
  },
  {
    name: "get_diary",
    description: "The Cronometer diary for one day, read live: every entry (entry_id, food, grams, meal, time, macros) plus the day's totals. Defaults to today in America/New_York.",
    inputSchema: { type: "object", properties: {
      day: { type: "string", description: "YYYY-MM-DD. Default today (America/New_York)." },
    } },
    annotations: { readOnlyHint: true },
    async handler(args, env) {
      const day = str(args, "day", { pattern: DAY, desc: "YYYY-MM-DD" }) ?? nyNow().day;
      const api = new Cronometer(env);
      const d = await api.diary(day);
      const entries = [];
      const totals = { energy_kcal: 0, protein_g: 0, carbs_g: 0, fat_g: 0, fiber_g: 0 };
      for (const e of (d.diary ?? []).filter((x) => x.type === "Serving")) {
        const food = await api.food(e.foodId);
        const per = macrosPer100(food);
        const k = (Number(e.grams) || 0) / 100;
        const macros = Object.fromEntries(Object.entries(per).map(([n, v]) => [n, r1(v * k)]));
        for (const t of Object.keys(totals)) totals[t] += macros[t] ?? 0;
        entries.push({ entry_id: String(e.servingId), food_id: e.foodId, food: food.name, grams: e.grams,
          meal: MEAL_NAMES[Number(e.order ?? 0) >> 16] ?? null, time: e.time ?? null, ...macros });
      }
      return { day, entries, totals: Object.fromEntries(Object.entries(totals).map(([n, v]) => [n, r1(v)])) };
    },
  },
  {
    name: "log_food",
    description: "Add a food to the Cronometer diary. Give grams, or quantity + measure_id (from search_foods or get_food) and the grams are computed. Name the meal; 'auto' lets Cronometer guess from the clock and often guesses wrong.",
    inputSchema: { type: "object", properties: {
      food_id: { type: "integer" },
      grams: { type: "number", exclusiveMinimum: 0, description: "Amount in grams. Either this or quantity." },
      quantity: { type: "number", exclusiveMinimum: 0, description: "Number of measure_id servings, e.g. 2 for two slices." },
      measure_id: { type: "integer", description: "Measure from search_foods/get_food. Default: the food's default measure." },
      meal: { type: "string", enum: ["breakfast", "lunch", "dinner", "snack", "auto"], default: "auto" },
      day: { type: "string", description: "YYYY-MM-DD. Default today (America/New_York)." },
      time: { type: "string", description: "HH:MM local wall-clock time. Default now." },
      translation_id: { type: "integer", default: 0 },
    }, required: ["food_id"] },
    async handler(args, env) {
      const foodId = num(args, "food_id", { required: true, min: 1 });
      let grams = num(args, "grams", { min: 0.01 });
      const quantity = num(args, "quantity", { min: 0.01 });
      if ((grams === undefined) === (quantity === undefined)) throw new ToolError("give exactly one of grams or quantity");
      const meal = (str(args, "meal") ?? "auto").toLowerCase();
      if (!(meal in MEAL_GROUPS)) throw new ToolError(`meal must be one of ${Object.keys(MEAL_GROUPS).join(", ")}`);
      const now = nyNow();
      const day = str(args, "day", { pattern: DAY, desc: "YYYY-MM-DD" }) ?? now.day;
      const time = str(args, "time", { pattern: /^\d{1,2}:\d{2}$/, desc: "HH:MM" });

      const api = new Cronometer(env);
      const food = await api.food(foodId);
      const measureId = num(args, "measure_id") ?? food.defaultMeasureId ?? 0;
      if (quantity !== undefined) {
        const m = (food.measures ?? []).find((x) => x.id === measureId);
        if (!m) throw new ToolError(`measure ${measureId} is not a measure of food ${foodId}; call get_food for the list`);
        grams = quantity * m.value;
      }
      await api.session();
      const group = MEAL_GROUPS[meal];
      const resp = await api.post("/api/v2/add_serving", { serving: {
        // order: meal group in the high 16 bits, position within the meal in the low 16.
        order: (group << 16) | 1, day: wireDay(day), time: time ? `${+time.split(":")[0]}:${+time.split(":")[1]}:0` : now.time,
        offset: null, source: null, userId: api.userId, servingId: null, type: "Serving",
        foodId, measureId, grams, translationId: num(args, "translation_id") ?? 0,
      }, config: { call_version: 2 } });
      const per = macrosPer100(food);
      return { entry_id: String(resp.id ?? resp.servingId ?? ""), day, meal, food: food.name, grams: r1(grams),
        energy_kcal: r1((per.energy_kcal ?? 0) * grams / 100), protein_g: r1((per.protein_g ?? 0) * grams / 100) };
    },
  },
  {
    name: "create_custom_food",
    description: "Create a custom Cronometer food for a restaurant meal or recipe that isn't in the database. Macros are per serving; returns the food_id and measure_id for log_food.",
    inputSchema: { type: "object", properties: {
      name: { type: "string" },
      serving_grams: { type: "number", exclusiveMinimum: 0 },
      serving_name: { type: "string", default: "1 serving" },
      calories: { type: "number", minimum: 0 },
      protein_g: { type: "number", minimum: 0 },
      fat_g: { type: "number", minimum: 0 },
      carbs_g: { type: "number", minimum: 0 },
      fiber_g: { type: "number", minimum: 0, default: 0 },
      sugar_g: { type: "number", minimum: 0, default: 0 },
      sodium_mg: { type: "number", minimum: 0, default: 0 },
    }, required: ["name", "serving_grams", "calories", "protein_g", "fat_g", "carbs_g"] },
    async handler(args, env) {
      const name = str(args, "name", { required: true }).trim();
      const g = num(args, "serving_grams", { required: true, min: 0.01 });
      const v = (k) => num(args, k, { min: 0 }) ?? 0;
      const scale = 100 / g; // Cronometer stores per 100 g
      const p = (x) => Math.round(x * scale * 100) / 100;
      const carbs = v("carbs_g"), fiber = v("fiber_g");
      const nutrients = [
        { id: WRITE_IDS.energy, amount: p(num(args, "calories", { required: true, min: 0 })) },
        { id: WRITE_IDS.protein, amount: p(v("protein_g")) },
        { id: WRITE_IDS.fat, amount: p(v("fat_g")) },
        { id: WRITE_IDS.carbs, amount: p(carbs) },
        { id: WRITE_IDS.fiber, amount: p(fiber) },
        { id: WRITE_IDS.sugar, amount: p(v("sugar_g")) },
        { id: WRITE_IDS.sodium, amount: p(v("sodium_mg")) },
        // Derived fields the app sends alongside; the server expects them.
        { id: -203, amount: p(v("protein_g")) }, { id: -204, amount: p(v("fat_g")) },
        { id: -205, amount: p(carbs) }, { id: -221, amount: 0 },
        { id: WRITE_IDS.net_carbs, amount: p(Math.max(0, carbs - fiber)) },
      ];
      const api = new Cronometer(env);
      const created = await api.post("/api/v2/add_food", { data: {
        id: 0, name, category: 0, owner: null, retired: null, source: null, defaultMeasureId: 0,
        comments: null, alternateId: null,
        measures: [{ id: 0, name: str(args, "serving_name") ?? "1 serving", value: g, amount: 1, type: "Atomic" }],
        labelType: "AMERICAN_2016", nutrients, properties: {}, foodTags: [],
      }, config: { call_version: 1 } });
      if (!created.id) throw new ToolError(`create_custom_food: no id in response: ${JSON.stringify(created).slice(0, 200)}`);
      const food = await api.food(created.id);
      return { food_id: food.id ?? created.id, measure_id: food.defaultMeasureId ?? null, name: food.name ?? name, serving_grams: g };
    },
  },
  {
    name: "delete_food_entries",
    description: "Delete diary entries by entry_id (from log_food or get_diary). Pass the day the entries are on; it defaults to today.",
    inputSchema: { type: "object", properties: {
      entry_ids: { type: "array", items: { type: "string" }, minItems: 1 },
      day: { type: "string", description: "YYYY-MM-DD. Default today (America/New_York)." },
    }, required: ["entry_ids"] },
    annotations: { destructiveHint: true },
    async handler(args, env) {
      const ids = Array.isArray(args.entry_ids) ? args.entry_ids.map(String) : [];
      if (!ids.length) throw new ToolError("entry_ids must be a non-empty array");
      const day = str(args, "day", { pattern: DAY, desc: "YYYY-MM-DD" }) ?? nyNow().day;
      const api = new Cronometer(env);
      const diary = (await api.diary(day)).diary ?? [];
      // v3 DELETE wants the full serving objects, each with id = servingId.
      const doomed = diary.filter((e) => ids.includes(String(e.servingId))).map((e) => ({ ...e, id: e.servingId }));
      const removed = doomed.map((e) => String(e.servingId));
      if (doomed.length) {
        const res = await api.v3("DELETE", "/diary-entries", { diaryEntries: doomed });
        if (res.status !== 204) throw new ToolError(`delete failed: HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
      }
      return { day, removed, missing: ids.filter((i) => !removed.includes(i)) };
    },
  },
];

export default {
  fetch: mcpHandler({
    name: "cronometer",
    version: "1.0.0",
    instructions: "Live read/write access to Santi's Cronometer diary. Times and default days are America/New_York. For history and trends query Supabase (fact_nutrition_entry / fact_nutrition_daily, source 'cronometer'), which cronometer-sync refreshes every 15 minutes.",
    tools,
  }),
};
