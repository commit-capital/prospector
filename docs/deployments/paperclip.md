# Deployment: paperclipai/paperclip

This is the deployment the tool was built for: triaging the ~3,000 open PRs on
`paperclipai/paperclip`, upstream OSS with a huge, fast-moving backlog. Commit
Capital holds org admin and works the backlog down from the app — merge the
good fixes, send the close-but-not-ready ones back to their authors with
specific asks, and close the duplicates / already-fixed / stale ones. Every
decision executes directly upstream as the `commitperclip` GitHub App; there is
no separate deploy step.

The goal: open the app and triage the backlog as easily as possible — with
clustering that's right, dedup that's right, and a suggested path that never
proposes merging anything with an open flag.

## Deployment configuration

- **Review bar:** `TRIAGE_REVIEW_PROVIDER=auto` — every automated reviewer and
  scanner the repository runs gates at its own bar (Greptile at 5/5; CodeRabbit
  with no open Critical/Major finding; Superagent with no open P1/P2 and a
  passing scan; Socket with no new dependency alerts). A PR any of them blocks
  routes to request-changes, never merge. `pipeline/reviewers.py` is the
  registry; a reviewer that goes quiet for 14 days stops gating on its own.
- **Store:** a shared Supabase Postgres via `TRIAGE_STORE_URL` (transaction
  pooler, port 6543), one store for every operator machine.
- **Repository profile:** the gitignored `profile.json` at the repo root
  (selected by `TRIAGE_PROFILE`) carries the Paperclip subsystem taxonomy,
  risk tiers, CODEOWNERS gating, trusted authors, and verify-suite adapter.
- **Bot identity:** `commitperclip`, a GitHub App installed on `paperclipai`;
  only machines that hold its private key (`TRIAGE_BOT_KEY_FILE`) can execute
  live writes — everywhere else, writes dry-run.

## Operating rules

`CLAUDE.md` at the repo root is the authoritative trust model for this
deployment — read it before doing anything that writes upstream. In short:
reads run as the operator's local `gh` login; writes go through the app
executor only, as `commitperclip`, gated per-PR by `pipeline/gates.py`, and
logged. Never hand-run `gh pr merge/close/comment` against `paperclipai/*`.
