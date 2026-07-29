#!/usr/bin/env node
// Trusted in-container runner for the VERIFY suite phases (baseline, regress).
// sandbox-run.sh mounts it read-only; run-phase.sh invokes it. It derives the
// pinned tree's own test plan from the repository's stabilized wrapper script
// (named by SUITE_CONFIG) and issues the
// wrapper's stabilized vitest invocations with explicit include-lists, keeping
// going past failures and reading a JSON report file per invocation. The result
// the host trusts is this process's exit code; the trailer printed last on
// stdout carries the failing-file set as data.
//
// Exit contract:
//   plan:                 0 plan JSON on stdout | 1 derivation failed
//   run --mode baseline:  0 suite ran to completion (failures are trailer data)
//                         1 infrastructure (missing report, accounting mismatch)
//   run --mode regress:   0 no non-excluded failures | 20 at least one | 1 infra
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync } from "node:fs";
import path from "node:path";

const ROOT = process.cwd();
const EXIT_INFRA = 1;
const EXIT_REGRESSED = 20;
let invocationIndex = 0;

function die(msg) {
  console.error(`[verify-suite] ${msg}`);
  process.exit(EXIT_INFRA);
}

// The repository contract, host-written from the profile's verify.suite
// section and mounted read-only (SUITE_CONFIG): the stabilized-wrapper script
// the plan derives from, the vitest project the serialized/general-server
// suites run under, an optional preflight npm script, and the names of the
// per-invocation fixture env vars the repository's tests expect.
function loadConfig() {
  const p = process.env.SUITE_CONFIG;
  if (!p) die("SUITE_CONFIG is required — the suite phases need their repository contract");
  let cfg;
  try {
    cfg = JSON.parse(readFileSync(p, "utf8"));
  } catch {
    die(`SUITE_CONFIG at ${p} is not parsable JSON`);
  }
  for (const key of ["wrapper", "server_project"]) {
    if (typeof cfg[key] !== "string" || !cfg[key]) die(`SUITE_CONFIG carries no ${key}`);
  }
  return cfg;
}

const CFG = loadConfig();
const WRAPPER = CFG.wrapper;
const SERVER_PROJECT = CFG.server_project;

function rel(p) {
  return path.isAbsolute(p) ? path.relative(ROOT, p).split(path.sep).join("/") : p;
}

// Per-invocation env, mirroring the wrapper's runVitest: NODE_ENV=test plus a
// fresh compact TMPDIR (and, when the contract names them, a fresh home dir
// and instance id) so Unix-socket fixtures stay under macOS path limits and
// no state leaks between invocations.
function invocationEnv() {
  invocationIndex += 1;
  const testRoot = mkdtempSync(`/tmp/vst-${process.pid}-${invocationIndex}-`);
  const env = {
    ...process.env,
    NODE_ENV: "test",
    TMPDIR: path.join(testRoot, "t"),
  };
  mkdirSync(env.TMPDIR, { recursive: true });
  if (CFG.home_env) {
    env[CFG.home_env] = path.join(testRoot, "h");
    mkdirSync(env[CFG.home_env], { recursive: true });
  }
  if (CFG.instance_env) {
    env[CFG.instance_env] = `vs-${process.pid}-${invocationIndex}`;
  }
  return env;
}

// --- plan derivation: reads only the tree as it stands when invoked. ---
// run-phase.sh calls `plan` BEFORE applying any patch, so the plan is a
// function of the pinned image alone and a PR cannot influence it.

function dryRun(extra) {
  const r = spawnSync("node", [WRAPPER, ...extra, "--dry-run"],
                      { cwd: ROOT, encoding: "utf8" });
  if (r.status !== 0) die(`${WRAPPER} --dry-run failed: ${r.stderr}`);
  try {
    return JSON.parse(r.stdout);
  } catch {
    die(`${WRAPPER} --dry-run emitted no parsable JSON`);
  }
}

