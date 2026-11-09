import test from "node:test";
import assert from "node:assert/strict";
import { BACKEND_PORT, ContainerRuntime, D1_INTERNAL_HOST, INACTIVITY_TIMEOUT_MS, containerEnvironment } from "./container-runtime.mjs";

function setup({ initiallyRunning = false, probeFailures = 0, failIntercept = false } = {}) {
  const events = [];
  const port = { async fetch(request) {
    const url = typeof request === "string" ? request : request.url;
    events.push(["fetch", url]);
    if (url === "http://container/health" && probeFailures-- > 0) throw Error("starting");
    return new Response(url === "http://container/health" ? "ok" : "backend response");
  } };
  const container = {
    running: initiallyRunning,
    images: { base: "image-reference" },
    async interceptOutboundHttp(host, target) {
      events.push(["intercept", host, target]);
      if (failIntercept) throw Error("cannot install intercept");
    },
    start(options) { events.push(["start", options]); this.running = true; },
    async setInactivityTimeout(ms) { events.push(["timeout", ms]); },
    monitor() { events.push(["monitor"]); return new Promise(() => {}); },
    getTcpPort(number) { assert.equal(number, BACKEND_PORT); return port; },
  };
  const ctx = { container, exports: { D1Gateway: () => "private-service" }, waitUntil: () => {} };
  const env = { JWT_SECRET_KEY: "secret", D1_BRIDGE_TOKEN: "token", UNRELATED_SECRET: "must-not-forward" };
  return { events, container, ctx, env, runtime: new ContainerRuntime(ctx, env, async () => {}) };
}

test("installs private D1 routing before starting and forwards the request", async () => {
  const { events, runtime } = setup({ probeFailures: 1 });
  const result = await runtime.fetch(new Request("https://example.com/api/hello?q=1"));
  assert.equal(await result.text(), "backend response");
  assert.equal(events[0][0], "intercept");
  assert.equal(events[0][1], D1_INTERNAL_HOST);
  const start = events.find(e => e[0] === "start")[1];
  assert.equal(start.instance, "standard-1");
  assert.equal(start.enableInternet, true);
  assert.equal(start.env.OPENTERMINALUI_ENV, "production");
  assert.equal(start.env.OPENTERMINALUI_CF_CONTAINER, "1");
  assert.equal(start.env.D1_GATEWAY_URL, `http://${D1_INTERNAL_HOST}`);
  assert.equal(start.env.UNRELATED_SECRET, undefined);
  assert.equal(events.find(e => e[0] === "timeout")[1], INACTIVITY_TIMEOUT_MS);
  assert.equal(events.filter(e => e[0] === "fetch").at(-1)[1], "http://container/api/hello?q=1");
});

test("concurrent requests share a single startup", async () => {
  const { events, runtime } = setup({ probeFailures: 1 });
  await Promise.all([
    runtime.fetch(new Request("https://example.com/api/one")),
    runtime.fetch(new Request("https://example.com/api/two")),
  ]);
  assert.equal(events.filter(e => e[0] === "start").length, 1);
  assert.equal(events.filter(e => e[0] === "intercept").length, 1);
});

test("does not start a container when the private intercept cannot be installed", async () => {
  const { events, runtime } = setup({ failIntercept: true });
  await assert.rejects(runtime.fetch(new Request("https://example.com/api/one")));
  assert.equal(events.filter(e => e[0] === "start").length, 0);
});

test("restores a running container without starting another one", async () => {
  const { events, runtime } = setup({ initiallyRunning: true });
  await runtime.restore();
  assert.equal(events.filter(e => e[0] === "start").length, 0);
  assert.equal(events.filter(e => e[0] === "intercept").length, 1);
  assert.equal(events.filter(e => e[0] === "monitor").length, 1);
});

test("does not allow a binding to override production mode", () => {
  assert.equal(containerEnvironment({ OPENTERMINALUI_ENV: "development" }).OPENTERMINALUI_ENV, "production");
});
