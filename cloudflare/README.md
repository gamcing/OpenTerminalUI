# Cloudflare deployment (experimental)

The repository root `wrangler.jsonc` and `npm run deploy` target the container, D1 and R2 experiment. The Deploy to Cloudflare button in the repository README points to this development branch. **The complete button flow, container image and Cloudflare persistence have not been verified in a real account. Do not use this for live data.**

## Experimental repository deployment

The root configuration declares a Durable Object container, a `DB` D1 binding, a `BACKTESTS` R2 binding and the required Worker secrets. The intended Deploy to Cloudflare flow creates a copy of the repository and provisions the declared resources; Workers Builds runs the root build and deploy scripts. Enter the administrator email and independently generated secrets when prompted. The root `.dev.vars.example` lists secret names only. Keep the Fernet provider encryption key stable and backed up: losing it makes saved provider settings unreadable. Containers and storage may incur Cloudflare charges.

The deploy command checks that the database ID is no longer a placeholder, applies D1 migrations using the `DB` binding, then deploys the Worker and container. This preflight cannot verify account ownership, secret values, resource permissions or real container operation. The migration and deployment are separate operations; a failed deployment may leave the migrations applied. After an administrator is created, remove the bootstrap password from both `secrets.required` in the copied repository and the Worker's secrets if it is no longer needed. Existing users without an administrator require manual recovery.

For a manual experimental deploy, create a D1 database and R2 bucket first, replace the placeholder ID and resource names in `wrangler.jsonc`, set the administrator email, and configure the listed Worker secrets. Then run `npm run deploy`. A local deploy using the Dockerfile requires a running Docker-compatible engine. A disposable local migration check is available with `npx wrangler@4 d1 migrations apply DB --local --persist-to .codex_tmp_container_migrations`; it does not deploy a container or provision remote resources.

## Frontend proxy for an existing backend

`cloudflare/wrangler.frontend.jsonc` retains the frontend-only deployment. It requires an **existing HTTPS OpenTerminalUI backend** with persistent storage, and does not provision or store its database in D1.

```sh
npm run build
npx wrangler@4 login
npm run deploy:frontend -- --var BACKEND_ORIGIN:https://backend.example.com
```

