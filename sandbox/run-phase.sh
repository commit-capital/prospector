#!/usr/bin/env bash
# In-container entrypoint for ONE verification phase. PID 1 is this trusted
# script; untrusted PR code runs as its child. The phase's result is this
# script's EXIT CODE, which the host observes and untrusted code cannot forge.
#
# Nothing is written to a mount: there is no results file to race. The captured
# stdout/stderr the host collects is the untrusted test's own output — evidence
# for the judgment agent and for the driver's dirty-green containment parse
# (gates.green_accepted), which can only ever accept a green that exited 20;
# the exit code stays the verdict's backbone.
#
# Sentinels are mirrored from pipeline/gates.py; test_verify_sentinels.py pins them.
set -uo pipefail

SENTINEL_PROBE_FAIL=10
SENTINEL_TEST_FAIL=20
SENTINEL_PATCH_CONFLICT=30

PHASE="${PHASE:?PHASE is required}"
TEST_CMD="${TEST_CMD:-pnpm -s test}"
PATCH_FILE="${PATCH_FILE:-}"
EXCLUDE_FILE="${EXCLUDE_FILE:-}"
SRC=/work/src

# 1. Fail-closed isolation gate, before any PR code, every boot.
bash /boot-probe.sh >&2 || exit "$SENTINEL_PROBE_FAIL"

cd "$SRC" || exit 1

# core.checkStat=minimal defeats the racy-git false mismatch when an index written
# by host git is re-read under Linux; structural to macOS+Colima.
apply_patch() {
  [ -n "$PATCH_FILE" ] || return 0
  # A PATCH_FILE that is set but not a readable regular file is fatal: exit 1
  # is not an accepted sentinel, so the host holds on it as an infrastructure
  # failure.
  [ -f "$PATCH_FILE" ] && [ -r "$PATCH_FILE" ] || {
    echo "PATCH_FILE is set but not a readable file: $PATCH_FILE" >&2
    exit 1
  }
  git -c core.checkStat=minimal apply --3way "$PATCH_FILE" >&2
}

# Run the command string inside a subshell. The parentheses are load-bearing: an
# `exit` in the string sets the subshell's status, so every path to PID 1's exit
# runs through the sentinel mapping in the case below. TEST_CMD is authored by the
# blind agent, whose input includes attacker-controlled PR text, so the string
# itself is untrusted and may name a sentinel.
run_test_cmd() {
  ( eval "$TEST_CMD" )
}

case "$PHASE" in
  apply-check)
    apply_patch || exit "$SENTINEL_PATCH_CONFLICT"
    exit 0
    ;;
  red)
    # PATCH_FILE, when set, is the test-only hunks: a test the diff adds does not
    # exist on pinned main, so red must apply it to run at all. Unset when the
    # test predates the diff (a linked-issue repro) — the base tree as pinned.
    apply_patch || exit "$SENTINEL_PATCH_CONFLICT"
    run_test_cmd
    [ $? -eq 0 ] && exit 0 || exit "$SENTINEL_TEST_FAIL"
    ;;
  repro)
    # No patch: the agent's independent repro, always against the base tree as
    # pinned — "did it fail on main?".
    run_test_cmd
    [ $? -eq 0 ] && exit 0 || exit "$SENTINEL_TEST_FAIL"
    ;;
  green)
    apply_patch || exit "$SENTINEL_PATCH_CONFLICT"
    run_test_cmd
    [ $? -eq 0 ] && exit 0 || exit "$SENTINEL_TEST_FAIL"
    ;;
  compile)
    # Merge-time compile preflight: the base tree is the current default-branch
    # HEAD and TEST_CMD is the profile's compile command — host-authored policy,
    # never agent text. The patch is the PR's full diff; a conflict means the PR
    # does not apply onto that HEAD. The explicit heap cap keeps every node
    # child (per-package tsc) inside the phase's 6g container memory.
    export NODE_OPTIONS="--max-old-space-size=4096"
    apply_patch || exit "$SENTINEL_PATCH_CONFLICT"
    run_test_cmd
    [ $? -eq 0 ] && exit 0 || exit "$SENTINEL_TEST_FAIL"
    ;;
  build)
    # Merge-gate build lane, sharing compile's contract: the profile's
    # whole-repo build command over the patched tree — host-authored policy,
    # never agent text. The heap cap keeps every node child inside the phase's
    # 6g container memory.
    export NODE_OPTIONS="--max-old-space-size=4096"
    apply_patch || exit "$SENTINEL_PATCH_CONFLICT"
    run_test_cmd
    [ $? -eq 0 ] && exit 0 || exit "$SENTINEL_TEST_FAIL"
    ;;
  baseline)
    # The pinned base's own full-suite run: the failing set it reports is the
    # regress phase's exclusion list. Failures are data — the phase fails only
    # on infrastructure, and the host refuses to pin on that.
    node /verify-suite.mjs plan > /tmp/verify-plan.json || exit 1
    node /verify-suite.mjs run --plan /tmp/verify-plan.json --mode baseline
    exit $?
    ;;
  regress)
    # The plan is derived from the pristine tree BEFORE the patch applies, so a
    # PR cannot influence which tests the suite runs. A conflict here
    # contradicts the already-passed apply-check; the host reads the 30 as
    # infrastructure for this phase, not as needs-rebase.
    node /verify-suite.mjs plan > /tmp/verify-plan.json || exit 1
    apply_patch || exit "$SENTINEL_PATCH_CONFLICT"
    [ -n "$EXCLUDE_FILE" ] || { echo "regress requires EXCLUDE_FILE" >&2; exit 1; }
    node /verify-suite.mjs run --plan /tmp/verify-plan.json --mode regress \
      --exclude "$EXCLUDE_FILE"
    rc=$?
    [ "$rc" -eq 0 ] && exit 0
    [ "$rc" -eq "$SENTINEL_TEST_FAIL" ] && exit "$SENTINEL_TEST_FAIL"
    exit 1
    ;;
  *)
    echo "unknown phase: $PHASE" >&2
    exit 1
    ;;
esac
