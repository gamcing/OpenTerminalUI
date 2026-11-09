// Internal D1 gateway for the future container backend. Not wired into the
// production Worker until the application data-access layer has been migrated.
const MAX_BODY_BYTES = 1024 * 1024;
const MAX_STATEMENTS = 50;
const MAX_SQL_BYTES = 64 * 1024;
const encoder = new TextEncoder();

function response(status, body) {
  return Response.json(body, { status, headers: { "cache-control": "no-store" } });
}

async function authorized(request, secret) {
  if (typeof secret !== "string" || secret.length < 32) return false;
  const auth = request.headers.get("authorization") || "";
  if (!auth.startsWith("Bearer ") || auth.length > 1024) return false;
  const [expected, actual] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(secret)),
    crypto.subtle.digest("SHA-256", encoder.encode(auth.slice(7))),
  ]);
  const left = new Uint8Array(expected);
  const right = new Uint8Array(actual);
  let mismatch = 0;
  for (let i = 0; i < left.length; i++) mismatch |= left[i] ^ right[i];
  return mismatch === 0;
}

async function readBody(request) {
  const declared = Number(request.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > MAX_BODY_BYTES) return null;
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > MAX_BODY_BYTES) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
}

function bindValue(value) {
  if (value === null || typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "boolean") return value ? 1 : 0;
  if (Array.isArray(value) && value.every(v => Number.isInteger(v) && v >= 0 && v <= 255)) {
    return new Uint8Array(value).buffer;
  }
  throw new Error("invalid parameter");
}

function prepareStatements(db, body) {
  if (!body || typeof body !== "object" || !Array.isArray(body.statements) ||
      body.statements.length === 0 || body.statements.length > MAX_STATEMENTS) {
    throw new Error("invalid statements");
  }
  return body.statements.map(item => {
    if (!item || typeof item.sql !== "string" || !item.sql.trim() ||
        encoder.encode(item.sql).length > MAX_SQL_BYTES || !Array.isArray(item.params) ||
        item.params.length > 100) throw new Error("invalid statement");
    // D1 batch owns the transaction. Callers cannot open one across requests.
    if (/^\s*(?:BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b/i.test(item.sql)) {
      throw new Error("explicit transactions are unsupported");
    }
    return db.prepare(item.sql).bind(...item.params.map(bindValue));
  });
}

export async function handleD1Bridge(request, env) {
  if (new URL(request.url).pathname !== "/batch") return response(404, { error: "not found" });
  if (request.method !== "POST") return response(405, { error: "method not allowed" });
  if (!env.DB || typeof env.D1_BRIDGE_TOKEN !== "string" || env.D1_BRIDGE_TOKEN.length < 32) {
    return response(503, { error: "database gateway not configured" });
  }
  if (!(await authorized(request, env.D1_BRIDGE_TOKEN))) return response(401, { error: "unauthorized" });
  if (!/^application\/json(?:\s*;|$)/i.test(request.headers.get("content-type") || "")) {
    return response(415, { error: "expected application/json" });
  }
  let statements;
  try {
    const text = await readBody(request);
    if (text === null) return response(413, { error: "request too large" });
    statements = prepareStatements(env.DB, JSON.parse(text));
  } catch {
    return response(400, { error: "invalid request" });
  }
  try {
    // D1 batch executes the entire list in one transaction or rolls it back.
    return response(200, { results: await env.DB.batch(statements) });
  } catch {
    return response(422, { error: "database batch failed" });
  }
}

export default { fetch: handleD1Bridge };
