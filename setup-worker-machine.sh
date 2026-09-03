#!/usr/bin/env bash
# Prospector — turn this machine into a work-queue processor.
#
# One command from a configured clone to a ready verify/autofix worker: repo
# dependencies, a Docker runtime, the hardened sandbox image, this machine's own
# pinned base, and the worker lane switches. Idempotent — re-running it on a
# provisioned machine changes nothing and says so.
#
# Runs in YOUR terminal, not from the app: the platform package manager may
# need a TTY, and it should run as you. The app's Setup view diagnoses and
# flips lane switches; it never installs anything.
#
# Usage: ./setup-worker-machine.sh [--verify-only] [--yes]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
ORIGINAL_ARGS=("$@")

case "$(uname -s)" in
  Darwin) PLATFORM=macos ;;
  Linux) PLATFORM=linux ;;
  *) echo "unsupported platform: $(uname -s) (Prospector workers need macOS or Linux)" >&2; exit 1 ;;
esac

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

fail() { warn "$1"; exit 1; }

if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
  fail "run this script as $SUDO_USER without sudo; it elevates only the package and service steps"
fi

confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  read -r -p "$1 [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    fail "this step needs root; install sudo or run the command as root"
  fi
}

node_usable() {
  local version major
  version="$(command -v node >/dev/null 2>&1 && node --version 2>/dev/null || true)"
  version="${version#v}"
  major="${version%%.*}"
  [ -n "$major" ] && [ "$major" -ge 24 ] 2>/dev/null
}

install_uv() {
  local installer
  command -v curl >/dev/null 2>&1 || fail "curl is required to install uv"
  installer="$(mktemp)"
  if ! curl -LsSf https://astral.sh/uv/install.sh -o "$installer"; then
    rm -f "$installer"
    fail "could not download the uv installer"
  fi
  sh "$installer"
  rm -f "$installer"
  PATH="$HOME/.local/bin:$PATH"
  export PATH
}

install_node_linux() {
  local installer
  installer="$(mktemp)"
  case "$LINUX_PM" in
    apt)
      curl -fsSL https://deb.nodesource.com/setup_24.x -o "$installer"
      as_root bash "$installer"
      as_root apt-get install -y nodejs
      ;;
    dnf)
      curl -fsSL https://rpm.nodesource.com/setup_24.x -o "$installer"
      as_root bash "$installer"
      as_root dnf install -y nodejs
      ;;
  esac
  rm -f "$installer"
}

install_linux_dependencies() {
  local packages=()
  if command -v apt-get >/dev/null 2>&1; then
    LINUX_PM=apt
    as_root apt-get update
    as_root apt-get install -y ca-certificates curl
    command -v docker >/dev/null 2>&1 || packages+=(docker.io)
    command -v gh >/dev/null 2>&1 || packages+=(gh)
    command -v jq >/dev/null 2>&1 || packages+=(jq)
    [ ${#packages[@]} -eq 0 ] || as_root apt-get install -y "${packages[@]}"
  elif command -v dnf >/dev/null 2>&1; then
    LINUX_PM=dnf
    as_root dnf install -y ca-certificates curl
    if ! command -v docker >/dev/null 2>&1; then
      as_root dnf install -y docker || as_root dnf install -y moby-engine
    fi
    if ! command -v gh >/dev/null 2>&1; then
      if ! as_root dnf install -y gh; then
        as_root dnf install -y dnf-plugins-core
        as_root dnf config-manager --add-repo \
          https://cli.github.com/packages/rpm/gh-cli.repo
        as_root dnf install -y gh --repo gh-cli
      fi
    fi
    command -v jq >/dev/null 2>&1 || as_root dnf install -y jq
  else
    fail "Linux setup supports apt-get and dnf; install Docker Engine, gh, jq, Node 24+, and uv, then re-run"
  fi
  node_usable || install_node_linux
  command -v uv >/dev/null 2>&1 || install_uv
}

start_linux_docker() {
  if command -v systemctl >/dev/null 2>&1; then
    as_root systemctl enable --now docker
  elif command -v service >/dev/null 2>&1; then
    as_root service docker start
  else
    fail "Docker Engine is installed but this host has no systemctl or service command to start it"
  fi
}

# --- 0. config ---------------------------------------------------------------
# The worker reads TRIAGE_REPO and the store from .env. Without one it would
# provision a machine that cannot see the queue it is meant to drain.
if [ ! -f "$ROOT/.env" ]; then
  warn "no .env at the repo root — copy .env.example to .env and fill in"
  warn "TRIAGE_REPO, TRIAGE_STORE_URL and the repository profile first."
  exit 1
fi

# --- 1. system dependencies --------------------------------------------------
# Named, not installed silently: this is the step that touches your machine
# itself.
say "system dependencies"
if [ "$PLATFORM" = macos ]; then
  MISSING=()
  for tool in colima docker gh jq uv; do
    command -v "$tool" >/dev/null 2>&1 || MISSING+=("$tool")
  done
  node_usable || MISSING+=(node)
  if [ ${#MISSING[@]} -gt 0 ]; then
    command -v brew >/dev/null 2>&1 \
      || fail "missing: ${MISSING[*]} — install Homebrew or install them yourself, then re-run"
    echo "missing: ${MISSING[*]}"
    if confirm "install them with Homebrew?"; then
      brew install "${MISSING[@]}"
    else
      fail "skipped — the steps below need them"
    fi
  else
    echo "all present: colima docker gh jq node uv"
  fi
else
  MISSING=()
  for tool in docker gh jq uv; do
    command -v "$tool" >/dev/null 2>&1 || MISSING+=("$tool")
  done
  node_usable || MISSING+=("Node 24+")
  if [ ${#MISSING[@]} -gt 0 ]; then
    echo "missing: ${MISSING[*]}"
    if confirm "install them for Linux?"; then
      install_linux_dependencies
    else
      fail "skipped — install them yourself, then re-run"
    fi
  else
    echo "all present: Docker Engine, gh, jq, node, uv"
  fi
fi

node_usable || fail "Node 24 or newer is required"
for tool in docker gh jq uv; do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool is required"
done

# --- 2. repo dependencies ----------------------------------------------------
say "repo dependencies"
"$ROOT/setup.sh"

# --- 3. Docker runtime -------------------------------------------------------
# Sized for the profile's merge-gate lanes: compile/build phases run in 6g
# containers and a merge preflight can overlap a worker run.
say "Docker runtime"
if docker info >/dev/null 2>&1; then
  echo "daemon already answering"
elif [ "$PLATFORM" = macos ]; then
  echo "starting Colima with 12GB (compile lanes run in 6g containers)"
  colima start --memory 12
else
  echo "starting Docker Engine"
  start_linux_docker
fi

if ! docker info >/dev/null 2>&1; then
  if [ "$PLATFORM" = linux ] && as_root docker info >/dev/null 2>&1; then
    WORKER_USER="${SUDO_USER:-$(id -un)}"
    if [ "$WORKER_USER" != root ]; then
      getent group docker >/dev/null 2>&1 || as_root groupadd docker
      as_root usermod -aG docker "$WORKER_USER"
      if command -v sg >/dev/null 2>&1; then
        printf -v RERUN '%q ' "$ROOT/setup-worker-machine.sh" "${ORIGINAL_ARGS[@]}"
        echo "added $WORKER_USER to the docker group; continuing with that group active"
        exec sg docker -c "$RERUN"
      fi
      fail "added $WORKER_USER to the docker group; sign out and back in, then re-run"
    fi
  fi
  fail "Docker is running but this user cannot reach its daemon"
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
