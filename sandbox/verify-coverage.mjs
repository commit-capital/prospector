#!/usr/bin/env node
// Trusted in-container reader for the coverage phase. sandbox-run.sh mounts it
// read-only; run-phase.sh runs it after the tests, from the tree's root. It
// reads the host-written request (`{"files": {path: [changed lines]}}`), the
// istanbul JSON the coverage provider wrote and, when present, vitest's JSON
// test report, and prints one trailer naming, per requested file, the changed
// lines that are executable — a statement starts on the line, or the line
// continues a statement holding no other — and those whose statement ran
// (`hit`), plus the run's test counts. A file the report does not carry is
// `measured: false`.
//
// The report is written by a process running the PR's code, so the trailer is
// evidence, never a verdict.
//
// Exit contract: 0 trailer printed | 1 the request or coverage report is
// unreadable.
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

const [requestPath, reportPath, testsPath] = process.argv.slice(2);

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
  const spans = [];
  for (const [id, loc] of Object.entries(entry.statementMap)) {
    const start = loc?.start?.line;
    if (!Number.isInteger(start)) continue;
    const end = Number.isInteger(loc?.end?.line) ? loc.end.line : start;
    spans.push({ start, end, ran: Number(entry.s[id]) > 0 });
  }
  const starts = new Map();  // line -> whether any statement starting there ran
  for (const s of spans) starts.set(s.start, starts.get(s.start) === true || s.ran);
  // A line no statement starts on counts when it lies inside a leaf statement
  // (one holding no other), so a changed continuation of a multi-line
  // expression is measured while a line inside a block is not.
  const leafRan = (line) => {
    let inner = null;
    for (const s of spans) {
      if (s.start < line && line <= s.end && (!inner || s.end - s.start < inner.end - inner.start)) inner = s;
    }
    if (!inner) return undefined;
    const holds = spans.some((o) => o !== inner && inner.start <= o.start && o.end <= inner.end
                                    && (o.start !== inner.start || o.end !== inner.end));
    return holds ? undefined : inner.ran;
  };
  const executable = [], hit = [];
  for (const l of Array.isArray(lines) ? lines : []) {
    const ran = starts.has(l) ? starts.get(l) : leafRan(l);
    if (ran === undefined) continue;
    executable.push(l);
    if (ran) hit.push(l);
  }
  files[file] = { measured: true, executable, hit };
}

// vitest's JSON report: how many test files there were and how many failed
// with no test run (they could not load), so a suite that could not load reads
// as such rather than as unexecuted lines.
let tests = null;
if (testsPath && existsSync(testsPath)) {
  try {
    const r = JSON.parse(readFileSync(testsPath, "utf8"));
    const n = (v) => (Number.isInteger(v) ? v : 0);
    const results = Array.isArray(r.testResults) ? r.testResults : [];
    const unloaded = results.filter((t) => t?.status === "failed"
                                           && !(Array.isArray(t.assertionResults) && t.assertionResults.length));
    tests = { files: results.length, failed_files: unloaded.length,
              passed: n(r.numPassedTests), failed: n(r.numFailedTests),
              skipped: n(r.numPendingTests) + n(r.numTodoTests) };
  } catch {
    tests = null;
  }
}

console.log(`===VERIFY-COVERAGE:BEGIN===\n${JSON.stringify({ files, tests })}\n===VERIFY-COVERAGE:END===`);
