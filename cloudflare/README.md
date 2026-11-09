# Cloudflare frontend deployment (partial)

This configuration deploys the frontend and a same-origin API proxy to Cloudflare Workers. It requires an **existing HTTPS OpenTerminalUI backend**. It does not deploy the Python backend, provision a database, or store application data in Cloudflare D1. This is not a complete one-click Cloudflare deployment.

## Requirements

- A Cloudflare account with Workers enabled and Node.js 20 or later.
- An existing OpenTerminalUI backend with persistent storage, served over HTTPS. Configure production secrets and authentication on the backend before exposing it to the internet.
- Access to deploy Workers with Wrangler.

## Deploy

From the repository root:

```sh
npm run build
npx wrangler@4 login
npx wrangler@4 deploy --var BACKEND_ORIGIN:https://backend.example.com
```

Replace `https://backend.example.com` with the backend origin (scheme and host, no path or trailing query). The origin must be HTTPS and must not be the Worker URL. API requests and WebSocket upgrades under `/api`, along with the backend health endpoints, are forwarded to this origin; all other requests are served from the built frontend. Requests to the backend fail with HTTP 503 if the origin is missing or invalid.

For Git-connected Workers Builds, set the build command to `npm run build`, the deploy command to `npx wrangler@4 deploy`, and configure `BACKEND_ORIGIN` as a Worker variable in the dashboard. Keep its value when changing deployment methods: Wrangler configuration and dashboard environment-variable settings can override each other.

The proxy does not authenticate or firewall the backend origin. Secure the backend itself and restrict direct origin access as appropriate for your setup. In particular, do not point it at a development backend with development authentication enabled.

## Remaining work for a full Cloudflare deployment

The FastAPI application uses SQLAlchemy sessions and local SQLite in multiple places. D1 cannot be substituted for these connections by changing a URL. A complete Cloudflare deployment needs a transaction-safe data-access migration, storage for durable files, a supported backend runtime, production secrets, and end-to-end tests covering restart and recovery. Do not place the existing SQLite databases on ephemeral container storage and treat them as durable.

Run the proxy tests with `npm run test:cloudflare`. They do not test a real Cloudflare deployment or the Python backend.

## Experimental D1 transaction gateway

The D1 gateway (`d1-bridge.mjs`) and Python batch client (`backend/cloudflare/d1.py`) are migration building blocks only. The saved-view integration is experimental and disabled by default; the gateway is **not included in the production Worker configuration**. Do not deploy it as the application database until the data-access migration and security review are complete.

The gateway accepts a complete set of precomputed statements and uses D1 `batch()` to execute them atomically. It cannot implement an interactive SQLAlchemy transaction or a read-dependent sequence of writes across requests. The client deliberately does not retry after a timeout or database error because the commit outcome may be unknown.

To test against a disposable local D1 database, start this command in one terminal:

```sh
npx wrangler@4 dev --local --config cloudflare/wrangler.d1-test.jsonc --port 8899 --ip 127.0.0.1 --var D1_BRIDGE_TOKEN:test-secret-only-not-for-production-1234567890 --persist-to .codex_tmp_d1
```

In a second terminal, run:

```sh
node cloudflare/d1-local.integration.mjs
python -m cloudflare.d1_local_integration
python -m cloudflare.identity_local_integration
```

The identity test deletes users from the local database; use an isolated disposable database. The identity repository is a standalone prototype and is not connected to authentication or authorization. The fixed token is **only for local testing**. Any future deployed gateway must use an independently generated high-entropy secret, protect it as a Cloudflare secret, limit access to the backend, and avoid exposing arbitrary SQL execution to clients. The tests exercise local D1 and do not establish production durability, transactional compatibility of the application, or a complete deployment.

## Experimental container runtime

`container-worker.mjs` and `wrangler.container-test.jsonc` are an isolated runtime prototype, not the production configuration. The singleton Durable Object starts the existing Docker image and forwards HTTP requests. Before starting, it installs an outbound HTTP intercept for `openterminal-d1.internal` pointing to a private Worker entrypoint. The D1 gateway remains unavailable from the public Worker routes. The Python client permits this HTTP origin only when the caller explicitly enables `allow_intercepted_container_origin`; that option is safe only inside a container whose intercept has been installed successfully.

The runtime uses a paid `standard-1` container and will stop after 30 minutes of inactivity. Background schedulers therefore do not yet have continuous execution guarantees. There is no D1 integration in the application database layer yet; in particular the existing production backend still expects PostgreSQL. Do not use this configuration as a complete deployment.

The runtime unit tests use a simulated container API. A complete verification still requires building the Dockerfile, launching the real container, testing private egress and WebSocket behavior, and validating the migrated application data and scheduler after restarts. Building the Dockerfile locally requires a running Docker-compatible engine; Cloudflare Workers Builds can build the image in CI.

## Experimental saved-view migration

The HTTP and MCP saved-view consumers have an opt-in D1 repository and an initial identity/saved-view schema in `migrations/0001_identity_saved_views.sql`. The default remains the original SQLAlchemy database. This is not a complete database migration: identity and every other application domain still use the original backend database, there is no automatic identity synchronization or import of existing saved views, and using separate stores can break referential integrity. **Do not enable the D1 saved-view flag for a live deployment.**

For local development only, start the local D1 gateway above, then run:

```sh
python -m cloudflare.saved_views_local_integration
python -m cloudflare.saved_views_api_local_integration
```

These tests reset the local test users and require an isolated disposable database. They exercise D1 schema creation, foreign keys, saved-view CRUD, HTTP routes, MCP reads and user isolation. Full deployment still requires identity migration, existing-data migration, the remaining application domains, durable files and end-to-end recovery checks.
