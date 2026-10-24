// Runs the Workers' fetch handlers against fake iCloud CalDAV and Cronometer
// servers, through the MCP JSON-RPC layer, the same way a client calls them.
//
//   deno test --allow-env workers/test/workers.test.js

import calendar from "../calendar-mcp/src/index.js";
import cronometer from "../cronometer-mcp/src/index.js";
import appleMusic from "../apple-music-mcp/src/index.js";
import { fold, parseEvents, parseIcsTime, patchEvent, zonedToInstant } from "../calendar-mcp/src/ical.js";

const assert = (cond, msg) => { if (!cond) throw new Error(`assertion failed: ${msg}`); };
const eq = (a, b, msg) => assert(JSON.stringify(a) === JSON.stringify(b), `${msg}: ${JSON.stringify(a)} !== ${JSON.stringify(b)}`);

class MemoryKV {
  constructor() { this.m = new Map(); }
  async get(k, type) { const v = this.m.get(k); return v === undefined ? null : type === "json" ? JSON.parse(v) : v; }
  async put(k, v) { this.m.set(k, v); }
  async delete(k) { this.m.delete(k); }
}

const TOKEN = "t0ken";
const ctx = { waitUntil() {} };

async function rpc(worker, env, method, params, { path = "/mcp", auth = true } = {}) {
  const res = await worker.fetch(new Request(`https://w.example${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", ...(auth ? { authorization: `Bearer ${TOKEN}` } : {}) },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  }), env, ctx);
  return { status: res.status, body: res.status === 200 ? await res.json() : await res.text() };
}

async function call(worker, env, name, args) {
  const { body } = await rpc(worker, env, "tools/call", { name, arguments: args });
  const text = body.result.content[0].text;
  if (body.result.isError) throw new Error(`tool error: ${text}`);
  return JSON.parse(text);
}

function withFetch(handler, fn) {
  return async () => {
    const real = globalThis.fetch;
    globalThis.fetch = async (input, init = {}) => {
      const req = new Request(input, init);
      return handler(req, await req.text());
    };
    try { return await fn(); } finally { globalThis.fetch = real; }
  };
}

// ---- iCalendar unit tests -------------------------------------------------------

Deno.test("zonedToInstant handles both sides of the November DST change", () => {
  eq(zonedToInstant("America/New_York", 2026, 10, 23, 9, 30).toISOString(), "2026-10-23T13:30:00.000Z", "EDT");
  eq(zonedToInstant("America/New_York", 2026, 11, 2, 9, 30).toISOString(), "2026-11-02T14:30:00.000Z", "EST");
  eq(zonedToInstant("Asia/Tokyo", 2026, 10, 23, 9, 0).toISOString(), "2026-10-23T00:00:00.000Z", "JST");
});

Deno.test("parseEvents unfolds lines, unescapes text, skips VALARM properties", () => {
  const ics = [
    "BEGIN:VCALENDAR", "BEGIN:VEVENT", "UID:abc", "SUMMARY:Lift\\, legs", "DESCRIPTION:line one\\nline", " two",
    "DTSTART;TZID=America/New_York:20261023T070000", "DTEND;TZID=America/New_York:20261023T080000",
    "BEGIN:VALARM", "SUMMARY:alarm text", "TRIGGER:-PT10M", "END:VALARM", "END:VEVENT", "END:VCALENDAR",
  ].join("\r\n");
  const [e] = parseEvents(ics);
  eq(e.summary, "Lift, legs", "summary");
  eq(e.description, "line one\nlinetwo", "unfolded description");
  eq(parseIcsTime(e.start, "UTC").instant.toISOString(), "2026-10-23T11:00:00.000Z", "TZID start");
});

Deno.test("fold keeps every line within 75 octets and round-trips", () => {
  const long = "DESCRIPTION:" + "ü".repeat(80);
  const folded = fold(long);
  for (const l of folded.split("\r\n")) assert(new TextEncoder().encode(l).length <= 75, "line too long");
  eq(folded.replace(/\r\n /g, ""), long, "round trip");
});

Deno.test("patchEvent replaces, removes and adds top-level properties only", () => {
  const ics = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\nSUMMARY:old\r\nLOCATION:gym\r\nBEGIN:VALARM\r\nSUMMARY:keep me\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n";
  const out = patchEvent(ics, { SUMMARY: ["SUMMARY:new"], LOCATION: null, URL: ["URL:https://x"] });
  assert(out.includes("SUMMARY:new") && !out.includes("SUMMARY:old"), "summary replaced");
  assert(!out.includes("LOCATION:"), "location removed");
  assert(out.includes("SUMMARY:keep me"), "alarm untouched");
  assert(out.indexOf("URL:https://x") < out.indexOf("END:VEVENT"), "url added inside the event");
});

// ---- MCP layer --------------------------------------------------------------------

Deno.test("auth: bearer header and /mcp/<token> path both work; anything else is 401", async () => {
  const env = { MCP_TOKEN: TOKEN, KV: new MemoryKV() };
  eq((await rpc(calendar, env, "ping", {}, { auth: false })).status, 401, "no auth");
  eq((await rpc(calendar, env, "ping", {}, { auth: false, path: "/mcp/wrong" })).status, 401, "wrong path token");
  eq((await rpc(calendar, env, "ping", {}, { auth: false, path: `/mcp/${TOKEN}` })).status, 200, "path token");
  eq((await rpc(calendar, { KV: new MemoryKV() }, "ping", {})).status, 401, "no MCP_TOKEN configured");
  const init = await rpc(calendar, env, "initialize", { protocolVersion: "2025-06-18" });
  eq(init.body.result.protocolVersion, "2025-06-18", "protocol echoed");
  const list = await rpc(cronometer, env, "tools/list", {});
  eq(list.body.result.tools.map((t) => t.name),
     ["search_foods", "get_food", "get_diary", "log_food", "create_custom_food", "delete_food_entries"], "cronometer tools");
});

// ---- calendar against a fake iCloud ------------------------------------------------

function fakeICloud() {
  // iCloud writes hrefs with an explicit :443; URL normalisation drops it.
  const rawHome = "https://p42-caldav.icloud.com:443/123/calendars/";
  const home = new URL(rawHome).toString();
  const store = new Map([[`${home}home/existing.ics`, {
    etag: '"e1"',
    ics: "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:existing\r\nSUMMARY:Dentist\r\nDTSTART:20261024T140000Z\r\nDTEND:20261024T150000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
  }]]);
  const ms = (body) => new Response(`<?xml version="1.0"?><multistatus xmlns="DAV:">${body}</multistatus>`, { status: 207 });
  const handler = (req, body) => {
    assert(req.headers.get("authorization") === `Basic ${btoa("me@icloud.com:app-pass")}`, "basic auth");
    const url = req.url;
    if (req.method === "PROPFIND" && url === "https://caldav.icloud.com/") {
      return ms(`<response><href>/</href><propstat><prop><current-user-principal><href>/123/principal/</href></current-user-principal></prop></propstat></response>`);
    }
    if (req.method === "PROPFIND" && url === "https://caldav.icloud.com/123/principal/") {
      return ms(`<response><href>/123/principal/</href><propstat><prop><calendar-home-set xmlns="urn:ietf:params:xml:ns:caldav"><href xmlns="DAV:">${rawHome}</href></calendar-home-set></prop></propstat></response>`);
    }
    if (req.method === "PROPFIND" && url === home) {
      return ms(`
        <response><href>/123/calendars/</href><propstat><prop><resourcetype><collection/></resourcetype></prop></propstat></response>
        <response><href>/123/calendars/home/</href><propstat><prop><displayname>Home</displayname>
          <resourcetype><collection/><calendar xmlns="urn:ietf:params:xml:ns:caldav"/></resourcetype>
          <supported-calendar-component-set xmlns="urn:ietf:params:xml:ns:caldav"><comp name="VEVENT"/></supported-calendar-component-set>
          <current-user-privilege-set><privilege><read/></privilege><privilege><write/></privilege></current-user-privilege-set>
          <calendar-color xmlns="http://apple.com/ns/ical/">#1BADF8FF</calendar-color></prop></propstat></response>
        <response><href>/123/calendars/tasks/</href><propstat><prop><displayname>Reminders</displayname>
          <resourcetype><collection/><calendar xmlns="urn:ietf:params:xml:ns:caldav"/></resourcetype>
          <supported-calendar-component-set xmlns="urn:ietf:params:xml:ns:caldav"><comp name="VTODO"/></supported-calendar-component-set></prop></propstat></response>
        <response><href>/123/calendars/inbox/</href><propstat><prop><resourcetype><collection/><schedule-inbox xmlns="urn:ietf:params:xml:ns:caldav"/></resourcetype></prop></propstat></response>`);
    }
    if (req.method === "REPORT" && url === `${home}home/`) {
      assert(body.includes("<c:expand"), "asks for server-side expansion");
      const rows = [...store].map(([href, v]) => `<response><href>${new URL(href).pathname}</href><propstat><prop>
        <getetag>${v.etag}</getetag><calendar-data xmlns="urn:ietf:params:xml:ns:caldav">${v.ics.replace(/\r/g, "&#13;").replace(/&/g, "&amp;").replace(/&amp;#13;/g, "&#13;")}</calendar-data>
        </prop></propstat></response>`).join("");
      return ms(rows);
    }
    if (req.method === "PUT") {
      const existing = store.get(url);
      if (req.headers.get("if-none-match") === "*" && existing) return new Response("", { status: 412 });
      if (req.headers.get("if-match") && existing && req.headers.get("if-match") !== existing.etag) return new Response("", { status: 412 });
      store.set(url, { etag: `"e${store.size + 2}"`, ics: body });
      return new Response(null, { status: existing ? 204 : 201 });
    }
    if (req.method === "GET") {
      const v = store.get(url);
      // iCloud serves a weak ETag on GET but compares If-Match strongly against the stored tag.
      return v ? new Response(v.ics, { headers: { etag: `W/${v.etag}` } }) : new Response("", { status: 404 });
    }
    if (req.method === "DELETE") return new Response(null, { status: store.delete(url) ? 204 : 404 });
    return new Response(`unexpected ${req.method} ${url}`, { status: 500 });
  };
  return { handler, store, home };
}

const calEnv = () => ({ MCP_TOKEN: TOKEN, KV: new MemoryKV(), ICLOUD_USERNAME: "me@icloud.com", ICLOUD_APP_PASSWORD: "app-pass" });
const icloud = fakeICloud();

Deno.test("calendar: discovery lists event calendars only", withFetch(icloud.handler, async () => {
  const out = await call(calendar, calEnv(), "list_calendars", {});
  eq(out.calendars, [{ calendar_id: "home", name: "Home", color: "#1BADF8FF", writable: true }], "calendars");
}));

Deno.test("calendar: create, list, update, delete round trip", withFetch(icloud.handler, async () => {
  const env = calEnv();
  const created = await call(calendar, env, "create_event", {
    title: "Squat day", start: "2026-10-27T06:30", end: "2026-10-27T07:45", location: "Gym, main floor",
    alert_minutes_before: 15,
  });
  eq(created.start, "2026-10-27T10:30:00.000Z", "start stored in UTC");
  eq(created.start_local, "Tue 2026-10-27 06:30", "local rendering");
  eq(created.location, "Gym, main floor", "escaped comma survives");
  const stored = icloud.store.get(created.event_id).ics;
  assert(stored.includes("TRIGGER:-PT15M") && stored.includes("LOCATION:Gym\\, main floor"), "ics content");

  const listed = await call(calendar, env, "list_events", { start: "2026-10-24", end: "2026-10-28" });
  eq(listed.events.map((e) => e.title), ["Dentist", "Squat day"], "sorted by start");
  eq((await call(calendar, env, "list_events", { start: "2026-10-24", end: "2026-10-28", query: "squat" })).count, 1, "query filter");

  const updated = await call(calendar, env, "update_event", { event_id: created.event_id, title: "Squat + pull", location: "" });
  eq(updated.title, "Squat + pull", "title changed");
  eq(updated.location, null, "location cleared");
  eq(updated.start, created.start, "time untouched");
  assert(icloud.store.get(created.event_id).ics.includes("TRIGGER:-PT15M"), "alarm kept through update");

  const allDay = await call(calendar, env, "create_event", { title: "Deload week", start: "2026-11-02", end: "2026-11-08", all_day: true });
  eq([allDay.start, allDay.end], ["2026-11-02", "2026-11-09"], "all-day end is exclusive");

  eq((await call(calendar, env, "delete_event", { event_id: created.event_id })).deleted, true, "deleted");
  eq((await call(calendar, env, "delete_event", { event_id: created.event_id })).deleted, false, "second delete is a no-op");
}));

Deno.test("calendar: refuses event ids outside the account's calendars", withFetch(icloud.handler, async () => {
  const { body } = await rpc(calendar, calEnv(), "tools/call", { name: "delete_event", arguments: { event_id: "https://evil.example/x.ics" } });
  assert(body.result.isError && /not in one of this account's calendars/.test(body.result.content[0].text), "refused");
}));

// ---- cronometer against a fake mobile API ------------------------------------------

function fakeCronometer() {
  const diary = [{ type: "Serving", servingId: 111, foodId: 7, grams: 200, order: (1 << 16) | 1, time: "8:05:00", day: "2026-10-23" }];
  let logins = 0;
  const food = { id: 7, name: "Greek Yogurt", defaultMeasureId: 70,
    measures: [{ id: 70, name: "cup", value: 245, amount: 1 }, { id: 71, name: "g", value: 1, amount: 1 }],
    nutrients: [{ id: 208, amount: 59 }, { id: 203, amount: 10.3 }, { id: 204, amount: 0.4 }, { id: 205, amount: 3.6 }] };
  const handler = async (req, body) => {
    const path = new URL(req.url).pathname;
    const json = body ? JSON.parse(body) : {};
    if (path === "/api/v2/login") { logins++; return Response.json({ id: 42, sessionKey: `tok${logins}` }); }
    if (path.startsWith("/api/v2/")) assert(json.auth?.userId === 42 && json.auth?.token, "v2 auth block");
    if (path === "/api/v2/find_food") return Response.json({ foods: [{ id: 7, name: "Greek Yogurt", measureId: 70 }] });
    if (path === "/api/v2/get_food") return Response.json(food);
    if (path === "/api/v2/get_diary") {
      eq(json.day, "2026-10-23", "non-padded day... (2026-10-23 has no padding to strip)");
      return Response.json({ diary });
    }
    if (path === "/api/v2/add_serving") {
      diary.push({ ...json.serving, servingId: 222 });
      return Response.json({ id: 222 });
    }
    if (path === "/api/v3/user/42/diary-entries" && req.method === "DELETE") {
      for (const e of json.diaryEntries) {
        assert(e.id === e.servingId, "v3 delete needs id = servingId");
        diary.splice(diary.findIndex((d) => d.servingId === e.servingId), 1);
      }
      return new Response(null, { status: 204 });
    }
    return new Response(`unexpected ${path}`, { status: 500 });
  };
  return { handler, diary, logins: () => logins };
}

const fakeCrono = fakeCronometer();
const cronoEnv = { MCP_TOKEN: TOKEN, KV: new MemoryKV(), CRONOMETER_USERNAME: "me", CRONOMETER_PASSWORD: "pw" };

Deno.test("cronometer: search, diary, log by quantity, delete; one login reused", withFetch(fakeCrono.handler, async () => {
  const s = await call(cronometer, cronoEnv, "search_foods", { query: "yogurt" });
  eq(s.foods[0].food_id, 7, "search");
  const d = await call(cronometer, cronoEnv, "get_diary", { day: "2026-10-23" });
  eq([d.entries[0].meal, d.entries[0].energy_kcal, d.totals.protein_g], ["breakfast", 118, 20.6], "diary macros");
  const logged = await call(cronometer, cronoEnv, "log_food", { food_id: 7, quantity: 2, measure_id: 70, meal: "snack", day: "2026-10-23", time: "15:30" });
  eq([logged.entry_id, logged.grams, logged.energy_kcal], ["222", 490, 289.1], "log by quantity");
  const added = fakeCrono.diary.find((e) => e.servingId === 222);
  eq([added.order >> 16, added.time, added.day], [4, "15:30:0", "2026-10-23"], "serving wire format");
  const del = await call(cronometer, cronoEnv, "delete_food_entries", { entry_ids: ["222", "999"], day: "2026-10-23" });
  eq([del.removed, del.missing], [["222"], ["999"]], "delete");
  eq(fakeCrono.logins(), 1, "session reused across calls");
}));

// ---- apple-music against a fake api.music.apple.com --------------------------------

// A fresh P-256 key per run, in the same PKCS#8 PEM armour as a MusicKit
// AuthKey_*.p8, so the Worker's real signing path is exercised. Generated
// rather than committed: private key material in the repo trips secret
// scanners forever, and an inert test key that has to be explained every time
// is worth less than one that never exists outside the process.
// The real key is the APPLE_MUSIC_PRIVATE_KEY Worker secret.
const TEST_P8 = await (async () => {
  const { privateKey } = await crypto.subtle.generateKey(
    { name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"],
  );
  const der = new Uint8Array(await crypto.subtle.exportKey("pkcs8", privateKey));
  let raw = "";
  for (const b of der) raw += String.fromCharCode(b);
  return [
    "-----BEGIN PRIVATE KEY-----",
    ...btoa(raw).match(/.{1,64}/g),
    "-----END PRIVATE KEY-----",
  ].join("\n");
})();

const jwtPart = (token, i) => JSON.parse(atob(token.split(".")[i].replace(/-/g, "+").replace(/_/g, "/")));

function musicEnv(extra = {}) {
  return {
    MCP_TOKEN: TOKEN,
    KV: new MemoryKV(),
    APPLE_MUSIC_TEAM_ID: "TEAM123456",
    APPLE_MUSIC_KEY_ID: "KEYID12345",
    APPLE_MUSIC_PRIVATE_KEY: TEST_P8,
    ...extra,
  };
}

const withUserToken = async (env, token = "MUT-xyz") =>
  env.KV.put("user_token", JSON.stringify({ token, stored_at: "2026-10-24T00:00:00Z" }));

function fakeAppleMusic({ forbid = false, status = 403, detail = "token expired" } = {}) {
  const seen = [];
  const song = (id, name) => ({
    id,
    type: "songs",
    attributes: {
      name, artistName: "Knocked Loose", albumName: "You Won't Go Before You're Supposed To",
      durationInMillis: 134_000, genreNames: ["Hardcore"], releaseDate: "2024-05-10",
      isrc: "USQX91234567", url: `https://music.apple.com/us/song/${id}`,
      // Noise the shaper is supposed to drop.
      artwork: { url: "https://is1-ssl.mzstatic.com/{w}x{h}bb.jpg", width: 3000, height: 3000 },
      previews: [{ url: "https://audio-ssl.itunes.apple.com/preview.m4a" }],
      playParams: { id, kind: "song" },
    },
  });
  const handler = async (req, body) => {
    const u = new URL(req.url);
    const json = body ? JSON.parse(body) : null;
    seen.push({
      method: req.method, path: u.pathname, search: u.search, body: json,
      dev: req.headers.get("authorization"), mut: req.headers.get("music-user-token"),
    });
    assert(u.origin === "https://api.music.apple.com", `wrong host ${u.origin}`);
    if (forbid || status !== 403) return Response.json({ errors: [{ detail }] }, { status });

    const route = `${req.method} ${u.pathname}`;
    if (route === "GET /v1/me/storefront") {
      return Response.json({ data: [{ id: "us", type: "storefronts", attributes: { name: "United States" } }] });
    }
    if (route === "GET /v1/catalog/us/search") {
      return Response.json({ results: { songs: { data: [song("1744776339", "Blinds")] } } });
    }
    if (route === "GET /v1/me/recent/played/tracks") {
      return Response.json({ data: [song("1744776339", "Blinds"), song("i.abc123", "Suffocate")] });
    }
    if (route === "GET /v1/me/history/heavy-rotation") {
      return Response.json({ data: [{ id: "a.1", type: "library-albums", attributes: { name: "Mania", artistName: "Rolo Tomassi", trackCount: 9 } }] });
    }
    if (route === "GET /v1/me/library/playlists") {
      return Response.json({
        data: [{
          id: "p.XYZ", type: "library-playlists",
          attributes: {
            name: "Lift", canEdit: true, isPublic: false, hasCatalog: false,
            description: { standard: "heavy day" }, dateAdded: "2026-10-01T12:00:00Z",
            trackTypes: ["library-songs"],
          },
        }],
        next: "/v1/me/library/playlists?offset=25",
      });
    }
    if (route === "GET /v1/me/library/playlists/p.XYZ/tracks") {
      return Response.json({ data: [song("i.abc123", "Suffocate")] });
    }
    if (route === "POST /v1/me/library/playlists") {
      return Response.json({
        data: [{ id: "p.NEW", type: "library-playlists", attributes: { name: json.attributes.name } }],
      }, { status: 201 });
    }
    if (route === "POST /v1/me/library/playlists/p.XYZ/tracks") return new Response(null, { status: 204 });
    if (route === "POST /v1/me/library") return new Response(null, { status: 202 });
    if (req.method === "PUT" && u.pathname.startsWith("/v1/me/ratings/")) {
      return Response.json({ data: [{ id: "1744776339", type: "ratings" }] });
    }
    return new Response(`unexpected ${route}`, { status: 500 });
  };
  return { handler, seen };
}

const amToken = fakeAppleMusic();
Deno.test("apple-music: developer token is a real ES256 JWT inside Apple's limits, signed once and cached", withFetch(amToken.handler, async () => {
  const env = musicEnv();
  await call(appleMusic, env, "search_catalog", { term: "knocked loose" });
  await call(appleMusic, env, "search_catalog", { term: "again" });

  const token = amToken.seen[0].dev.replace("Bearer ", "");
  eq(jwtPart(token, 0), { alg: "ES256", kid: "KEYID12345" }, "JWT header");
  const claims = jwtPart(token, 1);
  eq(claims.iss, "TEAM123456", "iss is the Team ID");
  assert(claims.exp - claims.iat <= 15_777_000, `exp ${claims.exp - claims.iat}s exceeds Apple's 15777000s cap`);
  assert(claims.origin === undefined, "API tokens carry no origin claim");
  eq(token.split(".").length, 3, "three JWT segments");
  // ES256 is raw r||s, 64 bytes, which is 86 base64url characters unpadded.
  eq(token.split(".")[2].length, 86, "raw r||s signature, not DER");
  eq(amToken.seen[1].dev, amToken.seen[0].dev, "token reused from KV on the second call");
}));

const amSearch = fakeAppleMusic();
Deno.test("apple-music: catalog search shapes results and drops artwork/preview noise", withFetch(amSearch.handler, async () => {
  const env = musicEnv();
  const out = await call(appleMusic, env, "search_catalog", { term: "knocked loose", limit: 3 });
  eq(out.storefront, "us", "default storefront");
  eq(out.results.songs[0], {
    id: "1744776339", type: "songs", title: "Blinds", artist: "Knocked Loose",
    album: "You Won't Go Before You're Supposed To", duration_s: 134,
    genres: ["Hardcore"], released: "2024-05-10", isrc: "USQX91234567",
    url: "https://music.apple.com/us/song/1744776339",
  }, "flattened song");
  const q = amSearch.seen[0].search;
  assert(q.includes("term=knocked+loose"), `spaces become + as the search docs require: ${q}`);
  assert(q.includes("types=songs") && q.includes("limit=3"), `search params: ${q}`);
  assert(!amSearch.seen[0].mut, "catalog search sends no Music-User-Token");

  const bad = await rpc(appleMusic, env, "tools/call", { name: "search_catalog", arguments: { term: "x", types: "tracks" } });
  assert(bad.body.result.isError, "unknown search type is rejected before the request");
}));

const amNoAuth = fakeAppleMusic();
Deno.test("apple-music: personal reads need a stored user token and say how to get one", withFetch(amNoAuth.handler, async () => {
  const env = musicEnv();
  const { body } = await rpc(appleMusic, env, "tools/call", { name: "get_recently_played", arguments: {} });
  assert(body.result.isError, "missing token is a tool error");
  const msg = body.result.content[0].text;
  assert(msg.includes("/authorize/"), `message points at the authorize page: ${msg}`);
  eq(amNoAuth.seen.length, 0, "no request reaches Apple without a user token");

  const status = await call(appleMusic, env, "auth_status", {});
  eq([status.developer_token, status.music_user_token], ["ok", "missing"], "auth_status before authorizing");
  assert(status.next_step.includes("Catalog search works without it"), "auth_status explains what still works");
}));

const amAuthorize = fakeAppleMusic();
Deno.test("apple-music: the authorize page mints an origin-pinned token and stores what Apple hands back", withFetch(amAuthorize.handler, async () => {
  const env = musicEnv();
  const denied = await appleMusic.fetch(new Request("https://w.example/authorize/wrong"), env, ctx);
  eq(denied.status, 401, "authorize needs the path secret");

  const res = await appleMusic.fetch(new Request(`https://w.example/authorize/${TOKEN}`), env, ctx);
  eq(res.status, 200, "authorize page served");
  const html = await res.text();
  assert(html.includes("js-cdn.music.apple.com/musickit/v3/musickit.js"), "loads MusicKit JS v3");
  const pageToken = /developerToken: "([^"]+)"/.exec(html)[1];
  const claims = jwtPart(pageToken, 1);
  eq(claims.origin, ["https://w.example"], "browser token is pinned to this Worker's origin");
  assert(claims.exp - claims.iat <= 3600, "browser token is short-lived");

  const post = await appleMusic.fetch(new Request(`https://w.example/authorize/${TOKEN}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ musicUserToken: "MUT-xyz" }),
  }), env, ctx);
  eq([post.status, (await post.json()).storefront], [200, "us"], "token verified and storefront cached");
  eq(amAuthorize.seen.at(-1).mut, "MUT-xyz", "verification call carries the new token");

  const stored = await env.KV.get("user_token", "json");
  eq(stored.token, "MUT-xyz", "token in KV");
  assert(stored.stored_at, "stored_at recorded");
  eq(await env.KV.get("storefront"), "us", "storefront cached for later catalog calls");
}));

const amReject = fakeAppleMusic({ forbid: true });
Deno.test("apple-music: a failed re-authorization costs nothing", withFetch(amReject.handler, async () => {
  const env = musicEnv();
  const post = await appleMusic.fetch(new Request(`https://w.example/authorize/${TOKEN}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ musicUserToken: "MUT-bad" }),
  }), env, ctx);
  eq(post.status, 502, "verification failure is reported");
  eq(await env.KV.get("user_token"), null, "a token Apple rejects is not left in KV");
  eq(amReject.seen.at(-1).mut, "MUT-bad", "the candidate token is what got verified");

  // And it must not clobber a token that already works.
  const env2 = musicEnv();
  await withUserToken(env2, "MUT-good");
  const again = await appleMusic.fetch(new Request(`https://w.example/authorize/${TOKEN}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ musicUserToken: "MUT-bad" }),
  }), env2, ctx);
  eq(again.status, 502, "still reported");
  eq((await env2.KV.get("user_token", "json")).token, "MUT-good", "the working token survives");
}));

const am401 = fakeAppleMusic({ status: 401, detail: "Unauthorized developer token" });
Deno.test("apple-music: a 401 drops the cached developer token so a fixed secret takes effect", withFetch(am401.handler, async () => {
  const env = musicEnv();
  await env.KV.put("dev_token", "stale.jwt.value");
  const { body } = await rpc(appleMusic, env, "tools/call", { name: "search_catalog", arguments: { term: "x" } });
  assert(body.result.isError, "401 surfaces as a tool error");
  eq(am401.seen.at(-1).dev, "Bearer stale.jwt.value", "the cached token was the one Apple rejected");
  eq(await env.KV.get("dev_token"), null, "cached developer token cleared; 150-day cache cannot outlive a config fix");
}));

const amRecent = fakeAppleMusic();
Deno.test("apple-music: recently played sends the required types parameter and the user token", withFetch(amRecent.handler, async () => {
  const env = musicEnv();
  await withUserToken(env);
  const out = await call(appleMusic, env, "get_recently_played", { limit: 5 });
  eq(out.tracks.length, 2, "two tracks");
  eq(out.tracks[0].title, "Blinds", "newest first, as Apple returns them");
  const q = amRecent.seen.at(-1).search;
  assert(q.includes("types=songs"), `types is a required query parameter here: ${q}`);
  assert(q.includes("limit=5"), `limit passed: ${q}`);
  eq(amRecent.seen.at(-1).mut, "MUT-xyz", "Music-User-Token sent");

  const over = await rpc(appleMusic, env, "tools/call", { name: "get_recently_played", arguments: { limit: 50 } });
  assert(over.body.result.isError, "Apple caps this endpoint at 30, so 50 is rejected locally");

  const heavy = await call(appleMusic, env, "get_heavy_rotation", {});
  eq(heavy.items[0], { id: "a.1", type: "library-albums", name: "Mania", artist: "Rolo Tomassi", track_count: 9 }, "mixed resource shaping");
}));

const amPlaylists = fakeAppleMusic();
Deno.test("apple-music: playlist writes use Apple's body shapes and infer catalog vs library ids", withFetch(amPlaylists.handler, async () => {
  const env = musicEnv();
  await withUserToken(env);

  const made = await call(appleMusic, env, "create_playlist", {
    name: "Heavy day", description: "squat PR attempts", track_ids: ["1744776339", "i.abc123"],
  });
  eq([made.playlist_id, made.name, made.tracks_added], ["p.NEW", "Heavy day", 2], "created");
  eq(amPlaylists.seen.at(-1).body, {
    attributes: { name: "Heavy day", description: "squat PR attempts" },
    relationships: {
      tracks: {
        data: [
          { id: "1744776339", type: "songs" },
          { id: "i.abc123", type: "library-songs" },
        ],
      },
    },
  }, "LibraryPlaylistCreationRequest body; 'i.' ids typed as library-songs");

  const empty = await call(appleMusic, env, "create_playlist", { name: "Empty" });
  eq(empty.tracks_added, 0, "a playlist with no seed tracks");
  assert(amPlaylists.seen.at(-1).body.relationships === undefined, "no empty tracks relationship sent");

  const appended = await call(appleMusic, env, "add_tracks_to_playlist", { playlist_id: "p.XYZ", track_ids: ["1744776339"] });
  eq(appended.tracks_added, 1, "appended");
  eq(amPlaylists.seen.at(-1).body, { data: [{ id: "1744776339", type: "songs" }] }, "LibraryPlaylistTracksRequest body");

  const forced = await call(appleMusic, env, "add_tracks_to_playlist", {
    playlist_id: "p.XYZ", track_ids: ["1744776339"], track_type: "music-videos",
  });
  eq(forced.tracks_added, 1, "explicit track_type honoured");
  eq(amPlaylists.seen.at(-1).body.data[0].type, "music-videos", "explicit type beats the id heuristic");

  const tracks = await call(appleMusic, env, "get_playlist_tracks", { playlist_id: "p.XYZ" });
  eq(tracks.tracks[0].id, "i.abc123", "playlist tracks read");

  const lists = await call(appleMusic, env, "list_library_playlists", {});
  eq(
    [lists.playlists[0].name, lists.playlists[0].description, lists.playlists[0].can_edit, lists.next_offset],
    ["Lift", "heavy day", true, "25"],
    "library playlist shape, DescriptionAttribute flattened, offset parsed out of next",
  );
}));

const amLibrary = fakeAppleMusic();
Deno.test("apple-music: add_to_library keeps literal ids[...] brackets; rate_song validates the value", withFetch(amLibrary.handler, async () => {
  const env = musicEnv();
  await withUserToken(env);

  const added = await call(appleMusic, env, "add_to_library", { song_ids: ["1744776339", "123"], album_ids: ["987"] });
  eq(added.requested, { songs: 2, albums: 1 }, "counts are what was requested, not confirmed");
  const q = amLibrary.seen.at(-1).search;
  assert(q.includes("ids[songs]=1744776339,123"), `brackets must not be percent-encoded: ${q}`);
  assert(q.includes("ids[albums]=987"), `album ids: ${q}`);
  assert(!q.includes("ids[playlists]"), `empty id lists are dropped: ${q}`);

  const none = await rpc(appleMusic, env, "tools/call", { name: "add_to_library", arguments: {} });
  assert(none.body.result.isError, "add_to_library with no ids is rejected");

  const rated = await call(appleMusic, env, "rate_song", { id: "1744776339", value: 1 });
  eq([rated.kind, rated.value], ["songs", 1], "catalog rating");
  eq(amLibrary.seen.at(-1).body, { type: "ratings", attributes: { value: 1 } }, "RatingRequest body");
  eq(amLibrary.seen.at(-1).path, "/v1/me/ratings/songs/1744776339", "catalog rating path");

  await call(appleMusic, env, "rate_song", { id: "i.abc123", value: -1 });
  eq(amLibrary.seen.at(-1).path, "/v1/me/ratings/library-songs/i.abc123", "library rating path");

  const { body } = await rpc(appleMusic, env, "tools/call", { name: "rate_song", arguments: { id: "1", value: 3 } });
  assert(body.result.isError && body.result.content[0].text.includes("1 (love) or -1"), "only 1 and -1 are allowed");
}));

const amStale = fakeAppleMusic({ forbid: true });
Deno.test("apple-music: a 403 on a personal call drops the stale user token", withFetch(amStale.handler, async () => {
  const env = musicEnv();
  await withUserToken(env, "MUT-stale");
  const { body } = await rpc(appleMusic, env, "tools/call", { name: "get_heavy_rotation", arguments: {} });
  assert(body.result.isError, "403 surfaces as a tool error");
  const msg = body.result.content[0].text;
  assert(msg.includes("token expired") && msg.includes("Re-run the authorize page"), `actionable message: ${msg}`);
  eq(await env.KV.get("user_token"), null, "stale token cleared so the next call says to re-authorize");
}));

Deno.test("apple-music: missing secrets are named, not guessed at", async () => {
  const env = { MCP_TOKEN: TOKEN, KV: new MemoryKV(), APPLE_MUSIC_TEAM_ID: "TEAM123456" };
  const status = await call(appleMusic, env, "auth_status", {});
  assert(status.developer_token.includes("APPLE_MUSIC_KEY_ID"), `names the missing key id: ${status.developer_token}`);
  assert(status.developer_token.includes("APPLE_MUSIC_PRIVATE_KEY"), "names the missing private key");

  const junk = musicEnv({ APPLE_MUSIC_PRIVATE_KEY: "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----" });
  const bad = await call(appleMusic, junk, "auth_status", {});
  assert(bad.developer_token.includes("APPLE_MUSIC_PRIVATE_KEY"), `a malformed key is named too: ${bad.developer_token}`);
});

Deno.test("apple-music: health is open, /mcp is not", async () => {
  const env = musicEnv();
  const health = await appleMusic.fetch(new Request("https://w.example/health"), env, ctx);
  eq([health.status, (await health.json()).server], [200, "apple-music"], "health");

  const { status } = await rpc(appleMusic, env, "tools/list", {}, { auth: false });
  eq(status, 401, "no bearer, no tools");

  const viaPath = await appleMusic.fetch(new Request(`https://w.example/mcp/${TOKEN}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }),
  }), env, ctx);
  const names = (await viaPath.json()).result.tools.map((t) => t.name);
  assert(names.includes("search_catalog") && names.includes("auth_status"), "path-secret auth works for claude.ai");
  eq(names.length, 12, "twelve tools");
});
