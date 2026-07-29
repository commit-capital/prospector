#!/usr/bin/env bash
# The core security proof: boot-probe passes on an --internal network and
# FAILS on the default bridge (where the spike showed host services are reachable).
set -euo pipefail
IMG="${PR_VERIFY_IMAGE:-pr-verify:local}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
NET=pr-verify-test-internal

cleanup() { docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker network rm "$NET" >/dev/null 2>&1 || true
docker network create --internal "$NET" >/dev/null

fail=0

echo "--- probe on --internal network: MUST pass (exit 0) ---"
if docker run --rm --network "$NET" \
     --cap-drop ALL --security-opt no-new-privileges:true \
     -v "$HERE/boot-probe.sh:/boot-probe.sh:ro" \
     "$IMG" bash /boot-probe.sh; then
  echo "internal: PASS (isolated, as required)"
else
  echo "internal: FAIL — probe rejected an isolated network"; fail=1
fi

echo "--- probe on default bridge: MUST fail (non-zero) ---"
if docker run --rm \
     -v "$HERE/boot-probe.sh:/boot-probe.sh:ro" \
     "$IMG" bash /boot-probe.sh; then
  echo "bridge: FAIL — probe passed on a NON-isolated network (fail-open!)"; fail=1
else
  echo "bridge: PASS (probe correctly refused a leaky network)"
fi

exit $fail
