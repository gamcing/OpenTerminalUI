import { DurableObject, WorkerEntrypoint } from "cloudflare:workers";
import { ContainerRuntime, keepScheduledContainerWarm } from "./container-runtime.mjs";
import { handleD1Bridge } from "./d1-bridge.mjs";
import { handleR2Bridge } from "./r2-bridge.mjs";

// This named entrypoint is only reachable through the container's outbound
// intercept. The public Worker does not route requests to it.
export class D1Gateway extends WorkerEntrypoint {
  fetch(request) { return handleD1Bridge(request, this.env); }
}

export class R2Gateway extends WorkerEntrypoint {
  fetch(request) { return handleR2Bridge(request, this.env); }
}

export class OpenTerminalContainer extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.runtime = new ContainerRuntime(ctx, env);
    if (ctx.container?.running) ctx.blockConcurrencyWhile(() => this.runtime.restore());
  }

  fetch(request) { return this.runtime.fetch(request); }

  // Internal RPC, reachable only through the Durable Object binding.
  async keepAlive() { await this.runtime.ensureReady(); }
}

export default {
  scheduled(_controller, env, ctx) {
    // Preserve the rejection in Workers observability; the next cron run retries.
    ctx.waitUntil(keepScheduledContainerWarm(env));
  },
  fetch(request, env) {
    // The singleton ensures that requests share one backend instance.
    return env.APP_CONTAINER.getByName("singleton").fetch(request);
  },
};
