#!/usr/bin/env bash
# The ONLY supported way to run the pr-verify sandbox. Runs ONE phase and exits
# with that phase's sentinel, which the trusted host reads as the authoritative
# result. Guarantees, in code:
#   - an --internal (isolated) Docker network,
#   - a container env built from an explicit allowlist (no host passthrough),
#   - capability drops + resource limits,
#   - read-only mounts only — no writable mount exists,
#   - the fail-closed boot probe runs before PR code (inside run-phase.sh).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NET="${PR_VERIFY_NET:-pr-verify-net}"

IMAGE="" PHASE="" PATCH="" EXCL="" SUITE_CFG="" PROBE_DENY_ARG="" TIER=0 TEST_CMD="pnpm -s test" BASE_SHA="unknown" HEAD_SHA="unknown"
while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE="$2"; shift 2;;
    --phase) PHASE="$2"; shift 2;;
    --patch) PATCH="$2"; shift 2;;
    --exclude-file) EXCL="$2"; shift 2;;
    --suite-config) SUITE_CFG="$2"; shift 2;;
    --probe-deny) PROBE_DENY_ARG="$2"; shift 2;;
    --tier) TIER="$2"; shift 2;;
    --test-cmd) TEST_CMD="$2"; shift 2;;
    --base-sha) BASE_SHA="$2"; shift 2;;
    --head-sha) HEAD_SHA="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ -n "$IMAGE" ] && [ -n "$PHASE" ] || {
  echo "usage: sandbox-run.sh --image IMG --phase apply-check|repro|red|green|compile|build|baseline|regress [--patch F] [--exclude-file F] [--suite-config F] [--probe-deny LIST] [--tier 0|1] [--test-cmd C] [--base-sha S] [--head-sha S]" >&2
  exit 2; }
case "$PHASE" in apply-check|repro|red|green|compile|build|baseline|regress) ;; *) echo "bad --phase: $PHASE" >&2; exit 2;; esac
# apply-check, green, compile, and build require a patch onto the base tree.
# red takes one optionally — the test-only hunks, so it can run a test the
# diff itself adds — and runs unpatched when omitted. repro always runs
# against the base tree as pinned and never takes one. regress requires one
# too, plus the exclusion file from the baseline phase's failing set.
case "$PHASE" in
  apply-check|green|compile|build|regress)
    [ -n "$PATCH" ] || { echo "usage: --phase $PHASE requires --patch F" >&2; exit 2; }
    ;;
esac
if [ "$PHASE" = "regress" ] && [ -z "$EXCL" ]; then
  echo "usage: --phase regress requires --exclude-file F" >&2; exit 2
fi
# The suite phases run verify-suite.mjs, which needs its repository contract.
case "$PHASE" in
  baseline|regress)
    [ -n "$SUITE_CFG" ] || { echo "usage: --phase $PHASE requires --suite-config F" >&2; exit 2; }
    ;;
esac

# 1. Isolated network, created and asserted here — never assumed.
docker network inspect "$NET" >/dev/null 2>&1 || docker network create --internal "$NET" >/dev/null
if [ "$(docker network inspect -f '{{.Internal}}' "$NET")" != "true" ]; then
  echo "refusing to run: network $NET is not --internal" >&2; exit 1
fi

# 2. Env allowlist — ONLY the vars set here enter the container. No host
# passthrough. Every value is host-authored: the launcher's own arguments,
# never content an agent wrote.
env_args=(
  -e "BASE_SHA=$BASE_SHA"
  -e "HEAD_SHA=$HEAD_SHA"
  -e "TIER=$TIER"
  -e "TEST_CMD=$TEST_CMD"
  -e "PHASE=$PHASE"
)
# PROBE_DENY is the boot probe's must-be-unreachable host:port list. Set only
# when the launcher overrides it; unset lets boot-probe.sh's built-in default
# apply inside the container.
if [ -n "$PROBE_DENY_ARG" ]; then
  env_args+=( -e "PROBE_DENY=$PROBE_DENY_ARG" )
fi
patch_mount=()
if [ -n "$PATCH" ]; then
  env_args+=( -e "PATCH_FILE=/patch/fix.patch" )
  patch_mount=( -v "$PATCH:/patch/fix.patch:ro" )
else
  env_args+=( -e "PATCH_FILE=" )
fi

# EXCLUDE_FILE, when set, is a host-written file path — the baseline phase's own
# failing-test set, never agent-authored content — mounted read-only.
exclude_mount=()
if [ -n "$EXCL" ]; then
  env_args+=( -e "EXCLUDE_FILE=/verify/exclude.json" )
  exclude_mount=( -v "$EXCL:/verify/exclude.json:ro" )
else
  env_args+=( -e "EXCLUDE_FILE=" )
fi

# SUITE_CONFIG, when set, is a host-written file path — the profile's full-suite
# repository contract (verify-suite.mjs reads wrapper path, project names, and
# fixture env names from it), the same trust shape as EXCLUDE_FILE.
suite_mount=()
if [ -n "$SUITE_CFG" ]; then
  env_args+=( -e "SUITE_CONFIG=/verify/suite-config.json" )
  suite_mount=( -v "$SUITE_CFG:/verify/suite-config.json:ro" )
else
  env_args+=( -e "SUITE_CONFIG=" )
fi

# The env dump exists only for the secretless test. It goes to stdout, which the
# host captures anyway — the container env stays the explicit allowlist above.
if [ "${PR_VERIFY_DEBUG:-}" = "1" ]; then
  container_cmd='env | sort; bash /run-phase.sh'
else
  container_cmd='bash /run-phase.sh'
fi

# 3. Run: caps dropped, limits set, read-only mounts only. The exit code IS the
# result — propagate it verbatim. The limits are per-phase: the whole-repo
# phases (compile and build run a whole-repo command whose peak needs 6g;
# baseline and regress run the full suite, whose many node processes are
# thread-heavy — and threads count against the pids cgroup, so a starved
# worker dies in uv_thread_create before it can run a single test) get the
# large class; every other phase runs a bounded test selection at 2g and 512
# tasks, which still stops a fork bomb in PR code.
MEM=2g
PIDS=512
case "$PHASE" in compile|build|baseline|regress) MEM=6g; PIDS=2048;; esac
docker run --rm --network "$NET" \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --pids-limit "$PIDS" --memory "$MEM" --cpus 2 \
  "${env_args[@]}" \
  -v "$HERE/boot-probe.sh:/boot-probe.sh:ro" \
  -v "$HERE/run-phase.sh:/run-phase.sh:ro" \
  -v "$HERE/verify-suite.mjs:/verify-suite.mjs:ro" \
  "${patch_mount[@]+"${patch_mount[@]}"}" \
  "${exclude_mount[@]+"${exclude_mount[@]}"}" \
  "${suite_mount[@]+"${suite_mount[@]}"}" \
  "$IMAGE" bash -lc "$container_cmd"
exit $?
