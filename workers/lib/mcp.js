// Minimal MCP server for Cloudflare Workers: stateless Streamable HTTP, JSON
// responses only (no SSE, no sessions), which is all a tools-only server needs.
//
// Auth is one shared secret, MCP_TOKEN, accepted two ways:
//   Authorization: Bearer <token>   scripts, bots, Claude Code
//   POST /mcp/<token>                claude.ai custom connectors, which can't
//                                    send headers (same path-secret pattern the
//                                    droplet's MCP server used)
//
// A tool is { name, description, inputSchema, handler(args, env, ctx) }. The
// handler returns any JSON-serialisable value; throwing ToolError (or anything
// else) becomes an isError result the model can read, not a JSON-RPC error.

export const PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"];

export class ToolError extends Error {}

function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function authorized(request, env) {
  const token = env.MCP_TOKEN;
  if (!token) return false;
  const header = request.headers.get("authorization") ?? "";
  if (header.startsWith("Bearer ") && timingSafeEqual(header.slice(7).trim(), token)) return true;
  const path = new URL(request.url).pathname.replace(/\/+$/, "");
  return path.startsWith("/mcp/") && timingSafeEqual(decodeURIComponent(path.slice(5)), token);
}

const rpcResult = (id, result) => ({ jsonrpc: "2.0", id, result });
const rpcError = (id, code, message) => ({ jsonrpc: "2.0", id, error: { code, message } });

async function handleMessage(msg, server, env, ctx) {
  if (!msg || msg.jsonrpc !== "2.0" || typeof msg.method !== "string") {
    return rpcError(msg?.id ?? null, -32600, "Invalid Request");
  }
  const isNotification = msg.id === undefined || msg.id === null;
  switch (msg.method) {
    case "initialize": {
      const asked = msg.params?.protocolVersion;
      return rpcResult(msg.id, {
        protocolVersion: PROTOCOL_VERSIONS.includes(asked) ? asked : PROTOCOL_VERSIONS[0],
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: server.name, version: server.version },
        instructions: server.instructions,
      });
    }
    case "ping":
      return rpcResult(msg.id, {});
    case "tools/list":
      return rpcResult(msg.id, {
        tools: server.tools.map(({ name, description, inputSchema, annotations }) =>
          ({ name, description, inputSchema, ...(annotations ? { annotations } : {}) })),
      });
    case "tools/call": {
      const tool = server.tools.find((t) => t.name === msg.params?.name);
      if (!tool) return rpcError(msg.id, -32602, `Unknown tool: ${msg.params?.name}`);
      try {
        const out = await tool.handler(msg.params?.arguments ?? {}, env, ctx);
        return rpcResult(msg.id, {
          content: [{ type: "text", text: JSON.stringify(out, null, 2) }],
          structuredContent: out && typeof out === "object" && !Array.isArray(out) ? out : { result: out },
        });
      } catch (e) {
        console.error(`tool ${tool.name} failed`, e);
        return rpcResult(msg.id, {
          content: [{ type: "text", text: e instanceof Error ? e.message : String(e) }],
          isError: true,
        });
      }
    }
    default:
      if (isNotification) return null; // notifications/initialized and friends
      return rpcError(msg.id, -32601, `Method not found: ${msg.method}`);
  }
}

/** Build a Worker fetch handler serving `server` at /mcp. */
export function mcpHandler(server) {
  return async (request, env, ctx) => {
    const path = new URL(request.url).pathname;
    if (path === "/health") return Response.json({ ok: true, server: server.name });
    if (path !== "/mcp" && !path.startsWith("/mcp/")) return new Response("Not found", { status: 404 });
    if (!authorized(request, env)) return new Response("Unauthorized", { status: 401 });
    if (request.method === "GET") return new Response("SSE not supported; POST JSON-RPC", { status: 405 });
    if (request.method === "DELETE") return new Response(null, { status: 204 });
    if (request.method !== "POST") return new Response("Method not allowed", { status: 405 });

    let body;
    try {
      body = await request.json();
    } catch {
      return Response.json(rpcError(null, -32700, "Parse error"), { status: 400 });
    }
    const batch = Array.isArray(body);
    const replies = (await Promise.all((batch ? body : [body]).map((m) => handleMessage(m, server, env, ctx))))
      .filter((r) => r !== null);
    if (!replies.length) return new Response(null, { status: 202 });
    return Response.json(batch ? replies : replies[0]);
  };
}

// ---- argument helpers ---------------------------------------------------------

export function str(args, key, { required = false, pattern, desc } = {}) {
  const v = args[key];
  if (v === undefined || v === null || v === "") {
    if (required) throw new ToolError(`${key} is required`);
    return undefined;
  }
  if (typeof v !== "string") throw new ToolError(`${key} must be a string`);
  if (pattern && !pattern.test(v)) throw new ToolError(`${key} must be ${desc ?? pattern}`);
  return v;
}

export function num(args, key, { required = false, min, max } = {}) {
  const v = args[key];
  if (v === undefined || v === null || v === "") {
    if (required) throw new ToolError(`${key} is required`);
    return undefined;
  }
  const n = Number(v);
  if (!Number.isFinite(n)) throw new ToolError(`${key} must be a number`);
  if (min !== undefined && n < min) throw new ToolError(`${key} must be >= ${min}`);
  if (max !== undefined && n > max) throw new ToolError(`${key} must be <= ${max}`);
  return n;
}

export const DAY = /^\d{4}-\d{2}-\d{2}$/;
