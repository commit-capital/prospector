#!/usr/bin/env bash
# Hermetic host-run tests for sandbox/verify-suite.mjs. A stub tree supplies
# the wrapper plan and a stub pnpm plays vitest — no Docker, no real suite.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
FIX="$HERE/fixtures/verify-suite"
export PATH="$FIX/bin:$PATH"
# Resolved (not logical) so a subshell's `cd "$WORK/..."` reports the same
# path Node's process.cwd() does — macOS's tmp dir sits behind a /var ->
# /private/var symlink, and run mode's accounting reconciles absolute report
# paths against cwd.
WORK="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$WORK"' EXIT
fail=0
note() { echo "  $1"; }
bad() { echo "FAIL: $1"; fail=1; }

# The repository contract every invocation reads (SUITE_CONFIG), matching the
# fixture tree's wrapper and the stub pnpm's project names.
export SUITE_CONFIG="$WORK/suite-config.json"
cat > "$SUITE_CONFIG" <<'CFG'
{"wrapper": "scripts/run-vitest-stable.mjs", "server_project": "@fix/server",
 "preflight": "preflight:workspace-links",
 "home_env": "FIX_HOME", "instance_env": "FIX_INSTANCE_ID"}
CFG

# A missing contract fails closed before anything runs.
( cd "$FIX/tree" && SUITE_CONFIG= node "$ROOT/verify-suite.mjs" plan > /dev/null 2>&1 )
[ $? -ne 0 ] && note "a missing SUITE_CONFIG fails closed" \
             || bad "verify-suite ran without its repository contract"

cd "$FIX/tree"

# --- plan derivation ---
plan="$WORK/plan.json"
if node "$ROOT/verify-suite.mjs" plan > "$plan"; then
  grep -q "route-a.test.ts" "$plan" || bad "plan lacks a serialized suite"
  grep -q "gs-one.test.ts" "$plan" || bad "plan lacks a general-server suite"
  grep -q "packages/lib/src/l1.test.ts" "$plan" || bad "plan lacks a workspace project file"
  note "plan derives all three groups"
else
  bad "plan mode exited nonzero on a good tree"
fi

grep -q '"source":"wrapper"' "$plan" || bad "the wrapper's plan does not say so"

# --- a wrapper that cannot answer: the plan comes from vitest's listing ---
listed_ok() {  # $1 = tree, $2 = what the case is
  local out
  out="$(cd "$1" && node "$ROOT/verify-suite.mjs" plan 2>/dev/null)" \
    || { bad "$2: the plan failed instead of listing"; return; }
  echo "$out" | grep -q '"source":"vitest-list"' || bad "$2: the plan does not come from the listing"
  echo "$out" | grep -q '"generalServer":\["server/src/__tests__/route-a.test.ts"' \
    || bad "$2: the listing's server files are not the general-server group"
  echo "$out" | grep -q '"@fix/lib":\["packages/lib/src/l1.test.ts"\]' \
    || bad "$2: the listing's workspace files are missing"
  note "$2: planned from vitest list"
}

# a wrapper whose source lacks the arrays read here
DOCTORED="$WORK/doctored"
cp -R "$FIX/tree" "$DOCTORED"
sed -i.bak 's/nonServerProjects/renamedProjects/g' "$DOCTORED/scripts/run-vitest-stable.mjs"
listed_ok "$DOCTORED" "a wrapper without the project arrays"

# --dry-run emitting non-JSON
GARBAGE="$WORK/tree-garbage"
cp -R "$FIX/tree" "$GARBAGE"
cat > "$GARBAGE/scripts/run-vitest-stable.mjs" <<'STUB'
const nonServerProjects = ["@fix/ui", "@fix/lib"];
void nonServerProjects;
console.log("not json at all");
STUB
listed_ok "$GARBAGE" "a non-JSON dry-run"

# a wrapper that does not know --dry-run
NODRY="$WORK/tree-nodry"
cp -R "$FIX/tree" "$NODRY"
printf '%s\n' 'console.error("unknown flag"); process.exit(2);' > "$NODRY/scripts/run-vitest-stable.mjs"
listed_ok "$NODRY" "a wrapper without --dry-run"

# no wrapper at all
NOWRAP="$WORK/tree-nowrap"
cp -R "$FIX/tree" "$NOWRAP"
rm "$NOWRAP/scripts/run-vitest-stable.mjs"
listed_ok "$NOWRAP" "a tree with no wrapper"

# --- a wrapper that answers with too little fails the plan closed ---

# an empty serialized list fails the plan closed
EMPTY="$WORK/tree-empty"
cp -R "$FIX/tree" "$EMPTY"
sed -i.bak 's/"server\/src\/__tests__\/route-a.test.ts",//; s/"server\/src\/__tests__\/authz-b.test.ts",\{0,1\}//' \
  "$EMPTY/scripts/run-vitest-stable.mjs"
( cd "$EMPTY" && node "$ROOT/verify-suite.mjs" plan > /dev/null 2>&1 )
[ $? -ne 0 ] && note "an empty serialized list fails the plan closed" \
             || bad "plan succeeded with no serialized suites"

# a single-project nonServerProjects array fails the plan closed (< 2)
SINGLE="$WORK/tree-single"
cp -R "$FIX/tree" "$SINGLE"
sed -i.bak 's/const nonServerProjects = \["@fix\/ui", "@fix\/lib"\]/const nonServerProjects = ["@fix\/ui"]/' \
  "$SINGLE/scripts/run-vitest-stable.mjs"
