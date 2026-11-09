// The backend owns these routes. Keep this list aligned with backend/main.py.
const backendPaths = new Set(["health", "healthz", "metrics-lite"]);
const appPaths = new Set([
  "login", "register", "forgot-access", "home", "stocks", "security",
  "commodities", "forex", "hotlists", "dashboard", "screener", "compare",
  "portfolio", "portfolio-lab", "mutual-funds", "watchlist", "news", "alerts",
  "paper", "risk", "correlation", "oms", "ops", "settings", "plugins",
  "saved-views", "cockpit", "model-lab", "equity", "fno", "backtesting",
  "account", "reports", "research",
]);

function originUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.pathname !== "/" || url.search || url.hash) return null;
    return url;
  } catch {
    return null;
  }
}

export async function handleRequest(request, env, fetchOrigin = fetch) {
  const url = new URL(request.url);
  const firstSegment = url.pathname.split("/")[1];
  if (firstSegment === "api" || backendPaths.has(firstSegment)) {
    const origin = originUrl(env.BACKEND_ORIGIN);
    if (!origin || origin.host === url.host) {
      return new Response("Backend origin is not configured", { status: 503 });
    }
    origin.pathname = url.pathname;
    origin.search = url.search;
    return fetchOrigin(new Request(origin, request), { redirect: "manual" });
  }

  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method not allowed", { status: 405, headers: { Allow: "GET, HEAD" } });
  }
  const assetResponse = await env.ASSETS.fetch(request);
  if (assetResponse.status !== 404) return assetResponse;
  // Missing files must remain 404, otherwise stale JS URLs receive HTML.
  if (url.pathname.startsWith("/assets/") || /\.[^/]+$/.test(url.pathname)) return assetResponse;
  const entry = appPaths.has(firstSegment) ? "/app.html" : "/index.html";
  return env.ASSETS.fetch(new Request(new URL(entry, url), request));
}

export default { fetch: handleRequest };
