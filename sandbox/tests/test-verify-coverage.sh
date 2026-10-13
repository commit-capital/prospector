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
{"files": {"src/a.ts": [1, 2, 3, 5], "src/unloaded.ts": [4]}}
REQ
cat > "$WORK/report.json" <<REP
{"$WORK/src/a.ts": {"path": "$WORK/src/a.ts",
  "statementMap": {"0": {"start": {"line": 1, "column": 0}, "end": {"line": 1, "column": 9}},
                   "1": {"start": {"line": 3, "column": 2}, "end": {"line": 4, "column": 1}},
                   "2": {"start": {"line": 3, "column": 9}, "end": {"line": 3, "column": 20}},
                   "3": {"start": {"line": 5, "column": 0}, "end": {"line": 5, "column": 9}}},
  "s": {"0": 1, "1": 0, "2": 2, "3": 0}}}
REP

out="$(cd "$WORK" && node "$ROOT/verify-coverage.mjs" request.json report.json)" \
  || bad "a readable request and report exited nonzero"
json="$(printf '%s\n' "$out" | sed -n '/===VERIFY-COVERAGE:BEGIN===/{n;p;}')"
[ "$json" = '{"files":{"src/a.ts":{"measured":true,"executable":[1,3,5],"hit":[1,3]},"src/unloaded.ts":{"measured":false,"executable":[],"hit":[]}}}' ] \
  && note "executable and hit lines come from statement starts; an absent file is unmeasured" \
  || bad "unexpected trailer: $json"

( cd "$WORK" && node "$ROOT/verify-coverage.mjs" request.json missing.json > /dev/null 2>&1 )
[ $? -eq 1 ] && note "a missing report exits 1" || bad "a missing report did not exit 1"

exit $fail
