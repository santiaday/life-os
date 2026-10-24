// withings-ingest
//
// Pulls Withings scale weigh-ins straight from the Withings cloud, so a weigh-in
// lands without opening the Withings app (which is the only thing that pushes
// it into Apple Health).
//
//   HEAD /withings-ingest                 Withings' reachability check on subscribe
//   POST /withings-ingest                 Withings notification (appli=1, body metrics)
//   GET  /withings-ingest/connect?state=  one-time link: redirects to Withings sign-in
//   GET  /withings-ingest/callback        OAuth return: stores tokens, subscribes, back-fills 90 days
//   POST /withings-ingest/sync            pg_cron safety net (x-cron-key); everything changed since the last run
//
// Writes raw_import (source withings, entity measuregrp) and
// fact_body_composition (source withings, one row per weigh-in).
//
// Credentials: WITHINGS_CLIENT_ID / WITHINGS_CLIENT_SECRET edge secrets, or the
// same values in Vault (withings_client_id / withings_client_secret). Tokens live in
// oauth_tokens (service 'withings'); the refresh token rotates on every refresh.
// The connect state is a one-time value in ingest_cursor ('withings.connect_state').

import { createClient } from "jsr:@supabase/supabase-js@2";

type Json = Record<string, any>;

const SOURCE = "withings";
const API = "https://wbsapi.withings.net";
const BASE = "https://dqnrcarouldholuqfxbb.supabase.co/functions/v1/withings-ingest";
const REDIRECT = `${BASE}/callback`;
const TZ = "America/New_York";
const BACKFILL_DAYS = 90;
// Withings meastype -> fact_body_composition column (values are kg or %).
const TYPES: Record<number, string> = {
  1: "weight_kg", 5: "lean_mass_kg", 6: "body_fat_pct", 8: "fat_mass_kg",
  76: "muscle_mass_kg", 77: "hydration_kg", 88: "bone_mineral_kg",
};

const sb = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, {
  auth: { persistSession: false },
});

function must<T>(r: { data: T; error: { message: string } | null }, what: string): T {
  if (r.error) throw new Error(`${what}: ${r.error.message}`);
  return r.data;
}

const nyFmt = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });
const r3 = (v: number) => Math.round(v * 1000) / 1000;

async function withings(path: string, params: Record<string, string | number>, token?: string): Promise<Json> {
  const res = await fetch(API + path, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded", ...(token ? { authorization: `Bearer ${token}` } : {}) },
    body: new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)])),
  });
  const data = await res.json().catch(() => null);
  if (!res.ok || !data || data.status !== 0) {
    throw new Error(`Withings ${path} ${params.action}: HTTP ${res.status} status ${data?.status} ${JSON.stringify(data?.error ?? "").slice(0, 200)}`);
  }
  return data.body;
}

// ---- tokens --------------------------------------------------------------------
/** Edge secrets first, then Vault (withings_client_id / withings_client_secret, see 0072). */
async function clientCreds() {
  const vault = async (name: string) => (must(await sb.rpc("withings_secret", { p_name: name }), "vault") as string | null) ?? undefined;
  const id = Deno.env.get("WITHINGS_CLIENT_ID") ?? await vault("withings_client_id");
  const secret = Deno.env.get("WITHINGS_CLIENT_SECRET") ?? await vault("withings_client_secret");
  if (!id || !secret) throw new Error("Withings client id / secret are in neither the edge secrets nor Vault");
  return { client_id: id, client_secret: secret };
}

async function saveTokens(body: Json) {
  must(await sb.from("oauth_tokens").upsert({
    service: SOURCE, access_token: body.access_token, refresh_token: body.refresh_token,
    expires_at: new Date(Date.now() + Number(body.expires_in ?? 10800) * 1000).toISOString(),
    metadata: { userid: String(body.userid), scope: body.scope ?? null }, updated_at: new Date().toISOString(),
  }, { onConflict: "service" }), "token save");
}

/** A valid access token, refreshed when within 5 minutes of expiry. */
async function accessToken(): Promise<{ token: string; userid: string }> {
  const row = must(await sb.from("oauth_tokens").select("access_token, refresh_token, expires_at, metadata")
    .eq("service", SOURCE).maybeSingle(), "token load") as Json | null;
  if (!row?.refresh_token) throw new Error("Withings is not connected yet; open the connect link");
  if (row.access_token && Date.parse(row.expires_at) - Date.now() > 5 * 60_000) {
    return { token: row.access_token, userid: String(row.metadata?.userid) };
  }
  const body = await withings("/v2/oauth2", {
    action: "requesttoken", grant_type: "refresh_token", refresh_token: row.refresh_token, ...(await clientCreds()),
  });
  await saveTokens(body);
  return { token: body.access_token, userid: String(body.userid) };
}

