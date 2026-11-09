// Private, bounded R2 gateway for backtest artifacts; not a public Worker route.
const MAX_BYTES = 8 * 1024 * 1024;
const PREFIX = "backtests/";
const NAME = /^([A-Za-z0-9_-]{1,128})-([a-f0-9]{64})\.(parquet|csv)$/;
const encoder = new TextEncoder();

function reply(status, body) {
  return Response.json(body, { status, headers: { "cache-control": "no-store" } });
}
async function authorized(request, secret) {
  const auth = request.headers.get("authorization") || "";
  if (typeof secret !== "string" || secret.length < 32 || !auth.startsWith("Bearer ") || auth.length > 1024) return false;
  const [a, b] = await Promise.all([crypto.subtle.digest("SHA-256", encoder.encode(secret)),
                                   crypto.subtle.digest("SHA-256", encoder.encode(auth.slice(7)))]);
  let difference = 0;
  const left = new Uint8Array(a), right = new Uint8Array(b);
  for (let i = 0; i < left.length; i++) difference |= left[i] ^ right[i];
  return difference === 0;
}
function hex(buffer) {
  return Array.from(new Uint8Array(buffer), x => x.toString(16).padStart(2, "0")).join("");
}
async function readBounded(request) {
  const length = request.headers.get("content-length");
  if (length && (!/^\d+$/.test(length) || Number(length) > MAX_BYTES)) return null;
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader(), parts = [];
  let size = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > MAX_BYTES) { await reader.cancel(); return null; }
    parts.push(value);
  }
  const result = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) { result.set(part, offset); offset += part.length; }
  return result;
}
export async function handleR2Bridge(request, env) {
  const url = new URL(request.url);
  if (!env.BACKTESTS || typeof env.R2_BRIDGE_TOKEN !== "string" || env.R2_BRIDGE_TOKEN.length < 32)
    return reply(503, { error: "object storage unavailable" });
  if (!await authorized(request, env.R2_BRIDGE_TOKEN)) return reply(401, { error: "unauthorized" });
  try {
    if (url.pathname === "/objects" && request.method === "GET") {
      const cursor = url.searchParams.get("cursor");
      if (url.searchParams.size > (cursor === null ? 0 : 1) || (cursor !== null && (cursor.length === 0 || cursor.length > 2048)))
        return reply(400, { error: "invalid cursor" });
      const page = await env.BACKTESTS.list({ prefix: PREFIX, limit: 500, ...(cursor ? { cursor } : {}) });
      const items = page.objects.filter(object => object.key.startsWith(PREFIX) && NAME.test(object.key.slice(PREFIX.length))).map(object => ({
        name: object.key.slice(PREFIX.length), bytes: object.size, updated_at: object.uploaded.getTime() / 1000,
      }));
      return reply(200, { items, cursor: page.truncated ? page.cursor : null });
    }
    const match = /^\/objects\/([^/]+)$/.exec(url.pathname);
    const name = match && NAME.exec(match[1]);
    if (!name || url.search) return reply(404, { error: "not found" });
    const key = PREFIX + match[1];
    if (request.method === "PUT") {
      const bytes = await readBounded(request);
      if (bytes === null) return reply(413, { error: "object too large" });
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      if (hex(digest) !== name[2]) return reply(400, { error: "digest mismatch" });
      const stored = await env.BACKTESTS.put(key, bytes, {
        onlyIf: new Headers({ "If-None-Match": "*" }), sha256: digest,
        httpMetadata: { contentType: "application/octet-stream" },
      });
      if (stored === null) {
        const existing = await env.BACKTESTS.get(key);
        if (!existing || existing.size !== bytes.length || existing.size > MAX_BYTES ||
            hex(await crypto.subtle.digest("SHA-256", await existing.arrayBuffer())) !== name[2])
          return reply(409, { error: "existing object mismatch" });
      }
      return reply(200, { name: match[1], bytes: bytes.length });
    }
    if (request.method === "GET") {
      const object = await env.BACKTESTS.get(key);
      if (!object) return reply(404, { error: "not found" });
      if (object.size > MAX_BYTES) return reply(500, { error: "invalid object" });
      const bytes = await object.arrayBuffer();
      if (bytes.byteLength !== object.size || hex(await crypto.subtle.digest("SHA-256", bytes)) !== name[2])
        return reply(500, { error: "invalid object" });
      return new Response(bytes, { headers: { "cache-control": "no-store", "content-type": "application/octet-stream" } });
    }
    return reply(405, { error: "method not allowed" });
  } catch {
    return reply(503, { error: "object storage unavailable" });
  }
}
export default { fetch: handleR2Bridge };
