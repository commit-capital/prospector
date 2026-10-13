#!/usr/bin/env node
// Trusted in-container reader for the coverage phase. sandbox-run.sh mounts it
// read-only; run-phase.sh runs it after the tests, from the tree's root. It
// reads the host-written request (`{"files": {path: [changed lines]}}`) and the
// istanbul JSON the coverage provider wrote, and prints one trailer naming, per
// requested file, the changed lines a statement starts on (`executable`) and
// those whose statement ran (`hit`). A file the report does not carry is
// `measured: false`.
//
// The report is written by a process running the PR's code, so the trailer is
// evidence, never a verdict.
//
// Exit contract: 0 trailer printed | 1 the request or report is unreadable.
import { readFileSync } from "node:fs";
import path from "node:path";

const [requestPath, reportPath] = process.argv.slice(2);

function load(file, what) {
  try {
    return JSON.parse(readFileSync(file, "utf8"));
  } catch (e) {
    console.error(`[verify-coverage] cannot read the ${what} ${file}: ${e.message}`);
    process.exit(1);
  }
}

const request = load(requestPath, "request");
const report = load(reportPath, "coverage report");
const root = process.cwd();

// The report keys files by absolute path; the request by repo-relative path.
const byPath = new Map();
for (const [key, entry] of Object.entries(report)) {
  const abs = typeof entry?.path === "string" ? entry.path : key;
  byPath.set(path.relative(root, abs).split(path.sep).join("/"), entry);
}

const files = {};
for (const [file, lines] of Object.entries(request.files ?? {})) {
  const entry = byPath.get(file);
  if (!entry || typeof entry.statementMap !== "object" || typeof entry.s !== "object") {
    files[file] = { measured: false, executable: [], hit: [] };
    continue;
  }
  const starts = new Map();  // line -> whether any statement starting there ran
  for (const [id, loc] of Object.entries(entry.statementMap)) {
    const line = loc?.start?.line;
    if (!Number.isInteger(line)) continue;
    starts.set(line, starts.get(line) === true || Number(entry.s[id]) > 0);
  }
  const wanted = (Array.isArray(lines) ? lines : []).filter((l) => starts.has(l));
  files[file] = { measured: true, executable: wanted, hit: wanted.filter((l) => starts.get(l)) };
}

console.log(`===VERIFY-COVERAGE:BEGIN===\n${JSON.stringify({ files })}\n===VERIFY-COVERAGE:END===`);
