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
python -m cloudflare.identity_auth_local_integration
python -m cloudflare.api_keys_local_integration
python -m cloudflare.identity_migration_local_integration
python -m cloudflare.identity_staging_local_integration
```

The identity tests delete users from the local database; use an isolated disposable database. D1 identity is connected to authentication and authorization only in explicitly enabled, loopback-only development tests; production use is rejected. The fixed token is **only for local testing**. Any future deployed gateway must use an independently generated high-entropy secret, protect it as a Cloudflare secret, limit access to the backend, and avoid exposing arbitrary SQL execution to clients. The tests exercise local D1 and do not establish production durability, transactional compatibility of the application, or a complete deployment.

## Experimental container runtime

`container-worker.mjs` and `wrangler.container-test.jsonc` are an isolated runtime prototype, not the production configuration. The singleton Durable Object starts the existing Docker image and forwards HTTP requests. Before starting, it installs an outbound HTTP intercept for `openterminal-d1.internal` pointing to a private Worker entrypoint. The D1 gateway remains unavailable from the public Worker routes. The Python client permits this HTTP origin only when the caller explicitly enables `allow_intercepted_container_origin`; that option is safe only inside a container whose intercept has been installed successfully.

The runtime uses a paid `standard-1` container and will stop after 30 minutes of inactivity. Background schedulers therefore do not yet have continuous execution guarantees. The experimental identity, API-key and saved-view integrations do not migrate the rest of the application database; the existing production backend still expects PostgreSQL. Do not use this configuration as a complete deployment.

The runtime unit tests use a simulated container API. A complete verification still requires building the Dockerfile, launching the real container, testing private egress and WebSocket behavior, and validating the migrated application data and scheduler after restarts. Building the Dockerfile locally requires a running Docker-compatible engine; Cloudflare Workers Builds can build the image in CI.

## Experimental saved-view migration

The HTTP and MCP saved-view consumers have an opt-in D1 repository and an initial identity/saved-view schema in `migrations/0001_identity_saved_views.sql`. The default remains the original SQLAlchemy database. This is not a complete database migration: identity and every other application domain still use the original backend database, there is no automatic identity synchronization or import of existing saved views, and using separate stores can break referential integrity. **Do not enable the D1 saved-view flag for a live deployment.**

For local development only, start the local D1 gateway above, then run:

```sh
python -m cloudflare.saved_views_local_integration
python -m cloudflare.saved_views_api_local_integration
```

These tests reset the local test users and require an isolated disposable database. They exercise D1 schema creation, foreign keys, saved-view CRUD, HTTP routes, MCP reads and user isolation. Full deployment still requires identity migration, existing-data migration, the remaining application domains, durable files and end-to-end recovery checks.

## Experimental identity and saved-view import

`backend/cloudflare/identity_migration.py` provides an offline copy primitive for users, refresh tokens, saved views, and API keys only. It preserves IDs and relationships and refuses a nonempty target. Its D1 writes are one atomic batch; the client rejects a snapshot exceeding its 1 MiB request limit. The caller must stop source writes, read from a stable source snapshot (SQLite read transaction or PostgreSQL repeatable read), and keep writes stopped throughout cutover. A timeout or database error can leave the commit outcome unknown: inspect the target before any retry. The snapshot contains password hashes, API-key hashes and refresh token IDs and must not be logged or stored in unencrypted files.

This primitive is not a deployment or a full migration: there is no production import command or coordinated cutover, and every other application domain and durable file still needs migration. Do not use it to enable the experimental stores for live users. The local integration test uses a disposable database.

`backend/cloudflare/identity_staging.py` adds an experimental staged path for larger snapshots after applying `migrations/0002_identity_import_staging.sql` through `migrations/0005_api_key_import_staging.sql` in order. The fifth migration refuses to run while an import is unfinished. It uploads bounded chunks into private staging tables, verifies the returned staged rows against the source, seals the immutable run, and checks the run ID, source snapshot fingerprint, row counts and row ordinals before copying all four tables and deleting the staging run in a single D1 batch. Individual source rows still need to fit the 768 KiB request budget, the complete source snapshot is currently held in memory, and D1 transaction limits can still prevent very large final copies. The caller must keep the snapshot immutable, ensure exclusive administrative access to staging, and retain the UUID before starting. An interrupted or failed run can leave password hashes, API-key hashes and token IDs in staging: inspect the outcome and use `abort_identity_snapshot` to delete the run when safe. A timeout during finalization can mean the import committed; inspect the destination before retrying. This remains a development primitive and is not a complete application migration.

## Experimental D1 identity authentication

The registration, login, refresh, protected-user lookup and bootstrap-admin paths can use D1 in isolated development tests. The `cloudflare.identity_auth_local_integration` test exercises these real routes and middleware with a disposable local D1 database and stubs unrelated services. It also checks the original SQLAlchemy path independently. The opt-in requires `OPENTERMINALUI_IDENTITY_STORE=d1`, `OPENTERMINALUI_D1_IDENTITY_EXPERIMENT=1`, `OPENTERMINALUI_D1_LOCAL_TEST=1`, an explicitly configured development environment and a loopback HTTP gateway. Misconfiguration and production environments fail closed rather than falling back to another identity store.

Do not use this mode with real users: other application domains still rely on the original SQLAlchemy database, so identities and foreign keys would diverge. Existing access tokens remain valid until expiry after a development-only password reset, matching the existing application behavior.

The isolated D1 identity mode also stores API-key hashes in D1 and uses them for public REST and MCP authentication after applying `migrations/0004_api_keys.sql`. It checks the full hash instead of relying on the display prefix, and revocation and last-use updates are atomic. The offline identity importer includes existing API-key IDs and hashes; it refuses unsupported permissions or malformed hashes rather than silently changing access. This remains unavailable in production until the rest of the data migration and deployment are complete.
