// Container lifecycle logic is kept separate from cloudflare:workers so that
// cold starts, failure handling and private egress routing can be tested.
export const D1_INTERNAL_HOST = "openterminal-d1.internal";
export const R2_INTERNAL_HOST = "openterminal-r2.internal";
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
  if (env.OPENTERMINALUI_CF_D1_SNAPSHOT_EXPERIMENT === "1") {
    if (!env.DB || typeof env.D1_BRIDGE_TOKEN !== "string" || env.D1_BRIDGE_TOKEN.length < 32 ||
        typeof env.OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY !== "string" ||
        env.OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY.length !== 44)
      throw new Error("experimental D1 snapshot storage requires a database and separate secrets");
    Object.assign(result, {
      OPENTERMINALUI_DATABASE_STORE: "d1_snapshot",
      OPENTERMINALUI_D1_SNAPSHOT_EXPERIMENT: "1",
      OPENTERMINALUI_D1_CONTAINER_EXPERIMENT: "1",
      OPENTERMINALUI_PROVIDER_KEYS_STORE: "database",
      OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY: env.OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY,
    });
  }
  if (env.OPENTERMINALUI_CF_R2_EXPERIMENT === "1") {
    if (!env.BACKTESTS || typeof env.R2_BRIDGE_TOKEN !== "string" || env.R2_BRIDGE_TOKEN.length < 32)
      throw new Error("experimental R2 storage requires a bucket and gateway token");
    Object.assign(result, {
      OPENTERMINALUI_BACKTEST_STORE: "r2",
      OPENTERMINALUI_R2_CONTAINER_EXPERIMENT: "1",
      R2_GATEWAY_URL: `http://${R2_INTERNAL_HOST}`,
      R2_BRIDGE_TOKEN: env.R2_BRIDGE_TOKEN,
    });
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
    await this.installPrivateIntercepts(container);
    containerEnvironment(this.env); // validate the current experimental configuration
    await container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS);
    this.observeExit(container);
  }

  async installPrivateIntercepts(container) {
    // Always restore both routes, including when the R2 setting was turned
    // off while an older container with R2 credentials is still running.
    const d1 = this.ctx.exports.D1Gateway({ props: {} });
    const r2 = this.ctx.exports.R2Gateway({ props: {} });
    await container.interceptOutboundHttp(D1_INTERNAL_HOST, d1);
    await container.interceptOutboundHttp(R2_INTERNAL_HOST, r2);
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
    const environment = containerEnvironment(this.env);
    await this.installPrivateIntercepts(container);
    if (!container.running) {
      container.start({
        image: container.images.base,
        instance: "standard-1",
        enableInternet: true,
        env: environment,
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