function extractProjects(source, name, minCount) {
  const m = source.match(new RegExp(`const ${name} = \\[([^\\]]*)\\]`, "s"));
  if (!m) die(`cannot find the ${name} array in ${WRAPPER}`);
  const projects = [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
  if (projects.length < minCount) die(`${name} in ${WRAPPER} parsed suspiciously small`);
  return projects;
}

function listProjectFiles(project) {
  const out = path.join(mkdtempSync("/tmp/vs-list-"), "list.json");
  const r = spawnSync("pnpm", ["exec", "vitest", "list", "--project", project,
                               "--filesOnly", `--json=${out}`],
                      { cwd: ROOT, env: invocationEnv(), encoding: "utf8" });
  if (r.status !== 0) die(`vitest list --project ${project} failed: ${r.stderr}`);
  let items;
  try {
    items = JSON.parse(readFileSync(out, "utf8"));
  } catch {
    die(`vitest list --project ${project} wrote no parsable JSON`);
  }
  // --filesOnly yields path strings; a plain --json yields {file} objects.
  return [...new Set(items.map((it) => rel(typeof it === "string" ? it : it.file)))];
}

function derivePlan() {
  const serialized = dryRun([]).selectedSerializedSuites;
  const generalServer = dryRun(["--mode", "general", "--group", "general-server",
                                "--shard-index", "0", "--shard-count", "1"])
    .selectedGeneralServerSuites;
  if (!Array.isArray(serialized) || serialized.length === 0) {
    die("the wrapper's plan carries no serialized suites");
  }
  if (!Array.isArray(generalServer) || generalServer.length === 0) {
    die("the wrapper's plan carries no general-server suites");
  }
  const source = readFileSync(path.join(ROOT, WRAPPER), "utf8");
  const nonServerProjects = extractProjects(source, "nonServerProjects", 2);
  const generalWorkspacesAProjects = extractProjects(source, "generalWorkspacesAProjects", 1);
  for (const project of generalWorkspacesAProjects) {
    if (!nonServerProjects.includes(project)) {
      die(`generalWorkspacesAProjects names ${project}, not in nonServerProjects`);
    }
  }
  const workspaceProjects = {};
  for (const project of nonServerProjects) {
    workspaceProjects[project] = listProjectFiles(project);
  }
  return { serialized, generalServer, workspaceProjects };
}

function preflight() {
  if (!CFG.preflight) return;
  const r = spawnSync("pnpm", ["-s", "run", CFG.preflight],
                      { cwd: ROOT, stdio: ["ignore", "ignore", "inherit"] });
  if (r.status !== 0) die(`${CFG.preflight} failed`);
}

// The wrapper's own invocation shapes, with two deliberate differences: every
// invocation gets an explicit include-list (minus the exclusion set), and a
// failing invocation is recorded and the run continues — a truncated run cannot
// produce a complete failing set.
function buildInvocations(plan, exclude) {
  const keep = (files) => files.filter((f) => !exclude.has(f));
  const invocations = [];
  for (const f of keep(plan.serialized)) {
    invocations.push({ label: f, files: [f],
                       args: ["--project", SERVER_PROJECT, f, "--pool=forks", "--isolate"] });
  }
  const gs = keep(plan.generalServer);
  if (gs.length) {
    invocations.push({ label: "general-server", files: gs,
                       args: ["--project", SERVER_PROJECT,
                              "--no-file-parallelism", "--maxWorkers=1", ...gs] });
  }
  for (const [project, files] of Object.entries(plan.workspaceProjects)) {
    const kept = keep(files);
    if (kept.length) {
      invocations.push({ label: project, files: kept,
                         args: ["--project", project, ...kept] });
    }
  }
  return invocations;
}

// The vitest process's own exit status is deliberately unread: the JSON report
// is the record. A nonzero exit with a clean report is the measured
// contention-noise anomaly (#555 post-mortem) — recorded as data, never a
// verdict. A missing or unaccountable report is infrastructure.
function runInvocation(inv, i) {
  const report = `/tmp/vs-report-${i}.json`;
  spawnSync("pnpm", ["exec", "vitest", "run", ...inv.args,
                     "--reporter=json", `--outputFile=${report}`],
            { cwd: ROOT, env: invocationEnv(), stdio: ["ignore", "ignore", "inherit"] });
  let parsed;
  try {
    parsed = JSON.parse(readFileSync(report, "utf8"));
  } catch {
    die(`${inv.label}: vitest wrote no parsable JSON report`);
  }
  if (!Array.isArray(parsed.testResults)) {
    die(`${inv.label}: report carries no testResults array`);
  }
  const names = parsed.testResults.map((t) => rel(t.name));
  const asked = new Set(inv.files);
  const got = new Set(names);
  for (const f of asked) {
    if (!got.has(f)) die(`accounting: ${f} missing from ${inv.label}'s report`);
  }
  for (const f of got) {
    if (!asked.has(f)) die(`accounting: ${inv.label}'s report names unrequested ${f}`);
  }
  const failed = parsed.testResults
    .filter((t) => t.status === "failed")
    .map((t) => rel(t.name));
  return { reported: names.length, failed,
           anomaly: parsed.success === false && failed.length === 0 };
}

// A planned, non-excluded file the tree no longer carries counts as FAILED,
// not as infrastructure: `run` is invoked after the patch applies, so a PR
// that deletes or renames a baseline test file reads as a regression — the
// conservative reading of "an existing test stopped passing" (spec §6.3).
// vitest cannot report a file it cannot find, so absence counts as a failure
// by policy, decided here before the accounting check ever sees the file.
function partitionMissing(plan, exclude) {
  const missing = [];
  const check = (files) => files.filter((f) => {
    if (exclude.has(f)) return true;
    if (existsSync(path.join(ROOT, f))) return true;
    missing.push(f);
    return false;
  });
  const pruned = {
    serialized: check(plan.serialized),
    generalServer: check(plan.generalServer),
    workspaceProjects: Object.fromEntries(
      Object.entries(plan.workspaceProjects).map(([p, files]) => [p, check(files)])),
  };
  return { pruned, missing };
}

function runMain(rest) {
  const opts = {};
  for (let i = 0; i < rest.length; i += 2) opts[rest[i]] = rest[i + 1];
  const runMode = opts["--mode"];
  if (runMode !== "baseline" && runMode !== "regress") {
    die("run needs --mode baseline|regress");
  }
  let plan;
  try {
    plan = JSON.parse(readFileSync(opts["--plan"], "utf8"));
  } catch {
    die("run needs --plan pointing at a plan JSON file");
  }
  let exclude = new Set();
  if (opts["--exclude"]) {
    let parsed;
    try {
      parsed = JSON.parse(readFileSync(opts["--exclude"], "utf8"));
    } catch {
      die("--exclude file is not parsable JSON");
    }
    if (!Array.isArray(parsed)) die("--exclude file must be a JSON array of paths");
    exclude = new Set(parsed.map(String));
  }
  preflight();
  const { pruned, missing } = partitionMissing(plan, exclude);
  const invocations = buildInvocations(pruned, exclude);
  const failed = [...missing];
  const anomalies = [];
  let reported = 0;
  let planned = missing.length;
  for (const inv of invocations) planned += inv.files.length;
  invocations.forEach((inv, i) => {
    const r = runInvocation(inv, i);
    reported += r.reported;
    failed.push(...r.failed);
    if (r.anomaly) anomalies.push(inv.label);
  });
  const result = { mode: runMode, planned, reported, excluded: exclude.size,
                   invocations: invocations.length, missing,
                   failed: [...new Set(failed)].sort(), anomalies };
  console.log("===VERIFY-SUITE:BEGIN===");
  console.log(JSON.stringify(result));
  console.log("===VERIFY-SUITE:END===");
  process.exitCode = (runMode === "regress" && result.failed.length) ? EXIT_REGRESSED : 0;
}

const [mode, ...rest] = process.argv.slice(2);
if (mode === "plan") {
  console.log(JSON.stringify(derivePlan()));
  process.exit(0);
} else if (mode === "run") {
  runMain(rest);
} else {
  die(`unknown mode ${mode}`);
}
