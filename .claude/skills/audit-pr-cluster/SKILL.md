---
name: audit-pr-cluster
description: Use when given a PR cluster ID and asked to check whether the pipeline's analysis/disposition is right and to draft the close-out comment(s). Independently verifies every load-bearing claim against the configured upstream repository, then writes one comment (or a few, if members diverge). Read-only — the human posts via the cockpit UI.
---

# audit-pr-cluster

The pipeline's ANALYZE phase already wrote a disposition and rationale for each
cluster. This skill is the **human-in-the-loop audit** of that output: given a
cluster ID, independently re-verify every load-bearing claim against the *current*
state of `TRIAGE_REPO`, decide whether you agree with the disposition,
and draft the close-out comment(s) the operator will post.

It sits between the pipeline's CLUSTER + ANALYZE phases (produce the analysis)
and the cockpit executor (posts the action). It is **read-only against
`TRIAGE_REPO`** — it never runs `gh pr close/comment/merge/review`. The operator
posts the drafted comment through the cockpit UI (gated, logged, as
`TRIAGE_BOT_LOGIN`); see the trust model in `CLAUDE.md`.

## Why this exists: the rationale is a claim, not a fact

The ANALYZE rationale is generated text written when the pipeline last ran. It can
be **stale** (master moved since) or **overstated** (the prose claims more than the
diff delivers). The whole job of this skill is to *not trust the prose* — confirm
each claim against the live repo before agreeing. Real failures caught by doing
this on past clusters:

- A "fix landed in Layout.tsx" rationale that was true at merge but where master
  had since **relocated** the code to a different file. Conclusion held; the
  description was stale.
- A "the contributed PRs implement exactly this" rationale where upstream actually
  shipped a **narrower policy** (mention-scoped grant) than the PRs wanted (blanket
  same-company). One member was a test-only PR whose assertions were **contradicted**
  by master, not "already reflected" as the rationale claimed.
- Headline summaries saying "all members unmergeable" when one was `MERGEABLE`
  (closeable on its merits, not on conflict).

## Input

A cluster ID as a number. Example: `/audit-pr-cluster 117`.

## Deployment configuration

