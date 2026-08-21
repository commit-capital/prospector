#!/usr/bin/env bash
# Prospector — dependency setup. Syncs the repo-root uv environment (pinned
# to Python 3.14.6 via .python-version + uv.lock) and installs frontend deps.
# Idempotent: safe to run standalone, from `prospector serve --dev`, or as
# Conductor's setup script (.conductor/settings.toml). Run from anywhere — paths are
# resolved to the repo root.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
FRONTEND="$ROOT/prospector_app/frontend"

# Put a Node >=24 on PATH and pick a pnpm >=11 (the frontend's pinned toolchain).
source "$ROOT/frontend-toolchain.sh"

# Require exactly the pinned interpreter — no "newest available" fallback —
# and fetch it through uv when it isn't present yet.
PINNED="$(cat "$ROOT/.python-version")"
if ! uv python find "$PINNED" >/dev/null 2>&1; then
  echo "→ python   installing $PINNED via uv"
  uv python install "$PINNED"
fi

# One locked environment for all Python (pipeline + backend), built from uv.lock.
( cd "$ROOT" && uv sync )

# `--frozen-lockfile`: a strict install from pnpm-lock.yaml that never rewrites
# it and fails if package.json and the lockfile have drifted, so every fresh
# worktree starts from the exact committed dependency tree with no spurious
# lockfile diff.
#
# Gate on node_modules/.modules.yaml, the marker pnpm writes only after an
# install finishes — not on node_modules/ existing. An interrupted install
# leaves a partial node_modules/ (packages linked, .bin/ symlinks missing), and
# a bare directory check would treat that as done and skip the repair, so
# `prospector serve --dev` keeps failing without self-healing. The marker
# is absent until the install completes, so a partial tree re-triggers the install.
if [ ! -e "$FRONTEND/node_modules/.modules.yaml" ]; then
  ( cd "$FRONTEND" && "${PNPM[@]}" install --frozen-lockfile )
fi

# One root .env per checkout: operator vars plus this worktree's dev ports.
# claim_dev_ports (sourced below) gives each checkout a port pair no sibling
# and not the primary holds, so concurrent app instances don't collide.
ENV_FILE="$ROOT/.env"
touch "$ENV_FILE"
primary="$(git -C "$ROOT" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p' | head -1)"

# Seed operator config into a fresh worktree's .env. .env is gitignored, so a new
# worktree starts without it; the operator vars (TRIAGE_REPO, TRIAGE_STORE_URL, …)
# live only in the primary checkout's .env. Copy every non-port line from there
# when this .env lacks the one required var, so the backend boots. The port block
# below stays per-worktree.
if ! grep -q '^TRIAGE_REPO=' "$ENV_FILE"; then
  src="$primary/.env"
  if [ -n "$primary" ] && [ "$src" != "$ENV_FILE" ] && [ -f "$src" ]; then
    grep -q '^TRIAGE_REPO=' "$src" && {
      grep -v -e '^API_PORT=' -e '^VITE_PORT=' -e '^# Per-worktree dev ports' "$src" \
        | cat - "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
      echo "→ config   seeded operator vars from $src"
    }
  fi
fi

# The repository policy profile is static, local, and gitignored. Conductor
# copies it via .worktreeinclude; this setup path also serves workspace hosts
# that start from tracked files and then run the repository setup script.
PROFILE_FILE="$ROOT/profile.json"
primary_profile="$primary/profile.json"
if [ -n "$primary" ] && [ "$primary" != "$ROOT" ] \
    && [ ! -e "$PROFILE_FILE" ] && [ -f "$primary_profile" ]; then
  cp -p "$primary_profile" "$PROFILE_FILE"
  echo "→ config   seeded repository profile from $primary_profile"
fi

source "$ROOT/dev-ports.sh"
claim_dev_ports "$ROOT" "$ENV_FILE"
write_launch_config "$ROOT"
