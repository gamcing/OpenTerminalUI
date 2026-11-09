import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { validateContainerConfig } from "./check-container-config.mjs";

const config = JSON.parse(await readFile(new URL("./wrangler.container-deploy.jsonc", import.meta.url), "utf8"));

test("deployment candidate refuses the placeholder D1 ID before any migration", () => {
  assert.deepEqual(validateContainerConfig(config), ["Set DB.database_id to the ID of the intended D1 database.",
    "Set BOOTSTRAP_ADMIN_EMAIL to the intended administrator email."]);
  const result = spawnSync(process.execPath, ["cloudflare/check-container-config.mjs"], { encoding: "utf8" });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /DB.database_id/);
});

test("deployment candidate requires both durable stores and their secret declarations", () => {
  const valid = structuredClone(config);
  valid.d1_databases[0].database_id = "12345678-aaaa-bbbb-cccc-123456789abc";
  valid.vars.BOOTSTRAP_ADMIN_EMAIL = "admin@example.test";
  assert.deepEqual(validateContainerConfig(valid), []);
  assert.notEqual(validateContainerConfig(null).length, 0);
  for (const mutate of [
    value => { value.vars.OPENTERMINALUI_CF_D1_SNAPSHOT_EXPERIMENT = "0"; },
    value => { value.vars.OPENTERMINALUI_CF_R2_EXPERIMENT = "0"; },
    value => { value.r2_buckets = []; },
    value => { value.secrets.required = []; },
    value => { value.vars.BOOTSTRAP_ADMIN_EMAIL = "invalid"; },
    value => { value.d1_databases[0].database_id = "invalid"; },
    value => { value.d1_databases[0].migrations_dir = "missing"; },
    value => { value.durable_objects.bindings = []; },
  ]) {
    const broken = structuredClone(valid);
    mutate(broken);
    assert.notEqual(validateContainerConfig(broken).length, 0);
  }
});