Replace the example origin with the backend origin, using HTTPS and no path or query. API and WebSocket requests under `/api`, plus backend health endpoints, are forwarded to it; other requests use the built frontend. A missing or invalid origin returns HTTP 503. For Git-connected Workers Builds, use `npm run build` and `npm run deploy:frontend` and configure `BACKEND_ORIGIN` for that Worker. Secure the backend itself and restrict direct origin access as appropriate; the proxy does not authenticate or firewall it.

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
python -m cloudflare.watchlists_local_integration
python -m cloudflare.identity_migration_local_integration
python -m cloudflare.identity_staging_local_integration
python -m cloudflare.user_layouts_local_integration
python -m cloudflare.sqlite_images_local_integration
python -m cloudflare.sqlite_gc_local_integration
python -m cloudflare.sqlalchemy_sqlite_local_integration
python -m cloudflare.application_snapshot_local_integration
python -m cloudflare.snapshot_process_local_integration
```

The identity tests delete users from the local database; use an isolated disposable database. D1 identity is connected to authentication and authorization only in explicitly enabled, loopback-only development tests; production use is rejected. The fixed token is **only for local testing**. Any future deployed gateway must use an independently generated high-entropy secret, protect it as a Cloudflare secret, limit access to the backend, and avoid exposing arbitrary SQL execution to clients. The tests exercise local D1 and do not establish production durability, transactional compatibility of the application, or a complete deployment.

## Experimental container runtime

`container-worker.mjs` and `wrangler.container-test.jsonc` are an isolated runtime prototype, not the production configuration. The singleton Durable Object starts the existing Docker image and forwards HTTP requests. Before starting, it installs an outbound HTTP intercept for `openterminal-d1.internal` pointing to a private Worker entrypoint. The D1 gateway remains unavailable from the public Worker routes. The Python client permits this HTTP origin only when the caller explicitly enables `allow_intercepted_container_origin`; that option is safe only inside a container whose intercept has been installed successfully.

The runtime uses a paid `standard-1` container and will stop after 30 minutes of inactivity. The prototype configuration includes a five-minute cron trigger. Setting the Worker variable `OPENTERMINALUI_CF_KEEPALIVE=1` explicitly opts in to starting and keeping the singleton container active, including when no users are connected. This can incur continuous container and Worker charges. The trigger is disabled by default; without it, background schedulers stop when the container becomes inactive. Even with it enabled, cron delays, failures and restarts can interrupt background jobs; this is not an exactly-once scheduler or a guarantee that missed jobs will run. Monitor scheduled-event failures and do not rely on this prototype for time-critical work. The experimental identity, API-key and saved-view integrations do not migrate the rest of the application database; the existing production backend still expects PostgreSQL. Do not use this configuration as a complete deployment.

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

`backend/cloudflare/identity_migration.py` provides an offline copy primitive for users, refresh tokens, saved views, API keys, watchlists, and launchpad layouts only. It preserves IDs and relationships and refuses a nonempty target. Its D1 writes are one atomic batch; the client rejects a snapshot exceeding its 1 MiB request limit. The caller must stop source writes, read from a stable source snapshot (SQLite read transaction or PostgreSQL repeatable read), and keep writes stopped throughout cutover. A timeout or database error can leave the commit outcome unknown: inspect the target before any retry. The snapshot contains password hashes, API-key hashes and refresh token IDs and must not be logged or stored in unencrypted files.

This primitive is not a deployment or a full migration: there is no production import command or coordinated cutover, and every other application domain and durable file still needs migration. Do not use it to enable the experimental stores for live users. The local integration test uses a disposable database.

`backend/cloudflare/identity_staging.py` adds an experimental staged path for larger snapshots after applying `migrations/0002_identity_import_staging.sql` through `migrations/0008_user_layouts.sql` in order. Staging schema upgrades refuse to run while an import is unfinished. It uploads bounded chunks into private staging tables, verifies the returned staged rows against the source, seals the immutable run, and checks the run ID, source snapshot fingerprint, row counts and row ordinals before copying all six tables and deleting the staging run in a single D1 batch. Individual source rows still need to fit the 768 KiB request budget, the complete source snapshot is currently held in memory, and D1 transaction limits can still prevent very large final copies. The caller must keep the snapshot immutable, ensure exclusive administrative access to staging, and retain the UUID before starting. An interrupted or failed run can leave password hashes, API-key hashes and token IDs in staging: inspect the outcome and use `abort_identity_snapshot` to delete the run when safe. A timeout during finalization can mean the import committed; inspect the destination before retrying. This remains a development primitive and is not a complete application migration.

## Experimental D1 identity authentication

The registration, login, refresh, protected-user lookup and bootstrap-admin paths can use D1 in isolated development tests. The `cloudflare.identity_auth_local_integration` test exercises these real routes and middleware with a disposable local D1 database and stubs unrelated services. It also checks the original SQLAlchemy path independently. The opt-in requires `OPENTERMINALUI_IDENTITY_STORE=d1`, `OPENTERMINALUI_D1_IDENTITY_EXPERIMENT=1`, `OPENTERMINALUI_D1_LOCAL_TEST=1`, an explicitly configured development environment and a loopback HTTP gateway. Misconfiguration and production environments fail closed rather than falling back to another identity store.

Do not use this mode with real users: other application domains still rely on the original SQLAlchemy database, so identities and foreign keys would diverge. Existing access tokens remain valid until expiry after a development-only password reset, matching the existing application behavior.

The isolated D1 identity mode also stores API-key hashes in D1 and uses them for public REST and MCP authentication after applying `migrations/0004_api_keys.sql`. It checks the full hash instead of relying on the display prefix, and revocation and last-use updates are atomic. The offline identity importer includes existing API-key IDs and hashes; it refuses unsupported permissions or malformed hashes rather than silently changing access. This remains unavailable in production until the rest of the data migration and deployment are complete.

## Experimental D1 watchlists

After applying `migrations/0006_watchlists.sql`, explicitly set `OPENTERMINALUI_WATCHLISTS_STORE=d1` alongside the isolated D1 identity test settings to exercise watchlist HTTP endpoints, MCP resources, agent read tools and filing-watch symbol selection against D1. Concurrent symbol additions are applied atomically, and user ownership is checked on every operation. The offline importer includes existing watchlist IDs and validates their JSON.

This is not a production deployment. Agent proposal and alert actions that write watchlists are intentionally unavailable in D1 mode until their own business records can participate in the same transaction. The separate legacy `watchlist_items` table and other business data are not migrated. Do not enable this mode for live users.

## Experimental D1 launchpad layouts

After applying `migrations/0008_user_layouts.sql`, set `OPENTERMINALUI_USER_LAYOUTS_STORE=d1` alongside the isolated D1 identity test settings to exercise launchpad layout reads and writes against D1. The offline importer preserves existing IDs, user keys, timestamps, and layout JSON; it refuses a nonempty layouts target and malformed source JSON. An unfinished staged import must be resolved before the migration can be applied.

The existing route stores an `anonymous` layout when no authenticated user is present; the D1 implementation preserves that shared behavior. This is not production-ready, and other application data still requires migration.

## Experimental complete SQLite image storage

`migrations/0009_sqlite_image_storage.sql`, `migrations/0010_sqlite_chunk_pool.sql` and `backend/cloudflare/sqlite_images.py` explore storing complete standalone SQLite images in private D1. A caller supplies an image UUID before staging, uploads bounded chunks, verifies the bytes and SHA-256 digest, and publishes with a revision check in one D1 batch. A concurrent writer based on an older revision is rejected. A lost publish response has an unknown outcome: inspect the current head before taking any recovery action. The integration test uses a disposable D1 database and exercises restoration, concurrent publishing and interrupted uploads.

**The image store alone is not an application database adapter.** A separately gated integration described below connects the experimental coordinator to SQLAlchemy and Alembic for local tests and a simulated container path. Capturing a connection with pending writes does not prove that the local SQLite commit will succeed. Correct integration still needs comprehensive coordination of all readers and writers, crash recovery, fencing across container instances, handling of unknown outcomes, durable non-database files, and validation under real Cloudflare failures. Do not use it for real data. The prototype reads each image into memory, has a 64 MiB image limit, and retains images and pooled chunks unless the experimental collector is explicitly invoked; it therefore has significant storage and transfer costs and is unsuitable for ongoing production use. The staged and published bytes may contain sensitive application data; do not log them or expose the private gateway. Interrupted uploads may leave staged data; retain the UUID and remove an abandoned unsealed image only after its outcome is known.

`backend/cloudflare/sqlalchemy_sqlite_prototype.py` tests one possible transaction coordinator against a private local SQLite file. It routes SQLAlchemy commits through immutable D1 images, checks the revision on transaction start, and refuses further operations after an uncertain remote publish or failed local commit. Its isolated integration test covers ORM writes, rollback, savepoints, deferred foreign keys, stale instances, concurrent writers, and recovery.

The coordinator has a gated experimental connection to part of the application described below. It requires every database operation to use its engine and a fresh private working directory; it does not cover existing direct SQLite connections, container startup, background tasks, non-database files, automatic recovery or real Cloudflare behavior. Each write still captures and verifies a complete image; production contention, storage growth and latency remain unresolved. It is a feasibility prototype; do not enable it for live data.

For integration testing, Alembic can accept an explicitly supplied synchronous SQLAlchemy connection through its programmatic configuration; its normal asynchronous CLI path remains unchanged. `cloudflare.application_snapshot_local_integration` runs the project's real migrations and model initialization against the prototype, seeds a test administrator, exercises real authentication and watchlist routes with unrelated fetcher dependencies isolated, then restores the data in a fresh local directory. This is targeted local verification, not a real container deployment or coverage of all application features.

## Local-only application snapshot experiment

For the local path, `OPENTERMINALUI_DATABASE_STORE=d1_snapshot` selects the prototype engine only when `OPENTERMINALUI_D1_SNAPSHOT_EXPERIMENT=1`, `OPENTERMINALUI_D1_LOCAL_TEST=1`, an explicit development or test environment, and an HTTP loopback D1 gateway are configured. Identity, saved views, watchlists, and layouts must all use `sqlalchemy` so that these records do not silently split between stores. Unknown or unsafe configuration fails rather than falling back to a local database. **Do not enable this experiment for real data or production.**

With the disposable local gateway running as described above, run `python -m cloudflare.snapshot_process_local_integration`. This executes the real Alembic CLI and admin seed in separate processes, then tests authentication and watchlist operations across further process restarts. One process imports the real application and exercises its login and watchlist routes; other route probes isolate unrelated market-fetcher dependencies. The test also exercises the futures instrument loader and instrument mapping service through the coordinated engine, verifies rollback, and reads their data after a restart. The real application lifespan and its background services are not started, and no container is verified. The test creates private temporary SQLite working copies and removes them on normal process exit. Abrupt process termination may leave temporary files; use only disposable test data.

The snapshot design still captures and verifies the complete database on each write, and caps an image at 64 MiB. Migration 0010 pools content-addressed chunks per database, so later snapshots upload only previously unseen chunks; migration 0011 supplies an explicit bounded collector, but it does not clean interrupted uploads automatically. Existing 0009 inline images remain readable. Older code cannot read images created with the pooled format, so do not downgrade after writing them. Other direct SQLite access, including the OHLCV cache and shared cache, and durable application files do not use the coordinator. Recovery and fencing under real Cloudflare failures, cost control, and a complete deploy workflow remain unresolved.

## Experimental snapshot collection

Migration `0011_sqlite_snapshot_gc.sql` adds persistent reader pins and bounded collection to the local snapshot prototype. `D1SQLiteImageStore.collect(name, keep_revisions=2, limit=32)` can remove old, unpinned images and unreferenced pooled chunks; the current head, active uploads and pinned images are preserved. A compact immutable publication receipt is retained so `published_receipt(image_id)` can confirm a previously committed image after its bytes have been collected. A missing receipt does not establish that an in-flight publish cannot still complete. Collection is manual and is not part of deployment or a scheduled background job.

**All readers must use the pin protocol before any collection is run.** Older versions can read without pins, so stop and upgrade every old process first; do not downgrade after collection is enabled. Pins never expire: a terminated reader or lost response can leave a pin and retain data indefinitely. Remove such a pin only after independently proving that its reader and any in-flight acquisition cannot run. Interrupted unsealed uploads likewise need separate outcome-aware cleanup. Receipts remain indefinitely, and the collector, failure recovery and D1 behavior still require real Cloudflare validation before production use.

## Experimental D1 container connection

The isolated container runtime can explicitly select the snapshot engine with Worker variable `OPENTERMINALUI_CF_D1_SNAPSHOT_EXPERIMENT=1`. It requires the `DB` D1 binding, a separate `D1_BRIDGE_TOKEN` secret of at least 32 characters and a stable `OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY` Fernet key. The runtime installs the private D1 intercept before starting the container and supplies a fixed internal gateway origin; it enables database-backed encrypted provider settings. Missing configuration or a failed intercept prevents a new container from starting. Changing settings does not alter an already running container's environment. The D1 schema for snapshot storage must already exist; this prototype does not provision or migrate the Cloudflare database.

In this experiment, a fresh database requires `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD` to create the first administrator. The email must be valid, and the password must be at least eight characters and at most 72 UTF-8 bytes. The startup script stops on a missing or invalid bootstrap credential, a database failure or an uncertain commit. On a later start, an existing administrator permits startup without these bootstrap values. If users exist without an administrator, startup stops for manual recovery rather than automatically promoting anyone. Store the password as a secret and remove it from the runtime after initialization if it is no longer needed.

This is **not a production deployment**. It is disabled by default and accepts only the specific internal HTTP origin with both container experiment flags. Its security depends on the private intercept, whose behavior has not been verified on a real Cloudflare container. It does not provide complete persistent file storage, proven multi-instance operation under real failures, or an upgrade path for existing data. Never enable it for live data. The `snapshot_process_local_integration` test exercises the real Alembic CLI, admin seed, database writes and app routes in separate processes with a simulated intercept to local Wrangler D1; it does not start a real container.

## Backtest jobs after a snapshot restart

In snapshot mode, application startup reloads persisted `queued` backtests into the singleton process's worker after restoring the database. Jobs left `running` by a stopped process are marked `failed` with an interruption message instead of being executed again; clients can submit a new run. Completed and previously failed jobs are preserved. Normal shutdown cancels the snapshot worker so the next start can reconcile its records. This recovery assumes that the startup fence is in effect and that only one application process owns the queue. A worker that fails during normal operation can still leave a job unfinished until a later restart. The recovery path is exercised with a local database and simulated container transport, not a deployed Cloudflare Container.

## Experimental encrypted provider settings

In the local snapshot experiment, set `OPENTERMINALUI_PROVIDER_KEYS_STORE=database` and `OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY` to a stable Fernet key before starting the app. Generate a key securely using `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`; treat it as a secret and retain it outside the database. The admin settings endpoint encrypts provider values before committing them to the coordinated database. On startup, the application restores them before importing provider services. Clearing a key persists an encrypted empty value so a process environment variable does not silently restore it. The normal `env_file` mode remains the default.

This storage mode is restricted to the D1 snapshot experiments and requires Alembic migration `0015_provider_secrets`. A missing or invalid key, a wrong key for existing data, or tampered ciphertext stops startup. Losing the encryption key makes saved settings unrecoverable; no key rotation or automatic import from an existing `.env` is provided. Historical snapshots can retain older ciphertext after a setting is cleared. No production Cloudflare secret provisioning, durable-file migration or real container validation is provided by this experiment.

## Experimental R2 backtest artifacts

`cloudflare/r2-bridge.mjs` and `backend/cloudflare/r2_backtests.py` provide a private, bounded local test path for backtest data-store artifacts. With the disposable R2 gateway running, set `OPENTERMINALUI_BACKTEST_STORE=r2`, `OPENTERMINALUI_R2_LOCAL_TEST=1`, an explicit test/development environment, `R2_GATEWAY_URL=http://127.0.0.1:8900`, and the local `R2_BRIDGE_TOKEN`. The normal local-file store remains the default. The local test adapter rejects non-loopback gateways; a separate, explicitly enabled container experiment accepts only the private intercepted origin described below. A failed upload never falls back to local storage.

