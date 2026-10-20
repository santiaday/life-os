// apple-health-ingest
//
// Receives Health Auto Export (iOS) "REST API" automation payloads (JSON,
// export version 2) and lands them in the life-os canonical tables:
//
//   metrics  -> fact_daily_metric     (source 'apple_watch', one row per day/metric)
//   sleep    -> fact_sleep_session    (one row per night, day = local wake date)
//   weight   -> fact_body_composition (method 'scale')
//   workouts -> fact_activity
//   whole batch -> raw_import (immutable, natural key = sha256 of body)
//
// Auth: header `x-ingest-key` is hashed and checked against ingest_token
// (name 'health_auto_export') by the check_ingest_token() RPC.
//
// HRV: Apple stores SDNN. Whoop stores RMSSD. They are different statistics,
// so Apple's lands as metric 'hrv_sdnn' and never overwrites 'hrv'.

import { createClient } from "jsr:@supabase/supabase-js@2";

const SOURCE = "apple_watch";
const sb = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } },
);

type Row = Record<string, unknown>;

// "2026-10-23 07:12:00 +0900" -> local date "2026-10-23" (phone's wall clock)
const localDay = (s: string) => s.slice(0, 10);
const toIso = (s: string) => {
  // "YYYY-MM-DD HH:MM:SS +HHMM" -> ISO
  const m = s.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\s*([+-]\d{2}):?(\d{2})$/);
  return m ? `${m[1]}T${m[2]}${m[3]}:${m[4]}` : new Date(s).toISOString();
};
const num = (v: unknown) => (typeof v === "number" && isFinite(v) ? v : typeof v === "string" && v !== "" && isFinite(+v) ? +v : null);

// HAE metric name -> [canonical metric, unit, aggregation, converter]
type Agg = "avg" | "sum" | "min" | "max" | "last";
const METRICS: Record<string, [string, string, Agg, (v: number, u: string) => number]> = {
  heart_rate_variability: ["hrv_sdnn", "ms", "avg", (v) => v],
  resting_heart_rate: ["resting_hr", "bpm", "avg", (v) => v],
  walking_heart_rate_average: ["walking_hr", "bpm", "avg", (v) => v],
  respiratory_rate: ["respiratory_rate", "rpm", "avg", (v) => v],
  blood_oxygen_saturation: ["spo2_avg", "pct", "avg", (v) => (v <= 1 ? v * 100 : v)],
  vo2_max: ["vo2_max", "ml/kg/min", "last", (v) => v],
  step_count: ["steps", "count", "sum", (v) => v],
  active_energy: ["calories_active", "kcal", "sum", (v, u) => (u.toLowerCase() === "kj" ? v / 4.184 : v)],
  basal_energy_burned: ["calories_bmr", "kcal", "sum", (v, u) => (u.toLowerCase() === "kj" ? v / 4.184 : v)],
  apple_exercise_time: ["active_minutes", "min", "sum", (v) => v],
  apple_stand_time: ["stand_minutes", "min", "sum", (v) => v],
  walking_running_distance: ["distance", "m", "sum", (v, u) => (u === "mi" ? v * 1609.344 : u === "km" ? v * 1000 : v)],
  flights_climbed: ["flights_climbed", "count", "sum", (v) => v],
  apple_sleeping_wrist_temperature: ["wrist_temp", "C", "avg", (v, u) => (u.toLowerCase().includes("f") ? (v - 32) / 1.8 : v)],
  time_in_daylight: ["daylight_minutes", "min", "sum", (v) => v],
  cardio_recovery: ["hr_recovery_1min", "bpm", "last", (v) => v],
};

const STRENGTH = /strength|weight|functional|core/i;
function activityType(name: string): [string, boolean] {
  const n = name.toLowerCase();
  if (STRENGTH.test(n)) return ["strength", true];
  if (/run/.test(n)) return ["run", false];
  if (/cycl|bike|spin/.test(n)) return ["cycle", false];
  if (/swim/.test(n)) return ["swim", false];
  if (/walk|hik/.test(n)) return ["walk", false];
  if (/hiit|cross|interval|row|elliptical|stair|boxing|kickbox|jump/.test(n)) return ["conditioning", false];
  if (/yoga|pilates|stretch|flexib|cooldown|mind/.test(n)) return ["mobility", false];
  if (/tennis|padel|pickle|squash|badminton|racquet/.test(n)) return ["racquet", false];
  if (/soccer|basketball|football|volleyball|golf|hockey|climb/.test(n)) return ["sport", false];
  return ["other", false];
}

