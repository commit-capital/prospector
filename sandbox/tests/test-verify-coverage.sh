#!/usr/bin/env bash
# Hermetic host-run tests for sandbox/verify-coverage.mjs over a hand-written
# istanbul report — no Docker, no provider.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$WORK"' EXIT
fail=0
note() { echo "  $1"; }
bad() { echo "FAIL: $1"; fail=1; }

cat > "$WORK/request.json" <<'REQ'
{"files": {"src/a.ts": [1, 2, 3, 5, 7, 11], "src/unloaded.ts": [4]}}
REQ
cat > "$WORK/report.json" <<REP
{"$WORK/src/a.ts": {"path": "$WORK/src/a.ts",
  "statementMap": {"0": {"start": {"line": 1, "column": 0}, "end": {"line": 1, "column": 9}},
                   "1": {"start": {"line": 3, "column": 2}, "end": {"line": 4, "column": 1}},
                   "2": {"start": {"line": 3, "column": 9}, "end": {"line": 3, "column": 20}},
                   "3": {"start": {"line": 5, "column": 0}, "end": {"line": 5, "column": 9}},
                   "4": {"start": {"line": 6, "column": 0}, "end": {"line": 8, "column": 2}},
                   "5": {"start": {"line": 10, "column": 0}, "end": {"line": 13, "column": 1}},
                   "6": {"start": {"line": 12, "column": 2}, "end": {"line": 12, "column": 9}}},
  "s": {"0": 1, "1": 0, "2": 2, "3": 0, "4": 3, "5": 1, "6": 0}}}
REP
cat > "$WORK/tests.json" <<'TST'
{"numTotalTestSuites": 9, "numFailedTestSuites": 3, "numPassedTests": 5,
 "numFailedTests": 0, "numPendingTests": 1, "numTodoTests": 1,
 "testResults": [{"status": "passed", "assertionResults": [{"status": "passed"}]},
                 {"status": "failed", "assertionResults": [{"status": "failed"}]},
                 {"status": "failed", "assertionResults": []}]}
TST

trailer() { sed -n '/===VERIFY-COVERAGE:BEGIN===/{n;p;}'; }

out="$(cd "$WORK" && node "$ROOT/verify-coverage.mjs" request.json report.json tests.json)" \
  || bad "a readable request and report exited nonzero"
json="$(printf '%s\n' "$out" | trailer)"
[ "$json" = '{"files":{"src/a.ts":{"measured":true,"executable":[1,3,5,7],"hit":[1,3,7]},"src/unloaded.ts":{"measured":false,"executable":[],"hit":[]}},"tests":{"files":3,"failed_files":1,"passed":5,"failed":0,"skipped":2}}' ] \
  && note "statement starts and leaf continuations are executable, a line inside a block is not; an absent file is unmeasured" \
  || bad "unexpected trailer: $json"

json="$(cd "$WORK" && node "$ROOT/verify-coverage.mjs" request.json report.json missing.json | trailer)"
case "$json" in *'"tests":null}') note "no test report leaves the counts null";;
  *) bad "a missing test report should leave tests null: $json";; esac

( cd "$WORK" && node "$ROOT/verify-coverage.mjs" request.json missing.json > /dev/null 2>&1 )
[ $? -eq 1 ] && note "a missing report exits 1" || bad "a missing report did not exit 1"

exit $fail
