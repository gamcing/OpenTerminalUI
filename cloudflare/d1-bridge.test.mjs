import test from "node:test";
import assert from "node:assert/strict";
import { handleD1Bridge } from "./d1-bridge.mjs";

const secret = "test-secret-only-not-for-production-1234567890";
const makeRequest = (payload, token = secret) => new Request("https://test.example/batch", {
  method: "POST",
  headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
  body: JSON.stringify(payload),
});
function fakeDb() {
  return {
    batches: [],
    prepare(sql) { return { bind(...params) { return { sql, params }; } }; },
    async batch(statements) { this.batches.push(statements); return statements.map(() => ({ success: true })); },
  };
}

test("accepts a bounded batch and preserves parameter bindings", async () => {
  const db = fakeDb();
  const res = await handleD1Bridge(makeRequest({ statements: [
    { sql: "INSERT INTO t VALUES (?, ?, ?)", params: ["name", true, [0, 255]] },
    { sql: "SELECT * FROM t WHERE id = ?", params: [7] },
  ] }), { DB: db, D1_BRIDGE_TOKEN: secret });
  assert.equal(res.status, 200);
  assert.equal(db.batches.length, 1);
  assert.equal(db.batches[0][0].params[1], 1);
  assert.deepEqual([...new Uint8Array(db.batches[0][0].params[2])], [0, 255]);
  assert.equal(db.batches[0][1].params[0], 7);
  assert.equal((await res.json()).results.length, 2);
});

test("fails closed for missing configuration and unauthorized callers", async () => {
  const db = fakeDb();
  for (const [configured, supplied, expected] of [[undefined, secret, 503], ["short", "short", 503], [secret, "wrong", 401]]) {
    const res = await handleD1Bridge(makeRequest({ statements: [{ sql: "SELECT 1", params: [] }] }, supplied), { DB: db, D1_BRIDGE_TOKEN: configured });
    assert.equal(res.status, expected);
  }
  assert.equal(db.batches.length, 0);
});

test("rejects invalid batches before executing any statement", async () => {
  const db = fakeDb();
  const inputs = [
    { statements: [] },
    { statements: [{ sql: "SELECT 1", params: [] }, { sql: "BEGIN", params: [] }] },
    { statements: [{ sql: "SELECT ?", params: [{}] }] },
    { statements: Array.from({ length: 51 }, () => ({ sql: "SELECT 1", params: [] })) },
  ];
  for (const input of inputs) {
    assert.equal((await handleD1Bridge(makeRequest(input), { DB: db, D1_BRIDGE_TOKEN: secret })).status, 400);
  }
  assert.equal(db.batches.length, 0);
});

test("rejects oversized request bodies and does not expose database errors", async () => {
  const db = fakeDb();
  const huge = makeRequest({ statements: [{ sql: "SELECT ?", params: ["x".repeat(1024 * 1024)] }] });
  assert.equal((await handleD1Bridge(huge, { DB: db, D1_BRIDGE_TOKEN: secret })).status, 413);
  db.batch = async () => { throw new Error("sensitive data"); };
  const res = await handleD1Bridge(makeRequest({ statements: [{ sql: "SELECT 1", params: [] }] }), { DB: db, D1_BRIDGE_TOKEN: secret });
  assert.equal(res.status, 422);
  assert.doesNotMatch(await res.text(), /sensitive/);
});
