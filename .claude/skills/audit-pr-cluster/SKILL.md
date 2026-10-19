---
name: audit-pr-cluster
description: Use when given a PR cluster ID and asked to check whether the pipeline's analysis/disposition is right and to draft the close-out comment(s). Independently verifies every load-bearing claim against the configured upstream repository, then writes one comment (or a few, if members diverge). Read-only — the human posts via the app UI.
---

# audit-pr-cluster

Given a cluster ID (`/audit-pr-cluster 117`), re-verify every load-bearing claim
in ANALYZE's rationale against the current `TRIAGE_REPO`, say whether you agree
with the disposition, and draft the close-out comment(s). The rationale is a
claim, not a fact: it can be stale (code moved since), or overstated (upstream
shipped a narrower policy than the PRs wanted, or "all unmergeable" when one is
mergeable).

This skill is read-only upstream: it runs no `gh` write verb. The operator posts
through the app as `TRIAGE_BOT_LOGIN`.

## Setup

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
env_value() { sed -n "s/^$1=//p" "$REPO_ROOT/.env" 2>/dev/null | tail -1; }
TRIAGE_REPO="${TRIAGE_REPO:-$(env_value TRIAGE_REPO)}"
API_PORT="${API_PORT:-$(env_value API_PORT)}"; API_PORT="${API_PORT:-8787}"
case "$TRIAGE_REPO" in */*) ;; *) echo "TRIAGE_REPO must be owner/name" >&2; exit 1;; esac
curl -s "http://localhost:$API_PORT/api/clusters/<cid>"
```

The response (`service.cluster_detail`) carries `root_problem`, `outcome`,
`rationale`, `analyzed_at`, and `prs[]` with each member's `number`,
`github_state`, `head_sha`, `disposition` and `proposed_action` (`upstream_pr`,
`canonical`, per-PR `rationale`).

## Steps

1. **List the claims** the disposition rests on: the canonical upstream PR(s)
   (a cluster may cite different ones per member), the mechanism said to have
   landed, and each member's claim (superseded, conflicting, bundles unrelated
   changes, weaker variant).
2. **Stop early if nothing is open.** Check each member live
   (`gh pr view <n> --repo "$TRIAGE_REPO" --json state`). If all are closed or
   merged, report that and whether it matches `outcome`, and stop. Otherwise
   audit the open members only.
3. **Verify each citation**: `gh pr view <upstream_pr> --repo "$TRIAGE_REPO"
   --json state,mergedAt,mergeCommit,files`. A wrong or unmerged citation
   voids the disposition.
4. **Confirm the mechanism is still on the default branch.** Search a
   SHA-pinned snapshot, never the code-search API (its index lags):

   ```bash
   BRANCH="$(gh repo view "$TRIAGE_REPO" --json defaultBranchRef -q .defaultBranchRef.name)"
   SHA="$(gh api "repos/$TRIAGE_REPO/commits/$BRANCH" --jq .sha)"
   UP="$(mktemp -d)"; trap 'rm -rf "$UP"' EXIT
   gh api "repos/$TRIAGE_REPO/tarball/$SHA" | tar -xz -C "$UP" --strip-components=1
   rg -n '<symbol>' "$UP"
   ```

   Read every file or function the rationale names, and note where it now
   lives if it moved.
5. **Check policy equivalence.** Does upstream do the same thing the PRs
   wanted, or something narrower? Upstream tests encode the chosen policy; a
   member that flips an assertion upstream keeps is contradicted, not
   superseded.
6. **Check each member live**: `gh pr view <n> --repo "$TRIAGE_REPO" --json
   state,mergeable,headRefOid,files`. A head that differs from `head_sha` means
   the analysis may be stale. The file list settles "bundles unrelated" and
   "weaker variant".
7. **Note secondary signals**: an unshipped feature bundled into a superseded
   PR (flag it as needing its own focused PR), and contributor ticket keys
   (`ABC-123`) in added source or tests, not just the title.
8. **State the verdict**: agree or not with `outcome` and each disposition, and
   name any part of the rationale that is wrong even where the conclusion holds.

## Drafting the comment

Use one comment for all members unless their close reasons genuinely differ.

- Open with thanks and a brief apology for the delay.
- Cite the upstream PR: number, what it did, merge date.
- Say "resolved upstream" only when equivalent; otherwise "addressed upstream
  with a narrower/different approach". Never claim "your fix already landed"
  when it didn't.
- Omit the merge-conflict line when closing as already fixed.
- Don't echo contributor ticket IDs.
- For a dropped unshipped extra, say a focused PR "would be much easier to
  evaluate"; never promise acceptance.
- Claim only what is true at post time; never state a companion action as done.
- Plain paragraphs, no `>` blockquotes: the operator pastes it into the app.

## Pre-fill link

Hand the operator one link that seeds the cluster page's comment boxes. The
page's `?drafts=` param is base64url of gzipped JSON `{"<pr>": "<text>"}`, open
members only. Copy it to the clipboard rather than printing it:

```bash
OUT="$SCRATCHPAD/cluster-<cid>-drafts-url.txt"
PORT="${VITE_PORT:-$(env_value VITE_PORT)}"; PORT="${PORT:-5173}"
python3 - "$OUT" "$PORT" <<'PY'
import base64, gzip, json, sys
cid = 23
drafts = {"383": "Thanks for this, …", "477": "Thanks for this, …"}
blob = base64.urlsafe_b64encode(gzip.compress(json.dumps(drafts).encode(), mtime=0)).decode()
open(sys.argv[1], "w").write(f"http://localhost:{sys.argv[2]}/clusters/{cid}?drafts={blob}")
PY
pbcopy < "$OUT"
```

Tell the operator it is on the clipboard (or `open "$(cat "$OUT")"`), that it
needs the dev app running, and that it never overwrites their edits.