( cd "$SINGLE" && node "$ROOT/verify-suite.mjs" plan > /dev/null 2>&1 )
[ $? -ne 0 ] && note "a suspiciously small project list fails the plan closed" \
             || bad "plan succeeded with one workspace project"

# generalWorkspacesAProjects naming a project outside nonServerProjects fails
# the plan closed (the subset assertion)
NOTSUBSET="$WORK/tree-notsubset"
cp -R "$FIX/tree" "$NOTSUBSET"
sed -i.bak 's/const generalWorkspacesAProjects = \["@fix\/ui"\]/const generalWorkspacesAProjects = ["@fix\/other"]/' \
  "$NOTSUBSET/scripts/run-vitest-stable.mjs"
( cd "$NOTSUBSET" && node "$ROOT/verify-suite.mjs" plan > /dev/null 2>&1 )
[ $? -ne 0 ] && note "a generalWorkspacesAProjects entry outside nonServerProjects fails the plan closed" \
             || bad "plan succeeded with generalWorkspacesAProjects not a subset of nonServerProjects"

# --- run mode ---
excl_none="$WORK/excl-none.json"; printf '[]' > "$excl_none"

# baseline with two failures: exits 0, trailer lists them as data
out=$(FIXTURE_FAIL="server/src/__tests__/gs-two.test.ts,packages/ui/src/u2.test.ts" \
      node "$ROOT/verify-suite.mjs" run --plan "$plan" --mode baseline)
rc=$?
[ "$rc" -eq 0 ] || bad "baseline with failures exited $rc, want 0"
echo "$out" | grep -q '===VERIFY-SUITE:BEGIN===' || bad "baseline printed no trailer"
echo "$out" | grep -q 'gs-two.test.ts' || bad "baseline trailer lacks a failing file"
echo "$out" | grep -q 'u2.test.ts' || bad "baseline trailer lacks the workspace failure"
note "baseline completes with failures as trailer data"

# regress excluding the baseline failures: excluded files never run -> exit 0
excl="$WORK/excl.json"
printf '%s' '["server/src/__tests__/gs-two.test.ts","packages/ui/src/u2.test.ts"]' > "$excl"
FIXTURE_FAIL="server/src/__tests__/gs-two.test.ts,packages/ui/src/u2.test.ts" \
  node "$ROOT/verify-suite.mjs" run --plan "$plan" --mode regress --exclude "$excl" > /dev/null
[ $? -eq 0 ] && note "regress is clean when only excluded files would fail" \
             || bad "regress flagged an excluded baseline failure"

# regress with a NEW failure: exit 20, trailer names it
out=$(FIXTURE_FAIL="packages/lib/src/l1.test.ts" \
      node "$ROOT/verify-suite.mjs" run --plan "$plan" --mode regress --exclude "$excl")
rc=$?
[ "$rc" -eq 20 ] || bad "regress with a new failure exited $rc, want 20"
echo "$out" | grep -q 'l1.test.ts' || bad "regress trailer lacks the new failure"
note "a new failure exits 20 and is named in the trailer"

# a listed plan runs, and the trailer says where its plan came from
listed="$WORK/listed.json"
( cd "$NOWRAP" && node "$ROOT/verify-suite.mjs" plan > "$listed" 2>/dev/null )
out=$( cd "$NOWRAP" && FIXTURE_FAIL="packages/lib/src/l1.test.ts" \
       node "$ROOT/verify-suite.mjs" run --plan "$listed" --mode regress --exclude "$excl_none" )
rc=$?
[ "$rc" -eq 20 ] || bad "a listed plan's regress with a new failure exited $rc, want 20"
echo "$out" | grep -q '"plan":"vitest-list"' || bad "the trailer does not name the listed plan"
note "a listed plan runs and names its planner"

# a tree without the contract's preflight script skips it
NOPRE="$WORK/tree-nopre"
cp -R "$FIX/tree" "$NOPRE"
printf '%s' '{"name":"fixture","scripts":{}}' > "$NOPRE/package.json"
( cd "$NOPRE" && PATH="$FIX/bin:$PATH" node "$ROOT/verify-suite.mjs" run --plan "$plan" \
    --mode baseline > /dev/null 2>&1 )
[ $? -eq 0 ] && note "a tree without the preflight script skips it" \
             || bad "a tree without the preflight script failed the run"

# accounting: a file missing from an invocation's report is infrastructure
FIXTURE_DROP="server/src/__tests__/gs-one.test.ts" \
  node "$ROOT/verify-suite.mjs" run --plan "$plan" --mode baseline > /dev/null 2>&1
[ $? -eq 1 ] && note "a dropped report file fails the accounting closed" \
             || bad "accounting let a missing file slide"

# a planned file the patched tree no longer carries reads as a failure (§6.3)
BROKEN="$WORK/tree-missing"
cp -R "$FIX/tree" "$BROKEN"
rm "$BROKEN/packages/lib/src/l1.test.ts"
out=$( cd "$BROKEN" && node "$ROOT/verify-suite.mjs" run --plan "$plan" --mode regress --exclude "$excl_none" )
rc=$?
[ "$rc" -eq 20 ] || bad "a removed planned file exited $rc, want 20"
echo "$out" | grep -q 'l1.test.ts' || bad "trailer does not name the removed file"
note "a removed planned file reads as a regression"

[ "$fail" = 0 ] && echo "PASS test-verify-suite" || echo "FAIL test-verify-suite"
exit "$fail"
