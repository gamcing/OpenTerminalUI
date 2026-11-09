import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { validateContainerConfig } from "./check-container-config.mjs";

const config = JSON.parse(await readFile(new URL("../wrangler.jsonc", import.meta.url), "utf8"));

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


test("development secrets are excluded from git and runtime data is excluded from container builds", async () => {
  for (const path of [".dev.vars", "cloudflare/.dev.vars", ".dev.vars.production"]) {
    const ignored = spawnSync("git", ["check-ignore", "--no-index", "--quiet", path]);
    assert.equal(ignored.status, 0, `${path} must be ignored`);
  }
  const example = spawnSync("git", ["check-ignore", "--no-index", "--quiet", ".dev.vars.example"]);
  assert.equal(example.status, 1, "the secret-name example must remain trackable");
  const ignore = await readFile(new URL("../.dockerignore", import.meta.url), "utf8");
  for (const pattern of ["**/.dev.vars*", "**/*.db", "**/*.sqlite", "data/*"]) {
    assert.ok(ignore.split(/\r?\n/).includes(pattern), `${pattern} must be excluded from the build context`);
  }
  const dockerfile = await readFile(new URL("../Dockerfile", import.meta.url), "utf8");
  assert.doesNotMatch(dockerfile, /^COPY\s+data\/\s+\.\/data\//m);
  for (const name of ["holidays.json", "nse_equity_symbols_all.csv", "nse_equity_symbols_all.txt",
    "nse_equity_symbols_eq.csv", "nse_equity_symbols_eq.txt", "sample_tickers.txt"]) {
    assert.ok(dockerfile.includes(`data/${name}`));
    assert.ok(ignore.split(/\r?\n/).includes(`!data/${name}`));
  }
});
