# Threat evidence: preserve a malicious PR's evidence the moment it is flagged

## Why

On 2026-10-02 the threat scan flagged five paperclipai/paperclip PRs (11987,
11988, 12035, 12041, 12063) by `zach-hermes`. They were opened in August with
honest content. On 2026-09-28 the author force-pushed all five with backdated
commits (new SHAs, the original author *and* committer timestamps, most of them
committed as the project's own `Paperclip <noreply@paperclip.ing>` identity).
Each push added the same 50 files' worth of payload: a `createRequire` smuggle
plus an obfuscated line hidden after thousands of spaces.

Once GitHub suspends an account, its fork and PRs can 404, and the diffs go with
them. Prospector recorded little that survives that:

- the `threat` section's `detail` keeps 120 characters of each matched line,
  which here is the innocent prefix before the whitespace padding;
- the shared `diffs` table holds the 200 KB-capped summarizer copy of a ~500 KB
  diff;
- the threat registry's incident log holds the PR, author, head SHA and
  signature names.

The evidence for this incident was captured by hand
(`~/Downloads/paperclip-malware-evidence-2026-10-02/`). This feature makes that
capture automatic, durable in the shared store, and safe to keep.

## Goals

- Every `malicious` verdict (attack signature or blocklisted actor) gets an
  evidence capture at the flagged head, without delaying the verdict or the
  block.
- The capture holds enough to explain the incident after GitHub removes it:
  the complete diff pinned to the flagged SHA, the diff the PR had before the
  force-push that produced it, PR/commit/actor metadata, the force-push history,
  where each signature matched, provenance, and a SHA-256 of every artifact.
- The payload is inert at rest, never reaches an agent, and never lands in a
  git work tree.
- Operators retrieve it from the PR page (a download) or the CLI.

## Non-goals

- Git objects. Automatic capture fetches nothing with git. The export README
  lists the `git fetch <url> <sha>` commands that reproduce the objects while
  GitHub still serves them.
- Reporting to GitHub, blocking at the org level, or notifying anyone.
- Decoding or analysing the payload.
- Closing the two detection gaps this incident exposed. Separate sessions are
  working on scanning the uncapped diff and rescanning when a head moves.

## Storage

A new table in the existing store (`TRIAGE_STORE_URL`, Supabase Postgres in
production, SQLite locally), declared in `pipeline/schema.py`:

```
threat_evidence
  id           Integer   primary key, autoincrement
  pr           Integer   indexed
  head_sha     String    indexed
  author       String    indexed
  captured_at  String    indexed
  complete     Boolean
  data         JSON      the record below; never holds payload text
  diff_gz      LargeBinary  nullable; gzip of the PR diff at head_sha
  prior_gz     LargeBinary  nullable; gzip of the PR diff at the head the latest
                            force-push replaced
```

`storekit.ensure_schema` creates the table, and `enable_rls` turns Row-Level
Security on, which denies Supabase's anon/authenticated REST roles.

Rows are insert-only. `Store` gets `append_threat_evidence`,
`threat_evidence_for(pr)` (metadata, no blobs), `threat_evidence_heads()` (the
`(pr, head_sha)` pairs with a complete capture), and
`load_threat_evidence(id)` (the row with blobs). It has no update or delete
accessor, and `store_edit.py` has no handler for the table.

A capture is attempted for a `(pr, head_sha)` pair until a complete row exists.
`complete` means the flagged diff was read whole (from the compare, or from a
per-file listing in which every file carried its patch, and not truncated);
other reads that fail are listed in `errors` without making the capture
incomplete, since some, such as a deleted actor's account, can never succeed.
An incomplete capture is written with whatever it got only when the head has
no row yet, so something survives if GitHub removes the PR before a complete
read succeeds, and repeated failing scans add no rows; a later complete
capture is a new row beside it. A capture that got nothing at all writes no
row. Each written capture appends a `threat-evidence:capture` run to the
runs ledger.

### The `data` record

```
pr, url, repo, title, body, state, created_at
base_sha, head_sha, head_ref, head_repo {full_name, id}
actor        {login, id, type, created_at}
commits      [{sha, author {name, email, date}, committer {name, email, date},
               verified, verification_reason}]
force_pushes [{at, actor, before, after}]          # GraphQL HeadRefForcePushedEvent
detection    {verdict, signatures, scanned_at,
              matches [{signature, file, diff_line}],      # into diff_gz, 1-based
              full_diff_signatures}                        # scan_diff ∪ locate over diff_gz
artifacts    {diff:  {bytes, sha256, source, complete, truncated},
              prior: {bytes, sha256, source, before_sha} | null}
provenance   {captured_at, captured_by (gh login), machine (settings.worker_id()),
              prospector_commit (git rev-parse HEAD), store_schema}
errors       [str]                                  # what a partial capture missed
```

`title` and `body` are attacker-written text. The app renders them as React
text (escaped), as it already does for every PR.

## Capture

`pipeline/threat_evidence.py` owns capture, the record shape, export and
verification. `threats.py` gains one pure function, `locate(diff_text)`, that
reports `(signature, file, diff_line)` for each match. It uses the same patterns
and file rules as `scan_diff`, so the two cannot disagree about what fires.

### When

`threat_scan.main` stamps every verdict and saves the registry exactly as it
does now. Then, for each PR whose verdict is `malicious` and whose
`(pr, head_sha)` has no complete capture, it calls
`threat_evidence.capture(store, flag)` (a `Flag`: PR, flagged head, author, signatures), one PR at a time. A capture
failure is logged and counted, never raised. The run's ledger stats gain
`evidence_captured`, `evidence_partial`, `evidence_failed` and
`evidence_already`.

The call sits next to `block_actor` and `record_incident`, the one place a
malicious verdict is acted on, so any path that records an incident also
captures. Before merging, check the in-flight head-move rescan work and route
its malicious verdicts through the same place.

### What it reads

These are read-only GitHub calls through `pipeline/gh.py` as the operator's
login, about six per PR:

1. `pulls/{n}`: metadata, base SHA, and the head GitHub shows now.
2. The diff pinned to SHAs: `compare/{base_sha}...{head_sha}` with
   `Accept: application/vnd.github.diff`. Verified on #11987: byte-identical to
   `gh pr diff` (513,166 bytes, same patch-id), and it works whether or not the
   head has since moved. On failure (GitHub refuses very large compares), the
   fallbacks in order:
   - while GitHub's head is still the flagged head, `diff_cache._synthesize_diff`
     (the per-file listing; marked incomplete when any file has no patch);
   - the scanner's cached copy (`cache/diffs/<head>.diff`, marked incomplete,
     `source: scan-cache`).
