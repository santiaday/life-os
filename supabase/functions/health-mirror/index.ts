// health-mirror
//
// Receives Tributary's full HealthKit export and writes it verbatim to the
// hk_* tables (see migration 0062). Upserts by HealthKit UUID, applies deletes,
// never transforms values beyond what the app already canonicalised.
// Auth: same x-ingest-key as apple-health-ingest.

import { createClient } from "jsr:@supabase/supabase-js@2";

type Json = Record<string, any>;
const sb = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, {
  auth: { persistSession: false },
});

async function upsert(table: string, rows: Json[], onConflict: string) {
  const keys = onConflict.split(",");
  const unique = new Map<string, Json>();
  for (const r of rows) unique.set(keys.map((k) => String(r[k])).join("|"), r);
  const list = [...unique.values()];
  for (let i = 0; i < list.length; i += 1000) {
    const { error } = await sb.from(table).upsert(list.slice(i, i + 1000), { onConflict });
    if (error) throw new Error(`${table}: ${error.message}`);
  }
  return list.length;
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("POST only", { status: 405 });
  const { data: ok } = await sb.rpc("check_ingest_token", {
    p_name: "health_auto_export", p_token: req.headers.get("x-ingest-key") ?? "",
  });
  if (ok !== true) return new Response("unauthorized", { status: 401 });

  let body: Json;
  try {
    body = await req.json();
  } catch {
    return new Response("bad json", { status: 400 });
  }
  const now = new Date().toISOString();
  const out: Record<string, number> = {};
  try {
    const samples = (body.samples ?? []) as Json[];
    if (samples.length) {
      out.samples = await upsert("hk_sample", samples.map((s) => ({
        uuid: s.uuid, type: s.type, kind: s.kind, value: s.value ?? null, unit: s.unit ?? null,
        label: s.label ?? null, start_ts: s.start, end_ts: s.end, source_name: s.source ?? null,
        source_bundle: s.bundle ?? null, device: s.device ?? null, metadata: s.meta ?? null, synced_at: now,
      })), "uuid");
    }
    const hourly = (body.hourly ?? []) as Json[];
    if (hourly.length) {
      out.hourly = await upsert("hk_hourly", hourly.map((h) => ({
        type: h.type, hour_start: h.hour, unit: h.unit, sum: h.sum ?? null, avg: h.avg ?? null,
        min: h.min ?? null, max: h.max ?? null, synced_at: now,
      })), "type,hour_start");
    }
    const workouts = (body.workouts ?? []) as Json[];
    if (workouts.length) {
      out.workouts = await upsert("hk_workout", workouts.map((w) => ({
        uuid: w.uuid, activity_type: w.activity_type, activity_name: w.activity_name, start_ts: w.start, end_ts: w.end,
        duration_s: w.duration_s ?? null, energy_kcal: w.energy_kcal ?? null, distance_m: w.distance_m ?? null,
        avg_hr: w.avg_hr ?? null, max_hr: w.max_hr ?? null, min_hr: w.min_hr ?? null,
        source_name: w.source ?? null, source_bundle: w.bundle ?? null, device: w.device ?? null,
        metadata: w.meta ?? null, statistics: w.statistics ?? null, activities: w.activities ?? null,
        events: w.events ?? null, hr_series: w.hr_series ?? null, route: w.route ?? null, synced_at: now,
      })), "uuid");
    }
    const objects = (body.objects ?? []) as Json[];
    if (objects.length) {
      out.objects = await upsert("hk_object", objects.map((o) => ({
        uuid: o.uuid, type: o.type, start_ts: o.start, end_ts: o.end, source_name: o.source ?? null,
        source_bundle: o.bundle ?? null, device: o.device ?? null, data: o.data ?? {}, synced_at: now,
      })), "uuid");
    }
    const summaries = (body.summaries ?? []) as Json[];
    if (summaries.length) {
      out.summaries = await upsert("hk_activity_summary", summaries.map((s) => ({ ...s, synced_at: now })), "day");
    }
    if (body.characteristics) {
      const { error } = await sb.from("hk_characteristics").upsert({ id: 1, data: body.characteristics, synced_at: now });
      if (error) throw new Error(`hk_characteristics: ${error.message}`);
      out.characteristics = 1;
    }
    const deleted = ((body.deleted ?? []) as string[]).filter(Boolean);
    if (deleted.length) {
      for (let i = 0; i < deleted.length; i += 500) {
        const chunk = deleted.slice(i, i + 500);
        for (const t of ["hk_sample", "hk_workout", "hk_object"]) {
          const { error } = await sb.from(t).delete().in("uuid", chunk);
          if (error) throw new Error(`${t} delete: ${error.message}`);
        }
      }
      out.deleted = deleted.length;
    }
    return Response.json({ ok: true, ...out });
  } catch (e) {
    console.error(e);
    return Response.json({ ok: false, error: String(e) }, { status: 500 });
  }
});
