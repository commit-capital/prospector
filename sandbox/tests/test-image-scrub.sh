#!/usr/bin/env bash
# An image layer is durable: a secret baked in survives a later layer deleting
# it. Prove the scrub runs BEFORE the build by planting a secret and asserting
# it never appears in the built image's filesystem.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SCRATCH="$HOME/.cache/pr-verify-tests"; mkdir -p "$SCRATCH"
CTX="$(mktemp -d "$SCRATCH/scrub.XXXXXX")"
cleanup() { rm -rf "$CTX"; docker rmi -f pr-verify-base:scrubtest-t0 >/dev/null 2>&1 || true; }
trap cleanup EXIT

mkdir -p "$CTX/src" "$CTX/pnpm-store"
cat > "$CTX/src/package.json" <<'JSON'
{ "name": "fixture", "version": "0.0.0", "private": true, "scripts": { "test": "true" } }
JSON
git -C "$CTX/src" init -q && git -C "$CTX/src" add -A \
  && GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t \
     git -C "$CTX/src" commit -qm base

# Scrub via the driver (the real code path), then build and inspect.
uv run python -c "
from pathlib import Path
from pipeline import verify_driver as vd
src = Path('$CTX/src')
(src / '.npmrc').write_text('//registry.npmjs.org/:_authToken=npm_POISONSECRET')
vd.scrub_checkout(src)
vd.assert_scrubbed(src)
" || { echo "scrub/assert failed"; exit 1; }

docker build -q --network none -t pr-verify-base:scrubtest-t0 --build-arg TIER=0 \
  -f "$HERE/Dockerfile.base" "$CTX" >/dev/null || { echo "build failed"; exit 1; }

if docker run --rm --network none --entrypoint bash pr-verify-base:scrubtest-t0 \
     -lc 'grep -rl "POISONSECRET" /work/src 2>/dev/null | head -1' | grep -q .; then
  echo "SECRET BAKED INTO IMAGE LAYER"; exit 1
fi
echo "image scrub ok"
