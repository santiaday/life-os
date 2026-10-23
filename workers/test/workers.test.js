// Runs the Workers' fetch handlers against fake iCloud CalDAV and Cronometer
// servers, through the MCP JSON-RPC layer, the same way a client calls them.
//
//   deno test --allow-env workers/test/workers.test.js

import calendar from "../calendar-mcp/src/index.js";
import cronometer from "../cronometer-mcp/src/index.js";
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
    try { await fn(); } finally { globalThis.fetch = real; }
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
      return v ? new Response(v.ics, { headers: { etag: v.etag } }) : new Response("", { status: 404 });
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