Resolve the repository and bot identity from the process environment first and
the gitignored root `.env` second:

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
env_value() { sed -n "s/^$1=//p" "$REPO_ROOT/.env" 2>/dev/null | tail -1; }
TRIAGE_REPO="${TRIAGE_REPO:-$(env_value TRIAGE_REPO)}"
TRIAGE_BOT_LOGIN="${TRIAGE_BOT_LOGIN:-$(env_value TRIAGE_BOT_LOGIN)}"
case "$TRIAGE_REPO" in */*) ;; *) echo "TRIAGE_REPO must be owner/name" >&2; exit 1;; esac
test -n "$TRIAGE_BOT_LOGIN" || { echo "TRIAGE_BOT_LOGIN is required" >&2; exit 1; }
```

Do not infer either value from the current checkout name or a human's GitHub
login.

## Source of the analysis

Read the cluster detail from the cockpit (same data the `/clusters/<cid>` page
shows — the store served through `service.cluster_detail`):

```bash
curl -s http://localhost:5174/api/clusters/<cid>   # dev cockpit; adjust port if needed
```

Key fields:
- `root_problem`, `outcome` (cluster state, e.g. `close-out`), `rationale`,
  `rationale_summary`, `analyzed_at`.
- `prs[]` — each with `number`, `title`, `author`, `github_state`, `head_sha`,
  `disposition`, and `proposed_action` (`action`, `upstream_pr`, `canonical`,
  `upstream_date`, per-PR `rationale`).

The store is the source of truth; never hand-parse generated markdown.

## Steps

### 1. Pull the analysis and list the claims

Fetch the cluster. Enumerate the **load-bearing claims** the disposition rests on.
For a typical `close-out`/`close-fixed` cluster these are:
- which upstream PR(s) the rationale credits as the canonical fix (`upstream_pr`,
  merge commit, date);
- *what mechanism* it claims landed (a function, field, action, route guard, SQL);
- the per-member claim (superseded / conflicting / bundles-unrelated / weaker variant).

A cluster may cite **more than one** canonical PR for different members — verify each
against the member it's attached to.

### 1.5. Short-circuit: abort early if every member is already closed

The audit exists to vet an action *before* the operator posts it. If every member PR
is already `CLOSED`/`MERGED` upstream, there is nothing left to post — the resolve
step is a no-op and the full verification below is wasted work.

The store's `github_state` can lag, so confirm live in one cheap call before bailing:

```bash
for n in <member numbers>; do
  gh pr view "$n" --repo "$TRIAGE_REPO" --json number,state -q '.number, .state'
done
```

If **all** members come back `CLOSED`/`MERGED`, **stop here**. Report that the cluster
is already resolved (with each member's state and, if available, `closedAt`), note
whether the disposition it was closed under matches the cluster's `outcome`, and skip
steps 2–8 — no claim-by-claim verification, no drafted comment. Only continue the full
audit when **at least one** member is still `OPEN` (an action is still pending). Mixed
clusters proceed on the open members alone.

### 2. Verify the canonical upstream citations

```bash
gh pr view <upstream_pr> --repo "$TRIAGE_REPO" \
  --json title,state,mergedAt,mergeCommit,author,files
```

Confirm `state == MERGED`, and that the merge commit / date match the rationale.
A wrong or unmerged citation invalidates the whole disposition.

### 3. Verify the mechanism is *still present on master* — read the code

This is the step that catches staleness. Do **not** stop at "the PR merged" — merged
code can be reverted, rewritten, or relocated. Confirm the claimed fix exists in the
**current** tree.

Fetch a SHA-pinned snapshot of the configured repository's current default
branch through read-only `gh` calls. This gives the audit a complete local tree
for accurate search without relying on a separately managed checkout:

```bash
DEFAULT_BRANCH="$(gh repo view "$TRIAGE_REPO" --json defaultBranchRef -q '.defaultBranchRef.name')"
DEFAULT_SHA="$(gh api "repos/$TRIAGE_REPO/commits/$DEFAULT_BRANCH" --jq '.sha')"
SNAPSHOT="$(mktemp -d)"
trap 'rm -rf "$SNAPSHOT"' EXIT
mkdir "$SNAPSHOT/tree"
gh api "repos/$TRIAGE_REPO/tarball/$DEFAULT_SHA" > "$SNAPSHOT/upstream.tgz"
tar -xzf "$SNAPSHOT/upstream.tgz" -C "$SNAPSHOT/tree" --strip-components=1
UP="$SNAPSHOT/tree"
gh api "repos/$TRIAGE_REPO/commits/$DEFAULT_SHA" \
  --jq '.sha[0:12] + " " + .commit.committer.date + " " + .commit.message'
```

Then search and read that pinned tree:

```bash
rg -n '<symbol>' "$UP/<path-or-directory>"
sed -n '<start>,<end>p' "$UP/<path>"
```

Never use the code-search API (`gh api search/code`) to locate symbols — its index
lags the default branch and has pointed audits at symbols already deleted from
the tree. A per-file read remains useful when the path is already known:

```bash
gh api "repos/$TRIAGE_REPO/contents/<path>" --jq '.content' | base64 -d | grep -nE '<symbol>'
```

PR metadata stays on `gh` either way — the clone carries no PR state (states,
mergeability, merge commits, diffs):

```bash
gh pr diff <upstream_pr> --repo "$TRIAGE_REPO" | grep -E '^[+-].*<symbol>'
```

If the rationale names a file/function/field, **read it**. Note when the live
location differs from the rationale's description (it may have moved) — fix the
description in your write-up even if the conclusion stands.

### 4. Check policy equivalence, not just mechanism presence

The dangerous case: the *mechanism* shipped but the *behavior* differs from what the
contributed PRs intended. Ask: does master's version do the **same thing** the PR
wanted, or a **narrower/different** thing?
- Read the upstream tests (they encode the chosen policy) — a member PR that flips an
  assertion master deliberately keeps is **contradicted**, not superseded.
- If upstream chose a narrower model, the honest close reason is "addressed upstream
  with a different/narrower approach," **not** "your fix already landed."

### 5. Verify member freshness and live state

For every member PR:

```bash
gh pr view <n> --repo "$TRIAGE_REPO" \
  --json number,state,mergeable,headRefOid,files
```

- **Staleness:** `headRefOid` should match the cluster's `head_sha`. If the head
  moved since `analyzed_at`, the analysis may be stale — say so.
- **State:** an already-`CLOSED`/`MERGED` member is a **no-op** for the resolve step
  (don't draft a close for it). Note it.
- **Mergeable:** spot-check headline claims like "all conflict" / "all unmergeable."
- **Files:** confirm "bundles unrelated changes" / "weaker variant" / which surface —
  the file list usually settles it (e.g. a 16-file "grab-bag" vs a 2-file focused fix).

### 6. Note the secondary signals worth surfacing

These don't change the disposition but belong in the write-up (and sometimes in the
comment):
- **Unbuilt extras being dropped:** a member may carry a genuinely-unshipped feature
  (dependency viz, a CEO-override path) bundled into an otherwise-superseded PR. Flag
  it as "not covered upstream; would need its own focused PR," so it isn't silently lost.
- **Leaked contributor identifiers:** contributor ticket keys (e.g. `AIM-`, `LAS-`,
  `PAX-`) embedded in **source/test** (not just the PR title/branch) would inject a
  foreign namespace into the codebase — an independent reason not to merge. Check the
  diff (`gh pr diff <n> | grep -E '^\+' | grep -E '<PREFIX>-[0-9]+'`), not just the title.

### 7. State the verdict

Say plainly whether you **agree** with the cluster `outcome` and each member's
`disposition`. If you agree, say so and summarize the corroboration. If the
conclusion holds but the rationale is inaccurate in a specific way (stale location,
overstated equivalence, "all unmergeable" when one is mergeable), call that out — the
disposition can be right for a slightly different reason.

### 8. Draft the close-out comment(s)

Default to **one** comment reused across all members when they share a single close
reason. Split into a few **only** when members genuinely diverge — e.g. a test-only
PR that's *contradicted* vs implementation PRs that are *superseded*, or a clean PR
vs one that needs a different note.

Comment style (converged conventions):
- Open with thanks + a brief apology for the delay.
- Cite the upstream PR (number + one-line of what it did + merge date) as the reason.
- Be **honest about why**: "resolved upstream" when truly equivalent; "addressed
  upstream with a narrower/different model" when policy diverged. Never claim "your
  fix already landed" if it didn't.
- **Omit the merge-conflict line** when closing as already-fixed — it's redundant and
  invites a pointless "should I rebase?" reply.
- **Do not echo contributor ticket IDs** back at the author — meaningless noise.
- If desirable unbuilt extras are being dropped, point at a separate focused PR as
  the path — but **never promise acceptance** ("would be very welcome"). We triage;
  the maintainers decide what they want. Phrase it as process, not endorsement: a
  focused PR "would be much easier to evaluate," "if you choose to pursue it."
- **Only claim what is true at post time.** Never state a companion action as done
  ("we've opened a tracking issue", "we've notified X") when it hasn't happened —
  a draft that depends on a to-do being executed first will eventually be posted
  without it. Leave the claim out of the draft; the operator can append "tracked
  in #NNNN" once the referenced thing actually exists.
- Keep it concise.
- **Output the drafted comment as plain text** — plain paragraphs, never markdown
  blockquotes (`>` prefix) or other line-prefixed formatting. The operator copies it
  straight into the cockpit UI, and a `>` on every line gets pasted literally.
  Inline `code` backticks are fine.

Output the verdict and the comment(s) inline for the operator to post. Optionally
save a briefing to `docs/pr-cluster-audits/cluster-<cid>.md` if asked.

### 9. Emit the pre-fill link

End with a single link that drops every drafted comment straight into the cluster
page's edit boxes, so the operator doesn't copy-paste each one. The cockpit reads a
`?drafts=` param — a base64url-encoded **gzipped** JSON map of `{ "<pr>": "<text>" }`
— seeds the boxes on load, then strips the param. Reuse the *same* text for members
that share a comment (gzip collapses the duplication and keeps the link short).

Don't print the raw URL — a ~1.5 KB base64 blob is finicky to select in a terminal.
Instead **write it to a scratchpad file and copy it to the clipboard** (`pbcopy`), so
the operator just pastes into the address bar (Cmd+L, Cmd+V), and offer the one-liner
that opens it directly. Build it deterministically (matches the cockpit's decoder —
gzip via the browser's `DecompressionStream`):

```bash
OUT="$SCRATCHPAD/cluster-<cid>-drafts-url.txt"   # your session scratchpad dir
# Resolve the cockpit port the same way `prospector serve --dev` does: $VITE_PORT → repo-root .env → 5173
PORT="${VITE_PORT:-$(sed -n 's/^VITE_PORT=//p' "$REPO_ROOT/.env" 2>/dev/null)}"; PORT="${PORT:-5173}"
python3 - "$OUT" "$PORT" <<'PY'
import json, base64, gzip, sys
cid = 23  # this cluster
drafts = {                       # every OPEN member → the comment it should post
  "383": "Thanks for this, …",   # reuse one string for members that share a comment
  "1174": "Thanks for this, …",
  "477": "Thanks for this, …",
}
blob = base64.urlsafe_b64encode(
    gzip.compress(json.dumps(drafts, ensure_ascii=False).encode("utf-8"), mtime=0)
).decode("ascii")
open(sys.argv[1], "w").write(f"http://localhost:{sys.argv[2]}/clusters/{cid}?drafts={blob}")
PY
pbcopy < "$OUT"                  # → clipboard; paste into the address bar
echo "✓ link copied ($(wc -c < "$OUT" | tr -d ' ') chars, :$PORT) · saved to $OUT"
# to launch it straight into the browser instead:  open "$(cat "$OUT")"
```

Only include members that are still **OPEN** (a closed/merged member is a no-op).
Tell the operator the link is on their clipboard (and give the `open "$(cat "$OUT")"`
one-liner); note that it needs the dev cockpit running and that opening it seeds — but
never clobbers — the edit boxes.

## Read-only contract

Reads run as the operator's `gh` login. This skill writes nothing upstream and
runs no `gh` write verb. The sanctioned upstream write path is the cockpit
executor (the cockpit UI) as `TRIAGE_BOT_LOGIN` — gated and logged.
