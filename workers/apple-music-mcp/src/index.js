// apple-music-mcp: read and write Apple Music over the official Apple Music API
// (https://api.music.apple.com). Replaces the /opt/apple-music-mcp plan that
// was going to run on the droplet; the droplet is gone, so this follows the
// same shape as cronometer-mcp and calendar-mcp.
//
// Two tokens, and the difference matters:
//
//   Developer token   an ES256 JWT this Worker signs itself from the MusicKit
//                     .p8 private key. Good for six months; cached in KV.
//                     Catalog endpoints need only this.
//   Music User Token  identifies the subscriber. Apple only issues it to
//                     MusicKit in a browser, so it cannot be minted here --
//                     GET /authorize/<MCP_TOKEN> serves a page that runs
//                     MusicKit JS and POSTs the token back into KV. Every
//                     /v1/me/* endpoint needs it. It expires in ~6 months;
//                     auth_status reports when it was stored.
//
// Bindings: KV  (developer token, music user token, storefront)
// Secrets:  MCP_TOKEN, APPLE_MUSIC_TEAM_ID, APPLE_MUSIC_KEY_ID,
//           APPLE_MUSIC_PRIVATE_KEY (the whole AuthKey_*.p8, BEGIN/END lines included)

import { ToolError, mcpHandler, num, str, timingSafeEqual } from "../../lib/mcp.js";

const BASE = "https://api.music.apple.com";
const MUSICKIT_JS = "https://js-cdn.music.apple.com/musickit/v3/musickit.js";

const DEV_TOKEN_KEY = "dev_token";
const USER_TOKEN_KEY = "user_token";
const STOREFRONT_KEY = "storefront";

// Apple caps exp at 15_777_000 s (~182.6 days) past iat and rejects anything
// longer with a 401. Mint for 180 days, re-mint at 150, so a cached token is
// never close to the edge.
const DEV_TOKEN_EXP_S = 180 * 86_400;
const DEV_TOKEN_TTL_S = 150 * 86_400;
// The token embedded in the authorize page is a different animal: it goes to a
// browser, so it is short-lived and pinned to this Worker's origin.
const PAGE_TOKEN_EXP_S = 3_600;

// Track types accepted by the playlist and rating endpoints.
const TRACK_TYPES = ["songs", "music-videos", "library-songs", "library-music-videos"];
const SEARCH_TYPES = ["activities", "albums", "apple-curators", "artists", "curators",
  "music-videos", "playlists", "record-labels", "songs", "stations"];

// ---- encoding -----------------------------------------------------------------

const b64url = (bytes) => {
  let s = "";
  for (const b of new Uint8Array(bytes)) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
};

const b64urlJson = (o) => b64url(new TextEncoder().encode(JSON.stringify(o)));

/** PKCS#8 DER out of a .p8 PEM. */
function pkcs8(pem) {
  const body = pem.replace(/-----[^-]+-----/g, "").replace(/\s+/g, "");
  if (!body) throw new ToolError("APPLE_MUSIC_PRIVATE_KEY is empty");
  let raw;
  try {
    raw = atob(body);
  } catch {
    throw new ToolError("APPLE_MUSIC_PRIVATE_KEY is not valid base64; paste the whole AuthKey_*.p8 file");
  }
  const der = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) der[i] = raw.charCodeAt(i);
  return der;
}

