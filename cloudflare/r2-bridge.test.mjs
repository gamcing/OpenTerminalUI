import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { handleR2Bridge } from "./r2-bridge.mjs";

const token = "test-secret-only-not-for-production-1234567890";
const digest = bytes => createHash("sha256").update(bytes).digest("hex");
const name = bytes => `bt_123-${digest(bytes)}.csv`;
function bucket() {
  const values = new Map();
  return { values,
    async put(key, data, options) {
      assert.equal(options.onlyIf.get("If-None-Match"), "*");
      if (values.has(key)) return null;
      values.set(key, Uint8Array.from(data));
      return { key };
    },
    async get(key) {
      const value = values.get(key);
      if (!value) return null;
      return { size: value.length, async arrayBuffer() { return Uint8Array.from(value).buffer; } };
    },
    async list() {
      return { objects: [...values].map(([key, value]) => ({ key, size: value.length, uploaded: new Date(0) })), truncated: false };
    },
  };
}
function request(path, method = "GET", body = undefined, auth = token) {
  return new Request("http://r2.test" + path, { method, headers: { authorization: "Bearer " + auth }, ...(body === undefined ? {} : { body }) });
}

test("stores immutable content-addressed artifacts and returns a verified catalog", async () => {
  const BACKTESTS = bucket(), env = { BACKTESTS, R2_BRIDGE_TOKEN: token }, bytes = new TextEncoder().encode("a,b\n1,2\n");
  const path = "/objects/" + name(bytes);
  assert.equal((await handleR2Bridge(request(path, "PUT", bytes), env)).status, 200);
  assert.equal((await handleR2Bridge(request(path, "PUT", bytes), env)).status, 200);
  assert.deepEqual(new Uint8Array(await (await handleR2Bridge(request(path), env)).arrayBuffer()), bytes);
  const list = await (await handleR2Bridge(request("/objects"), env)).json();
  assert.deepEqual(list.items.map(row => row.name), [name(bytes)]);
  assert.equal(list.cursor, null);
  BACKTESTS.values.set("backtests/" + name(bytes), new TextEncoder().encode("corrupt"));
  assert.equal((await handleR2Bridge(request(path), env)).status, 500);
  assert.equal((await handleR2Bridge(request(path, "PUT", bytes), env)).status, 409);
});

test("rejects unauthenticated, malformed, mismatched and oversized writes", async () => {
  const BACKTESTS = bucket(), env = { BACKTESTS, R2_BRIDGE_TOKEN: token }, bytes = new Uint8Array([1, 2]);
  assert.equal((await handleR2Bridge(request("/objects", "GET", undefined, "bad"), env)).status, 401);
  assert.equal((await handleR2Bridge(request("/objects/../unsafe", "PUT", bytes), env)).status, 404);
  assert.equal((await handleR2Bridge(request("/objects/" + name(new Uint8Array([9])), "PUT", bytes), env)).status, 400);
  assert.equal((await handleR2Bridge(request("/objects/" + name(bytes), "PUT", new Uint8Array(8 * 1024 * 1024 + 1)), env)).status, 413);
  assert.equal((await handleR2Bridge(request("/objects?cursor="), env)).status, 400);
  assert.equal(BACKTESTS.values.size, 0);
  assert.equal((await handleR2Bridge(request("/objects"), {})).status, 503);
});
