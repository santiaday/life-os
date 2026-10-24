// iCalendar (RFC 5545) and CalDAV-XML helpers. Just enough of each format for
// reading and writing single VEVENTs; no dependencies, so it runs as-is on
// Workers.

// ---- time zones --------------------------------------------------------------

function wallClockIn(tz, ms) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
}

export function validTimeZone(tz) {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: tz });
    return true;
  } catch {
    return false;
  }
}

/** The instant at which `tz`'s wall clock reads the given fields. */
export function zonedToInstant(tz, y, mo, d, h = 0, mi = 0, s = 0) {
  const target = Date.UTC(y, mo - 1, d, h, mi, s);
  let t = target - (wallClockIn(tz, target) - target);
  t = t - (wallClockIn(tz, t) - target); // second pass settles DST edges
  return new Date(t);
}

/** "2026-10-23T09:30:00" in tz, or with its own offset/Z. Returns a Date. */
export function parseLocalDateTime(value, tz) {
  if (/[zZ]$|[+-]\d{2}:?\d{2}$/.test(value)) {
    const d = new Date(value);
    if (isNaN(d)) throw new Error(`bad datetime: ${value}`);
    return d;
  }
  const m = value.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$/);
  if (!m) throw new Error(`bad datetime (want YYYY-MM-DDTHH:MM): ${value}`);
  return zonedToInstant(tz, +m[1], +m[2], +m[3], +m[4], +m[5], +(m[6] ?? 0));
}

