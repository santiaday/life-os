# Apple Music MCP

`workers/apple-music-mcp` is a Cloudflare Worker. It was originally scoped to
run in Docker at `/opt/apple-music-mcp` on the DigitalOcean droplet; the droplet
is gone (see `CUTOVER.md`), so it follows `cronometer-mcp` and `calendar-mcp`
instead: same `workers/lib/mcp.js`, same path-secret auth, same `workers.dev`
hostname, no reverse proxy and no DNS record to manage.

Apple Music is a good fit for a Worker. The developer token is an ES256 JWT,
which WebCrypto signs natively, so there is no native crypto dependency and
nothing to compile.

## Two tokens, and why only one of them can be automated

| Token | Where it comes from | Lifetime | Needed for |
|---|---|---|---|
| Developer token | This Worker signs it from the MusicKit `.p8` private key | Apple caps `exp` at 15,777,000 s (~6 months); minted for 180 days, cached in KV, re-minted at 150 | Every request |
| Music User Token | Apple issues it **only to MusicKit running in a browser** | ~6 months | Every `/v1/me/*` path: library, history, playlists, ratings |

The Music User Token cannot be minted server-side at any price — that is why
the original plan left `APPLE_MUSIC_USER_TOKEN` blank. Instead the Worker serves
its own authorize page: `GET /authorize/<MCP_TOKEN>` loads MusicKit JS v3, signs
a short-lived developer token pinned to the Worker's own origin, and the page
POSTs the resulting user token back into KV. Same shape as `hevy-mcp`'s
authorize page.

## 1. Apple Developer prerequisites

In the Apple Developer account (Certificates, Identifiers & Profiles → Keys),
create a key with **MusicKit** enabled and download `AuthKey_<KEYID>.p8`. Apple
serves that file exactly once.

Three values come out of this:

- **Team ID** — 10 characters, Membership page in the sidebar.
- **Key ID** — 10 characters, shown on the key you just created.
- **The `.p8` file itself** — a P-256 PKCS#8 PEM.

The `.p8` never goes on a disk this repo can see and never gets `scp`'d
anywhere. It becomes a Worker secret.

## 2. Deploy

The KV namespace already exists (`apple-music-mcp-kv`,
`d7e62ead813e418fb6f0d0400c520e79`) and is wired into `wrangler.toml`.

```sh
cd workers/apple-music-mcp

# Path secret. Generated locally and piped straight in, so it never lands in a
# shell transcript; MCP_TOKEN is kept in the variable only to build the URL.
MCP_TOKEN=$(openssl rand -hex 32)
printf '%s' "$MCP_TOKEN" | npx wrangler secret put MCP_TOKEN

printf '%s' 'ABCDE12345' | npx wrangler secret put APPLE_MUSIC_TEAM_ID
printf '%s' 'FGHIJ67890' | npx wrangler secret put APPLE_MUSIC_KEY_ID
npx wrangler secret put APPLE_MUSIC_PRIVATE_KEY < ~/Downloads/AuthKey_FGHIJ67890.p8

npx wrangler deploy

echo "https://apple-music-mcp.santiagoaday7.workers.dev/mcp/$MCP_TOKEN"
```

`APPLE_MUSIC_PRIVATE_KEY` is the whole file, `-----BEGIN PRIVATE KEY-----` and
`-----END PRIVATE KEY-----` lines included. The Worker strips the armour itself.

Deploying before the secrets are set is harmless — a Worker cannot crash-loop.
Every tool returns a message naming the secrets that are missing, and
`auth_status` lists them.

## 3. Mint the Music User Token

Open this in a browser signed in to the Apple Music subscription, on the same
machine, and approve:

```
https://apple-music-mcp.santiagoaday7.workers.dev/authorize/<MCP_TOKEN>
```

The page verifies the token against `/v1/me/storefront` before keeping it, so a
"Done" message means it actually works. A token Apple rejects is deleted rather
than stored.

Re-run this page when `get_recently_played` or any other library tool reports an
expired token: on a 403 the Worker drops the stored token so the next call says
so plainly instead of failing the same way twice. Expect that roughly every six
months.

## 4. Connect it

claude.ai custom connector (cannot send headers, so the secret rides in the path):

```
https://apple-music-mcp.santiagoaday7.workers.dev/mcp/<MCP_TOKEN>
```

Claude Code:

```sh
claude mcp add --transport http apple-music \
  https://apple-music-mcp.santiagoaday7.workers.dev/mcp \
  --header "Authorization: Bearer <MCP_TOKEN>"
```

## Tools

| Tool | Needs user token | Notes |
|---|---|---|
| `auth_status` | no | What this Worker holds and what is missing. Start here when anything reports an auth problem. |
| `search_catalog` | no | Returns catalog IDs for the write tools. |
| `get_storefront` | yes | Reads and caches the subscriber's storefront. |
| `get_recently_played` | yes | The listening-history read. Apple caps it at 30 per call. |
| `get_heavy_rotation` | yes | Month-scale summary, not a timeline. |
| `list_library_playlists` | yes | `can_edit` says whether appending will work. |
| `list_library_songs` | yes | |
| `get_playlist_tracks` | yes | |
| `create_playlist` | yes | Optionally seeded with tracks. |
| `add_tracks_to_playlist` | yes | |
| `add_to_library` | yes | Apple returns 202 and silently drops IDs it cannot add, so the counts are requested, not confirmed. |
| `rate_song` | yes | 1 or -1. Apple supports nothing else. |

Catalog IDs are numeric and come from `search_catalog`. Library IDs start with
`i.` and come from the `list_library_*` tools. They are not interchangeable; the
write tools infer the type from that prefix and take a `track_type` override.

## Checks

```sh
curl -s https://apple-music-mcp.santiagoaday7.workers.dev/health

curl -s https://apple-music-mcp.santiagoaday7.workers.dev/mcp \
  -H "Authorization: Bearer $MCP_TOKEN" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"auth_status","arguments":{}}}'
```

Tests, including the ES256 signing path and Apple's exact request bodies,
against a fake `api.music.apple.com`:

```sh
deno test --allow-env workers/test/workers.test.js
```

## Rotation

```sh
cd workers/apple-music-mcp
MCP_TOKEN=$(openssl rand -hex 32)
printf '%s' "$MCP_TOKEN" | npx wrangler secret put MCP_TOKEN   # then update the connector URL
```

Rotating the MusicKit key means a new `.p8` and a new Key ID, plus dropping the
JWT the Worker cached under the old key — it holds that for 150 days otherwise.
`wrangler kv` defaults to the local namespace, so `--remote` is not optional
here:

```sh
npx wrangler kv key delete --binding=KV dev_token --remote
```
