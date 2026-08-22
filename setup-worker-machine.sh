#!/usr/bin/env bash
# Prospector — turn this machine into a work-queue processor.
#
# One command from a fresh clone to a running verify/autofix worker: repo
# dependencies, a Docker runtime, the hardened sandbox image, this machine's own
# pinned base, and the worker lane switches. Idempotent — re-running it on a
# provisioned machine changes nothing and says so.
#
# Runs in YOUR terminal, not from the app: `brew` and `sudo` need a TTY, and
# they should run as you. The app's Setup view diagnoses and flips lane
# switches; it never installs anything.
#
# Usage: ./setup-worker-machine.sh [--verify-only] [--yes]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

VERIFY_ONLY=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --verify-only) VERIFY_ONLY=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m→ %s\033[0m\n' "$1"; }
warn() { printf '\033[33m! %s\033[0m\n' "$1"; }

confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  read -r -p "$1 [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

# --- 0. config ---------------------------------------------------------------
# The worker reads TRIAGE_REPO and the store from .env. Without one it would
# provision a machine that cannot see the queue it is meant to drain.
if [ ! -f "$ROOT/.env" ]; then
  warn "no .env at the repo root — copy .env.example to .env and fill in"
  warn "TRIAGE_REPO, TRIAGE_STORE_URL and the bot identity first."
  exit 1
fi

# --- 1. repo dependencies ----------------------------------------------------
say "repo dependencies"
"$ROOT/setup.sh"

# --- 2. system dependencies --------------------------------------------------
# Named, not installed silently: this is the step that touches your machine
# rather than the checkout.
say "system dependencies"
MISSING=()
for tool in colima docker gh jq node; do
  command -v "$tool" >/dev/null 2>&1 || MISSING+=("$tool")
done

if [ ${#MISSING[@]} -gt 0 ]; then
  if ! command -v brew >/dev/null 2>&1; then
    warn "missing: ${MISSING[*]} — and no Homebrew to install them with."
    warn "Install them yourself, then re-run."
    exit 1
  fi
  echo "missing: ${MISSING[*]}"
  if confirm "install them with Homebrew?"; then
    brew install "${MISSING[@]}"
  else
    warn "skipped — the steps below will fail without them."
    exit 1
  fi
else
  echo "all present: colima docker gh jq node"
fi

# --- 3. the Docker VM --------------------------------------------------------
# Sized for the profile's merge-gate lanes: compile/build phases run in 6g
# containers and a merge preflight can overlap a worker run.
say "Docker runtime"
if docker info >/dev/null 2>&1; then
  echo "daemon already answering"
else
  echo "starting Colima with 12GB (compile lanes run in 6g containers)"
  colima start --memory 12
fi

# --- 4. sandbox image + this machine's base ----------------------------------
# Both are local artifacts. The base pin is per machine, so this builds THIS
# machine's copy rather than adopting another's.
say "hardened sandbox image"
uv run python pipeline/verify_driver.py build-image

say "pinned base (clone + image + captured baseline — this takes a while)"
uv run python pipeline/verify_driver.py prepare-base --tier 1

# --- 5. lane switches --------------------------------------------------------
# Written through the same allowlist the app's Setup view writes, so the two
# can never disagree about what a lane switch is.
say "worker lane switches"
FLAGS='{"TRIAGE_VERIFY_WORKER": "1", "TRIAGE_VERIFY_AUTOHUNT": "1"}'
if [ "$VERIFY_ONLY" = 0 ]; then
  if uv run python -c "from pipeline import settings; raise SystemExit(0 if settings.push_identity_configured() else 1)"; then
    FLAGS='{"TRIAGE_VERIFY_WORKER": "1", "TRIAGE_VERIFY_AUTOHUNT": "1", "TRIAGE_FIX_WORKER": "1"}'
  else
    warn "no contributor-push identity configured — enabling verification only."
    warn "Set one up on the app's Setup tab (or TRIAGE_PUSH_LOGIN, TRIAGE_PUSH_EMAIL"
    warn "and TRIAGE_PUSH_SSH_KEY_FILE in .env) to run autofix too."
  fi
fi
FLAGS="$FLAGS" uv run python -c "
import json, os
from prospector_app.backend import worker_control
worker_control.set_flags(json.loads(os.environ['FLAGS']))
print('set: ' + ', '.join(f'{k}={v}' for k, v in json.loads(os.environ['FLAGS']).items()))
"

# --- 6. what this machine can now do ----------------------------------------
say "readiness"
uv run python -c "
from prospector_app.backend import worker_readiness as wr
r = wr.report()
for c in r['checks']:
    mark = 'ok  ' if c['ok'] else 'todo'
    line = f\"  [{mark}] {c['label']}: {c['detail']}\"
    print(line + (f\" — {c['remedy']}\" if c['remedy'] else ''))
print()
print('verification: ' + ('ready' if r['ready'] else 'NOT ready'))
print('autofix:      ' + ('ready' if r['autofix_ready'] else 'not enabled'))
"

say "done — start the app (\`uv run prospector serve\`) and the worker threads start with it"