export function formatLocal(date, tz) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", weekday: "short",
  }).formatToParts(date).map((x) => [x.type, x.value]));
  return `${p.weekday} ${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
}

export const icsUtc = (d) => d.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
export const icsDate = (day) => day.replace(/-/g, "");

// ---- iCalendar ---------------------------------------------------------------

export const escapeText = (s) =>
  String(s).replace(/\\/g, "\\\\").replace(/;/g, "\\;").replace(/,/g, "\\,").replace(/\r?\n/g, "\\n");
const unescapeText = (s) =>
  s.replace(/\\(n|N|,|;|\\)/g, (_, c) => (c === "n" || c === "N" ? "\n" : c));

const unfold = (ics) => ics.replace(/\r?\n[ \t]/g, "");

/** Fold to 75 octets per line, as RFC 5545 requires. */
export function fold(line) {
  const bytes = new TextEncoder().encode(line);
  if (bytes.length <= 75) return line;
  const out = [];
  let cur = "", curLen = 0, limit = 75;
  for (const ch of line) {
    const n = new TextEncoder().encode(ch).length;
    if (curLen + n > limit) {
      out.push(cur);
      cur = "";
      curLen = 0;
      limit = 74; // continuation lines start with a space
    }
    cur += ch;
    curLen += n;
  }
  out.push(cur);
  return out.join("\r\n ");
}

export function parseProperty(line) {
  let inQuote = false, colon = -1;
  for (let i = 0; i < line.length; i++) {
    if (line[i] === '"') inQuote = !inQuote;
    else if (line[i] === ":" && !inQuote) { colon = i; break; }
  }
  if (colon < 0) return null;
  const [name, ...rawParams] = line.slice(0, colon).split(";");
  const params = {};
  for (const p of rawParams) {
    const eq = p.indexOf("=");
    if (eq > 0) params[p.slice(0, eq).toUpperCase()] = p.slice(eq + 1).replace(/^"|"$/g, "");
  }
  return { name: name.toUpperCase(), params, value: line.slice(colon + 1) };
}

/** A DTSTART/DTEND/RECURRENCE-ID value -> { date } for all-day, { instant } otherwise. */
export function parseIcsTime(prop, fallbackTz) {
  if (!prop) return null;
  const v = prop.value.trim();
  if (prop.params.VALUE === "DATE" || /^\d{8}$/.test(v)) {
    return { date: `${v.slice(0, 4)}-${v.slice(4, 6)}-${v.slice(6, 8)}` };
  }
  const m = v.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$/);
  if (!m) return null;
  const f = [+m[1], +m[2], +m[3], +m[4], +m[5], +m[6]];
  if (m[7]) return { instant: new Date(Date.UTC(f[0], f[1] - 1, f[2], f[3], f[4], f[5])) };
  const tz = prop.params.TZID && validTimeZone(prop.params.TZID) ? prop.params.TZID : fallbackTz;
  return { instant: zonedToInstant(tz, ...f) };
}

/** Every VEVENT in an iCalendar object, ignoring nested components (VALARM). */
export function parseEvents(ics) {
  const events = [];
  let cur = null, depth = 0;
  for (const line of unfold(ics).split(/\r?\n/)) {
    if (!line) continue;
    if (/^BEGIN:VEVENT$/i.test(line)) { cur = { props: {} }; depth = 0; continue; }
    if (!cur) continue;
    if (/^END:VEVENT$/i.test(line)) { events.push(cur); cur = null; continue; }
    if (/^BEGIN:/i.test(line)) { depth++; continue; }
    if (/^END:/i.test(line)) { depth--; continue; }
    if (depth > 0) continue;
    const p = parseProperty(line);
    if (p && !(p.name in cur.props)) cur.props[p.name] = p;
  }
  return events.map((e) => ({
    uid: e.props.UID?.value ?? null,
    summary: e.props.SUMMARY ? unescapeText(e.props.SUMMARY.value) : null,
    location: e.props.LOCATION ? unescapeText(e.props.LOCATION.value) : null,
    description: e.props.DESCRIPTION ? unescapeText(e.props.DESCRIPTION.value) : null,
    url: e.props.URL?.value ?? null,
    status: e.props.STATUS?.value ?? null,
    rrule: e.props.RRULE?.value ?? null,
    start: e.props.DTSTART ?? null,
    end: e.props.DTEND ?? null,
    duration: e.props.DURATION?.value ?? null,
    recurrenceId: e.props["RECURRENCE-ID"] ?? null,
  }));
}

/**
 * Replace (or add, or with null remove) top-level properties of the first
 * VEVENT, leaving every other line, VALARMs included, as it was.
 * `changes` maps a property name to full replacement lines (without folding).
 */
export function patchEvent(ics, changes) {
  const lines = unfold(ics).split(/\r?\n/).filter((l) => l !== "");
  const names = new Set(Object.keys(changes).map((n) => n.toUpperCase()));
  const out = [];
  let inEvent = false, depth = 0, patched = false;
  for (const line of lines) {
    if (!patched && /^BEGIN:VEVENT$/i.test(line)) { inEvent = true; depth = 0; out.push(line); continue; }
    if (inEvent) {
      if (/^END:VEVENT$/i.test(line)) {
        for (const add of Object.values(changes)) if (add) out.push(...add);
        out.push(line);
        inEvent = false;
        patched = true;
        continue;
      }
      if (/^BEGIN:/i.test(line)) depth++;
      else if (/^END:/i.test(line)) depth--;
      else if (depth === 0) {
        const p = parseProperty(line);
        if (p && names.has(p.name)) continue;
      }
    }
    out.push(line);
  }
  if (!patched) throw new Error("no VEVENT to update");
  return out.map(fold).join("\r\n") + "\r\n";
}

// ---- CalDAV XML --------------------------------------------------------------

const decodeXml = (s) => s
  .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
  .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
  .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(+d))
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&apos;/g, "'")
  .replace(/&amp;/g, "&");

/** <d:response> blocks of a multistatus body, whatever namespace prefixes the server picked. */
export function xmlResponses(xml) {
  return [...xml.matchAll(/<([A-Za-z][\w-]*:)?response(?=[\s>])[^>]*>([\s\S]*?)<\/\1response>/g)].map((m) => m[2]);
}

/** Text of the first element with this local name, decoded; null if absent. */
export function xmlText(xml, local) {
  const m = xml.match(new RegExp(`<([A-Za-z][\\w-]*:)?${local}(?=[\\s/>])[^>]*?(?:/>|>([\\s\\S]*?)</\\1${local}>)`));
  return m ? decodeXml((m[2] ?? "").trim()) : null;
}

export const xmlHas = (xml, local) => new RegExp(`<([A-Za-z][\\w-]*:)?${local}(?=[\\s/>])`).test(xml);

export const xmlEscape = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
