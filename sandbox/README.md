# pr-verify — secretless, fail-closed PR verification sandbox

Runs an untrusted PR's tests in a Docker sandbox that is provably network-isolated
and holds zero secrets. Foundation for the VERIFY pipeline phase
(`pipeline/verify_driver.py` calls `sandbox-run.sh`).

## Setting up a verify machine

Any number of machines can run the sandbox. Each carries its own pinned base
and tracks the default branch on its own daily cadence, and the queue claim is a
compare-and-swap in the shared store, so two machines never pick up the same PR.

**`./setup-worker-machine.sh` does all of the below**, idempotently, from a
fresh clone; the app's 🛠️ Setup tab reports what a machine is still missing and
flips its lane switches. The steps are listed here for anyone provisioning by
hand or debugging a machine that will not come up:

1. **A Docker daemon.** On macOS, Colima (or any runtime whose VM shares
   `$HOME` — the scratch root must be mountable; see `TRIAGE_VERIFY_SCRATCH`
   in `.env.example`). Size the VM for the profile's merge-gate lanes: the
   compile/build phases run in 6g containers and a merge preflight can overlap
   a worker run, so with lanes configured give the VM ~12GB
   (`colima start --memory 12`).
2. **Build the hardened image:** `uv run python pipeline/verify_driver.py
   build-image` — bakes the profile's pnpm pin (`verify.pnpm_version`) into
   `pr-verify:local`.
3. **Pin a base:** `uv run python pipeline/verify_driver.py prepare-base
   [--tier 1]` — clones the pinned default-branch SHA, scrubs it, builds the
   per-batch base image, and (when the profile has a `verify.suite` contract)
   captures the full-suite baseline. The pin lands in the shared store under
   this hostname; the clone and image it names are on local disk, so a machine
   verifies only against the base it prepared itself.
4. **Enable the worker:** set `TRIAGE_VERIFY_WORKER=1` (and optionally
   `TRIAGE_VERIFY_AUTOHUNT=1`) in this machine's `.env`, then restart the app
   backend. Two machines' pins sit a few hours apart at worst, and every run
   records the base it used (`verify.against_base_sha`), so a result always
   names what it was proven against. The Control tab lists each machine's pin.
5. **Point the boot probe at this machine's sensitive services** via
   `TRIAGE_VERIFY_PROBE_DENY` (host:port entries that must be unreachable
   from the sandbox). Unset keeps `boot-probe.sh`'s built-in default.

## Base artifact retention

Each pin leaves two durable artifacts keyed by its SHA: the
`pr-verify-base:<sha12>-t<tier>` image (~3.5GB) and the scrubbed clone under
`$TRIAGE_VERIFY_SCRATCH/base/<sha12>/` (~1.6GB, clone plus prefetched pnpm
store). Each worker re-pins its own machine daily, so `prepare-base` reclaims
what it supersedes there.

**A generation is a base SHA.** The clone is keyed by the SHA alone while the
image tag also carries the tier, so retention keeps or drops a whole SHA — that
is what keeps the two in lockstep.

**Two generations survive: the pinned SHA and the most recently created other
one.** The second slot is load-bearing. `verify_pr` reads the pin once at run
start and holds that image and clone for the whole run, and `compile_preflight`
builds and uses an image for current default-branch HEAD from the app's merge
and autofix threads — either can be mid-run against a non-pinned SHA while the
worker re-pins. Keeping the newest non-pinned generation covers that without
locking. Image removal is un-forced besides, so a phase container running right
now refuses the removal and keeps its clone too.

`prepare-base` sweeps on both sides of its build: before, so a disk that is
already full can be recovered (the build is what fails when it is full, which
would otherwise leave no path back), and again once the new pin is saved. The
sweep also prunes what the two-stage build leaves behind — dangling images
carrying the `prospector.verify-base=1` label that `Dockerfile.base` stamps on
both its stages, and BuildKit cache unused for 24h. A sweep never fails a pin:
its errors are reported and swallowed.

To reclaim by hand, or to see what would go:

```
uv run python pipeline/verify_driver.py gc --dry-run
uv run python pipeline/verify_driver.py gc
```

Direct `sandbox-run.sh` invocations (the tests here) also honor
`PR_VERIFY_NET` (isolated network name, default `pr-verify-net`) and
`PR_VERIFY_DEBUG=1` (dump the container env to stdout — used by the
secretless test). The pipeline's launcher strips both from its environment
allowlist, so they never affect driver-initiated runs.

## Guarantee

- **Isolation is enforced in code, fail-closed.** `sandbox-run.sh` creates and
  asserts an `--internal` Docker network; `boot-probe.sh` runs inside the
  container before any PR code and aborts if public egress or the host
  (`192.168.5.2:3100/:11434`, `host.docker.internal`) is reachable. Proven by
  `tests/test-isolation.sh` (passes on internal, refuses the default bridge).
