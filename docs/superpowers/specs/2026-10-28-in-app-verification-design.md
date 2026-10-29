# In-app verification

## Why

The operator's expectation (2026-10-28): the Verification queue runs the
triaged app itself, not only its unit tests. Today VERIFY
(`pipeline/verify_driver.py`, `pipeline/verify_pr.py`) runs the target app's own
vitest commands in a `--network none` container, decides from exit codes, and
never boots the server or opens a browser. The repro prompts tell the agent to
give up on any bug that "needs a real browser session". `profile.py` rejects a
`verify.e2e_cmd` key on purpose: the lane was reserved and never built. Fix PRs
fill the target app's required "Screenshots" section with `- n/a` because nothing
produces one (`issue_triage/fix_pr_body.py`).

What the operator wants from a bug-fix PR:

1. **Reproduce the bug in the running app before the fix, then fail to
   reproduce it after.** This is the in-app version of the red→green proof
   VERIFY already does with tests.
2. **A headed browser regression suite** that clicks through basic tasks on
   the PR's build. Failures flag the PR.
3. **Real screenshots**: evidence for the operator, and actual images in the
   "Screenshots" section of fix PRs.

## What the target app already gives us

Recon of the pinned base (`b22633676`, 2026-10-14):

- `tests/e2e/` holds 44 Playwright specs plus `playwright.config.ts`. Its
  `webServer` builds the UI into `server/ui-dist` and runs
  `<app-cli> onboard --yes --run` in a throwaway `<APP>_HOME`, on
  `127.0.0.1:3199`, with `<APP>_DEPLOYMENT_MODE=local_trusted` (no login)
  and fixed placeholder signing secrets.
- Postgres is `embedded-postgres` (an npm dependency), so there's no external
  database.
- `NODE_OPTIONS` imports fixtures (`fixtures/ai-connection-provider.mjs`,
  `agent-chat-github.mjs`) that stand in for the AI provider and GitHub.
- The target app's CI never runs this suite on PRs. `e2e.yml` is
  `workflow_dispatch`, allowlisted, `main` only. Nobody knows today which
  specs pass, which flake, or which quietly need the network.

## Goals

- **A browser-capable sandbox.** The sandbox image gains a Playwright Chromium
  (baked in at image build, where the network is allowed; a phase container
  still has no egress). Phases that boot the app keep every existing
  isolation guarantee: `boot-probe.sh` first, `--network none`, no secrets,
  exit-code verdicts. The app listens only on the container's loopback.
- **A curated regression suite per pinned base.** When a machine pins a base
  (`prepare_base`), it runs the whole configured Playwright suite against that
  base, offline, several times. Specs that pass every time form that base's
  e2e baseline. The rest are recorded with their reason (failed, flaked,
  timed out) as "not covered". The baseline renews whenever the pin moves, so
  new upstream specs join and broken ones drop out on their own. This mirrors
  the existing suite baseline (`pinned_baseline`, `implausible_baseline`),
  including refusing a baseline that fails most of its plan.
- **Regression check on a PR.** Boot the PR's build and run only the
  baseline specs. A spec that passed on the base and fails on the PR,
  confirmed by one re-run of the failing specs, is an e2e regression. This
  feeds `gates.verify_outcome` as a new signal alongside `regress`: a
  confirmed e2e regression demotes a would-be `verified-fix` to `regressed`,
  and escalate keeps primacy. Playwright's exit status and its JSON report
  decide. No agent is involved.
- **In-app reproduction.** The blind stage (committed before anything boots)
  also judges whether the claimed bug is visible in the app. When it is, an
  agent writes one Playwright spec against the pinned base that drives the UI
  to the reported failure and asserts the correct behavior. Like the PR's own
  test, that spec must fail on the base (twice) and pass with the PR applied
  (twice). The spec is validated and threat-scanned like other agent-written
  test files (`validate_test_files`), and it lives inside `tests/e2e/` so the
  repository's fixtures and helpers apply.
- **Screenshots out of the sandbox.** Every booted phase captures
  screenshots: each in-app repro step on the base and the PR, and each
  failing regression spec. They come back to the host through a new channel
  (see Constraints), are stored as PR evidence and shown on the PR page.
