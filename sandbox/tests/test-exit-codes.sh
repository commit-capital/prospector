#!/usr/bin/env bash
# The host-visible contract: each phase container exits with a SENTINEL the host
# accepts only on an exact match. A red is 20 and nothing else, so untrusted code
# cannot manufacture one by killing PID 1: SIGKILL sent to a PID namespace's init
# from inside that namespace is discarded, so the kill is a no-op and exits 0.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SCRATCH="$HOME/.cache/pr-verify-tests"; mkdir -p "$SCRATCH"
CTX="$(mktemp -d "$SCRATCH/ctx.XXXXXX")"
cleanup() { rm -rf "$CTX"; docker rmi -f pr-verify-base:exitcodes-t0 >/dev/null 2>&1 || true; }
trap cleanup EXIT

# A base image whose "test" is driven by a marker file the patch creates:
# red (no patch) fails; green (patched) passes. Exactly the red->green shape.
# pnpm-store must exist even at Tier 0 — Dockerfile.base COPYs it unconditionally.
mkdir -p "$CTX/src" "$CTX/pnpm-store"
cat > "$CTX/src/package.json" <<'JSON'
{ "name": "fixture", "version": "0.0.0", "private": true,
  "scripts": {
    "test": "node -e \"process.exit(require('fs').existsSync('fixed.txt')?0:1)\"",
    "killpid1": "kill -9 1"
  } }
JSON
git -C "$CTX/src" init -q && git -C "$CTX/src" add -A \
  && GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t \
     git -C "$CTX/src" commit -qm base
cat > "$CTX/fix.patch" <<'PATCH'
diff --git a/fixed.txt b/fixed.txt
new file mode 100644
--- /dev/null
+++ b/fixed.txt
@@ -0,0 +1 @@
+fixed
PATCH
cat > "$CTX/bad.patch" <<'PATCH'
diff --git a/nonexistent.txt b/nonexistent.txt
--- a/nonexistent.txt
+++ b/nonexistent.txt
@@ -1 +1 @@
-was
+now
PATCH

docker build -q --network none -t pr-verify-base:exitcodes-t0 \
  --build-arg TIER=0 -f "$HERE/Dockerfile.base" "$CTX" >/dev/null || {
    echo "base image build failed"; exit 1; }

run() { bash "$HERE/sandbox-run.sh" --image pr-verify-base:exitcodes-t0 \
          --test-cmd 'pnpm -s test' --base-sha b --head-sha h --tier 0 "$@" >/dev/null 2>&1
        echo $?; }

fail=0
check() { # check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "  ok $1 -> $3"; else echo "  FAIL $1: want $2 got $3"; fail=1; fi
}
check "apply-check clean patch = 0"  0  "$(run --phase apply-check --patch "$CTX/fix.patch")"
check "apply-check conflict = 30"    30 "$(run --phase apply-check --patch "$CTX/bad.patch")"
check "apply-check with no --patch = 2 (launcher usage error)" 2 "$(run --phase apply-check)"
check "red on base = 20 (test fails)" 20 "$(run --phase red)"
check "red with patch = 0 (red applies its optional patch)" 0 \
  "$(run --phase red --patch "$CTX/fix.patch")"
check "red with bad patch = 30 (a conflict is still a conflict on red)" 30 \
  "$(run --phase red --patch "$CTX/bad.patch")"
check "green with patch = 0"          0  "$(run --phase green --patch "$CTX/fix.patch")"
check "green with bad patch = 30"     30 "$(run --phase green --patch "$CTX/bad.patch")"

# compile (the merge-time compile preflight) shares green's contract: apply the
# full diff, run the command, sentinel exit.
check "compile with patch, passing cmd = 0" 0 \
  "$(run --phase compile --patch "$CTX/fix.patch" --test-cmd 'pnpm -s test')"
check "compile failing cmd = 20" 20 \
  "$(run --phase compile --patch "$CTX/fix.patch" --test-cmd 'exit 1')"
check "compile with bad patch = 30"   30 "$(run --phase compile --patch "$CTX/bad.patch")"
check "compile with no --patch = 2 (launcher usage error)" 2 "$(run --phase compile)"

# An untrusted CHILD killing PID 1 must NOT be able to present as a red. The
# fixture's "killpid1" script runs as a pnpm-spawned child, not PID 1 itself:
# run-phase.sh runs the command in a subshell which forks/execs pnpm, and pnpm
# forks the script's shell, so `kill -9 1` executes several process levels below
# PID 1. SIGKILL sent to a PID namespace's init from inside that namespace is
# discarded, so the kill is a no-op and the phase exits exactly 0. check()
# asserts exact equality, pinning the measured value.
check "kill -9 1 (as child) -> 0, not a red" 0 \
  "$(run --phase red --test-cmd 'pnpm -s run killpid1')"

# The command STRING is untrusted too, independently of the code it runs: TEST_CMD
# is authored by the blind agent, whose input includes attacker-controlled PR text
# (the diff, the PR body, linked issues). A prompt-injected agent could emit a
# string that names a sentinel directly. run-phase.sh confines the string to a
# subshell, so its `exit` sets the subshell's status and PID 1's exit still comes
# from the trusted sentinel mapping. A string naming 10 (a probe failure, which
# aborts the entire batch) or 30 (a patch conflict) is reported as what the host
# can actually attest — a command that exited nonzero — never as the sentinel the
# string named.
check "test-cmd 'exit 10' cannot forge a probe failure"  20 "$(run --phase red --test-cmd 'exit 10')"
check "test-cmd 'exit 30' cannot forge a patch conflict" 20 "$(run --phase red --test-cmd 'exit 30')"
check "test-cmd 'exit 1' cannot forge an infra error"    20 "$(run --phase red --test-cmd 'exit 1')"
check "test-cmd 'exit 10' cannot forge a probe failure on green" 20 \
  "$(run --phase green --patch "$CTX/fix.patch" --test-cmd 'exit 10')"
check "test-cmd 'exit 30' cannot forge a patch conflict on compile" 20 \
  "$(run --phase compile --patch "$CTX/fix.patch" --test-cmd 'exit 30')"

# build (the second merge-gate lane) shares compile's contract.
check "build with patch, passing cmd = 0" 0 \
  "$(run --phase build --patch "$CTX/fix.patch" --test-cmd 'pnpm -s test')"
check "build failing cmd = 20" 20 \
  "$(run --phase build --patch "$CTX/fix.patch" --test-cmd 'exit 1')"
check "build with bad patch = 30"   30 "$(run --phase build --patch "$CTX/bad.patch")"
check "build with no --patch = 2 (launcher usage error)" 2 "$(run --phase build)"
check "test-cmd 'exit 30' cannot forge a patch conflict on build" 20 \
  "$(run --phase build --patch "$CTX/fix.patch" --test-cmd 'exit 30')"
# `exit 20` is the one string whose named sentinel coincides with the value the
# mapping emits for any failing command. The 20 here is the mapping's own reading
# of a command that exited nonzero, which is an accurate account of `exit 20`.
check "test-cmd 'exit 20' is mapped, not forged"         20 "$(run --phase red --test-cmd 'exit 20')"

[ "$fail" = 0 ] && echo "exit-code contract ok"
exit $fail