Objects use names containing their SHA-256 digest, and the gateway limits artifacts to 8 MiB, verifies their bytes, and uses a conditional create. Repeating the same upload is idempotent; a timeout still leaves the write outcome uncertain. The catalog lists these objects, and stored paths use the `r2://backtests/` prefix. Existing local files are not migrated automatically, objects are not deleted automatically, and large artifacts need a separate design. Contents may be sensitive; do not expose the gateway publicly or use the fixed local test token outside tests.

Run `npx wrangler@4 dev --local --config cloudflare/wrangler.r2-test.jsonc --port 8900 --ip 127.0.0.1 --var R2_BRIDGE_TOKEN:test-secret-only-not-for-production-1234567890 --persist-to .codex_tmp_r2` in one terminal and `python -m cloudflare.r2_backtest_local_integration` in another. This uses local Wrangler storage and separate Python processes, including a simulated container intercept; it does not deploy an R2 bucket or prove production Cloudflare durability. Other application files remain unmigrated.

For the isolated container runtime prototype, `wrangler.container-test.jsonc` declares a `BACKTESTS` R2 binding. Explicitly setting Worker variable `OPENTERMINALUI_CF_R2_EXPERIMENT=1` and the separate `R2_BRIDGE_TOKEN` secret enables the backtest adapter in newly started containers. The runtime installs outbound intercepts for both D1 and R2 before starting or restoring a container, including when the R2 flag has since been disabled. It forwards the R2 token only when explicitly enabled and rejects missing bucket or token configuration. Changing these settings does not rewrite the environment of an already running container; restart it to apply them.

