import test from "node:test";
import assert from "node:assert/strict";
import { BACKEND_PORT, ContainerRuntime, D1_INTERNAL_HOST, R2_INTERNAL_HOST, INACTIVITY_TIMEOUT_MS, containerEnvironment, keepScheduledContainerWarm } from "./container-runtime.mjs";

function setup({ initiallyRunning = false, probeFailures = 0, unhealthy = false, poisoned = false,
  stayPoisoned = false, failDestroy = false, failIntercept = false, failHost = "" } = {}) {
  const events = [];
  let resolveMonitor;
  const port = { async fetch(request) {
    const url = typeof request === "string" ? request : request.url;
    events.push(["fetch", url]);
    if (url === "http://container/health" && probeFailures-- > 0) throw Error("starting");
    if (url === "http://container/health" && poisoned) return new Response("unavailable", {
      status: 503, headers: { "X-OpenTerminalUI-Recovery": "d1-snapshot" },
    });
    if (url === "http://container/health" && unhealthy) return new Response("unavailable", { status: 503 });
    return new Response(url === "http://container/health" ? "ok" : "backend response");
  } };
  const container = {
    running: initiallyRunning,
    images: { base: "image-reference" },
    async interceptOutboundHttp(host, target) {
      events.push(["intercept", host, target]);
      if (failIntercept || host === failHost) throw Error("cannot install intercept");
    },
    start(options) { events.push(["start", options]); this.running = true; },
    async setInactivityTimeout(ms) { events.push(["timeout", ms]); },
    monitor() { events.push(["monitor"]); return new Promise(resolve => { resolveMonitor = resolve; }); },
    async destroy() {
      events.push(["destroy"]);
      if (failDestroy) throw Error("destroy failed");
      this.running = false;
      if (!stayPoisoned) poisoned = false;
      resolveMonitor?.();
    },
    getTcpPort(number) { assert.equal(number, BACKEND_PORT); return port; },
  };
  const ctx = { container, exports: { D1Gateway: () => "private-service", R2Gateway: () => "private-r2-service" }, waitUntil: () => {} };
  const env = { JWT_SECRET_KEY: "secret", D1_BRIDGE_TOKEN: "token", UNRELATED_SECRET: "must-not-forward" };
  return { events, container, ctx, env, runtime: new ContainerRuntime(ctx, env, async () => {}) };
}

test("installs private D1 routing before starting and forwards the request", async () => {
  const { events, runtime } = setup({ probeFailures: 1 });
  const result = await runtime.fetch(new Request("https://example.com/api/hello?q=1"));
  assert.equal(await result.text(), "backend response");
  assert.equal(events[0][0], "intercept");
  assert.equal(events[0][1], D1_INTERNAL_HOST);
  assert.equal(events[1][1], R2_INTERNAL_HOST);
  const start = events.find(e => e[0] === "start")[1];
  assert.equal(start.instance, "standard-1");
  assert.equal(start.enableInternet, true);
  assert.equal(start.env.OPENTERMINALUI_ENV, "production");
  assert.equal(start.env.OPENTERMINALUI_CF_CONTAINER, "1");
  assert.equal(start.env.D1_GATEWAY_URL, `http://${D1_INTERNAL_HOST}`);
  assert.equal(start.env.UNRELATED_SECRET, undefined);
  assert.equal(start.env.R2_BRIDGE_TOKEN, undefined);
  assert.equal(start.env.OPENTERMINALUI_BACKTEST_STORE, undefined);
  assert.equal(events.find(e => e[0] === "timeout")[1], INACTIVITY_TIMEOUT_MS);
  assert.equal(events.filter(e => e[0] === "fetch").at(-1)[1], "http://container/api/hello?q=1");
});

