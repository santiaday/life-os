// calendar-mcp: read and write Apple (iCloud) Calendar over CalDAV, so Claude,
// scripts on the laptop and automated bots can all manage the same calendars.
//
// iCloud CalDAV needs the Apple ID email and an app-specific password
// (appleid.apple.com -> Sign-In and Security -> App-Specific Passwords); the
// normal Apple ID password is rejected.
//
// Bindings: KV  (discovered calendar-home URL)
// Secrets:  ICLOUD_USERNAME, ICLOUD_APP_PASSWORD, MCP_TOKEN
// Vars:     DEFAULT_TZ (default America/New_York)

import { DAY, ToolError, mcpHandler, str } from "../../lib/mcp.js";
import {
  escapeText, fold, formatLocal, icsDate, icsUtc, parseEvents, parseIcsTime, parseLocalDateTime,
  patchEvent, validTimeZone, xmlHas, xmlResponses, xmlText,
} from "./ical.js";

const ROOT = "https://caldav.icloud.com";
const HOME_KEY = "calendar-home";
const PRODID = "-//life-os//calendar-mcp//EN";

const tzOf = (env, args) => {
  const tz = str(args, "timezone") ?? env.DEFAULT_TZ ?? "America/New_York";
  if (!validTimeZone(tz)) throw new ToolError(`unknown time zone: ${tz}`);
  return tz;
};

// ---- CalDAV transport ------------------------------------------------------------

async function dav(env, method, url, { body, depth, headers = {} } = {}) {
  const { ICLOUD_USERNAME: user, ICLOUD_APP_PASSWORD: pass } = env;
  if (!user || !pass) throw new ToolError("ICLOUD_USERNAME / ICLOUD_APP_PASSWORD secrets are not set on this Worker");
  const res = await fetch(url, {
    method,
    headers: {
      authorization: `Basic ${btoa(`${user}:${pass}`)}`,
      ...(body ? { "content-type": method === "PUT" ? "text/calendar; charset=utf-8" : "application/xml; charset=utf-8" } : {}),
      ...(depth !== undefined ? { depth: String(depth) } : {}),
      ...headers,
    },
    body,
  });
  if (res.status === 401) throw new ToolError("iCloud rejected the credentials. ICLOUD_APP_PASSWORD must be an app-specific password, not the Apple ID password.");
  return res;
}

async function davXml(env, method, url, body, depth) {
  const res = await dav(env, method, url, { body, depth });
  const text = await res.text();
  if (res.status !== 207 && !res.ok) throw new ToolError(`${method} ${url} -> HTTP ${res.status}: ${text.slice(0, 300)}`);
  return text;
}

const abs = (href, base) => new URL(href, base).toString();

