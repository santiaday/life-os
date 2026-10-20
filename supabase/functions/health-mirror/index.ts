// health-mirror
//
// Receives Tributary's full HealthKit export and writes it verbatim to the
// hk_* tables (see migration 0062). Upserts by HealthKit UUID, applies deletes,
// never transforms values beyond what the app already canonicalised — except
// Health Records lab Observations, which are also written to fact_lab_result.
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

// ---- Health Records → life-os labs -------------------------------------
// FHIR lab Observations (DSTU2 or R4) become fact_lab_result rows with
// source 'apple_health'; unify/labs.py projects them into fact_lab_measurement
// and clusters them with Whoop / external panels from the same draw.

const slug = (x: string) =>
  x.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 80);

async function sha256(x: string) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(x));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Vendor biomarker ids (FK target of fact_lab_result) and canonical aliases.
let vocab: { at: number; ids: Set<string>; alias: Map<string, string> } | null = null;
async function loadVocab() {
  if (vocab && Date.now() - vocab.at < 5 * 60_000) return vocab;
  const ids = new Set<string>();
  const alias = new Map<string, string>();
  const a = await sb.from("dim_lab_biomarker").select("biomarker_id");
  if (a.error) throw new Error(`dim_lab_biomarker: ${a.error.message}`);
  for (const r of a.data ?? []) ids.add(r.biomarker_id);
  const b = await sb.from("dim_biomarker_alias").select("alias,biomarker_key");
  if (!b.error) for (const r of b.data ?? []) alias.set(r.alias, r.biomarker_key);
  vocab = { at: Date.now(), ids, alias };
  return vocab;
}

// Lab-method suffixes Quest/LabCorp append to analyte names.
const SUFFIXES = [/_lc_ms_ms$/, /_ms_ms$/, /_ms$/, /_calc(ulated)?$/, /_serum$/, /_plasma$/, /_whole_blood$/,
  /_blood$/, /_ia$/, /_eia$/, /_immunoassay$/, /_direct$/, /_automated$/, /_auto$/];

function candidates(slugged: string): string[] {
  const out = [slugged];
  let cur = slugged;
  for (let i = 0; i < 3; i++) {
    const next = SUFFIXES.reduce((x, re) => x.replace(re, ""), cur);
    if (next === cur || !next) break;
    out.push(next);
    cur = next;
  }
  return out;
}

/** Existing vendor id when one matches (directly or via a known alias), else
 *  the slug itself, registered so the FK holds and nothing is ever dropped. */
async function resolveBiomarkers(rows: Json[]) {
  const v = await loadVocab();
  const fresh = new Map<string, Json>();
  for (const r of rows) {
    const slugged = r.biomarker_id as string;
    let hit: string | null = null;
    for (const c of candidates(slugged)) {
      if (v.ids.has(c)) { hit = c; break; }
      const key = v.alias.get(c);
      if (key && v.ids.has(key)) { hit = key; break; }
    }
    if (hit) {
      r.biomarker_id = hit;
    } else if (!fresh.has(slugged)) {
      fresh.set(slugged, { biomarker_id: slugged, title: r.reference_ranges?.name ?? slugged, unit: r.unit,
        category: "Health Records", notes: `Auto-added from Apple Health Records (LOINC ${r.reference_ranges?.loinc ?? "?"})` });
    }
  }
  if (fresh.size) {
    const { error } = await sb.from("dim_lab_biomarker").upsert([...fresh.values()], { onConflict: "biomarker_id", ignoreDuplicates: true });
    if (error) throw new Error(`dim_lab_biomarker: ${error.message}`);
    for (const k of fresh.keys()) v.ids.add(k);
  }
  return fresh.size;
}

const OUT = new Set(["H", "HH", "HU", "L", "LL", "LU", "A", "AA", "POS", ">", "<"]);

async function labRow(o: Json, now: string): Promise<Json | null> {
  const d = o.data ?? {};
  if (!String(d.clinical_type ?? "").includes("LabResultRecord")) return null;
  const r = d.resource;
  if (!r || typeof r !== "object" || r.resourceType !== "Observation") return null;
  if (["entered-in-error", "cancelled"].includes(r.status)) return null;
  const when: string = r.effectiveDateTime ?? r.effectivePeriod?.start ?? r.issued ?? o.start;
  if (!when) return null;
  const codings: Json[] = r.code?.coding ?? [];
  const name: string = r.code?.text || codings.find((c) => c.display)?.display || d.display_name;
  if (!name) return null;
  const loinc = codings.find((c) => String(c.system ?? "").includes("loinc"))?.code ?? null;
  const vq = r.valueQuantity;
  const num = typeof vq?.value === "number" ? vq.value : null;
  const text = num === null
    ? (r.valueString ?? r.valueCodeableConcept?.text ?? r.valueCodeableConcept?.coding?.[0]?.display ??
      (r.valueBoolean !== undefined ? String(r.valueBoolean) : null) ?? (vq ? `${vq.comparator ?? ""}${vq.value ?? ""}` : null))
    : null;
  const rr = (r.referenceRange ?? [])[0] ?? {};
  const interp = Array.isArray(r.interpretation) ? r.interpretation[0] : r.interpretation;
  const flag = String(interp?.coding?.[0]?.code ?? "").toUpperCase();
  const provider = o.source ?? "Health Records";
  const date = String(when).slice(0, 10);
  return {
    test_id: `apple_health:${slug(provider)}:${date}`,
    test_date: date,
    biomarker_id: slug(name),
    value_numeric: num,
    value_text: text ?? (num !== null ? String(num) : null),
    unit: vq?.unit ?? vq?.code ?? null,
    status_type: OUT.has(flag) ? "OUT_OF_RANGE" : null,
    source: "apple_health",
    lab_provider: provider,
    reference_ranges: { low: rr.low?.value ?? null, high: rr.high?.value ?? null, text: rr.text ?? null,
      loinc, name, flag: flag || null, fhir_id: d.fhir_id ?? null, hk_uuid: o.uuid },
    source_row_hash: await sha256(`apple_health:${o.uuid}`),
    updated_at: now,
  };
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
      // Labs are derived data: a mapping problem is logged and reported but
      // never fails the request, so the app's mirror keeps moving.
      const labs = (await Promise.all(objects.map((o) => labRow(o, now)))).filter(Boolean) as Json[];
      if (labs.length) {
        try {
          out.new_biomarkers = await resolveBiomarkers(labs);
          out.labs = await upsert("fact_lab_result", labs, "test_id,biomarker_id");
        } catch (e) {
          console.error("labs", e);
          out.lab_errors = labs.length;
        }
      }
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
      for (let i = 0; i < deleted.length; i += 150) {
        // Kept small: PostgREST puts the id list in the URL.
        const chunk = deleted.slice(i, i + 150);
        for (const t of ["hk_sample", "hk_workout", "hk_object"]) {
          const { error } = await sb.from(t).delete().in("uuid", chunk);
          if (error) throw new Error(`${t} delete: ${error.message}`);
        }
        const hashes = await Promise.all(chunk.map((u) => sha256(`apple_health:${u}`)));
        const { error } = await sb.from("fact_lab_result").delete().in("source_row_hash", hashes);
        if (error) throw new Error(`fact_lab_result delete: ${error.message}`);
      }
      out.deleted = deleted.length;
    }
    return Response.json({ ok: true, ...out });
  } catch (e) {
    console.error(e);
    return Response.json({ ok: false, error: String(e) }, { status: 500 });
  }
});