- **Secretless.** The container env is an explicit allowlist of host-authored
  vars; no host passthrough. Proven by `tests/test-launcher-secretless.sh`
  (poisoned host secrets never appear inside).
- **No agent CLIs.** The image has node/pnpm/git/jq/psql only — no
  claude/codex/gh. (psql is there because the target repository's backup tests
  spawn it; it has nothing to connect to inside the isolated network.)
- **The verdict is the exit code, and it is unforgeable.** Each phase is its own
  container whose PID 1 is the trusted `run-phase.sh`; the host reads the result
  from `docker run`'s exit status. Untrusted PR code runs as a child and cannot
  forge its own PID 1's exit. A red is accepted ONLY on exactly 20. There is no
  writable mount, so no verdict file exists for a detached writer to race. Proven
  by `tests/test-exit-codes.sh` and `tests/test-no-writable-mount.sh`. (The one
  place captured output feeds policy — the dirty-green containment parse — can
  only accept a green whose exit was 20, in the direction an attacker already
  controls by exiting 0; see "Run" below.)

## Run

One phase per invocation, against a base image built from `Dockerfile.base`:

    bash sandbox/sandbox-run.sh \
      --image pr-verify-base:<sha12>-t0 --phase apply-check|repro|red|green \
      --patch /path/to/fix.patch --tier 0 --test-cmd 'pnpm -s test' \
      --base-sha <base-sha> --head-sha <pr-sha>

The exit code is the result; the container's stdout/stderr streams to the
launcher's. Sentinels are declared in `pipeline/gates.py` and mirrored in
`run-phase.sh`:

| code | meaning |
| ---- | ------- |
| 0  | phase succeeded (test passed / patch applies) |
| 10 | boot probe failed |
| 20 | test failed — the ONLY accepted red |
| 30 | patch did not apply |
| *  | infrastructure error, never a signal |

The captured output is the untrusted test's own stdout: advisory evidence for a
later judgment agent. One bounded exception reads it deterministically — when
red AND green both exit 20, the driver parses each leg's failed-tests report
and `gates.green_accepted` may accept the green as contamination-contained:
every green failure also failed red, and none is a test the PR's own test
hunks mention (e.g. a pre-existing test spawning a binary absent from the
image fails identically in both legs). The exemption only ever upgrades a
failing green whose author could more simply have exited 0, so output still
cannot manufacture a verdict the exit code alone would not grant, and it can
never turn a pass into a fail; its accuracy is bounded by the confirm re-run,
the diff-membership check, the regress leg, and the dirty-green finding the
record carries.

The code under test is baked into the base image (`verify_driver.py
prepare-base` builds it from a scrubbed clone at a pinned default-branch SHA), so a
phase container mounts no source at all — untrusted writes land in its own
copy-on-write layer. Each phase runs in its own container from that image, so
a red phase's writes cannot appear in the green phase's container.

## Full-suite regression gate: `baseline` and `regress`

Two more phases, run the same way (`sandbox-run.sh --phase baseline|regress`),
layer a full-suite regression check on top of the four phases above. Both drive
`verify-suite.mjs`, a trusted Node script mounted read-only at
`/verify-suite.mjs` (alongside `run-phase.sh` and `boot-probe.sh`) — the one
phase where no agent-authored string runs; its entire command surface is trusted
code. The active repository profile supplies the target repository's wrapper
path, server project, and per-invocation state environment names. The Python
driver validates them and passes them through the launcher's explicit allowlist;
the runner validates them again before reading target code.

- **`baseline` — once per batch, inside `prepare-base`.** No patch. The phase
  derives the plan (`node /verify-suite.mjs plan`) against the pristine pinned
  tree, then runs it (`node /verify-suite.mjs run --mode baseline`) with no
  exclusions. Exit 0 means the plan ran to completion — failing tests are data
  in the trailer, not a phase failure; anything else is infrastructure.
  `verify_driver.py`'s `prepare_base` refuses to write the `verify_base` pin
  unless this phase exits 0 and the trailer parses, so a pinned base always
  carries a captured baseline.
- **`regress` — per PR, only after a clean red→green.** Requires both `--patch`
  and `--exclude-file` (the pinned baseline's failing-test set). The phase
  derives the plan pre-patch — before the patch applies — so a PR cannot
  influence which tests run, applies the full patch, then runs
  `verify-suite.mjs` in regress mode with the exclusion set. Exit 0 = no
  failures outside the exclusion set; exit 20 = at least one; anything else is
  infrastructure. A patch-apply conflict here is infrastructure too, not
  `needs-rebase` — it contradicts the `apply-check` phase that already passed.

`verify-suite.mjs`'s own exit contract (`plan` / `run --mode baseline` /
`run --mode regress`) is documented in its file header; `run-phase.sh` reads it
directly with no sentinel translation for `baseline`, and folds `regress`'s
non-{0,20} exits to infrastructure explicitly.

Notable properties, all enforced in code and covered by
`tests/test-verify-suite.sh`:

- **Plan derivation is always pre-patch, in-container.** `plan` reads the
  configured wrapper's `--dry-run` for the serialized suites and the
  general-server file list, plus the two workspace project arrays extracted
  from the wrapper's own source (`nonServerProjects`, `generalWorkspacesAProjects`)
  — all before any patch is applied, so the plan is a function of the pinned
  image alone. Every extraction is asserted (non-empty serialized/
  general-server lists, ≥2 `nonServerProjects`, ≥1 `generalWorkspacesAProjects`),
  and every `generalWorkspacesAProjects` entry must also appear in
  `nonServerProjects`; a miss exits non-sentinel.
- **Include-lists, not excludes.** Every vitest invocation gets an explicit
  file list minus the exclusion set — no `--exclude` flag, so no per-project
  path-resolution trap.
- **Keeps going past failures**, unlike the wrapper it derives invocations
  from, which exits at its first failing invocation — a truncated run cannot
  produce a complete failing set (baseline) or complete findings (regress).
- **Accounting is exact and asserted.** Every planned, non-excluded file must
  appear in exactly one invocation's JSON report file (stdout is never
  parsed for results); a missing report or an unaccounted-for file is an
  infrastructure exit, never a verdict.
- **A planned file the tree no longer carries counts as failed, not
  infrastructure.** `regress` runs after the patch applies, so a PR that
  deletes or renames a baseline test file reads as a regression — the
  conservative reading of "an existing test stopped passing."
- **One delimited trailer, last on stdout.** `===VERIFY-SUITE:BEGIN===` /
  `===VERIFY-SUITE:END===` wrap a single JSON line — mode, the failing-file
  set, and counts — inside the host's 8192-byte capture tail.

`EXCLUDE_FILE` (`sandbox-run.sh --exclude-file F`) is a host-written JSON
array of the pinned baseline's failing tests, mounted read-only at
`/verify/exclude.json`. It carries a file path the host wrote from the
`verify_base` pin, never agent-authored content, the same trust shape as
`PATCH_FILE`. `SUITE_CONFIG` (`--suite-config F`, required for the
baseline/regress phases) is the same shape again: the profile's `verify.suite`
repository contract — wrapper path, server project, optional preflight script,
fixture env-var names — mounted read-only at `/verify/suite-config.json`.
A repository whose profile has no `verify.suite` section runs no suite phases
at all: `prepare-base` pins without a baseline and the regress leg records a
deliberate `no-suite-config` skip.

## Tiers

Both tiers run every phase `--internal`, with no egress and no secrets. They
differ only in whether the base image installed the default branch's pinned
dependencies. `tier` is a property of the batch's base image, chosen by
`verify_driver.py prepare-base --tier`.

- **Tier 0 (default):** the base image carries no installed dependencies.
- **Tier 1:** `Dockerfile.base` bakes those pinned dependencies into the base
  image, installed **offline** from a prefetched pnpm store. `prepare-base` runs
  `pnpm fetch` into a store dir from inside `pr-verify:local` — the same image,
  and so the same platform and pnpm, that installs from the store;
  `Dockerfile.base` COPYs it and runs `pnpm install --offline
  --frozen-lockfile` under `--network none`, which makes egress during
  install structurally impossible. Because install is offline and frozen,
  nothing can fetch a package outside that store — a PR-introduced
  dependency is structurally impossible here. Dependency-changing PRs
  (touching `package.json` / `pnpm-lock.yaml` / `pnpm-workspace.yaml`) are
  refused before a sandbox boots by the deps-touched gate, so the base image's
  deps are the only thing ever installed.
- **Tier 2 (live agent):** OUT OF SCOPE — deferred behind a krunkit VM rebuild.

## Before the gate blocks anything

**Measurement checkpoint.** The first `prepare-base` baseline run on the
target machine IS the measurement of a complete stabilized full-suite run —
upstream's own wrapper truncates at its first failing invocation on a red
the default branch, so `baseline` mode is what makes a complete run observable at all. If
that run exceeds ~20 minutes, the number goes back to the operator before
`regress` is relied on for anything — a 5-PR batch would then cost over two
hours of regress runs, and parallelizing the independent workspace-project
invocations inside `verify-suite.mjs` is a decision made with that number in
hand, not ahead of it.

**Validation protocol.** Four sandbox runs, all against the same pinned base,
before a `regressed` outcome is allowed to block a merge:

1. a known-good PR — must come out clean;
2. the same PR again — must come out clean **again**;
3. a deliberate in-tree break (an unconditional throw added to a component the
   suite covers) — must come out regressed;
4. the same break again — must come out regressed **again**.

Both directions, twice. 4/4 or the substrate is not ready and the outcome
stays non-blocking until it is — this is the control that caught the
regression gate's previous, abandoned attempt (#563): flaky under raw
parallelism, quiet on a known-good PR only sometimes.

## Test

    bash sandbox/tests/run-tests.sh
