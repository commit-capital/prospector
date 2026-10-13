# Changed-line coverage — the design review's deterministic companion

## Why

The design review (`pipeline/design_review.py`) judges whether a PR's tests
would catch a broken implementation, but only by reading them. Changed-line
coverage measures one thing an agent cannot: of the lines the PR adds to
non-test source, how many its related tests execute. It is advisory evidence —
shown beside the design review and handed to its reviewer — never a gate.
Mutation testing is out of scope.

## Where it runs

A step inside the design lane (`verify_worker.run_design`), under the design
claim and before the agent: `pipeline/coverage.py:measure` on this machine's
pinned base image (the one verify runs on; nothing is built). A coverage
failure never stops the review: the entry records the failure and the agent
runs without the evidence. `TRIAGE_DESIGN_COVERAGE` (default on, editable)
turns the step off.

## The measurement

1. **Changed lines** — the `+` lines of each file the PR's patch
   (`verify_driver.fetch_patch`) touches that is JS/TS source
   (`.ts .tsx .js .jsx .mjs .cjs .mts .cts`, not `.d.ts`) and not a test path.
   None → `skipped`. A patch that changes a dependency manifest → `skipped`
   (PR dependencies are never installed). At most `MAX_LINES` changed lines
   are measured.
2. **Related tests** — the PR's own touched test files, then
   `resolve_evidence.related_tests(pin clone, changed source paths)`, capped
   at `resolve_evidence.MAX_RELATED_TESTS`. None → `untested`, with no
   container run.
3. **Provider** — the target repository installs no coverage provider, and the
   sandbox installs offline from the default branch's lockfile. `coverage.provider_dir`
   reads the root importer's `vitest` version from the pin clone's
   `pnpm-lock.yaml` and fetches `@vitest/coverage-v8@<version>` once into
   `<verify scratch>/coverage-tool/<version>/` from a networked container
   (`npm install --ignore-scripts --omit=peer`, as the image's unprivileged
   user), marking it complete. A lock serializes concurrent fetches.
4. **Phase `coverage`** — `sandbox-run.sh` mounts the provider directory at
   `/coverage-tool` and the host-written request (`{"files": {path: [lines]}}`)
   at `/verify/coverage-request.json`, both read-only. `run-phase.sh` applies
   the patch, copies the provider into `node_modules/@vitest/coverage-v8`, runs
   the host-built command (the profile's runner over the related tests with
   `--coverage.enabled --coverage.provider=v8 --coverage.reporter=json
   --coverage.reportOnFailure --coverage.reportsDirectory=/tmp/cov` and one
   `--coverage.include` per changed file), ignores the tests' exit, and runs the
   mounted `verify-coverage.mjs`, which prints one trailer
   `===VERIFY-COVERAGE:BEGIN=== {...} ===VERIFY-COVERAGE:END===`: per changed
   file, the changed lines a statement starts on (`executable`) and those whose
   statement ran (`hit`). The exit code means only infrastructure: 0 measured,
   30 patch conflict, anything else the harness's fault.
5. **Host** — parses the last trailer and builds the record.

**Trust:** the trailer and the coverage file are written inside a container
running the PR's code, which can forge both. The figure is advisory and the
reviewer's prompt calls it self-reported.

## Storage

`reviews["design"].extra.coverage`, written with the entry by
`Store.save_design_review` (head-stamped through `reviewed_sha`; ingest already
carries the entry verbatim, so no schema bump):

```
{"status": "measured" | "untested" | "skipped" | "error", "reason": str | None,
 "base_sha": str, "tests": [str], "executable": int, "hit": int,
 "files": [{"path": str, "executable": int, "hit": int, "uncovered": [[start, end]],
            "measured": bool}],
 "duration_s": float}
```

`files` is capped at 50 and each `uncovered` list at 20 ranges.

## Evidence for the reviewer

`design_review.review(..., coverage=record)` fills a `__COVERAGE__` block in
the prompt: the totals, the tests run, and the uncovered ranges per file,
labelled as the PR's own self-reported test run, to weigh in the `test`
category.

## Display

`coverage.summary(record)` — "coverage 62% (31/50 changed lines)", "coverage
skipped: …", "no related tests", "coverage not measured: …" — is appended to the Design review
check row's detail. The PR page's Design review block shows the line and the
uncovered ranges per file.

## Cost

One 2g container per reviewed head, at most `MAX_RELATED_TESTS` test files,
`TIMEOUT_SECONDS` (900); one provider fetch per vitest version per machine.

## Testing

Unit tests for changed-line parsing, the lockfile version read, the trailer
parse, the record and summary, the command build, `measure`'s skip and error
paths with the phase stubbed, the prompt block, `run_design` carrying coverage
into the entry, and the check-row detail; `sandbox/tests` exercises
`verify-coverage.mjs` against a fixture coverage file.