// ---- measurements -----------------------------------------------------------------
function rowFor(g: Json, now: string): Json {
  const v: Record<string, number> = {};
  for (const m of g.measures ?? []) {
    const col = TYPES[m.type];
    if (col) v[col] = Number(m.value) * 10 ** Number(m.unit);
  }
  const at = new Date(Number(g.date) * 1000);
  // attrib 0 = device measure attributed to the user, 1 = device measure Withings
  // couldn't attribute, 2/4 = typed in by hand.
  const manual = g.attrib === 2 || g.attrib === 4;
  return {
    measurement_uid: `${SOURCE}:${g.grpid}`, source: SOURCE, method: manual ? "manual" : "scale",
    measured_at: at.toISOString(), day: nyFmt.format(at),
    weight_kg: v.weight_kg != null ? r3(v.weight_kg) : null,
    body_fat_pct: v.body_fat_pct != null ? r3(v.body_fat_pct) : null,
    fat_mass_kg: v.fat_mass_kg != null ? r3(v.fat_mass_kg) : null,
    lean_mass_kg: v.lean_mass_kg != null ? r3(v.lean_mass_kg) : null,
    muscle_mass_kg: v.muscle_mass_kg != null ? r3(v.muscle_mass_kg) : null,
    bone_mineral_kg: v.bone_mineral_kg != null ? r3(v.bone_mineral_kg) : null,
    body_water_pct: v.hydration_kg != null && v.weight_kg ? r3(100 * v.hydration_kg / v.weight_kg) : null,
    confidence: g.attrib === 0 ? 0.9 : 0.6,
    payload: { grpid: g.grpid, attrib: g.attrib, deviceid: g.deviceid ?? null, model: g.model ?? null, modified: g.modified ?? null },
    updated_at: now,
  };
}

/** Fetches every measure group in the window (or changed since `lastupdate`) and upserts it. */
async function pull(window: { startdate?: number; enddate?: number; lastupdate?: number }) {
  const { token } = await accessToken();
  const now = new Date().toISOString();
  const groups: Json[] = [];
  let offset: number | undefined;
  do {
    const body = await withings("/measure", {
      action: "getmeas", meastypes: Object.keys(TYPES).join(","), category: 1,
      ...Object.fromEntries(Object.entries(window).filter(([, x]) => x !== undefined)) as Record<string, number>,
      ...(offset ? { offset } : {}),
    }, token);
    groups.push(...(body.measuregrps ?? []));
    offset = body.more ? Number(body.offset) : undefined;
  } while (offset);

  if (groups.length) {
    must(await sb.from("raw_import").upsert(groups.map((g) => ({
      source: SOURCE, entity: "measuregrp", natural_key: String(g.grpid),
      occurred_on: nyFmt.format(new Date(Number(g.date) * 1000)), payload: g, file_name: "withings_api", imported_at: now,
    })), { onConflict: "source,entity,natural_key" }), "raw upsert");
    const rows = groups.map((g) => rowFor(g, now)).filter((r) => r.weight_kg != null || r.body_fat_pct != null);
    if (rows.length) must(await sb.from("fact_body_composition").upsert(rows, { onConflict: "measurement_uid" }), "body upsert");
    const last = rows.map((r) => r.day as string).sort().at(-1);
    if (last) {
      await sb.from("source_health").update({ last_row_day: last, last_checked_at: now })
        .eq("source_key", "withings.body").or(`last_row_day.lt.${last},last_row_day.is.null`);
    }
    return { groups: groups.length, rows: rows.length };
  }
  return { groups: 0, rows: 0 };
}

async function logRun(dataType: string, fn: () => Promise<Json>) {
  const started = new Date().toISOString();
  const run = must(await sb.from("ingestion_runs").insert({ source: SOURCE, data_type: dataType, started_at: started, status: "running" })
    .select("id").single(), "run insert") as Json;
  try {
    const out = await fn();
    await sb.from("ingestion_runs").update({ status: "success", finished_at: new Date().toISOString(),
      rows_upserted: out.rows ?? null, metadata: out }).eq("id", run.id);
    return out;
  } catch (e) {
    await sb.from("ingestion_runs").update({ status: "failure", finished_at: new Date().toISOString(),
      error_message: String(e).slice(0, 2000) }).eq("id", run.id);
    throw e;
  }
}

