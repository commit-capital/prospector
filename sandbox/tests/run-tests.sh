#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

echo "== building pr-verify:local =="
docker build -t pr-verify:local -f "$ROOT/Dockerfile" "$ROOT" || exit 1
# Every test resolves its sandbox image through base-image.sh, which takes
# an explicit BASE_IMAGE first.
export BASE_IMAGE=pr-verify:local

fail=0
for t in test-image-hardening.sh test-isolation.sh test-exit-codes.sh \
         test-no-writable-mount.sh test-launcher-secretless.sh test-image-scrub.sh \
         test-tier1-offline-install.sh test-verify-suite.sh; do
  echo "== $t =="
  if bash "$HERE/$t"; then echo "PASS $t"; else echo "FAIL $t"; fail=1; fi
done
[ "$fail" = 0 ] && echo "ALL PR-VERIFY TESTS PASSED" || echo "SOME TESTS FAILED"
exit $fail