const qty = (o: unknown): number | null => (o && typeof o === "object" ? num((o as Row).qty) : num(o));
const unitsOf = (o: unknown) => (o && typeof o === "object" ? String((o as Row).units ?? "") : "");

function toKg(v: number, u: string) {
  return u.toLowerCase().startsWith("lb") ? v * 0.45359237 : v;
}
function toMeters(v: number, u: string) {
  const x = u.toLowerCase();
  return x === "mi" ? v * 1609.344 : x === "km" ? v * 1000 : x === "yd" ? v * 0.9144 : v;
}
function toKcal(v: number, u: string) {
  return u.toLowerCase() === "kj" ? v / 4.184 : v;
}

async function upsert(table: string, rows: Row[], onConflict: string) {
  for (let i = 0; i < rows.length; i += 500) {
    const { error } = await sb.from(table).upsert(rows.slice(i, i + 500), { onConflict });
    if (error) throw new Error(`${table}: ${error.message}`);
  }
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("POST only", { status: 405 });
  const key = req.headers.get("x-ingest-key") ?? "";
  const { data: ok, error: authErr } = await sb.rpc("check_ingest_token", { p_name: "health_auto_export", p_token: key });
  if (authErr || ok !== true) return new Response("unauthorized", { status: 401 });

  const body = await req.text();
  let doc: Row;
  try {
    doc = JSON.parse(body);
  } catch {
    return new Response("bad json", { status: 400 });
  }
  const data = ((doc.data ?? doc) as Row) || {};
  const metrics = (data.metrics ?? []) as Row[];
  const workouts = (data.workouts ?? []) as Row[];

  const now = new Date().toISOString();
  const counts: Record<string, number> = {};

  // ---- raw landing ---------------------------------------------------------
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body))))
    .map((b) => b.toString(16).padStart(2, "0")).join("");
  if (body.length < 4_000_000) {
    const { error } = await sb.from("raw_import").upsert({
      source: SOURCE, entity: "hae_batch", natural_key: hash, occurred_on: null,
      payload: doc, file_name: "health_auto_export",
    }, { onConflict: "source,entity,natural_key", ignoreDuplicates: true });
    if (error) console.error("raw_import", error.message);
  }

  // ---- daily metrics -------------------------------------------------------
  const daily = new Map<string, { metric: string; unit: string; agg: Agg; vals: number[]; day: string }>();
  const bodyRows: Row[] = [];
  const sleepRows: Row[] = [];
  const sleepSegs = new Map<string, Row[]>(); // unaggregated sleep: keyed by wake day

  for (const m of metrics) {
    const name = String(m.name ?? "");
    const units = String(m.units ?? "");
    const pts = (m.data ?? []) as Row[];

    if (name === "weight_body_mass" || name === "body_fat_percentage" || name === "lean_body_mass") {
      for (const p of pts) {
        const v = num(p.qty);
        if (v == null || !p.date) continue;
        const ts = toIso(String(p.date));
        const uid = `${SOURCE}:${ts}`;
        let r = bodyRows.find((x) => x.measurement_uid === uid);
        if (!r) {
          r = { measurement_uid: uid, source: SOURCE, method: "scale", measured_at: ts, day: localDay(String(p.date)),
                payload: { origin: p.source ?? null }, updated_at: now };
          bodyRows.push(r);
        }
        if (name === "weight_body_mass") r.weight_kg = +toKg(v, units).toFixed(3);
        if (name === "body_fat_percentage") r.body_fat_pct = v <= 1 ? +(v * 100).toFixed(2) : v;
        if (name === "lean_body_mass") r.lean_mass_kg = +toKg(v, units).toFixed(3);
      }
      continue;
    }

    if (name === "sleep_analysis") {
      for (const p of pts) {
        if (p.sleepEnd || p.inBedEnd || p.totalSleep != null || p.asleep != null) {
          // aggregated: one record per night, values in hours
          const start = String(p.sleepStart ?? p.inBedStart ?? p.date);
          const end = String(p.sleepEnd ?? p.inBedEnd ?? p.date);
          const h = (k: string) => { const v = num(p[k]); return v == null ? null : Math.round(v * 3600); };
          const asleep = h("totalSleep") ?? h("asleep");
          const day = localDay(end);
          const inBedS = h("inBed");
          const tib = inBedS && inBedS > 0 ? inBedS : Math.round((Date.parse(toIso(end)) - Date.parse(toIso(start))) / 1000);
          sleepRows.push({
            sleep_uid: `${SOURCE}:${day}`, source: SOURCE, source_sleep_id: day,
            start_ts: toIso(start), end_ts: toIso(end), day, is_nap: false,
            time_in_bed_s: tib, asleep_s: asleep, awake_s: h("awake"),
            light_s: h("core"), deep_s: h("deep"), rem_s: h("rem"),
            efficiency_pct: asleep && tib ? +(100 * asleep / tib).toFixed(1) : null,
            is_primary: true, payload: { origin: p.source ?? null, raw: p }, updated_at: now,
          });
        } else if (p.startDate && p.endDate) {
          const day = localDay(String(p.endDate));
          (sleepSegs.get(day) ?? sleepSegs.set(day, []).get(day)!).push(p);
        }
      }
      continue;
    }

    if (name === "heart_rate") {
      for (const p of pts) {
        if (!p.date) continue;
        const day = localDay(String(p.date));
        for (const [k, metric, agg] of [["Avg", "avg_hr_day", "avg"], ["Max", "max_hr_day", "max"], ["Min", "min_hr", "min"]] as const) {
          const v = num(p[k]);
          if (v == null) continue;
          const key = `${day}|${metric}`;
          const e = daily.get(key) ?? { metric, unit: "bpm", agg: agg as Agg, vals: [], day };
          e.vals.push(v);
          daily.set(key, e);
        }
      }
      continue;
    }

    const spec = METRICS[name];
    if (!spec) continue;
    const [metric, unit, agg, conv] = spec;
    for (const p of pts) {
      const v = num(p.qty);
      if (v == null || !p.date) continue;
      const day = localDay(String(p.date));
      const key = `${day}|${metric}`;
      const e = daily.get(key) ?? { metric, unit, agg, vals: [], day };
      e.vals.push(conv(v, units));
      daily.set(key, e);
    }
  }

  // unaggregated sleep segments -> one session per wake day
  for (const [day, segs] of sleepSegs) {
    let start = Infinity, end = -Infinity;
    const s: Record<string, number> = {};
    for (const p of segs) {
      const a = Date.parse(toIso(String(p.startDate))), b = Date.parse(toIso(String(p.endDate)));
      start = Math.min(start, a); end = Math.max(end, b);
      const stage = String(p.value ?? "").toLowerCase();
      s[stage] = (s[stage] ?? 0) + (b - a) / 1000;
    }
    const asleep = (s.core ?? 0) + (s.deep ?? 0) + (s.rem ?? 0) + (s.asleep ?? 0) + (s.unspecified ?? 0);
    const tib = s["in bed"] ?? s.inbed ?? (end - start) / 1000;
    sleepRows.push({
      sleep_uid: `${SOURCE}:${day}`, source: SOURCE, source_sleep_id: day,
      start_ts: new Date(start).toISOString(), end_ts: new Date(end).toISOString(), day, is_nap: false,
      time_in_bed_s: Math.round(tib), asleep_s: Math.round(asleep), awake_s: Math.round(s.awake ?? 0),
      light_s: Math.round(s.core ?? 0), deep_s: Math.round(s.deep ?? 0), rem_s: Math.round(s.rem ?? 0),
      efficiency_pct: tib ? +(100 * asleep / tib).toFixed(1) : null,
      is_primary: true, payload: { segments: segs.length }, updated_at: now,
    });
  }

  const metricRows: Row[] = [];
  for (const e of daily.values()) {
    const v = e.agg === "sum" ? e.vals.reduce((a, b) => a + b, 0)
      : e.agg === "avg" ? e.vals.reduce((a, b) => a + b, 0) / e.vals.length
      : e.agg === "min" ? Math.min(...e.vals)
      : e.agg === "max" ? Math.max(...e.vals)
      : e.vals[e.vals.length - 1];
    metricRows.push({ day: e.day, metric: e.metric, source: SOURCE, value: +v.toFixed(3), unit: e.unit,
                      payload: { n: e.vals.length, agg: e.agg }, updated_at: now });
  }
  // calories_total = active + basal when both present
  const byDay = new Map<string, Row[]>();
  for (const r of metricRows) (byDay.get(r.day as string) ?? byDay.set(r.day as string, []).get(r.day as string)!).push(r);
  for (const [day, rs] of byDay) {
    const a = rs.find((r) => r.metric === "calories_active"), b = rs.find((r) => r.metric === "calories_bmr");
    if (a && b) metricRows.push({ day, metric: "calories_total", source: SOURCE,
      value: +((a.value as number) + (b.value as number)).toFixed(1), unit: "kcal", payload: { derived: "active+bmr" }, updated_at: now });
  }

  // ---- workouts ------------------------------------------------------------
  const actRows: Row[] = workouts.flatMap((w) => {
    const startRaw = String(w.start ?? ""), endRaw = String(w.end ?? "");
    if (!startRaw) return [];
    const name = String(w.name ?? "Workout");
    const [type, resist] = activityType(name);
    const id = String(w.id ?? `${startRaw}|${name}`);
    const start = toIso(startRaw);
    const end = endRaw ? toIso(endRaw) : null;
    const dur = num(w.duration) ?? (end ? (Date.parse(end) - Date.parse(start)) / 1000 : null);
    const hr = (w.heartRate ?? {}) as Row;
    const energy = w.activeEnergyBurned ?? w.activeEnergy;
    const dist = w.distance;
    return [{
      activity_uid: `${SOURCE}:${id}`, source: SOURCE, source_activity_id: id,
      start_ts: start, end_ts: end, day: localDay(startRaw), duration_s: dur,
      activity_type: type, vendor_type: name.toLowerCase(), name, is_resistance: resist,
      avg_hr: Math.round(qty(w.avgHeartRate) ?? qty(hr.avg) ?? 0) || null,
      max_hr: Math.round(qty(w.maxHeartRate) ?? qty(hr.max) ?? 0) || null,
      min_hr: Math.round(qty(hr.min) ?? 0) || null,
      kcal: qty(energy) != null ? +toKcal(qty(energy)!, unitsOf(energy)).toFixed(1) : null,
      distance_m: qty(dist) != null ? +toMeters(qty(dist)!, unitsOf(dist)).toFixed(1) : null,
      elevation_gain_m: qty(w.elevationUp) != null ? +toMeters(qty(w.elevationUp)!, unitsOf(w.elevationUp)).toFixed(1) : null,
      is_primary: true,
      payload: { location: w.location ?? null, intensity: w.intensity ?? null },
      updated_at: now,
    }];
  });

  try {
    if (metricRows.length) { await upsert("fact_daily_metric", metricRows, "day,metric,source"); counts.metrics = metricRows.length; }
    if (sleepRows.length) { await upsert("fact_sleep_session", sleepRows, "sleep_uid"); counts.sleep = sleepRows.length; }
    if (bodyRows.length) { await upsert("fact_body_composition", bodyRows, "measurement_uid"); counts.body = bodyRows.length; }
    if (actRows.length) { await upsert("fact_activity", actRows, "activity_uid"); counts.workouts = actRows.length; }
  } catch (e) {
    console.error(e);
    return new Response(JSON.stringify({ ok: false, error: String(e) }), { status: 500, headers: { "content-type": "application/json" } });
  }

  // keep freshness registry current
  const days = [...metricRows, ...sleepRows, ...bodyRows, ...actRows].map((r) => r.day as string).sort();
  if (days.length) {
    for (const [k, rows] of [["apple_watch.metric", metricRows], ["apple_watch.sleep", sleepRows], ["apple_watch.body", bodyRows], ["apple_watch.activity", actRows]] as const) {
      if (!rows.length) continue;
      const last = rows.map((r) => r.day as string).sort().at(-1);
      await sb.from("source_health").update({ last_row_day: last, last_checked_at: now }).eq("source_key", k).lt("last_row_day", last!);
      await sb.from("source_health").update({ last_row_day: last, last_checked_at: now }).eq("source_key", k).is("last_row_day", null);
    }
  }

  return new Response(JSON.stringify({ ok: true, ...counts, first_day: days[0] ?? null, last_day: days.at(-1) ?? null }),
    { headers: { "content-type": "application/json" } });
});
