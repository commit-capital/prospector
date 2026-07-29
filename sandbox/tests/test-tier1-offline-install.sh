#!/usr/bin/env bash
# Tier 1 bakes the pinned checkout's dependencies into the base image:
# prefetch_store fills a pnpm store from inside the sandbox image, and the image
# build installs from it under --network none. This builds a real Tier 1 image
# from a fixture that depends on `ms`, then runs a phase against it.
#
# One passing phase carries three proofs at once:
#   - corepack resolves the fixture's pnpm@9.15.4 pin from the image's build-time
#     cache, with no egress (`pnpm -s test` runs at all),
#   - `pnpm install --offline --frozen-lockfile` resolves every package from the
#     host-prefetched store (`ms` is present),
#   - pnpm's hardlinks into node_modules/.pnpm outlive `rm -rf /work/pnpm-store`
#     in the same layer (`ms` is still readable after the store is gone).
#
# The Tier 0 control at the end is what keeps that from passing vacuously.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
FIXTURE="$(cd "$(dirname "$0")/fixtures/tier1" && pwd)"
SCRATCH="$HOME/.cache/pr-verify-tests"; mkdir -p "$SCRATCH"
CTX="$(mktemp -d "$SCRATCH/tier1.XXXXXX")"
cleanup() {
  rm -rf "$CTX"
  docker rmi -f pr-verify-base:tier1-t1 pr-verify-base:tier1-t0 >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "$CTX/src"
cp "$FIXTURE/package.json" "$FIXTURE/pnpm-lock.yaml" "$FIXTURE/test.js" "$CTX/src/"

# Prefetch through the driver's own function, the way prepare-base does it, so a
# change there that breaks the offline install fails here.
(cd "$ROOT" && uv run python -c "
from pathlib import Path
from pipeline import verify_driver as vd
vd.prefetch_store(Path('$CTX/src'), Path('$CTX/pnpm-store'))
") || { echo "prefetch_store failed"; exit 1; }

# The store is the input the whole tier depends on; an empty one would make the
# install below a no-op worth no assertion.
[ -n "$(ls -A "$CTX/pnpm-store" 2>/dev/null)" ] || {
  echo "prefetch_store produced an empty store"; exit 1; }
# prefetch_store removes the node_modules `pnpm fetch` leaves behind. Baked into
# the image it makes the in-image install exit 0 without installing.
[ ! -e "$CTX/src/node_modules" ] || {
  echo "prefetch_store left a host-built node_modules in src"; exit 1; }

fail=0
check() { # check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "  ok $1 -> $3"; else echo "  FAIL $1: want $2 got $3"; fail=1; fi
}
run() { # run <image> <tier>
  bash "$HERE/sandbox-run.sh" --image "$1" --phase red \
    --test-cmd 'pnpm -s test' --base-sha b --head-sha h --tier "$2" >/dev/null 2>&1
  echo $?
}

docker build -q --network none -t pr-verify-base:tier1-t1 \
  --build-arg TIER=1 -f "$HERE/Dockerfile.base" "$CTX" >/dev/null || {
    echo "tier 1 base image build failed"; exit 1; }

check "tier 1: phase imports its dependency offline" 0 "$(run pr-verify-base:tier1-t1 1)"

# The store is removed in the same layer that installs from it. Its directory
# entry must be gone from the image the phases actually run.
if docker run --rm --network none --entrypoint bash pr-verify-base:tier1-t1 \
     -lc 'test -e /work/pnpm-store' >/dev/null 2>&1; then
  echo "  FAIL /work/pnpm-store survives into the image"; fail=1
else
  echo "  ok /work/pnpm-store is gone from the image"
fi

# Tier 0 skips the install, so the same fixture cannot import `ms` and the phase
# is a red. Without this, the Tier 1 assertion above would also pass for a `ms`
# that arrived some way other than the offline install.
docker build -q --network none -t pr-verify-base:tier1-t0 \
  --build-arg TIER=0 -f "$HERE/Dockerfile.base" "$CTX" >/dev/null || {
    echo "tier 0 control image build failed"; exit 1; }

check "tier 0 control: same fixture cannot import it" 20 "$(run pr-verify-base:tier1-t0 0)"

[ "$fail" = 0 ] && echo "tier 1 offline install ok"
exit $fail