/** principal -> calendar-home-set, cached in KV (it never changes for an account). */
async function calendarHome(env) {
  const cached = await env.KV.get(HOME_KEY);
  if (cached) return cached;
  const p1 = await davXml(env, "PROPFIND", `${ROOT}/`,
    '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>', 0);
  const principal = xmlText(xmlText(p1, "current-user-principal") ?? "", "href");
  if (!principal) throw new ToolError("iCloud returned no current-user-principal");
  const principalUrl = abs(principal, ROOT);
  const p2 = await davXml(env, "PROPFIND", principalUrl,
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-home-set/></d:prop></d:propfind>', 0);
  const home = xmlText(xmlText(p2, "calendar-home-set") ?? "", "href");
  if (!home) throw new ToolError("iCloud returned no calendar-home-set");
  const url = abs(home, principalUrl);
  await env.KV.put(HOME_KEY, url);
  return url;
}

async function listCalendars(env) {
  const home = await calendarHome(env);
  const xml = await davXml(env, "PROPFIND", home, `<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
      xmlns:a="http://apple.com/ns/ical/"><d:prop><d:displayname/><d:resourcetype/>
      <c:supported-calendar-component-set/><a:calendar-color/><d:current-user-privilege-set/></d:prop></d:propfind>`, 1);
  const out = [];
  for (const r of xmlResponses(xml)) {
    const type = xmlText(r, "resourcetype") ?? "";
    if (!/<(?:[\w-]+:)?calendar(?:\s[^>]*)?\/?>/.test(type)) continue;
    const comps = [...(xmlText(r, "supported-calendar-component-set") ?? "").matchAll(/name="([^"]+)"/g)].map((m) => m[1]);
    if (comps.length && !comps.includes("VEVENT")) continue; // reminders lists
    const privs = xmlText(r, "current-user-privilege-set") ?? "";
    const href = abs(xmlText(r, "href"), home);
    out.push({
      calendar_id: href.replace(/\/+$/, "").split("/").pop(),
      name: xmlText(r, "displayname") || "(untitled)",
      color: xmlText(r, "calendar-color") || null,
      writable: !privs || xmlHas(privs, "write") || xmlHas(privs, "all"),
      url: href,
    });
  }
  return out;
}

async function resolveCalendar(env, ref) {
  const cals = await listCalendars(env);
  if (!ref) {
    const first = cals.find((c) => c.writable);
    if (!first) throw new ToolError("no writable calendar found");
    return first;
  }
  const lc = ref.toLowerCase();
  const hit = cals.find((c) => c.calendar_id === ref || c.url === ref) ?? cals.find((c) => c.name.toLowerCase() === lc);
  if (!hit) throw new ToolError(`no calendar "${ref}". Calendars: ${cals.map((c) => c.name).join(", ")}`);
  return hit;
}

// ---- events ------------------------------------------------------------------------

function describe(ev, cal, href, etag, tz) {
  const s = parseIcsTime(ev.start, tz);
  let e = parseIcsTime(ev.end, tz);
  const allDay = !!s?.date;
  if (!e && s?.instant && ev.duration) {
    const m = ev.duration.match(/^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$/);
    if (m) {
      const secs = ((+m[1] || 0) * 7 + (+m[2] || 0)) * 86400 + (+m[3] || 0) * 3600 + (+m[4] || 0) * 60 + (+m[5] || 0);
      e = { instant: new Date(s.instant.getTime() + secs * 1000) };
    }
  }
  return {
    event_id: href,
    uid: ev.uid,
    calendar: cal.name,
    title: ev.summary,
    all_day: allDay,
    start: allDay ? s.date : s?.instant?.toISOString() ?? null,
    end: allDay ? e?.date ?? null : e?.instant?.toISOString() ?? null,
    start_local: allDay ? s.date : s?.instant ? formatLocal(s.instant, tz) : null,
    end_local: allDay ? e?.date ?? null : e?.instant ? formatLocal(e.instant, tz) : null,
    location: ev.location,
    notes: ev.description,
    url: ev.url,
    status: ev.status,
    recurring: !!ev.rrule || !!ev.recurrenceId,
    etag,
  };
}

async function queryCalendar(env, cal, start, end, tz) {
  const range = `start="${icsUtc(start)}" end="${icsUtc(end)}"`;
  const xml = await davXml(env, "REPORT", cal.url, `<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
      <d:prop><d:getetag/><c:calendar-data><c:expand ${range}/></c:calendar-data></d:prop>
      <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"><c:time-range ${range}/>
      </c:comp-filter></c:comp-filter></c:filter></c:calendar-query>`, 1);
  const out = [];
  for (const r of xmlResponses(xml)) {
    const data = xmlText(r, "calendar-data");
    if (!data) continue;
    const href = abs(xmlText(r, "href"), cal.url);
    for (const ev of parseEvents(data)) out.push(describe(ev, cal, href, xmlText(r, "getetag"), tz));
  }
  return out;
}

/** start/end tool args -> { dtstart, dtend } iCalendar lines and the parsed values. */
function eventTimes(args, tz, { required }) {
  const allDay = args.all_day === true;
  const start = str(args, "start", { required });
  const end = str(args, "end");
  if (!start) {
    if (end) throw new ToolError("pass start together with end");
    return null;
  }
  if (allDay) {
    if (!DAY.test(start) || (end && !DAY.test(end))) throw new ToolError("all-day events take start/end as YYYY-MM-DD");
    // DTEND is exclusive: a one-day event on the 23rd ends on the 24th.
    const last = end ?? start;
    const next = new Date(`${last}T12:00:00Z`);
    next.setUTCDate(next.getUTCDate() + 1);
    if (last < start) throw new ToolError("end is before start");
    return { lines: { DTSTART: [`DTSTART;VALUE=DATE:${icsDate(start)}`],
                      DTEND: [`DTEND;VALUE=DATE:${icsDate(next.toISOString().slice(0, 10))}`] } };
  }
  let s, e;
  try {
    s = parseLocalDateTime(start, tz);
    e = end ? parseLocalDateTime(end, tz) : new Date(s.getTime() + 3600_000);
  } catch (err) {
    throw new ToolError(err.message);
  }
  if (e <= s) throw new ToolError("end must be after start");
  // UTC times need no VTIMEZONE block; every client renders them in local time.
  return { lines: { DTSTART: [`DTSTART:${icsUtc(s)}`], DTEND: [`DTEND:${icsUtc(e)}`] } };
}

const optionalText = (args, key, prop) => {
  if (!(key in args)) return {};
  const v = args[key];
  return { [prop]: v === null || v === "" ? null : [`${prop}:${escapeText(v)}`] };
};

function alarmLines(minutes) {
  return ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder", `TRIGGER:-PT${Math.round(minutes)}M`, "END:VALARM"];
}

// ---- tools -------------------------------------------------------------------------

const timeArgs = {
  start: { type: "string", description: "Timed: YYYY-MM-DDTHH:MM in `timezone` (or with an explicit offset). All-day: YYYY-MM-DD." },
  end: { type: "string", description: "Same format as start. Timed default: start + 1h. All-day: last day of the event, inclusive." },
  all_day: { type: "boolean", default: false },
  timezone: { type: "string", description: "IANA zone for naive times. Default America/New_York." },
};

const tools = [
  {
    name: "list_calendars",
    description: "The iCloud calendars that hold events: name, calendar_id, colour, and whether it can be written.",
    inputSchema: { type: "object", properties: {} },
    annotations: { readOnlyHint: true },
    handler: async (_args, env) => ({ calendars: (await listCalendars(env)).map(({ url, ...c }) => c) }),
  },
  {
    name: "list_events",
    description: "Events between two dates, recurring events expanded into their occurrences. Searches every calendar unless one is named. `query` filters on title, location and notes.",
    inputSchema: { type: "object", properties: {
      start: { type: "string", description: "YYYY-MM-DD or YYYY-MM-DDTHH:MM (local). Default: today." },
      end: { type: "string", description: "Exclusive. Default: start + 7 days." },
      calendar: { type: "string", description: "Calendar name or calendar_id. Default: all." },
      query: { type: "string" },
      timezone: timeArgs.timezone,
    } },
    annotations: { readOnlyHint: true },
    async handler(args, env) {
      const tz = tzOf(env, args);
      const asInstant = (v) => parseLocalDateTime(DAY.test(v) ? `${v}T00:00` : v, tz);
      const todayLocal = formatLocal(new Date(), tz).slice(4, 14);
      let start, end;
      try {
        start = asInstant(str(args, "start") ?? todayLocal);
        end = args.end ? asInstant(str(args, "end")) : new Date(start.getTime() + 7 * 86_400_000);
      } catch (e) {
        throw new ToolError(e.message);
      }
      if (end <= start) throw new ToolError("end must be after start");
      if (end - start > 366 * 86_400_000) throw new ToolError("range is limited to one year");
      const cals = args.calendar ? [await resolveCalendar(env, str(args, "calendar"))] : await listCalendars(env);
      const q = str(args, "query")?.toLowerCase();
      const events = (await Promise.all(cals.map((c) => queryCalendar(env, c, start, end, tz)))).flat()
        .filter((e) => !q || [e.title, e.location, e.notes].some((f) => f?.toLowerCase().includes(q)))
        .sort((a, b) => String(a.start).localeCompare(String(b.start)));
      return { timezone: tz, start: start.toISOString(), end: end.toISOString(), count: events.length, events };
    },
  },
  {
    name: "create_event",
    description: "Create an event on an iCloud calendar. Returns its event_id for update_event / delete_event.",
    inputSchema: { type: "object", properties: {
      title: { type: "string" },
      ...timeArgs,
      calendar: { type: "string", description: "Calendar name or calendar_id. Default: the first writable calendar." },
      location: { type: "string" },
      notes: { type: "string" },
      url: { type: "string" },
      alert_minutes_before: { type: "number", minimum: 0, description: "Add a notification this many minutes before start." },
    }, required: ["title", "start"] },
    async handler(args, env) {
      const tz = tzOf(env, args);
      const title = str(args, "title", { required: true });
      const times = eventTimes(args, tz, { required: true });
      const cal = await resolveCalendar(env, str(args, "calendar"));
      if (!cal.writable) throw new ToolError(`calendar "${cal.name}" is read-only`);
      const uid = `${crypto.randomUUID()}@life-os`;
      const lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", `PRODID:${PRODID}`, "CALSCALE:GREGORIAN", "BEGIN:VEVENT",
        `UID:${uid}`, `DTSTAMP:${icsUtc(new Date())}`, ...times.lines.DTSTART, ...times.lines.DTEND,
        `SUMMARY:${escapeText(title)}`,
        ...(args.location ? [`LOCATION:${escapeText(args.location)}`] : []),
        ...(args.notes ? [`DESCRIPTION:${escapeText(args.notes)}`] : []),
        ...(args.url ? [`URL:${args.url}`] : []),
        ...(typeof args.alert_minutes_before === "number" ? alarmLines(args.alert_minutes_before) : []),
        "END:VEVENT", "END:VCALENDAR",
      ];
      const href = `${cal.url.replace(/\/?$/, "/")}${encodeURIComponent(uid)}.ics`;
      const res = await dav(env, "PUT", href, { body: lines.map(fold).join("\r\n") + "\r\n", headers: { "if-none-match": "*" } });
      if (!res.ok) throw new ToolError(`create failed: HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
      const [created] = await queryEvent(env, href, cal, tz);
      return created ?? { event_id: href, uid, calendar: cal.name, title };
    },
  },
  {
    name: "update_event",
    description: "Change an event by event_id (from list_events or create_event). Only the fields you pass change; pass an empty string to clear location, notes or url. Moving one occurrence of a recurring event isn't supported: the change would move the whole series, so it is refused.",
    inputSchema: { type: "object", properties: {
      event_id: { type: "string" },
      title: { type: "string" },
      ...timeArgs,
      location: { type: "string" },
      notes: { type: "string" },
      url: { type: "string" },
    }, required: ["event_id"] },
    async handler(args, env) {
      const tz = tzOf(env, args);
      const href = str(args, "event_id", { required: true });
      const cal = await calendarFor(env, href);
      const res = await dav(env, "GET", href);
      if (res.status === 404) throw new ToolError("event not found; list_events again for a current event_id");
      if (!res.ok) throw new ToolError(`fetch failed: HTTP ${res.status}`);
      const etag = res.headers.get("etag");
      const ics = await res.text();
      const [current] = parseEvents(ics);
      const times = eventTimes(args, tz, { required: false });
      if (times && current?.rrule) throw new ToolError("this is a recurring series; moving it here would move every occurrence. Change it in the Calendar app.");
      const changes = {
        ...(args.title ? { SUMMARY: [`SUMMARY:${escapeText(args.title)}`] } : {}),
        ...optionalText(args, "location", "LOCATION"),
        ...optionalText(args, "notes", "DESCRIPTION"),
        ...("url" in args ? { URL: args.url ? [`URL:${args.url}`] : null } : {}),
        ...(times ? { DTSTART: times.lines.DTSTART, DTEND: times.lines.DTEND, DURATION: null } : {}),
        DTSTAMP: [`DTSTAMP:${icsUtc(new Date())}`],
        SEQUENCE: [`SEQUENCE:${Math.floor(Date.now() / 1000)}`],
      };
      if (Object.keys(changes).length === 2) throw new ToolError("nothing to change");
      const put = await dav(env, "PUT", href, { body: patchEvent(ics, changes), headers: etag ? { "if-match": etag } : {} });
      if (put.status === 412) throw new ToolError("the event changed since it was read; list_events and try again");
      if (!put.ok) throw new ToolError(`update failed: HTTP ${put.status}: ${(await put.text()).slice(0, 300)}`);
      const [updated] = await queryEvent(env, href, cal, tz);
      return updated ?? { event_id: href, updated: true };
    },
  },
  {
    name: "delete_event",
    description: "Delete an event by event_id. For a recurring event this deletes the whole series.",
    inputSchema: { type: "object", properties: { event_id: { type: "string" } }, required: ["event_id"] },
    annotations: { destructiveHint: true },
    async handler(args, env) {
      const href = str(args, "event_id", { required: true });
      await calendarFor(env, href);
      const res = await dav(env, "DELETE", href);
      if (res.status === 404) return { event_id: href, deleted: false, reason: "already gone" };
      if (!res.ok) throw new ToolError(`delete failed: HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
      return { event_id: href, deleted: true };
    },
  },
];

/** The calendar an event href lives in; refuses hrefs outside this account's calendars. */
async function calendarFor(env, href) {
  const cals = await listCalendars(env);
  const cal = cals.find((c) => href.startsWith(c.url.replace(/\/?$/, "/")));
  if (!cal) throw new ToolError("event_id is not in one of this account's calendars");
  return cal;
}

async function queryEvent(env, href, cal, tz) {
  const res = await dav(env, "GET", href);
  if (!res.ok) return [];
  const etag = res.headers.get("etag");
  return parseEvents(await res.text()).map((ev) => describe(ev, cal, href, etag, tz));
}

export default {
  fetch: mcpHandler({
    name: "apple-calendar",
    version: "1.0.0",
    instructions: "Read and write Santi's Apple (iCloud) calendars. Naive times are America/New_York unless a timezone is passed. Use list_events before update_event/delete_event to get a current event_id.",
    tools,
  }),
};

