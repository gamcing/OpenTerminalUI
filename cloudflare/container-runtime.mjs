// Container lifecycle logic is kept separate from cloudflare:workers so that
// cold starts, failure handling and private egress routing can be tested.
export const D1_INTERNAL_HOST = "openterminal-d1.internal";
export const BACKEND_PORT = 8000;
export const INACTIVITY_TIMEOUT_MS = 30 * 60 * 1000;

const forwardedSettings = [
  "JWT_SECRET_KEY", "CACHE_SIGNING_KEY", "BOOTSTRAP_ADMIN_EMAIL", "BOOTSTRAP_ADMIN_PASSWORD",
  "DATABASE_URL", "OPENTERMINALUI_SQLITE_URL", "D1_BRIDGE_TOKEN", "REDIS_URL",
  "FMP_API_KEY", "FINNHUB_API_KEY", "FINNHUB_WS_ENABLED", "FRED_API_KEY",
  "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_FEED", "ALPACA_ADJUSTMENT",
  "KITE_API_KEY", "KITE_API_SECRET", "KITE_ACCESS_TOKEN", "FXMACRODATA_API_KEY",
  "OPENAI_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "GEMINI_API_KEY",
  "AGENT_PROVIDER", "AGENT_MODEL", "AGENT_FALLBACK_MODELS", "AGENT_DEBATE_ENABLED",
  "OPENTERMINALUI_PREFETCH_ENABLED", "OPENTERMINALUI_CORS_ORIGINS", "OPENTERMINALUI_SAVED_VIEWS_STORE",
];

// Explicit opt-in: keeping a container warm incurs continuous compute charges.
// A scheduled invocation only wakes the same singleton used for user traffic.
export async function keepScheduledContainerWarm(env) {
  if (env.OPENTERMINALUI_CF_KEEPALIVE !== "1") return false;
  await env.APP_CONTAINER.getByName("singleton").keepAlive();
  return true;
}

export function containerEnvironment(env) {
  const result = {
    OPENTERMINALUI_ENV: "production",
    OPENTERMINALUI_CF_CONTAINER: "1",
    D1_GATEWAY_URL: `http://${D1_INTERNAL_HOST}`,
  };
  for (const name of forwardedSettings) {
    if (typeof env[name] === "string" && env[name] !== "") result[name] = env[name];
  }
  return result;
}

export class ContainerRuntime {
  constructor(ctx, env, wait = ms => new Promise(resolve => setTimeout(resolve, ms))) {
    this.ctx = ctx;
    this.env = env;
    this.wait = wait;
    this.starting = null;
    this.monitoring = null;
  }

  async restore() {
    const container = this.ctx.container;
    if (!container?.running) return;
    await this.installD1Intercept(container);
    await container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS);
    this.observeExit(container);
  }

  async installD1Intercept(container) {
    // Register before starting, so an intercept failure never falls back to
    // public DNS with the database token. The gateway is a loopback service,
    // not a route on the public Worker.
    const gateway = this.ctx.exports.D1Gateway({ props: {} });
    await container.interceptOutboundHttp(D1_INTERNAL_HOST, gateway);
  }

  observeExit(container) {
    if (this.monitoring) return;
    this.monitoring = container.monitor().catch(() => {
      // Do not log process output or configuration: both may contain secrets.
      console.error("OpenTerminalUI container stopped unexpectedly");
    }).finally(() => { this.monitoring = null; });
    this.ctx.waitUntil(this.monitoring);
  }

  async ensureReady() {
    if (!this.starting) {
      this.starting = this.startAndWait().finally(() => { this.starting = null; });
    }
    return this.starting;
  }

  async startAndWait() {
    const container = this.ctx.container;
    if (!container) throw new Error("Container binding is unavailable");
    await this.installD1Intercept(container);
    if (!container.running) {
      container.start({
        image: container.images.base,
        instance: "standard-1",
        enableInternet: true,
        env: containerEnvironment(this.env),
      });
    }
    await container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS);
    this.observeExit(container);
    const port = container.getTcpPort(BACKEND_PORT);
    for (let attempt = 0; attempt < 100; attempt++) {
      try {
        const probe = await port.fetch("http://container/health", { signal: AbortSignal.timeout(1000) });
        await probe.body?.cancel();
        if (probe.ok) return;
      } catch {
        // The process can need time to migrate its database and bind the port.
      }
      await this.wait(500);
    }
    throw new Error("Container did not become ready");
  }

  async fetch(request) {
    await this.ensureReady();
    const url = new URL(request.url);
    url.protocol = "http:";
    url.host = "container";
    const forwarded = new Request(url, request);
    forwarded.headers.delete("host");
    return this.ctx.container.getTcpPort(BACKEND_PORT).fetch(forwarded);
  }
}