The Python adapter accepts `http://openterminal-r2.internal` only with the container experiment flags and a production container environment. It relies on the runtime intercept being installed: never set those flags in an ordinary process or route that hostname to an external service. Worker tests simulate intercept installation and failures; the Python integration simulates the transport against local R2. No real container or Cloudflare R2 deployment has been verified, the D1 backend and other persistent files remain incomplete, and this experiment is not a production deployment.

The `/health` endpoint returns HTTP 503 and a recovery marker once the snapshot coordinator has detected an unsafe state, such as a changed remote revision, an uncertain publish, or a local commit failure after publication. When the container D1 snapshot experiment is enabled, the runtime recognizes that specific marker, stops the unhealthy container, reinstalls its private intercepts, and starts a replacement. A recovery is limited to once per request and once per 60 seconds while the Durable Object instance remains in memory; eviction resets that cooldown. Other health failures do not trigger this recovery. Existing in-flight requests may be interrupted, and their external side effects and write outcomes can be unknown; the proxy never automatically replays an application request.

Each experimental container process first publishes an identical snapshot at the next D1 revision before serving work. This startup fence makes a delayed publication against the previous revision fail its atomic compare-and-swap. A failed or uncertain fence stops startup, and a later process must restore and fence again. A restart can only be considered safe if all writers use this protocol and D1 batch operations provide the expected atomic ordering. The fence does not stop a previously running process from performing unrelated external side effects, and it adds a snapshot publication for each migration, seed and application process. Health checks only observe failures already detected by the process; they do not contact D1 or detect an idle instance becoming stale. These recovery and fencing behaviors have been tested against local D1 and simulated container APIs, not real Cloudflare failures or containers.
