import test from "node:test";
import assert from "node:assert/strict";
import { handleRequest } from "./worker.mjs";

function environment(files = {}) {
  const paths = [];
  return {
    BACKEND_ORIGIN: "https://backend.example",
    paths,
    ASSETS: { async fetch(request) {
      const path = new URL(request.url).pathname;
      paths.push(path);
      return Object.hasOwn(files, path) ? new Response(files[path]) : new Response("missing", { status: 404 });
    } },
  };
}

const request = (path, init) => new Request(`https://app.example${path}`, init);

test("proxies API requests with method, query and credentials", async () => {
  const env = environment();
  let forwarded;
  const response = await handleRequest(request("/api/portfolio?id=1", {
    method: "POST", headers: { authorization: "Bearer test", cookie: "csrf=abc" }, body: "payload",
  }), env, async (req, options) => {
    forwarded = req;
    assert.equal(options.redirect, "manual");
    return new Response("ok", { status: 201 });
  });
  assert.equal(response.status, 201);
  assert.equal(forwarded.url, "https://backend.example/api/portfolio?id=1");
  assert.equal(forwarded.method, "POST");
  assert.equal(forwarded.headers.get("authorization"), "Bearer test");
  assert.equal(forwarded.headers.get("cookie"), "csrf=abc");
  assert.equal(await forwarded.text(), "payload");
  assert.deepEqual(env.paths, []);
});

test("refuses missing, unsafe, or recursive backend origins", async () => {
  for (const origin of [undefined, "http://backend.example", "https://user:pass@backend.example", "https://backend.example/path", "https://app.example"]) {
    const env = environment();
    env.BACKEND_ORIGIN = origin;
    const response = await handleRequest(request("/api/test"), env, () => { throw Error("must not proxy"); });
    assert.equal(response.status, 503);
  }
});

test("proxies websocket upgrades and health paths", async () => {
  const env = environment();
  for (const path of ["/api/ws/quotes", "/health", "/healthz", "/metrics-lite"]) {
    await handleRequest(request(path, { headers: { upgrade: "websocket" } }), env, async req => {
      assert.equal(new URL(req.url).pathname, path);
      assert.equal(req.headers.get("upgrade"), "websocket");
      return new Response("ok");
    });
  }
});

test("serves existing assets and the appropriate frontend shell", async () => {
  const env = environment({ "/app.html": "app", "/index.html": "landing", "/asset.js": "js" });
  assert.equal(await (await handleRequest(request("/asset.js"), env)).text(), "js");
  assert.equal(await (await handleRequest(request("/portfolio/123"), env)).text(), "app");
  assert.equal(await (await handleRequest(request("/unknown"), env)).text(), "landing");
  assert.equal((await handleRequest(request("/assets/missing.js"), env)).status, 404);
  assert.equal((await handleRequest(request("/missing.css"), env)).status, 404);
  assert.equal((await handleRequest(request("/portfolio", { method: "POST" }), env)).status, 405);
});