// URLSearchParams is no good here: it percent-encodes both the brackets in
// `ids[songs]` and the commas separating list values, and Apple's docs spell
// both literally. Keys are our own literals, so pass them through raw; encode
// values but leave the two characters Apple expects to see -- "," (a legal
// query sub-delimiter) and " " as "+", which the search docs ask for by name.
function qs(params) {
  const parts = [];
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    const value = Array.isArray(v) ? v.join(",") : String(v);
    parts.push(`${k}=${encodeURIComponent(value).replace(/%20/g, "+").replace(/%2C/g, ",")}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

const compact = (o) => Object.fromEntries(
  Object.entries(o).filter(([, v]) => v !== undefined && v !== null && !(Array.isArray(v) && !v.length)),
);

// ---- client -------------------------------------------------------------------

class AppleMusic {
  constructor(env) {
    this.env = env;
  }

  /** Signed developer token, cached in KV. */
  async devToken() {
    const cached = await this.env.KV.get(DEV_TOKEN_KEY);
    if (cached) return cached;
    const token = await this.mint(DEV_TOKEN_EXP_S);
    await this.env.KV.put(DEV_TOKEN_KEY, token, { expirationTtl: DEV_TOKEN_TTL_S });
    return token;
  }

  async mint(ttlSeconds, origin) {
    const { APPLE_MUSIC_TEAM_ID: iss, APPLE_MUSIC_KEY_ID: kid, APPLE_MUSIC_PRIVATE_KEY: pem } = this.env;
    const missing = [
      !iss && "APPLE_MUSIC_TEAM_ID",
      !kid && "APPLE_MUSIC_KEY_ID",
      !pem && "APPLE_MUSIC_PRIVATE_KEY",
    ].filter(Boolean);
    if (missing.length) {
      throw new ToolError(`Worker secrets are not set: ${missing.join(", ")}. Set them with npx wrangler secret put <name>.`);
    }
    const now = Math.floor(Date.now() / 1000);
    const header = { alg: "ES256", kid };
    const payload = { iss, iat: now, exp: now + ttlSeconds, ...(origin ? { origin: [origin] } : {}) };
    const input = `${b64urlJson(header)}.${b64urlJson(payload)}`;
    let key;
    try {
      key = await crypto.subtle.importKey("pkcs8", pkcs8(pem), { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
    } catch (e) {
      if (e instanceof ToolError) throw e;
      throw new ToolError(`APPLE_MUSIC_PRIVATE_KEY is not a P-256 PKCS#8 key: ${e.message}`);
    }
    // WebCrypto returns ECDSA signatures as raw r||s, which is already the JWS
    // ES256 encoding -- no DER unwrapping needed.
    const sig = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key, new TextEncoder().encode(input));
    return `${input}.${b64url(sig)}`;
  }

  async userToken(override) {
    if (override) return override;
    const stored = await this.env.KV.get(USER_TOKEN_KEY, "json");
    if (!stored?.token) {
      const where = this.env.SELF_ORIGIN ? `${this.env.SELF_ORIGIN}/authorize/<MCP_TOKEN>` : "/authorize/<MCP_TOKEN>";
      throw new ToolError(`No Music User Token stored. Open ${where} in a browser signed in to Apple Music and approve access; Apple only issues this token to a browser.`);
    }
    return stored.token;
  }

  /** The user's own storefront once known, else the configured default. */
  async storefront(explicit) {
    if (explicit) return explicit;
    return (await this.env.KV.get(STOREFRONT_KEY)) ?? this.env.DEFAULT_STOREFRONT ?? "us";
  }

  async request(method, path, { query = {}, body, personal = false, userToken } = {}) {
    const headers = { authorization: `Bearer ${await this.devToken()}` };
    if (personal) headers["music-user-token"] = await this.userToken(userToken);
    if (body !== undefined) headers["content-type"] = "application/json";
    const res = await fetch(BASE + path + qs(query), {
      method,
      headers,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (res.status === 204 || res.status === 202) return null;
    const text = await res.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = null;
      }
    }
    if (!res.ok) {
      const detail = data?.errors?.map((e) => e.detail || e.title).filter(Boolean).join("; ") || text.slice(0, 300);
      if (res.status === 401 || res.status === 403) {
        // A 403 on a /v1/me/* path is almost always a stale Music User Token;
        // drop it so the next call says to re-authorize instead of repeating
        // this. Only when the call actually used the stored token, though --
        // the authorize page verifies a candidate through here, and that must
        // not evict a token that still works.
        if (personal && !userToken) await this.env.KV.delete(USER_TOKEN_KEY);
        // A 401 is Apple rejecting the developer token itself -- wrong Team ID,
        // wrong key, wrong algorithm. It is cached for 150 days, so without
        // this a fixed secret would keep failing until the cache expired.
        if (res.status === 401) await this.env.KV.delete(DEV_TOKEN_KEY);
        throw new ToolError(`Apple Music rejected the request (${res.status}): ${detail || "token invalid or expired"}.${personal ? " Re-run the authorize page to refresh the Music User Token." : ""}`);
      }
      if (res.status === 429) throw new ToolError("Apple Music is rate-limiting this developer token (429). Retry in a minute.");
      throw new ToolError(`Apple Music ${method} ${path} returned ${res.status}: ${detail}`);
    }
    return data;
  }

  get = (path, opts) => this.request("GET", path, opts);
}

