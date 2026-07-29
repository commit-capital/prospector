#!/usr/bin/env bash
# There is no writable mount: the verdict lives in the exit code the host
# observes, so no file exists for a detached writer to race.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
grep -qE '^\s*-v .*:/work/out' "$HERE/sandbox-run.sh" && {
  echo "FAIL: sandbox-run.sh still mounts /work/out"; exit 1; }
grep -q 'RESULTS_PATH' "$HERE/sandbox-run.sh" && {
  echo "FAIL: sandbox-run.sh still exports RESULTS_PATH"; exit 1; }
# Every -v must be :ro, wherever it appears on the line — including inside
# the patch_mount array assignment.
if grep -oE '\-v "[^"]+"' "$HERE/sandbox-run.sh" | grep -v ':ro"'; then
  echo "FAIL: a non-read-only mount survives"; exit 1
fi
echo "no writable mount ok"

# --- Hardening flags survive -------------------------------------------
# Pins the rest of the launcher's fail-closed guarantees so a later edit
# can't silently drop one: the --internal network (both the create and the
# runtime refusal check), dropped capabilities, no-new-privileges, and the
# resource limits. grep -F (fixed string) needs no regex escaping for
# `{{.Internal}}`.
required=(
  'docker network create --internal'
  '{{.Internal}}'
  '--cap-drop ALL'
  '--security-opt no-new-privileges:true'
  '--pids-limit'
  '--memory'
  '--cpus'
)
for pat in "${required[@]}"; do
  # -e marks $pat as the pattern. Several patterns begin with "--", and
  # grep's own option parser treats a leading "--" argument as a flag.
  grep -qF -e "$pat" "$HERE/sandbox-run.sh" || {
    echo "FAIL: sandbox-run.sh missing hardening flag: $pat"; exit 1; }
done
echo "hardening flags pinned ok"
