#!/usr/bin/env bash
# Prospector — take this machine back out of the work queues.
#
# The reverse of setup-worker-machine.sh, in tiers. With no option it turns
# every worker lane switch off, so the next app start runs no worker threads.
# --artifacts removes this machine's sandbox artifacts: every base image, the
# hardened sandbox image, the scratch clone, and its base pin in the store.
# --vm stops and deletes the Colima VM on macOS; Linux runs Docker Engine
# directly and has no Docker VM here. --packages uninstalls the platform's
# Docker runtime (never gh, jq, or node — other things on the machine use them).
#
# Runs in YOUR terminal, not from the app: the platform package manager may
# need a TTY, and it should run as you. The app's Setup tab stops the work on a
# running app (its worker threads); the switches written here are what the next
# start reads.
#
# Usage: ./teardown-worker-machine.sh [--artifacts] [--vm] [--packages] [--yes]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

case "$(uname -s)" in
  Darwin) PLATFORM=macos ;;
  Linux) PLATFORM=linux ;;
  *) echo "unsupported platform: $(uname -s) (Prospector workers need macOS or Linux)" >&2; exit 1 ;;
esac

ARTIFACTS=0
VM=0
PACKAGES=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --artifacts) ARTIFACTS=1 ;;
    --vm) VM=1 ;;
    --packages) PACKAGES=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m→ %s\033[0m\n' "$1"; }
warn() { printf '\033[33m! %s\033[0m\n' "$1"; }

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    warn "this step needs root; install sudo or run the command as root"
    return 1
  fi
}

confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  read -r -p "$1 [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

# --- 1. lane switches --------------------------------------------------------
# Always. Written through the same allowlist the app's Setup view writes.
say "worker lane switches"
if [ -f "$ROOT/.env" ]; then
  uv run python -c "
from prospector_app.backend import worker_control
worker_control.set_flags({k: '' for k in worker_control.WRITABLE})
print('off: ' + ', '.join(worker_control.WRITABLE))
"
else
  warn "no .env at the repo root — nothing to switch off."
fi

# --- 2. sandbox artifacts ----------------------------------------------------
# Images, scratch clone, and this machine's base pin. Needs the Docker daemon
# to remove the images; without it the clone and pin still go.
if [ "$ARTIFACTS" = 1 ]; then
  say "sandbox artifacts"
  if ! docker info >/dev/null 2>&1; then
    warn "the Docker daemon is not answering — the images stay until it is; the clone and pin go now."
  fi
  if confirm "remove this machine's base images, sandbox image, scratch clone, and base pin?"; then
    uv run python pipeline/verify_driver.py teardown || warn "teardown did not complete — see above"
  else
    echo "skipped"
  fi
fi

# --- 3. the Docker VM --------------------------------------------------------
if [ "$VM" = 1 ]; then
  if [ "$PLATFORM" = linux ]; then
    say "Docker runtime"
    echo "Linux uses Docker Engine directly; there is no Colima VM to delete."
  else
    say "Docker VM"
    if command -v colima >/dev/null 2>&1; then
      if confirm "stop and delete the Colima VM (every container and image in it goes)?"; then
        colima stop || true
        colima delete --force
      else
        echo "skipped"
      fi
    else
      echo "colima is not installed — nothing to delete"
    fi
  fi
fi

# --- 4. runtime packages -----------------------------------------------------
# Only the runtime packages that exist for the sandbox. gh, jq, and node stay.
if [ "$PACKAGES" = 1 ]; then
  say "runtime packages"
  if [ "$PLATFORM" = macos ] && command -v brew >/dev/null 2>&1; then
    PRESENT=()
    for pkg in colima docker; do
      brew list --formula "$pkg" >/dev/null 2>&1 && PRESENT+=("$pkg")
    done
    if [ ${#PRESENT[@]} -gt 0 ]; then
      if confirm "brew uninstall ${PRESENT[*]}?"; then
        brew uninstall "${PRESENT[@]}"
      else
        echo "skipped"
      fi
    else
      echo "neither colima nor docker is installed through Homebrew — nothing to uninstall"
    fi
  elif [ "$PLATFORM" = linux ] && command -v apt-get >/dev/null 2>&1; then
    PRESENT=()
    for pkg in docker.io docker-ce docker-ce-cli containerd.io; do
      dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'ok installed' \
        && PRESENT+=("$pkg")
    done
    if [ ${#PRESENT[@]} -gt 0 ]; then
      if confirm "stop Docker Engine and uninstall ${PRESENT[*]}?"; then
        as_root systemctl disable --now docker 2>/dev/null || true
        as_root apt-get remove -y "${PRESENT[@]}"
      else
        echo "skipped"
      fi
    else
      echo "Docker Engine is not installed through apt — nothing to uninstall"
    fi
  elif [ "$PLATFORM" = linux ] && command -v dnf >/dev/null 2>&1; then
    PRESENT=()
    for pkg in docker docker-ce docker-ce-cli containerd.io moby-engine; do
      rpm -q "$pkg" >/dev/null 2>&1 && PRESENT+=("$pkg")
    done
    if [ ${#PRESENT[@]} -gt 0 ]; then
      if confirm "stop Docker Engine and uninstall ${PRESENT[*]}?"; then
        as_root systemctl disable --now docker 2>/dev/null || true
        as_root dnf remove -y "${PRESENT[@]}"
      else
        echo "skipped"
      fi
    else
      echo "Docker Engine is not installed through dnf — nothing to uninstall"
    fi
  else
    warn "no supported package manager found — uninstall the Docker runtime yourself."
  fi
fi

# --- 5. where this machine stands now ----------------------------------------
say "readiness"
uv run python -c "
from prospector_app.backend import worker_readiness as wr
r = wr.report()
for c in r['checks']:
    mark = 'ok  ' if c['ok'] else 'off '
    print(f\"  [{mark}] {c['label']}: {c['detail']}\")
print()
print('this machine processes no work' if not r['ready'] else 'this machine is still ready to verify — its lanes are off')
"

say "done"