// ---- shaping ------------------------------------------------------------------
// The raw payloads carry artwork templates, previews and editorial notes that
// would swamp a model's context. Keep the fields a caller can act on.

function songOf(r) {
  const a = r.attributes ?? {};
  return compact({
    id: r.id,
    type: r.type,
    title: a.name,
    artist: a.artistName,
    album: a.albumName,
    duration_s: a.durationInMillis === undefined ? undefined : Math.round(a.durationInMillis / 1000),
    genres: a.genreNames,
    released: a.releaseDate,
    isrc: a.isrc,
    content_rating: a.contentRating,
    url: a.url,
  });
}

function playlistOf(r) {
  const a = r.attributes ?? {};
  return compact({
    id: r.id,
    type: r.type,
    name: a.name,
    // Library playlists carry description as { standard, short }, catalog ones too.
    description: a.description?.standard ?? a.description?.short,
    curator: a.curatorName,
    can_edit: a.canEdit,
    is_public: a.isPublic,
    has_catalog: a.hasCatalog,
    date_added: a.dateAdded,
    track_types: a.trackTypes,
    url: a.url,
  });
}

function resourceOf(r) {
  const t = r.type ?? "";
  if (t.endsWith("songs") || t.endsWith("music-videos")) return songOf(r);
  if (t.includes("playlist")) return playlistOf(r);
  const a = r.attributes ?? {};
  return compact({
    id: r.id,
    type: r.type,
    name: a.name,
    artist: a.artistName,
    curator: a.curatorName,
    genres: a.genreNames,
    released: a.releaseDate,
    track_count: a.trackCount,
    url: a.url,
  });
}

/**
 * Catalog IDs are numeric; library IDs start with "i.". That is a convention,
 * not a documented guarantee, so `type` always wins when the caller sets it.
 */
function trackType(id, explicit) {
  if (explicit) {
    if (!TRACK_TYPES.includes(explicit)) throw new ToolError(`type must be one of ${TRACK_TYPES.join(", ")}`);
    return explicit;
  }
  return String(id).startsWith("i.") ? "library-songs" : "songs";
}

function idList(args, key, { required = false } = {}) {
  const v = args[key];
  if (v === undefined || v === null || v === "") {
    if (required) throw new ToolError(`${key} is required`);
    return [];
  }
  const ids = (Array.isArray(v) ? v : String(v).split(",")).map((x) => String(x).trim()).filter(Boolean);
  if (required && !ids.length) throw new ToolError(`${key} is required`);
  return ids;
}

// ---- tools --------------------------------------------------------------------