test("does not forward requests when the running container reports unhealthy", async () => {
  const { events, runtime } = setup({ initiallyRunning: true, unhealthy: true });
  await assert.rejects(runtime.fetch(new Request("https://example.com/api/one")), /did not become ready/);
  assert.equal(events.filter(e => e[0] === "start").length, 0);
  assert.equal(events.filter(e => e[0] === "fetch").length, 100);
  assert.ok(events.filter(e => e[0] === "fetch").every(e => e[1] === "http://container/health"));
});

function enableSnapshot(env) {
  Object.assign(env, { OPENTERMINALUI_CF_D1_SNAPSHOT_EXPERIMENT: "1", DB: {},
    D1_BRIDGE_TOKEN: "d".repeat(32), OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY: "k".repeat(44) });
}

test("recovers a poisoned D1 container once and reinstalls intercepts before restart", async () => {
  const { events, runtime, env } = setup({ initiallyRunning: true, poisoned: true });
  enableSnapshot(env);
  const [a, b] = await Promise.all([
    runtime.fetch(new Request("https://example.com/api/one", { method: "POST" })),
    runtime.fetch(new Request("https://example.com/api/two")),
  ]);
  assert.equal(await a.text(), "backend response");
  assert.equal(await b.text(), "backend response");
  assert.equal(events.filter(e => e[0] === "destroy").length, 1);
  assert.equal(events.filter(e => e[0] === "start").length, 1);
  assert.equal(events.filter(e => e[0] === "monitor").length, 2);
  const destroyed = events.findIndex(e => e[0] === "destroy");
  const started = events.findIndex(e => e[0] === "start");
  assert.deepEqual(events.slice(destroyed + 1, started).map(e => e[0]), ["intercept", "intercept"]);
  assert.equal(events.filter(e => e[0] === "fetch" && e[1] === "http://container/api/one").length, 1);
});

test("limits repeated recovery and never forwards a request to a poisoned replacement", async () => {
  const { events, runtime, env } = setup({ initiallyRunning: true, poisoned: true, stayPoisoned: true });
  enableSnapshot(env);
  runtime.now = () => 1000;
  await assert.rejects(runtime.fetch(new Request("https://example.com/api/one")), /cooling down/);
  await assert.rejects(runtime.fetch(new Request("https://example.com/api/two")), /cooling down/);
  assert.equal(events.filter(e => e[0] === "destroy").length, 1);
  assert.equal(events.filter(e => e[0] === "start").length, 1);
  assert.ok(events.filter(e => e[0] === "fetch").every(e => e[1] === "http://container/health"));
  runtime.now = () => 62000;
  await assert.rejects(runtime.fetch(new Request("https://example.com/api/three")), /cooling down/);
  assert.equal(events.filter(e => e[0] === "destroy").length, 2);
});

test("does not recover without explicit snapshot mode or when destruction fails", async () => {
  const disabled = setup({ initiallyRunning: true, poisoned: true });
  await assert.rejects(disabled.runtime.fetch(new Request("https://example.com/")), /did not become ready/);
  assert.equal(disabled.events.filter(e => e[0] === "destroy").length, 0);
  const broken = setup({ initiallyRunning: true, poisoned: true, failDestroy: true });
  enableSnapshot(broken.env);
  await assert.rejects(broken.runtime.fetch(new Request("https://example.com/")), /destroy failed/);
  assert.equal(broken.events.filter(e => e[0] === "start").length, 0);
  assert.ok(broken.events.filter(e => e[0] === "fetch").every(e => e[1] === "http://container/health"));
});

test("concurrent requests share a single startup", async () => {
  const { events, runtime } = setup({ probeFailures: 1 });
  await Promise.all([
    runtime.fetch(new Request("https://example.com/api/one")),
    runtime.fetch(new Request("https://example.com/api/two")),
  ]);
  assert.equal(events.filter(e => e[0] === "start").length, 1);
  assert.equal(events.filter(e => e[0] === "intercept").length, 2);
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
  assert.equal(events.filter(e => e[0] === "intercept").length, 2);
  assert.equal(events.filter(e => e[0] === "monitor").length, 1);
});

