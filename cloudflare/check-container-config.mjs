import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const placeholderId = "00000000-0000-0000-0000-000000000001";
const requiredSecrets = ["JWT_SECRET_KEY", "CACHE_SIGNING_KEY", "D1_BRIDGE_TOKEN", "R2_BRIDGE_TOKEN",
  "OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY"];

export function validateContainerConfig(config) {
  const errors = [];
  const database = Array.isArray(config?.d1_databases) ? config.d1_databases.find(item => item?.binding === "DB") : null;
  const id = database?.database_id;
  if (typeof id !== "string" || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(id) ||
      id === placeholderId || id === "00000000-0000-0000-0000-000000000000")
    errors.push("Set DB.database_id to the ID of the intended D1 database.");
  if (typeof database?.database_name !== "string" || !database.database_name.trim())
    errors.push("Set DB.database_name to the intended D1 database name.");
  if (database?.migrations_dir !== "cloudflare/migrations")
    errors.push("DB.migrations_dir must point to the bundled D1 migrations.");
  const bucket = Array.isArray(config?.r2_buckets) ? config.r2_buckets.find(item => item?.binding === "BACKTESTS") : null;
  if (typeof bucket?.bucket_name !== "string" || !bucket.bucket_name.trim())
    errors.push("Set BACKTESTS.bucket_name to the intended R2 bucket.");
  if (config?.vars?.OPENTERMINALUI_CF_D1_SNAPSHOT_EXPERIMENT !== "1" ||
      config?.vars?.OPENTERMINALUI_CF_R2_EXPERIMENT !== "1")
    errors.push("Both experimental durable storage switches must be enabled.");
  const email = config?.vars?.BOOTSTRAP_ADMIN_EMAIL;
  if (typeof email !== "string" || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim()))
    errors.push("Set BOOTSTRAP_ADMIN_EMAIL to the intended administrator email.");
  const bindings = config?.durable_objects?.bindings;
  if (config?.main !== "cloudflare/container-worker.mjs" || !Array.isArray(bindings) ||
      !bindings.some(item => item?.name === "APP_CONTAINER" && item?.class_name === "OpenTerminalContainer"))
    errors.push("The container Worker and its Durable Object binding are required.");
  if (!Array.isArray(config?.secrets?.required) || !requiredSecrets.every(name => config.secrets.required.includes(name)))
    errors.push("The required container secrets declaration is incomplete.");
  return errors;
}

async function main() {
  let config;
  try {
    config = JSON.parse(await readFile(new URL("../wrangler.jsonc", import.meta.url), "utf8"));
  } catch {
    console.error("Cannot read the container deployment configuration.");
    process.exitCode = 1;
    return;
  }
  const errors = validateContainerConfig(config);
  if (errors.length) {
    for (const error of errors) console.error(error);
    process.exitCode = 1;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
