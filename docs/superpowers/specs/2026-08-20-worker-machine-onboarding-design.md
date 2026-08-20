# Provisioning a machine as a work-queue processor

## Problem

Verification and autofix now run on any number of machines, but turning a
machine into one is a manual checklist spread across `sandbox/README.md`,
`.env.example`, and `README.md`: install a Docker runtime, size its VM, build
the hardened image, pin and build a base, set worker flags, restart the backend.
Every step fails quietly in its own way, and nothing on the machine tells you
which one you are missing — a half-provisioned machine looks identical to a
working one until its queue silently parks.

## Goals

- One command takes a fresh clone to a running worker.
- The app tells you what this machine is missing, and what to run about it.
- Worker flags are changeable from the app and stick across a restart.

## Non-goals

- Controlling another machine's worker. The Setup view configures the backend
  it is served from. The Control tab already reports every machine read-only.
- Installing system software from the backend. The script runs in the
  operator's terminal, where `brew` and `sudo` have a TTY and run as them.
- A general `.env` editor. Worker flags only.

## Shape

Four pieces, each usable on its own.

### `setup-worker-machine.sh`

One idempotent command, run from the operator's own terminal. Calls `setup.sh`
first (already idempotent), then does the worker delta: reports missing brew
formulae and installs them on confirmation, starts Colima sized for the
profile's merge-gate lanes, runs `verify_driver.py build-image` and
`prepare-base`, and writes the worker flags through the same allowlist the app
writes. Re-running it on a provisioned machine changes nothing and says so.

Deliberately not registered in `.conductor/settings.toml`,
`.superset/config.json`, or `.claude/launch.json`: those wire dev entry points,
and a worktree must not provision Docker on creation.

### `prospector_app/backend/worker_readiness.py`

The ONE answer to "what is this machine missing", modeled on `caps.py`. Each
check reports pass/fail, what it looked at, and the remedy. The script runs the
same module as its preflight and its closing verification, so the script and
the app can never disagree about what ready means.

Checks: Docker daemon answering; hardened `pr-verify:local` image built; this
host's base pin present with its clone and image on local disk; the push
identity configured and its key holding `key_safety_failure`'s bar; bot token
mintable; store schema not behind this checkout; worker flags set.

Read-only and side-effect free — safe to poll.

### `prospector_app/backend/worker_control.py`

Two operations, both local to this backend.

`set_flags` writes an allowlisted subset of `.env`
(`TRIAGE_VERIFY_WORKER`, `TRIAGE_VERIFY_AUTOHUNT`, `TRIAGE_FIX_WORKER`,
`TRIAGE_FIX_AUTOHUNT`, `TRIAGE_FIX_AUTOPUSH`) in place, preserving every other
line byte-for-byte and never reading a credential-bearing one back to the
caller. A key outside the allowlist is a hard error, not a silent skip.

`apply` reloads `.env` into `os.environ` and reconciles the running threads:
start a worker whose flag turned on, signal `stop` on one that turned off.

### The Setup view

Readiness rows with their remedies, the one command to copy, and the flag
controls. Polls readiness so rows turn green as the script progresses.

## Settings change

`settings.FIX_WORKER`, `FIX_AUTOHUNT`, and `FIX_AUTOPUSH` are module constants
frozen at import, so a flag written to `.env` cannot take effect without a
restart. They become accessors (`fix_worker_enabled()`, `fix_autohunt()`,
`fix_autopush()`), matching `push_identity_configured()`, which is already one.
Four non-test call sites. `verify_worker` already reads `os.environ` per call.

## Restartable workers

`startup()` in both workers starts daemon threads once and `stop` is a
module-level `Event` that stays set. Both gain a `shutdown()` that sets `stop`
and joins, and `startup()` clears `stop` and refuses to double-start by
checking whether its threads are alive. This is what makes a flag toggle real
rather than a prompt to restart the process.

## Error handling

A readiness check that raises reports as failing with the exception as its
detail — a check that cannot answer is not evidence the machine is ready. A
flag write that fails leaves `.env` untouched (write to a temp file in the same
directory, then replace). `apply` reconciles what it can and reports per-worker
outcomes, so one worker failing to start does not hide the other succeeding.

## Testing

- `worker_readiness`: each check's pass, fail, and raise paths against stubs.
- `worker_control.set_flags`: round-trips a flag; preserves unrelated lines
  including credential-bearing ones; rejects a key outside the allowlist;
  leaves the file untouched when the write fails.
- `worker_control.apply`: starts a worker whose flag turned on, stops one that
  turned off, reports both outcomes.
- Both workers: `shutdown()` then `startup()` leaves a live worker.
- `settings`: the three accessors read the current environment.