const tools = [
  {
    name: "auth_status",
    description: "Which credentials this Worker holds: whether the developer token can be signed, whether a Music User Token is stored and when, and the storefront in use. Call this first when any other tool reports an auth problem.",
    inputSchema: { type: "object", properties: {} },
    annotations: { readOnlyHint: true },
    handler: async (_args, env) => {
      const am = new AppleMusic(env);
      const stored = await env.KV.get(USER_TOKEN_KEY, "json");
      const out = {
        developer_token: "ok",
        music_user_token: stored?.token ? "stored" : "missing",
        music_user_token_stored_at: stored?.stored_at,
        storefront: await am.storefront(),
        authorize_url: env.SELF_ORIGIN ? `${env.SELF_ORIGIN}/authorize/<MCP_TOKEN>` : "/authorize/<MCP_TOKEN>",
      };
      try {
        await am.devToken();
      } catch (e) {
        out.developer_token = e.message;
      }
      if (!stored?.token) {
        out.next_step = "Open authorize_url (with the real MCP_TOKEN) in a browser signed in to an Apple Music subscription. Catalog search works without it; every library and history tool needs it.";
      }
      return compact(out);
    },
  },
  {
    name: "get_storefront",
    description: "The subscriber's Apple Music storefront (country code), read from Apple and cached for later catalog calls. Needs a Music User Token.",
    inputSchema: { type: "object", properties: {} },
    annotations: { readOnlyHint: true },
    handler: async (_args, env) => {
      const am = new AppleMusic(env);
      const data = await am.get("/v1/me/storefront", { personal: true });
      const sf = data?.data?.[0];
      if (sf?.id) await env.KV.put(STOREFRONT_KEY, sf.id);
      return { storefront: sf?.id, name: sf?.attributes?.name };
    },
  },
  {
    name: "search_catalog",
    description: "Search the Apple Music catalog. Needs only the developer token, so it works before authorizing. Returns catalog IDs usable with create_playlist, add_tracks_to_playlist, add_to_library and rate_song.",
    inputSchema: {
      type: "object",
      properties: {
        term: { type: "string", description: "What to search for." },
        types: { type: "string", description: `Comma-separated resource types. Default songs. One or more of: ${SEARCH_TYPES.join(", ")}.` },
        limit: { type: "integer", description: "Results per type, 1-25. Default 10." },
        storefront: { type: "string", description: "Two-letter storefront override; defaults to the subscriber's." },
      },
      required: ["term"],
    },
    annotations: { readOnlyHint: true },
    handler: async (args, env) => {
      const am = new AppleMusic(env);
      const term = str(args, "term", { required: true });
      const types = (str(args, "types") ?? "songs").split(",").map((t) => t.trim()).filter(Boolean);
      for (const t of types) {
        if (!SEARCH_TYPES.includes(t)) throw new ToolError(`types must be drawn from ${SEARCH_TYPES.join(", ")}; got ${t}`);
      }
      const limit = num(args, "limit", { min: 1, max: 25 }) ?? 10;
      const storefront = await am.storefront(str(args, "storefront"));
      const data = await am.get(`/v1/catalog/${storefront}/search`, { query: { term, types, limit } });
      const results = {};
      for (const [kind, block] of Object.entries(data?.results ?? {})) {
        results[kind] = (block.data ?? []).map(resourceOf);
      }
      return { storefront, term, results };
    },
  },
  {
    name: "get_recently_played",
    description: "Tracks the subscriber played most recently, newest first. The single best read for 'what was I listening to'. Needs a Music User Token.",
    inputSchema: {
      type: "object",
      properties: {
        limit: { type: "integer", description: "1-30. Apple caps this endpoint at 30 per call. Default 30." },
        types: { type: "string", description: "Comma-separated track types to include. Default songs. One or more of: songs, music-videos, library-songs, library-music-videos." },
      },
    },
    annotations: { readOnlyHint: true },
    handler: async (args, env) => {
      const am = new AppleMusic(env);
      const limit = num(args, "limit", { min: 1, max: 30 }) ?? 30;
      const types = (str(args, "types") ?? "songs").split(",").map((t) => t.trim()).filter(Boolean);
      for (const t of types) {
        if (!TRACK_TYPES.includes(t)) throw new ToolError(`types must be drawn from ${TRACK_TYPES.join(", ")}; got ${t}`);
      }
      // types is a required query parameter on this endpoint.
      const data = await am.get("/v1/me/recent/played/tracks", { query: { types, limit }, personal: true });
      return { tracks: (data?.data ?? []).map(songOf) };
    },
  },
  {
    name: "get_heavy_rotation",
    description: "Albums, playlists and stations the subscriber has played most over roughly the last month. Mixed resource types. Needs a Music User Token.",
    inputSchema: {
      type: "object",
      properties: { limit: { type: "integer", description: "1-10. Default 10." } },
    },
    annotations: { readOnlyHint: true },
    handler: async (args, env) => {
      const am = new AppleMusic(env);
      const limit = num(args, "limit", { min: 1, max: 10 }) ?? 10;
      const data = await am.get("/v1/me/history/heavy-rotation", { query: { limit }, personal: true });
      return { items: (data?.data ?? []).map(resourceOf) };
    },
  },
  {
    name: "list_library_playlists",
    description: "The subscriber's own library playlists. can_edit says whether add_tracks_to_playlist will work on one. Needs a Music User Token.",
    inputSchema: {
      type: "object",
      properties: {
        limit: { type: "integer", description: "1-100. Default 25." },
        offset: { type: "string", description: "Page offset from a previous response's next_offset." },
      },
    },
    annotations: { readOnlyHint: true },
    handler: async (args, env) => {
      const am = new AppleMusic(env);
      const limit = num(args, "limit", { min: 1, max: 100 }) ?? 25;
      const data = await am.get("/v1/me/library/playlists", { query: { limit, offset: str(args, "offset") }, personal: true });
      return compact({ playlists: (data?.data ?? []).map(playlistOf), next_offset: nextOffset(data) });
    },
  },
  {
    name: "list_library_songs",
    description: "Songs in the subscriber's library. Library IDs start with 'i.' and are not catalog IDs. Needs a Music User Token.",
    inputSchema: {
      type: "object",
      properties: {
        limit: { type: "integer", description: "1-100. Default 25." },
        offset: { type: "string", description: "Page offset from a previous response's next_offset." },
      },
    },
    annotations: { readOnlyHint: true },
    handler: async (args, env) => {
      const am = new AppleMusic(env);
      const limit = num(args, "limit", { min: 1, max: 100 }) ?? 25;
      const data = await am.get("/v1/me/library/songs", { query: { limit, offset: str(args, "offset") }, personal: true });
      return compact({ songs: (data?.data ?? []).map(songOf), next_offset: nextOffset(data) });
    },
  },
  {
    name: "get_playlist_tracks",
    description: "Tracks in one library playlist, in playlist order. Needs a Music User Token.",
    inputSchema: {
      type: "object",
      properties: {
        playlist_id: { type: "string", description: "Library playlist ID from list_library_playlists." },
        limit: { type: "integer", description: "1-100. Default 100." },
        offset: { type: "string", description: "Page offset from a previous response's next_offset." },
      },
      required: ["playlist_id"],
    },
    annotations: { readOnlyHint: true },
    handler: async (args, env) => {
      const am = new AppleMusic(env);
      const id = str(args, "playlist_id", { required: true });
      const limit = num(args, "limit", { min: 1, max: 100 }) ?? 100;
      const data = await am.get(`/v1/me/library/playlists/${encodeURIComponent(id)}/tracks`, {
        query: { limit, offset: str(args, "offset") },
        personal: true,
      });
      return compact({ playlist_id: id, tracks: (data?.data ?? []).map(songOf), next_offset: nextOffset(data) });
    },
  },
  {
    name: "create_playlist",
    description: "Create a playlist in the subscriber's library, optionally seeded with tracks. Returns its library ID. Needs a Music User Token.",
    inputSchema: {
      type: "object",
      properties: {
        name: { type: "string", description: "Playlist name." },
        description: { type: "string", description: "Optional description." },
        track_ids: { type: "array", items: { type: "string" }, description: "Catalog song IDs (from search_catalog) or library IDs, in order." },
        track_type: { type: "string", description: "Force the type for every ID: songs, music-videos, library-songs or library-music-videos. Inferred per ID when omitted." },
      },
      required: ["name"],
    },
    handler: async (args, env) => {
      const am = new AppleMusic(env);
      const name = str(args, "name", { required: true });
      const ids = idList(args, "track_ids");
      const forced = str(args, "track_type");
      const body = {
        attributes: compact({ name, description: str(args, "description") }),
        ...(ids.length
          ? { relationships: { tracks: { data: ids.map((id) => ({ id, type: trackType(id, forced) })) } } }
          : {}),
      };
      const data = await am.request("POST", "/v1/me/library/playlists", { body, personal: true });
      const created = data?.data?.[0];
      return compact({ playlist_id: created?.id, name: created?.attributes?.name ?? name, tracks_added: ids.length });
    },
  },
  {
    name: "add_tracks_to_playlist",
    description: "Append tracks to an existing library playlist. Only works where list_library_playlists reports can_edit true. Needs a Music User Token.",
    inputSchema: {
      type: "object",
      properties: {
        playlist_id: { type: "string", description: "Library playlist ID." },
        track_ids: { type: "array", items: { type: "string" }, description: "Catalog song IDs or library IDs, in order." },
        track_type: { type: "string", description: "Force the type for every ID. Inferred per ID when omitted." },
      },
      required: ["playlist_id", "track_ids"],
    },
    handler: async (args, env) => {
      const am = new AppleMusic(env);
      const id = str(args, "playlist_id", { required: true });
      const ids = idList(args, "track_ids", { required: true });
      const forced = str(args, "track_type");
      await am.request("POST", `/v1/me/library/playlists/${encodeURIComponent(id)}/tracks`, {
        body: { data: ids.map((t) => ({ id: t, type: trackType(t, forced) })) },
        personal: true,
      });
      return { playlist_id: id, tracks_added: ids.length };
    },
  },
  {
    name: "add_to_library",
    description: "Add catalog resources to the subscriber's library. Apple accepts the request and silently drops IDs it cannot add, so the counts here are what was requested, not confirmed. Needs a Music User Token.",
    inputSchema: {
      type: "object",
      properties: {
        song_ids: { type: "array", items: { type: "string" }, description: "Catalog song IDs." },
        album_ids: { type: "array", items: { type: "string" }, description: "Catalog album IDs." },
        playlist_ids: { type: "array", items: { type: "string" }, description: "Catalog playlist IDs." },
      },
    },
    handler: async (args, env) => {
      const am = new AppleMusic(env);
      const songs = idList(args, "song_ids");
      const albums = idList(args, "album_ids");
      const playlists = idList(args, "playlist_ids");
      if (!songs.length && !albums.length && !playlists.length) {
        throw new ToolError("give at least one of song_ids, album_ids or playlist_ids");
      }
      await am.request("POST", "/v1/me/library", {
        query: compact({ "ids[songs]": songs, "ids[albums]": albums, "ids[playlists]": playlists }),
        personal: true,
      });
      return {
        requested: compact({
          songs: songs.length || undefined,
          albums: albums.length || undefined,
          playlists: playlists.length || undefined,
        }),
        note: "Apple returned 202 Accepted; items appear in the library after a short delay.",
      };
    },
  },
  {
    name: "rate_song",
    description: "Love (1) or dislike (-1) a song. Apple supports no other values. Catalog and library ratings for the same song stay in sync. Needs a Music User Token.",
    inputSchema: {
      type: "object",
      properties: {
        id: { type: "string", description: "Catalog song ID, or a library ID starting with 'i.'." },
        value: { type: "integer", description: "1 to love, -1 to dislike." },
      },
      required: ["id", "value"],
    },
    handler: async (args, env) => {
      const am = new AppleMusic(env);
      const id = str(args, "id", { required: true });
      const value = num(args, "value", { required: true });
      if (value !== 1 && value !== -1) throw new ToolError("value must be 1 (love) or -1 (dislike)");
      const kind = id.startsWith("i.") ? "library-songs" : "songs";
      await am.request("PUT", `/v1/me/ratings/${kind}/${encodeURIComponent(id)}`, {
        body: { type: "ratings", attributes: { value } },
        personal: true,
      });
      return { id, kind, value };
    },
  },
];

