#!/usr/bin/env bash
# Sourced by every test that runs or builds on the sandbox image. Sets
# BASE_IMAGE to the image the tests use, resolved the way the driver does: an
# explicit BASE_IMAGE in the environment, else the tag the active profile keys
# (verify_driver.sandbox_image, read from the checkout's .env through uv), else
# pr-verify:local. Exits when the chosen image is not on the daemon, so a test
# never waits on BuildKit pulling a tag that exists nowhere.
_tests_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_repo_root="$(cd "$_tests_dir/../.." && pwd)"

if [ -z "${BASE_IMAGE:-}" ]; then
  BASE_IMAGE="$(cd "$_repo_root" && uv run python -c \
    'from pipeline import verify_driver; print(verify_driver.sandbox_image())' 2>/dev/null)" \
    || BASE_IMAGE=""
fi
: "${BASE_IMAGE:=pr-verify:local}"
export BASE_IMAGE

docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || {
  echo "sandbox image '$BASE_IMAGE' is not on the Docker daemon." >&2
  echo "Build it (docker build -t '$BASE_IMAGE' -f sandbox/Dockerfile sandbox)," \
       "or set BASE_IMAGE to an image that exists." >&2
  exit 1
}
echo "sandbox image: $BASE_IMAGE"
