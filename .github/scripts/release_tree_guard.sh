#!/usr/bin/env bash
# Release-tree guard: deployment identifiers, operating data, and
# developer-machine paths stay out of the generic release tree. Git-history
# cleanup is a separate maintainer decision.
set -euo pipefail
fail=0

identifier_allowlist=".github/release-identifier-allowlist.txt"
# Split known deployment names so this scanner does not match its own source.
# The project_ref pattern catches any concrete Supabase MCP project binding while
# allowing documentation placeholders such as project_ref=<id>.
identifier_re='paper''clip|commitper''clip|brandon''burr|aayme''loglu|project_ref=[[:alnum:]]{20}'
identifier_matches="$(mktemp)"
identifier_allowed="$(mktemp)"
trap 'rm -f "$identifier_matches" "$identifier_allowed"' EXIT

git grep -IilE "$identifier_re" -- . \
  ':!.github/release-identifier-allowlist.txt' \
  ':!.github/scripts/release_tree_guard.sh' >"$identifier_matches" || true
LC_ALL=C sort -u -o "$identifier_matches" "$identifier_matches"
sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' "$identifier_allowlist" \
  | LC_ALL=C sort -u >"$identifier_allowed"

unexpected_identifiers="$(comm -23 "$identifier_matches" "$identifier_allowed")"
if [[ -n "$unexpected_identifiers" ]]; then
  echo "FAIL: deployment identifiers outside the release allowlist:" >&2
  while IFS= read -r f; do
    git grep -niE "$identifier_re" -- "$f" >&2
  done <<<"$unexpected_identifiers"
  fail=1
fi

stale_identifier_allowlist="$(comm -13 "$identifier_matches" "$identifier_allowed")"
if [[ -n "$stale_identifier_allowlist" ]]; then
  echo "FAIL: stale deployment-identifier allowlist entries:" >&2
  echo "$stale_identifier_allowlist" >&2
  fail=1
fi

if git ls-files -- 'pr-training-data/*' 'docs/issue-briefings/*' 'patches/*' 'issue_triage/verdicts/*.json' | grep -q .; then
  echo "FAIL: tracked operating-data files:" >&2
  git ls-files -- 'pr-training-data/*' 'docs/issue-briefings/*' 'patches/*' 'issue_triage/verdicts/*.json' >&2
  fail=1
fi

store_globs=(
  '*.db' '*.db-wal' '*.db-shm'
)
if git ls-files -- "${store_globs[@]}" | grep -q .; then
  echo "FAIL: tracked store database files:" >&2
  git ls-files -- "${store_globs[@]}" >&2
  fail=1
fi

for f in STATUS.md pipeline/STATUS.md ISSUE-STATUS.md issue_triage/SUMMARY.md; do
  if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    echo "FAIL: generated projection is tracked: $f" >&2
    fail=1
  fi
done

if git grep -nE '/Users/[A-Za-z]+' -- '*.md' >&2; then
  echo "FAIL: developer absolute paths in tracked markdown (above)" >&2
  fail=1
fi

exit "$fail"
