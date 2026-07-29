#!/usr/bin/env bash
# With poisoned secrets in the HOST env, the launcher must NOT leak them into
# the container, and must run on an internal (isolated) network.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
# Scratch under $HOME (Colima virtiofs shares only $HOME).
SCRATCH="$HOME/.cache/pr-verify-tests"; mkdir -p "$SCRATCH"
CTX="$(mktemp -d "$SCRATCH/ctx.XXXXXX")"
cleanup() { rm -rf "$CTX"; docker rmi -f pr-verify-base:secretless-t0 >/dev/null 2>&1 || true; }
trap cleanup EXIT

export ANTHROPIC_API_KEY="sk-ant-POISON-should-not-leak"
export GITHUB_TOKEN="ghp_POISON-should-not-leak"
export DEPLOYMENT_PRIVATE_KEY="POISON-PRIVATE-KEY-should-not-leak"

# Build a throwaway base image with the code under test baked in.
mkdir -p "$CTX/src" "$CTX/pnpm-store"
cat > "$CTX/src/package.json" <<'JSON'
{ "name": "envdump", "version": "0.0.0", "private": true, "scripts": { "test": "true" } }
JSON
git -C "$CTX/src" init -q && git -C "$CTX/src" add -A \
  && GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t \
     git -C "$CTX/src" commit -qm base
docker build -q --network none -t pr-verify-base:secretless-t0 --build-arg TIER=0 \
  -f "$HERE/Dockerfile.base" "$CTX" >/dev/null || { echo "build failed"; exit 1; }

# PR_VERIFY_DEBUG=1 dumps the container env to stdout, which the host captures.
DUMP="$(PR_VERIFY_DEBUG=1 bash "$HERE/sandbox-run.sh" \
  --image pr-verify-base:secretless-t0 --phase red --tier 0 \
  --test-cmd 'true' --base-sha b --head-sha h 2>&1 || true)"
echo "--- container env ---"; echo "$DUMP"
# The container env must contain PHASE=red for this invocation. Requiring that
# line proves a container actually booted and its env reached the host, so an
# empty or docker-error DUMP cannot pass this check by having nothing to find.
echo "$DUMP" | grep -q '^PHASE=red$' || { echo "no container env dump produced"; exit 1; }
if echo "$DUMP" | grep -qi 'POISON'; then echo "SECRET LEAKED INTO CONTAINER"; exit 1; fi
echo "secretless ok"
