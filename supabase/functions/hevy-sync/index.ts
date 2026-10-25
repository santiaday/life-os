// hevy-sync
//
// Pulls Hevy workouts (per-set reps / loads / RPE) into life-os. Replaces the
// droplet's ingest_hevy + unify/sources/hevy.py with identical row shapes:
//
//   raw_hevy_workout       immutable payload (upsert on hevy_workout_id)
//   dim_hevy_exercise      exercise template catalog (FK target for sets)
//   fact_strength_workout  / fact_strength_set     legacy Hevy-specific facts
//   fact_activity / _exercise / _set               canonical unified facts
//
// Incremental via GET /v1/workouts/events?since=<cursor>; first run (or ?full=1)
// walks /v1/workouts. Deletes in Hevy delete everywhere here.
// Invoked by pg_cron every 30 min with header x-cron-key (checked against
// ingest_token 'cron'). Hevy key: the HEVY_API_KEY function secret, or Vault
// (hevy_api_key, read through hevy_api_key(), see 0075).

import { createClient } from "jsr:@supabase/supabase-js@2";

type Row = Record<string, unknown>;
type Json = Record<string, any>;

const SOURCE = "hevy";
const HEVY = "https://api.hevyapp.com/v1";
const sb = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, {
  auth: { persistSession: false },
});

const nyFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" });
const nyDay = (iso: string) => nyFmt.format(new Date(iso));
const norm = (s: string | null | undefined) =>
  (s ?? "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
const SUFFIXES = ["_barbell", "_dumbbell", "_cable", "_machine", "_smith_machine", "_pulleymachine", "_bodyweight",
  "_kettlebell", "_band", "_l", "_r", "_left", "_right", "_alternating"];
const int = (v: unknown) => (v === null || v === undefined || v === "" || isNaN(Number(v)) ? null : Math.trunc(Number(v)));
const num = (v: unknown) => (v === null || v === undefined || v === "" || isNaN(Number(v)) ? null : Number(v));

function must<T>(r: { data: T; error: { message: string } | null }, what: string): T {
  if (r.error) throw new Error(`${what}: ${r.error.message}`);
  return r.data;
}

async function hevy(apiKey: string, path: string, params: Record<string, string | number> = {}): Promise<Json> {
  const url = new URL(HEVY + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
  for (let attempt = 0; attempt < 4; attempt++) {
    const res = await fetch(url, { headers: { "api-key": apiKey, accept: "application/json" } });
    if (res.status === 429 || res.status >= 500) {
      await new Promise((r) => setTimeout(r, 1000 * 2 ** attempt));
      continue;
    }
    if (!res.ok) throw new Error(`Hevy ${path} ${res.status}: ${(await res.text()).slice(0, 200)}`);
    return await res.json();
  }
  throw new Error(`Hevy ${path}: gave up after retries`);
}

async function pages(apiKey: string, path: string, key: string, pageSize: number, extra: Record<string, string> = {}) {
  const out: Json[] = [];
  for (let page = 1; page < 10_000; page++) {
    const data = await hevy(apiKey, path, { page, pageSize, ...extra });
    const items = (data[key] ?? []) as Json[];
    out.push(...items);
    const pageCount = Number(data.page_count ?? 0);
    if (!items.length || (pageCount && page >= pageCount) || items.length < pageSize) break;
  }
  return out;
}

class Resolver {
  constructor(private vocab: Map<string, string>, private hevyAlias: Map<string, string>) {}
  static async load() {
    const vocab = must(await sb.from("dim_exercise_vocab").select("alias, exercise_key"), "vocab") as Json[];
    const alias = must(await sb.from("dim_exercise_alias").select("vendor_key, exercise_key").eq("source", SOURCE), "alias") as Json[];
    return new Resolver(new Map(vocab.map((r) => [r.alias, r.exercise_key])), new Map(alias.map((r) => [r.vendor_key, r.exercise_key])));
  }
  /** Same order as unify/taxonomy.resolve_exercise: template id, label, suffix-stripped. */
  async resolve(label: string, templateId: string | null): Promise<string> {
    if (templateId && this.hevyAlias.has(templateId)) return this.hevyAlias.get(templateId)!;
    const cands = [norm(templateId), norm(label)].filter(Boolean);
    for (const c of [...cands]) for (const s of SUFFIXES) if (c.endsWith(s)) cands.push(c.slice(0, -s.length));
    let key: string | undefined;
    for (const c of cands) if ((key = this.vocab.get(c))) break;
    const known = !!key;
    key = key ?? (norm(label) || "unknown");
    if (!known) {
      await sb.from("dim_exercise").upsert(
        { exercise_key: key, display_name: label || key, notes: "auto-derived from vendor label; needs review" },
        { onConflict: "exercise_key", ignoreDuplicates: true },
      );
    }
    const vendorKey = templateId || norm(label);
    must(await sb.from("dim_exercise_alias").upsert(
      { source: SOURCE, vendor_key: vendorKey, exercise_key: key, vendor_label: label, confidence: known ? 1.0 : 0.4, updated_at: new Date().toISOString() },
      { onConflict: "source,vendor_key" },
    ), "alias upsert");
    this.hevyAlias.set(vendorKey, key);
    return key;
  }
}

async function ensureTemplates(apiKey: string, ids: Set<string>) {
  if (!ids.size) return;
  const have = must(await sb.from("dim_hevy_exercise").select("exercise_template_id").in("exercise_template_id", [...ids]), "templates") as Json[];
  const missing = [...ids].filter((id) => !have.some((h) => h.exercise_template_id === id));
  if (!missing.length) return;
  const all = await pages(apiKey, "/exercise_templates", "exercise_templates", 100);
  const rows = all.map((t) => ({
    exercise_template_id: t.id,
    title: t.title || "(untitled)",
    exercise_type: t.type ?? t.exercise_type ?? null,
    primary_muscle_group: t.primary_muscle_group ?? null,
    secondary_muscle_groups: t.secondary_muscle_groups ?? [],
    equipment: t.equipment ?? null,
    is_custom: !!t.is_custom,
    payload: t,
    fetched_at: new Date().toISOString(),
  }));
  for (let i = 0; i < rows.length; i += 500) {
    must(await sb.from("dim_hevy_exercise").upsert(rows.slice(i, i + 500), { onConflict: "exercise_template_id" }), "templates upsert");
  }
  // Templates Hevy no longer lists (deleted customs) still need an FK target.
  const stillMissing = missing.filter((id) => !all.some((t) => t.id === id));
  if (stillMissing.length) {
    must(await sb.from("dim_hevy_exercise").upsert(
      stillMissing.map((id) => ({ exercise_template_id: id, title: "(unknown template)", is_custom: true, fetched_at: new Date().toISOString() })),
      { onConflict: "exercise_template_id" },
    ), "placeholder templates");
  }
}

async function writeWorkout(w: Json, resolver: Resolver, now: string) {
  if (!w.start_time || !w.end_time) return 0;
  const id = String(w.id);
  const start = new Date(w.start_time).toISOString();
  const end = new Date(w.end_time).toISOString();
  const day = nyDay(start);

  const raw = must(await sb.from("raw_hevy_workout").upsert(
    { hevy_workout_id: id, payload: w, updated_at_src: w.updated_at ?? null, deleted: false, fetched_at: now },
    { onConflict: "hevy_workout_id" },
  ).select("id").single(), "raw upsert") as Json;

  // Flatten sets exactly like ingest_hevy.transforms.explode_workout_sets
  const sets: Row[] = [];
  for (const [i, ex] of ((w.exercises ?? []) as Json[]).entries()) {
    const exIndex = int(ex.index) ?? i;
    for (const s of (ex.sets ?? []) as Json[]) {
      sets.push({
        hevy_workout_id: id, exercise_index: exIndex, set_index: int(s.index) ?? 0,
        exercise_template_id: ex.exercise_template_id ?? null, exercise_title: ex.title || "(untitled)",
        set_type: s.type ?? null, weight_kg: num(s.weight_kg), reps: int(s.reps), rpe: num(s.rpe),
        distance_meters: num(s.distance_meters), duration_seconds: int(s.duration_seconds),
        superset_id: int(ex.superset_id), notes: ex.notes ?? null,
        workout_start_ts: start, workout_end_ts: end, updated_at: now, // day is generated from workout_start_ts
      });
    }
  }
  const working = sets.filter((s) => s.set_type !== "warmup" && s.weight_kg != null && s.reps != null);
  const totalReps = sets.reduce((a, s) => a + ((s.reps as number) ?? 0), 0);
  const volume = Math.round(working.reduce((a, s) => a + (s.weight_kg as number) * (s.reps as number), 0) * 100) / 100;
  const uniq = new Set(sets.map((s) => s.exercise_template_id).filter(Boolean)).size;
  const duration = Math.max(0, Math.round((Date.parse(end) - Date.parse(start)) / 1000));

  must(await sb.from("fact_strength_workout").upsert({
    hevy_workout_id: id, raw_id: raw.id, title: w.title ?? null, description: w.description ?? null,
    start_ts: start, end_ts: end, duration_seconds: duration, total_sets: sets.length, // day is generated from start_ts
    total_reps: totalReps, total_volume_kg: volume, unique_exercises: uniq, updated_at: now,
  }, { onConflict: "hevy_workout_id" }), "strength workout");
  must(await sb.from("fact_strength_set").delete().eq("hevy_workout_id", id), "strength sets delete");
  if (sets.length) must(await sb.from("fact_strength_set").insert(sets), "strength sets insert");

  // Canonical layer (same shape as unify/sources/hevy.py)
  const auid = `${SOURCE}:${id}`;
  must(await sb.from("fact_activity").upsert({
    activity_uid: auid, source: SOURCE, source_activity_id: id, start_ts: start, end_ts: end, day,
    duration_s: duration, activity_type: "strength", vendor_type: "strength", name: w.title ?? null,
    is_resistance: true, total_sets: sets.length, total_reps: totalReps, total_volume_kg: volume,
    unique_exercises: uniq, is_primary: true, payload: { description: w.description ?? "" }, updated_at: now,
  }, { onConflict: "activity_uid" }), "activity");
  must(await sb.from("fact_activity_exercise").delete().eq("activity_uid", auid), "exercises delete");
  must(await sb.from("fact_activity_set").delete().eq("activity_uid", auid), "sets delete");

  const byEx = new Map<number, Row[]>();
  for (const s of sets) {
    const k = s.exercise_index as number;
    if (!byEx.has(k)) byEx.set(k, []);
    byEx.get(k)!.push(s);
  }
  const exRows: Row[] = [], setRows: Row[] = [];
  for (const [ei, group] of [...byEx.entries()].sort((a, b) => a[0] - b[0])) {
    const label = group[0].exercise_title as string;
    const key = await resolver.resolve(label, (group[0].exercise_template_id as string) ?? null);
    const weights = group.map((s) => s.weight_kg as number).filter((v) => v);
    const vol = group.reduce((a, s) => a + ((s.reps as number) ?? 0) * ((s.weight_kg as number) ?? 0), 0);
    exRows.push({
      activity_uid: auid, exercise_index: ei, exercise_key: key, vendor_exercise: label, vendor_category: null,
      source: SOURCE, day, granularity: "set", set_count: group.length,
      total_reps: group.reduce((a, s) => a + ((s.reps as number) ?? 0), 0) || null,
      total_volume_kg: vol || null, max_weight_kg: weights.length ? Math.max(...weights) : null,
      duration_s: group.reduce((a, s) => a + ((s.duration_seconds as number) ?? 0), 0) || null,
      is_pr: false, updated_at: now,
    });
    for (const s of group) {
      setRows.push({
        activity_uid: auid, exercise_index: ei, set_index: s.set_index, exercise_key: key, vendor_exercise: label,
        source: SOURCE, day, set_type: s.set_type, reps: s.reps, weight_kg: s.weight_kg,
        duration_s: s.duration_seconds, distance_m: s.distance_meters, rpe: s.rpe, avg_hr: null, is_pr: false,
        volume_kg: ((s.reps as number) ?? 0) * ((s.weight_kg as number) ?? 0) || null, updated_at: now,
      });
    }
  }
  if (exRows.length) must(await sb.from("fact_activity_exercise").insert(exRows), "exercises insert");
  if (setRows.length) must(await sb.from("fact_activity_set").insert(setRows), "sets insert");
  return 1;
}

async function deleteWorkout(id: string) {
  await sb.from("fact_activity").delete().eq("activity_uid", `${SOURCE}:${id}`);
  await sb.from("fact_strength_set").delete().eq("hevy_workout_id", id);
  await sb.from("fact_strength_workout").delete().eq("hevy_workout_id", id);
  await sb.from("raw_hevy_workout").update({ deleted: true }).eq("hevy_workout_id", id);
}

Deno.serve(async (req) => {
  const { data: ok } = await sb.rpc("check_ingest_token", { p_name: "cron", p_token: req.headers.get("x-cron-key") ?? "" });
  if (ok !== true) return new Response("unauthorized", { status: 401 });
  const apiKey = Deno.env.get("HEVY_API_KEY") ?? ((await sb.rpc("hevy_api_key")).data as string | null);
  if (!apiKey) return Response.json({ ok: false, error: "Hevy API key is in neither the HEVY_API_KEY secret nor Vault" }, { status: 503 });

  const full = new URL(req.url).searchParams.get("full") === "1";
  const now = new Date().toISOString();
  try {
    const cur = must(await sb.from("ingest_cursor").select("cursor").eq("name", "hevy.workouts").maybeSingle(), "cursor") as Json | null;
    const since: string | null = full ? null : cur?.cursor ?? null;

    const updated: Json[] = [];
    const deleted: string[] = [];
    if (!since) {
      updated.push(...await pages(apiKey, "/workouts", "workouts", 10));
    } else {
      for (const e of await pages(apiKey, "/workouts/events", "events", 10, { since })) {
        if (e.type === "deleted") deleted.push(String(e.id));
        else if (e.workout) updated.push(e.workout);
      }
    }

    await ensureTemplates(apiKey, new Set(updated.flatMap((w) => (w.exercises ?? []).map((x: Json) => x.exercise_template_id).filter(Boolean))));
    const resolver = await Resolver.load();
    let written = 0;
    for (const w of updated) written += await writeWorkout(w, resolver, now);
    for (const id of deleted) await deleteWorkout(id);

    // Cursor: start of this run minus 5 min so a workout saved mid-run is re-read next time.
    must(await sb.from("ingest_cursor").upsert(
      { name: "hevy.workouts", cursor: new Date(Date.parse(now) - 5 * 60_000).toISOString(), updated_at: now },
      { onConflict: "name" },
    ), "cursor upsert");

    const days = updated.filter((w) => w.start_time).map((w) => nyDay(new Date(w.start_time).toISOString())).sort();
    const twoWeeksAgo = nyDay(new Date(Date.now() - 14 * 86_400_000).toISOString());
    const mergeSince = days.length && days[0] < twoWeeksAgo ? days[0] : twoWeeksAgo;
    const merged = must(await sb.rpc("merge_apple_watch_overlaps", { p_since: mergeSince }), "merge") as number | null;
    if (days.length) {
      await sb.from("source_health").update({ last_row_day: days.at(-1), last_checked_at: now })
        .eq("source_key", "hevy.activity").lt("last_row_day", days.at(-1)!);
    }
    return Response.json({ ok: true, mode: since ? "events" : "full", workouts: written, deleted: deleted.length, merged_apple_watch: merged ?? 0 });
  } catch (e) {
    console.error(e);
    return Response.json({ ok: false, error: String(e) }, { status: 500 });
  }
});