test("explicit D1 snapshot experiment uses only the private gateway and durable settings", async () => {
  const { events, runtime, env } = setup();
  Object.assign(env, { OPENTERMINALUI_CF_D1_SNAPSHOT_EXPERIMENT: "1", DB: {},
    D1_BRIDGE_TOKEN: "d".repeat(32), OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY: "k".repeat(44),
    D1_GATEWAY_URL: "http://attacker.invalid", OPENTERMINALUI_DATABASE_STORE: "sqlalchemy" });
  await runtime.fetch(new Request("https://example.com/api/one"));
  assert.deepEqual(events.slice(0, 3).map(e => e[0]), ["intercept", "intercept", "start"]);
  const config = events[2][1].env;
  assert.equal(config.D1_GATEWAY_URL, `http://${D1_INTERNAL_HOST}`);
  assert.equal(config.OPENTERMINALUI_DATABASE_STORE, "d1_snapshot");
  assert.equal(config.OPENTERMINALUI_D1_SNAPSHOT_EXPERIMENT, "1");
  assert.equal(config.OPENTERMINALUI_D1_CONTAINER_EXPERIMENT, "1");
  assert.equal(config.OPENTERMINALUI_PROVIDER_KEYS_STORE, "database");
  assert.equal(config.OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY, "k".repeat(44));
});

test("D1 experiment fails closed if a required binding, secret, or intercept is missing", async () => {
  for (const missing of ["DB", "D1_BRIDGE_TOKEN", "OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY", "intercept"]) {
    const { events, runtime, env } = setup({ failHost: missing === "intercept" ? D1_INTERNAL_HOST : "" });
    Object.assign(env, { OPENTERMINALUI_CF_D1_SNAPSHOT_EXPERIMENT: "1", DB: {},
      D1_BRIDGE_TOKEN: "d".repeat(32), OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY: "k".repeat(44) });
    if (missing !== "intercept") delete env[missing];
    await assert.rejects(runtime.fetch(new Request("https://example.com/")));
    assert.equal(events.filter(e => e[0] === "start").length, 0);
  }
});

test("worker environment cannot turn on the D1 adapter without its explicit switch", () => {
  const config = containerEnvironment({ DB: {}, D1_BRIDGE_TOKEN: "d".repeat(32),
    OPENTERMINALUI_DATABASE_STORE: "d1_snapshot", OPENTERMINALUI_D1_CONTAINER_EXPERIMENT: "1",
    OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY: "k".repeat(44) });
  assert.equal(config.OPENTERMINALUI_DATABASE_STORE, undefined);
  assert.equal(config.OPENTERMINALUI_D1_CONTAINER_EXPERIMENT, undefined);
  assert.equal(config.OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY, undefined);
});

test("installs both private intercepts before enabling experimental R2", async () => {
  const { events, runtime, env } = setup();
  Object.assign(env, { OPENTERMINALUI_CF_R2_EXPERIMENT: "1", BACKTESTS: {},
    R2_BRIDGE_TOKEN: "r".repeat(32), R2_GATEWAY_URL: "http://attacker.invalid",
    OPENTERMINALUI_BACKTEST_STORE: "local" });
  await runtime.fetch(new Request("https://example.com/api/one"));
  assert.deepEqual(events.slice(0, 3).map(e => e[0]), ["intercept", "intercept", "start"]);
  assert.equal(events[1][1], R2_INTERNAL_HOST);
  assert.equal(events[1][2], "private-r2-service");
  const config = events[2][1].env;
  assert.equal(config.R2_GATEWAY_URL, `http://${R2_INTERNAL_HOST}`);
  assert.equal(config.R2_BRIDGE_TOKEN, "r".repeat(32));
  assert.equal(config.OPENTERMINALUI_R2_CONTAINER_EXPERIMENT, "1");
  assert.equal(config.OPENTERMINALUI_BACKTEST_STORE, "r2");
});