/** Apple paginates with a `next` href carrying an offset query parameter. */
function nextOffset(data) {
  const next = data?.next;
  if (!next) return undefined;
  const m = /[?&]offset=([^&]+)/.exec(next);
  return m ? decodeURIComponent(m[1]) : undefined;
}

// ---- authorize page -----------------------------------------------------------
// Apple issues a Music User Token only to MusicKit running in a browser. This
// page is the whole reason the Worker serves HTML: it signs a short-lived,
// origin-pinned developer token, hands it to MusicKit, and posts the resulting
// user token back. Same idea as hevy-mcp's authorize page.

function page(devToken) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Apple Music MCP</title>
<style>
  :root { color-scheme: light dark; font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #fafafa; color: #111; }
  @media (prefers-color-scheme: dark) { body { background: #111; color: #f4f4f5; } }
  main { max-width: 32rem; padding: 2rem; text-align: center; }
  h1 { font-size: 1.25rem; margin: 0 0 .5rem; }
  p { line-height: 1.5; margin: .5rem 0; }
  button { font: inherit; padding: .6rem 1.2rem; border-radius: .5rem; border: 0; background: #fa233b; color: #fff; cursor: pointer; }
  button[disabled] { opacity: .5; cursor: default; }
  code { font-family: ui-monospace, monospace; font-size: .85em; }
  #msg { min-height: 3rem; }
</style>
</head>
<body>
<main>
  <h1>Apple Music MCP</h1>
  <p>Sign in to Apple Music to give this MCP server a Music User Token. It is stored in the Worker's KV and lasts about six months.</p>
  <p><button id="go" disabled>Loading MusicKit&hellip;</button></p>
  <p id="msg"></p>
</main>
<script src="${MUSICKIT_JS}" data-web-components async></script>
<script>
const msg = document.getElementById("msg");
const go = document.getElementById("go");
document.addEventListener("musickitloaded", async () => {
  try {
    await MusicKit.configure({
      developerToken: ${JSON.stringify(devToken)},
      app: { name: "life-os apple-music-mcp", build: "1" },
    });
  } catch (e) {
    msg.textContent = "MusicKit rejected the developer token: " + e.message;
    return;
  }
  go.disabled = false;
  go.textContent = "Authorize Apple Music";
  go.onclick = async () => {
    go.disabled = true;
    msg.textContent = "Waiting for Apple\\u2026";
    try {
      const music = MusicKit.getInstance();
      await music.authorize();
      const token = music.musicUserToken;
      if (!token) throw new Error("Apple returned no Music User Token");
      const res = await fetch(location.pathname, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ musicUserToken: token }),
      });
      if (!res.ok) throw new Error(await res.text());
      const body = await res.json();
      msg.textContent = "Done. Storefront " + (body.storefront || "unknown") + ". You can close this tab.";
    } catch (e) {
      go.disabled = false;
      msg.textContent = "Failed: " + (e && e.message ? e.message : e);
    }
  };
});
</script>
</body>
</html>`;
}

function pathSecretOk(url, env) {
  const token = env.MCP_TOKEN;
  if (!token) return false;
  const rest = url.pathname.replace(/^\/authorize\/?/, "").replace(/\/+$/, "");
  return timingSafeEqual(decodeURIComponent(rest), token);
}

async function authorize(request, env, url) {
  if (!pathSecretOk(url, env)) return new Response("Unauthorized", { status: 401 });
  const am = new AppleMusic(env);

  if (request.method === "GET") {
    let devToken;
    try {
      devToken = await am.mint(PAGE_TOKEN_EXP_S, url.origin);
    } catch (e) {
      return new Response(e.message, { status: 500 });
    }
    return new Response(page(devToken), {
      headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
    });
  }

  if (request.method !== "POST") return new Response("Method not allowed", { status: 405 });

  let body;
  try {
    body = await request.json();
  } catch {
    return new Response("Expected JSON", { status: 400 });
  }
  const token = typeof body?.musicUserToken === "string" ? body.musicUserToken.trim() : "";
  if (!token) return new Response("musicUserToken is required", { status: 400 });

  // Prove the token works before storing it. Re-authorizing with a token Apple
  // rejects must not cost a working one.
  let storefront;
  try {
    const data = await am.get("/v1/me/storefront", { personal: true, userToken: token });
    storefront = data?.data?.[0]?.id;
  } catch (e) {
    return new Response(`Apple rejected the new token, so the stored one is untouched: ${e.message}`, { status: 502 });
  }

  await env.KV.put(USER_TOKEN_KEY, JSON.stringify({ token, stored_at: new Date().toISOString() }));
  if (storefront) await env.KV.put(STOREFRONT_KEY, storefront);
  return Response.json({ ok: true, storefront });
}

// ---- entrypoint ---------------------------------------------------------------

const server = {
  name: "apple-music",
  version: "1.0.0",
  instructions: [
    "Apple Music for Santi's account. Catalog search needs no user token; everything under the library, history and ratings needs the Music User Token stored by the authorize page.",
    "IDs are not interchangeable: catalog IDs are numeric and come from search_catalog; library IDs start with 'i.' and come from list_library_* . Pass catalog IDs when adding newly found music, library IDs when operating on what is already saved.",
    "get_recently_played is the listening history read. get_heavy_rotation is a month-scale summary, not a timeline.",
    "If any tool reports an auth problem, call auth_status before retrying.",
  ].join("\n"),
  tools,
};

const rpc = mcpHandler(server);

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    // Tool handlers and error messages quote the authorize URL, so the Worker
    // needs to know the host it was reached on.
    const scoped = { ...env, SELF_ORIGIN: url.origin };
    if (url.pathname === "/authorize" || url.pathname.startsWith("/authorize/")) {
      return authorize(request, scoped, url);
    }
    return rpc(request, scoped, ctx);
  },
};