3. `pulls/{n}/commits`.
4. GraphQL `HeadRefForcePushedEvent` timeline items (before/after SHAs, actor,
   time).
5. The PR diff at the head before the flagged one, `compare/{base_sha}...{before}`.
   `before` is the latest force-push's `before` when its `after` is the flagged
   head; when GitHub shows no such force-push, the newest earlier head of the
   PR in the shared `diffs` cache. (GitHub hides a blocked actor's force-push
   events: by 17:01 UTC on 2026-10-02 the five PRs' timelines no longer listed
   them, while the `diffs` cache still held every PR's 2026-08-27 head.) When
   GitHub does not serve that diff, the cache's copy stands in
   (`source: diff-cache`, complete only when uncapped). (A two-dot compare 404s and a
   three-dot `before...after` returns the whole PR, because the force-push
   rewrote the commit onto the same parent.)
6. `users/{login}`.

Each diff is capped at 25 MB before compression. Anything over that is cut,
with `truncated: true` and `complete: false`. Measured: #11987's 513 KB diff
gzips to 52 KB, and its prior diff (21 KB, the 6 files the honest PR touched)
to 5 KB.

Bodies are compressed in memory and written to the row. The capture never
writes a diff to disk.

## Export and verification

`threat_evidence.export(row, out_dir)` writes:

```
README.md                 what was flagged, when, by whom; the commit/force-push
                          story; the git fetch commands; safety notes
record.json               the data record
pr-<n>-<head7>.diff       diff_gz, decompressed
pr-<n>-<before7>.prior.diff   prior_gz, when present
force-push-changes.diff   derived: the blocks of the flagged diff for files whose
                          block differs from the prior diff, i.e. what the push
                          changed. For #11987 that is the 50 payload files.
SHA256SUMS
```

Before writing, every artifact's SHA-256 is checked against `data.artifacts`. A
mismatch refuses the export. Files are written read-only. The export refuses an
`out_dir` inside a git work tree (`git rev-parse --is-inside-work-tree`), so
evidence cannot be committed by accident.

The zip the app serves holds the same files, built in memory by the same code.

## Safety

- **Read-only capture.** The capture makes GitHub API reads and nothing else:
  no clone, checkout, install, build or execution.
- **Inert at rest.** Payload bytes exist only in the gzip columns. The JSON
  record holds match locations, not matched text. Anything reading the store as
  text, such as the Supabase table viewer, a `contains` search or a dump, never
  sees the payload. RLS denies the Supabase REST roles.
- **Out of agents' reach.** `prospector_app/agent/store-read` gets no
  subcommand for the table, no agent prompt includes evidence, and no evidence
  is written into any agent's `read_root`. The payload is attacker-written text,
  so this also closes a prompt-injection channel.
- **Append-only.** Insert and read accessors only.
- **Integrity.** SHA-256 over the uncompressed bytes, taken at capture and
  checked on every export and download.
- **Export lands outside repositories.** The export writes inert files only
  (`.diff`, `.json`, `.md`, `SHA256SUMS`), read-only, and refuses an
  `out_dir` inside a git work tree.
- **Downloads.** The app serves `application/zip`,
  `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`. Each
  download appends a `threat-evidence:export` run to the runs ledger
  (operator, machine, PR, capture id, `via`) for chain of custody. The Activity
  log is for actions on GitHub, and reads an unknown kind as a comment.

## Surfaces

### CLI

```
uv run python -m pipeline.threat_evidence capture --pr N      # capture one flagged PR now
uv run python -m pipeline.threat_evidence capture --backfill  # every registry incident without a complete capture
uv run python -m pipeline.threat_evidence list [--author LOGIN] [--pr N]
uv run python -m pipeline.threat_evidence export --pr N [--id ID] --out DIR
uv run python -m pipeline.threat_evidence verify [--pr N]     # re-hash every stored artifact
```

`capture --pr N` refuses a PR whose stored verdict is not `malicious`. The
backfill takes the incidents from the threat registry. A PR GitHub no longer
serves is reported as failed, and the next backfill tries it again.

### App

- `GET /api/prs/{n}/evidence`: the PR's captures as metadata (no blobs):
  `id, head_sha, captured_at, captured_by, complete, artifacts, force_pushes,
  detection.signatures`.
- `GET /api/prs/{n}/evidence/{id}/bundle.zip`: the export as a zip, hash-checked
  and recorded in the runs ledger.
- PR page: inside the existing "⛔ Merge blocked — flagged malicious" callout,
  one line per capture, e.g.
  `Evidence preserved 2026-10-02 17:10 UTC · complete · force-pushed 2026-09-28 (was 86c1869)`,
  and a **Download evidence** button. With no capture yet, the callout reads
  "Evidence not captured yet" and offers the existing single-PR threat-scan
  button, which now captures.

## Schema version

Bump `STORE_SCHEMA_VERSION` 29 → 30 ("the threat_evidence table; an older
threat scan flags a malicious PR without preserving its evidence"). This follows
the bumps for new tables (11 alerts, 18 advisories, 25 claims). It also means an
older checkout cannot write to the store until it updates, so a stale machine
cannot run a threat scan that silently skips capture. Rollout: update the app
machine and the Studio workers together, then run `capture --backfill` once.

## Testing

Unit (`pipeline/tests/test_threat_evidence.py`), with GitHub reads faked at the
`pipeline.gh` boundary, against a SQLite store:

- a complete capture writes one row whose blobs decompress to the fetched diffs
  and whose hashes match;
- head moved since the scan: the diff is fetched by the flagged SHA, not the
  current head;
- compare refused → per-file fallback → scan-cache fallback, each marked
  incomplete with its source; a later complete capture adds a second row;
- an existing complete capture means no GitHub calls and no write;
- force-push history: `prior` fetched only when the latest push produced the
  flagged head; `force-push-changes.diff` holds exactly the changed file blocks;
- `locate` agrees with `scan_diff` on every signature in the existing threat
  tests;
- `data` never contains a line of the payload (a fixture payload string is
  absent from `json.dumps(data)`);
- export refuses inside a git work tree, refuses on a hash mismatch, writes
  read-only files;
- `threat_scan.main` stamps and saves the registry before capturing, and a
  capture exception leaves the stamp, the registry and the ledger entry intact.

App (`prospector_app/backend/tests`): the metadata endpoint has no blob fields;
the zip has the expected names, headers and a `threat-evidence:export` ledger
run.

Frontend: `pnpm run build` and lint the touched files.

End-to-end, against the live store after merge: `capture --backfill`, then
`export --pr 11987`. Its `force-push-changes.diff` must have patch-id
`5e56c29534cf5663948fc228b87f8f47d92d02b5`, the payload's id in the hand-made
evidence, and its PR diff must have patch-id
`15de2acc7b5d9f9d3c5748656f0dad8534f5cf1b`.