test("refuses start for missing R2 settings or failed R2 intercept", async () => {
  for (const failHost of ["", R2_INTERNAL_HOST]) {
    const { events, runtime, env } = setup({ failHost });
    env.OPENTERMINALUI_CF_R2_EXPERIMENT = "1";
    if (failHost) Object.assign(env, { BACKTESTS: {}, R2_BRIDGE_TOKEN: "r".repeat(32) });
    await assert.rejects(runtime.fetch(new Request("https://example.com/")));
    assert.equal(events.filter(e => e[0] === "start").length, 0);
  }
});

test("restores R2 interception even after the experiment is disabled", async () => {
  const { events, runtime } = setup({ initiallyRunning: true });
  await runtime.restore();
  assert.deepEqual(events.filter(e => e[0] === "intercept").map(e => e[1]), [D1_INTERNAL_HOST, R2_INTERNAL_HOST]);
});

test("a restore fails closed if the R2 intercept cannot be installed", async () => {
  const { events, runtime } = setup({ initiallyRunning: true, failHost: R2_INTERNAL_HOST });
  await assert.rejects(runtime.restore());
  assert.equal(events.filter(e => e[0] === "start" || e[0] === "fetch").length, 0);
  assert.deepEqual(events.filter(e => e[0] === "intercept").map(e => e[1]), [D1_INTERNAL_HOST, R2_INTERNAL_HOST]);
});

test("untrusted worker variables cannot turn on the R2 adapter", () => {
  const config = containerEnvironment({ OPENTERMINALUI_BACKTEST_STORE: "r2",
    OPENTERMINALUI_R2_CONTAINER_EXPERIMENT: "1", R2_GATEWAY_URL: "http://attacker.invalid",
    R2_BRIDGE_TOKEN: "r".repeat(32), BACKTESTS: {} });
  assert.equal(config.OPENTERMINALUI_BACKTEST_STORE, undefined);
  assert.equal(config.R2_BRIDGE_TOKEN, undefined);
  assert.equal(config.R2_GATEWAY_URL, undefined);
});

test("does not allow a binding to override production mode", () => {
  assert.equal(containerEnvironment({ OPENTERMINALUI_ENV: "development" }).OPENTERMINALUI_ENV, "production");
});


test("scheduled keepalive is disabled unless explicitly enabled", async () => {
  let calls = 0;
  const env = { APP_CONTAINER: { getByName() { calls++; throw Error("must not start"); } } };
  assert.equal(await keepScheduledContainerWarm(env), false);
  assert.equal(await keepScheduledContainerWarm({ ...env, OPENTERMINALUI_CF_KEEPALIVE: "true" }), false);
  assert.equal(calls, 0);
});

test("scheduled keepalive reaches the traffic singleton and reports failures", async () => {
  const names = [];
  let wakes = 0;
  const env = { OPENTERMINALUI_CF_KEEPALIVE: "1", APP_CONTAINER: { getByName(name) {
    names.push(name);
    return { async keepAlive() { wakes++; } };
  } } };
  assert.equal(await keepScheduledContainerWarm(env), true);
  assert.deepEqual(names, ["singleton"]);
  assert.equal(wakes, 1);
  env.APP_CONTAINER.getByName = () => ({ async keepAlive() { throw Error("startup failed"); } });
  await assert.rejects(keepScheduledContainerWarm(env), /startup failed/);
});

test("scheduled and foreground activity share container startup", async () => {
  const { events, runtime } = setup({ probeFailures: 1 });
  const env = { OPENTERMINALUI_CF_KEEPALIVE: "1", APP_CONTAINER: { getByName() {
    return { keepAlive: () => runtime.ensureReady() };
  } } };
  await Promise.all([keepScheduledContainerWarm(env), runtime.fetch(new Request("https://example.com/api/one"))]);
  assert.equal(events.filter(e => e[0] === "start").length, 1);
});
