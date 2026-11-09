// Run only against a disposable local Wrangler D1 instance.
import assert from "node:assert/strict";
const endpoint = "http://127.0.0.1:8899/batch";
const token = "test-secret-only-not-for-production-1234567890";
async function send(statements, supplied = token) {
  return fetch(endpoint, {
    method: "POST", headers: { "content-type": "application/json", authorization: `Bearer ${supplied}` },
    body: JSON.stringify({ statements }),
  });
}
const statement = (sql, params = []) => ({ sql, params });
const unauthorized = await send([statement("SELECT 1")], "wrong");
assert.equal(unauthorized.status, 401);
let result = await send([statement("CREATE TABLE IF NOT EXISTS bridge_test (id INTEGER PRIMARY KEY, value TEXT UNIQUE)")]);
assert.equal(result.status, 200, await result.text());
result = await send([statement("DELETE FROM bridge_test")]);
assert.equal(result.status, 200, await result.text());
result = await send([statement("INSERT INTO bridge_test VALUES (?, ?)", [1, "same"]), statement("INSERT INTO bridge_test VALUES (?, ?)", [2, "same"])]);
assert.equal(result.status, 422, await result.text());
result = await send([statement("SELECT count(*) AS n FROM bridge_test")]);
assert.equal(result.status, 200);
assert.equal((await result.json()).results[0].results[0].n, 0, "failed batch must be rolled back");
result = await send([statement("INSERT INTO bridge_test VALUES (?, ?)", [1, "one"]), statement("INSERT INTO bridge_test VALUES (?, ?)", [2, "two"])]);
assert.equal(result.status, 200, await result.text());
result = await send([statement("SELECT count(*) AS n FROM bridge_test")]);
assert.equal((await result.json()).results[0].results[0].n, 2);
console.log("Local D1: authentication, atomic rollback and committed batch verified");