// ---- connect -----------------------------------------------------------------------
const STATE_KEY = "withings.connect_state";
const html = (msg: string, status = 200) =>
  new Response(`<!doctype html><meta name="viewport" content="width=device-width"><body style="font:16px system-ui;padding:24px">${msg}</body>`,
    { status, headers: { "content-type": "text/html; charset=utf-8" } });

async function checkState(state: string | null) {
  const row = must(await sb.from("ingest_cursor").select("cursor, updated_at").eq("name", STATE_KEY).maybeSingle(), "state") as Json | null;
  return !!state && row?.cursor === state && Date.now() - Date.parse(row.updated_at) < 60 * 60_000;
}

Deno.serve(async (req) => {
  const url = new URL(req.url);
  const route = url.pathname.replace(/^.*\/withings-ingest/, "").replace(/\/+$/, "");

  try {
    if (req.method === "HEAD") return new Response(null, { status: 200 });

    if (req.method === "GET" && route === "/connect") {
      const state = url.searchParams.get("state");
      if (!(await checkState(state))) return html("This connect link has expired. Ask Claude for a new one.", 403);
      const auth = new URL("https://account.withings.com/oauth2_user/authorize2");
      auth.search = new URLSearchParams({ response_type: "code", client_id: (await clientCreds()).client_id,
        scope: "user.metrics", redirect_uri: REDIRECT, state: state! }).toString();
      return Response.redirect(auth.toString(), 302);
    }

    if (req.method === "GET" && route === "/callback") {
      const state = url.searchParams.get("state"), code = url.searchParams.get("code");
      if (!code || !(await checkState(state))) return html("This sign-in link has expired. Ask Claude for a new one.", 403);
      await sb.from("ingest_cursor").update({ cursor: null, updated_at: new Date().toISOString() }).eq("name", STATE_KEY);
      const body = await withings("/v2/oauth2", {
        action: "requesttoken", grant_type: "authorization_code", code, redirect_uri: REDIRECT, ...(await clientCreds()),
      });
      await saveTokens(body);
      await withings("/notify", { action: "subscribe", callbackurl: BASE, appli: 1 }, body.access_token);
      const out = await logRun("backfill", () =>
        pull({ startdate: Math.floor(Date.now() / 1000) - BACKFILL_DAYS * 86400, enddate: Math.floor(Date.now() / 1000) }));
      must(await sb.from("ingest_cursor").upsert({ name: "withings.lastupdate", cursor: String(Math.floor(Date.now() / 1000) - 300),
        updated_at: new Date().toISOString() }, { onConflict: "name" }), "cursor");
      return html(`Withings connected. ${out.rows} weigh-ins from the last ${BACKFILL_DAYS} days are in life-os, and new ones arrive on their own.`);
    }

    if (req.method === "POST" && route === "/sync") {
      const { data: ok } = await sb.rpc("check_ingest_token", { p_name: "cron", p_token: req.headers.get("x-cron-key") ?? "" });
      if (ok !== true) return new Response("unauthorized", { status: 401 });
      const cur = must(await sb.from("ingest_cursor").select("cursor").eq("name", "withings.lastupdate").maybeSingle(), "cursor") as Json | null;
      const runAt = Math.floor(Date.now() / 1000);
      const out = await logRun("poll", () => pull({ lastupdate: Number(cur?.cursor ?? runAt - 7 * 86400) }));
      must(await sb.from("ingest_cursor").upsert({ name: "withings.lastupdate", cursor: String(runAt - 300),
        updated_at: new Date().toISOString() }, { onConflict: "name" }), "cursor");
      return Response.json({ ok: true, ...out });
    }

    if (req.method === "POST" && route === "") {
      // Withings notification. It carries no values, only the window to fetch.
      const form = new URLSearchParams(await req.text());
      const appli = Number(form.get("appli")), start = Number(form.get("startdate")), end = Number(form.get("enddate"));
      const linked = must(await sb.from("oauth_tokens").select("metadata").eq("service", SOURCE).maybeSingle(), "token") as Json | null;
      if (appli !== 1 || !start || !end || String(linked?.metadata?.userid) !== form.get("userid")) {
        return new Response("ignored", { status: 200 });
      }
      // Always 200: Withings drops the subscription after repeated failures, and
      // the hourly /sync picks up anything a failed notification missed.
      const out = await logRun("notify", () => pull({ startdate: start, enddate: end })).catch((e) => ({ error: String(e) }));
      return Response.json({ ok: !("error" in out), ...out });
    }

    return new Response("not found", { status: 404 });
  } catch (e) {
    console.error(e);
    return Response.json({ ok: false, error: String(e) }, { status: 500 });
  }
});