- **Issue-fix factory parity.** The issue-fix lane (`issue_triage/fix_lane.py`)
  uses the same in-app reproduction and regression run in its prove stage,
  and `fix_pr_body.py` fills the "Screenshots" section with before/after
  images when they exist (`- n/a` only when the bug isn't visible in the UI).
- **Configured in the profile.** A `verify.e2e` section (replacing the
  reserved, rejected `e2e_cmd`) names the Playwright config, the spec
  directory, the boot timeout, and any specs to always exclude. A profile
  without it has no in-app lane, and everything behaves as today.

## Non-goals

- Writing a regression suite of our own. The suite is whatever the target app
  ships that runs reliably offline.
- Live AI providers, real GitHub, or any network access from a booted app.
  Specs that need them land in "not covered".
- Visual-diff or pixel-baseline testing. Screenshots are evidence for a
  human, not a gate.
- Multi-user and authenticated-mode configs. Only the default
  `playwright.config.ts` (`local_trusted`) runs.
- Running in-app checks on PRs that don't touch app code. The diff's paths
  decide (builder's call on the exact rule; docs-only and CI-only diffs skip).

## Anti-goals

- **An agent's word decides an outcome.** The repro agent writes a spec. Only
  the host's observed exits decide whether it reproduced.
- **A flaky suite blocks good PRs.** A spec that isn't solidly green on the
  base never gates. One confirming re-run on a PR failure.
- **Weakening the sandbox to make the app boot.** If a spec needs network or a
  secret, it is not covered. The sandbox doesn't open up.
- **Unbounded cost.** Booting builds the UI and runs specs serially (the
  upstream config uses `workers: 1`, 60s per test). The e2e phases are
  metered by `pipeline/capacity.py` like other agent and sandbox work, and
  retries end via the existing stamps and caps (no unbounded lane retries).

## Constraints

- **Trust model.** Contributor code now runs a server and a browser, still
  inside the same container with no network and no secrets. The host still
  decides from exit codes and Playwright's report, which the host parses.
  These are the security-sensitive pieces and need security-focused tests:
  - the image change (Chromium and its system libraries);
  - the new artifact channel;
  - any change to what a phase may write.
- **Getting images out.** `run-phase.sh` today writes nothing to a mount; its
  only output is stdout and the exit code. Screenshots need an output
  directory. The host must treat everything in it as untrusted:
  - accept only PNG files, under a per-file and per-run size cap and a count
    cap;
  - check the file signature, and re-encode or strip metadata before storing;
  - never follow links.
  The Playwright JSON report comes back the same way, as untrusted data the
  host parses defensively. Its pass/fail never overrides the exit code.
- **Store.** Screenshots are files, not rows: store a reference (path or
  content hash) in the PR's verify evidence, stamped with the head SHA like
  every other verify fact (`pipeline/freshness.py`). Adding e2e signals to
  the verify record changes its shape, so check whether
  `schema.STORE_SCHEMA_VERSION` needs a bump.
- **Machine resources.** A booted phase is a large phase (UI build plus
  embedded Postgres plus Chromium). It runs under the existing large-phase
  host lock (`_LargePhaseLock`). `setup-worker-machine.sh` and the Setup tab
  report whether a machine's image has the browser.
- **Profile-driven, not app-hardcoded.** App-specific commands,
  paths and env live in `profile.json`'s `verify.e2e`, not in Prospector code.

## Coverage checkpoint

Once the sandbox boots the target app and runs its suite, and before any outcome
signal is built on top, measure how much of the suite works:

- Run all 44 specs on the pinned base, several times, and report each spec as
  reliable, flaky or failing.
- Give each failure its cause: needs network, needs a secret or live
  provider, a gap in our sandbox (missing fonts or system libraries, `/dev/shm`
  size, timezone or locale, memory, timeouts), or broken upstream.

The operator's bar is illustrative, not a fixed threshold. If around 40 are
reliable, ship. If only around 20 are, the next step is improving the sandbox
to recover the specs that fail for sandbox reasons. Re-measure, then bring the
results to the operator before going further. Failures that need network or
secrets stay out by design (Non-goals).

## Decided so far (operator)

- Reproduce in-app before the fix, fail to reproduce after.
- The regression suite is the target app's own specs that prove reliable offline.
- Measure how many specs are reliable in the sandbox before building on them,
  and fix sandbox gaps if too few are (Coverage checkpoint).
- **An in-app red→green counts as proof.** A repro spec that fails twice on
  the base and passes twice with the PR can make the outcome `verified-fix`
  on its own, including for PRs with no unit test. This is a new success path
  in `gates.verify_outcome` (and `_authored_lane_outcome`). Every guard the
  test lanes have applies here too: the blind faithfulness verdict committed
  before boot, escalate on a flaky repro, the e2e regression demotion. The new
  path needs security-focused tests, and it supersedes the one-directional
  call below.
- **Screenshots live in Supabase.** The store is already Supabase Postgres.
  Images go in a Supabase Storage bucket in the same project, readable by
  public URL, so a GitHub PR body can show them. The store row keeps only the
  object key and content hash. Details:
  - Objects are named by content hash, and only host-validated PNGs are
    uploaded.
  - The Storage credential is a new registered setting
    (`settings_registry`). It is never in any agent's environment
    (`safety_guard.agent_env`) and is denied to agents' read rules like the
    other keys.
  - A public bucket publishes whatever is uploaded. Screenshots come from a
    throwaway local instance with placeholder secrets, but the host still
    caps size and count, and uploads only screenshots tied to a PR that
    reaches a proposal or the PR page.
  - Retention follows `verify_gc`.
  - Without the setting, screenshots stay local and fix PRs say `- n/a`.
- **Headless is fine** as long as it's a real browser. Use Chromium's full
  headless mode, the same rendering engine as headed, not a DOM emulator.
  Upstream's `headless: true` stands, and no virtual display goes in the
  image.
- Fix PRs' "Screenshots" section carries real images.

## My calls (operator to check)

- Baseline selection runs the full suite 3 times at pin time. "Reliable"
  means passing all 3.
- A PR-side e2e failure gets exactly one confirming re-run of the failing
  specs.
- An e2e regression maps to the existing `regressed` outcome, not a new one.
- An in-app repro that still fails with the PR applied makes the outcome
  `not-verified`. One that never reproduced on the base is recorded and
  doesn't gate. The PR then falls back to the test lanes.
- Scope is profile-driven. The first profile is the target app's.
- **The base's specs judge the PR.** For the regression run, the base's
  `tests/e2e/` (specs, config, fixtures) is laid over the PR tree, so a
  contributor can't weaken a check or change the boot command. If a PR edits a
  baseline spec and that spec then fails, the outcome is `escalate`, not
  `regressed`: a deliberate UI change needs a human to read it. The in-app
  repro spec goes in a path no PR can touch.
- **Browsers track the base.** The pinned base's Playwright version decides
  the Chromium build. That build is fetched at pin time in the networked
  prefetch step (the same place as `prefetch_store`) and baked into the base
  image. Phase containers stay offline.
- **When the regression run happens.** It runs on app-touching PRs whose
  cheap phases didn't already end the run (`needs-rebase`, `not-verified`,
  and similar). Its result shows on the PR page whatever the outcome. In the
  issue-fix lane, a confirmed e2e regression blocks the proposal.

## Left to the builder

- Exact phase names and how they slot into `verify_driver.verify_pr`'s
  sequence, and whether the base build of the UI is cached in the base image.
- The diff-path rule for "touches app code".
- Screenshot naming, storage location under the scratch root, retention (fold
  into `verify_gc`), and how the PR page renders them.
- How the fix PR body lays out the images (before/after pairs, captions).
- The prompt for the in-app repro agent (extend `BLIND_PROMPT` and
  `AUTHOR_PROMPT`, or add a sibling).
- How the work splits into PRs. A natural order: image, boot and artifact
  channel, then the coverage checkpoint (a report, plus sandbox fixes if
  needed), then the base suite and regression signal, then in-app repro, then
  issue-fix and screenshots in fix PRs.
