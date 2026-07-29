#!/usr/bin/env bash
# Asserts the pr-verify image has build tooling but NO agent CLIs / gh.
set -euo pipefail
IMG="${PR_VERIFY_IMAGE:-pr-verify:local}"

fail=0
have()    { docker run --rm "$IMG" sh -c "command -v $1 >/dev/null 2>&1"; }
absent()  { ! have "$1"; }

check() { printf '%-32s' "$1"; if eval "$2"; then echo ok; else echo FAIL; fail=1; fi; }

check "node present"        'have node'
check "pnpm present"        'have pnpm'
check "git present"         'have git'
check "jq present"          'have jq'
check "claude ABSENT"       'absent claude'
check "codex ABSENT"        'absent codex'
check "gh ABSENT"           'absent gh'
check "runs as non-root"    '[ "$(docker run --rm "$IMG" id -u)" != "0" ]'

exit $fail
