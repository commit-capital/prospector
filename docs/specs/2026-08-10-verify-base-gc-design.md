# Verify sandbox base GC — reclaiming pinned-base artifacts

> Design record. Once built, current behavior is defined by
> `pipeline/verify_gc.py`, `pipeline/verify_driver.py`, and
> `prospector_app/backend/verify_worker.py`.

`verify_driver.py prepare-base` builds two durable artifacts per pin: a
`pr-verify-base:<sha12>-t<tier>` Docker image (~3.5GB) and a scrubbed clone
under `SCRATCH/base/<sha12>/` (~1.6GB). Nothing in the repo ever deletes
either. Both accumulate one generation per re-pin, and the daily refresh in
`verify_worker.maybe_refresh_base` re-pins roughly every 24h.

## The incident (2026-08-10, Brandons-Mac-Studio)

After ~10 re-pins:

- Colima's docker disk was full — `/dev/vdb1 59G 56G 560M 100%` — carrying 8
  stale `pr-verify-base` tags plus ~7.5GB of dangling build layers.
- `SCRATCH/base` held 15 clone dirs totalling 24GB, of which exactly one (the
  current pin) was reachable.

The consequences cascaded in a specific order, and the order matters to the
design:

1. The daily refresh died with `CalledProcessError` on its `docker run` while
   moving the pin `4813ed3f0c6d -> ebf2b8ff7904`. It kept the old pin and wrote
   `{'ok': False, ...}` to the runs ledger. That is correct fail-safe behavior,
   and nothing surfaced it.
2. The Docker daemon later went down. Every verify pickup then hit
   `verify_pr._image_exists` returning False, parked as `waiting-for-base`, and
   retried every `BASE_RETRY_SECONDS` for hours until each request's 6h
   `WAITING_FOR_BASE_MAX_HOURS` budget expired.

Manual recovery (prune dangling, `docker rmi` all but the pinned tag, `rm -rf`
all but the pinned clone) took the docker disk 56G → 6.5G and the host 24G →
2.1G. Without retention this recurs in roughly ten more re-pins.

## Three defects

Retention is the cause. The other two are what made a full disk take a day to
diagnose: a misleading park message and an invisible failure. The park message
is #73's, merged separately; retention and the invisible failure are this
change.

## Concurrency: who touches these artifacts

Retention depth is a function of who can be reading an artifact while GC runs.

`verify_pr._run_inner` reads the pin **once** at run start
(`verify_pr.py:432`) and holds the resulting `image` and `clone` for the whole
multi-container run. The re-pin that would GC them fires from
`maybe_refresh_base` inside the verify worker's drain loop, and that loop is
single-threaded — `maybe_refresh_base()` then `run_one(n)` run sequentially
(`verify_worker.py:370-376`). So the worker never races itself.

Two other callers do overlap it:

- `compile_preflight` is invoked from `executor.py:722` (a merge request
  thread) and `fix_worker.py:432` (the autofix worker thread). Both build and
  then use `base_image_tag(resolve_base_sha(), 1)` — an image for *current
  default-branch HEAD*, which is frequently not the pinned SHA.
- An operator running `prepare-base` from the CLI while the worker has a run in
  flight.

A GC keyed strictly on "the pinned SHA" would therefore be able to delete an
image another thread is mid-run on. **Retention keeps the pinned SHA plus the
single most recently created other SHA.** A preflight image built for current
HEAD is by construction the newest, so it is protected without any new locking
between the app's request threads and the worker.

Two further facts constrain the design:

- `build_base_image` already `shutil.rmtree`s the clone dir before rebuilding
  (`verify_driver.py:238-239`), so clones never accumulate for a *repeated*
  SHA — only across distinct SHAs.
- Every sandbox container runs `--rm` (`sandbox/sandbox-run.sh:124`,
  `verify_driver.py:213`), so an un-forced `docker rmi` fails only while a phase
  container is actively running against that image. That is a second,
  independent safety net under the retention rule.

## Retention — `pipeline/verify_gc.py`

A new module. `verify_driver.py` is already 1423 lines, and GC has one job.

**A generation is a base SHA, not a `(sha, tier)` pair.** The clone dir is keyed
by sha12 alone while the image tag carries the tier, so a per-tier rule could
delete a clone that a surviving image of another tier still needs. Keeping or
dropping a whole SHA keeps the two in lockstep.

```
list_generations()                     -> list[Generation]  # shells to docker + scans SCRATCH/base
plan(gens, pinned_sha)                 -> Plan              # PURE — the policy
collect(pinned_sha, *, dry_run=False)  -> dict              # orchestrates; NEVER raises
```

`collect` takes the pinned SHA rather than a `Store`, so nothing in this module
depends on the store layer.

`plan` keeps the pinned SHA plus the most recently created other SHA;
everything else is reclaimable. Creation times come from `docker image ls
--format '{{.Tag}}\t{{.CreatedAt}}'`, whose leading three whitespace-separated
fields parse as `%Y-%m-%d %H:%M:%S %z` (the trailing timezone abbreviation has
no strptime directive and is dropped). One call, and each line carries its own
tag, so a tag that disappears mid-survey cannot misalign a timestamp onto its
neighbour. A clone dir with no surviving image falls back to its directory
mtime.

`plan` never places the pinned SHA in the delete set regardless of what the
timestamps say. That is an explicit invariant with its own test, not an
emergent property of the sort — a malformed or missing timestamp must not be
able to reclaim the live pin.

Deletion order per reclaimed generation: `docker rmi` (no `-f`) for each of its
image tags, then `shutil.rmtree` on its clone dir.

Then the build-side pass, which is what reclaims the 7.5GB.
`sandbox/Dockerfile.base` is a two-stage build whose builder stage stages a
1.9GB pnpm store that never enters the shipped image; those layers are the
leftovers. Both builder implementations are covered:

- `docker image prune -f --filter label=prospector.verify-base=1` — our own
  dangling images only. The label comes from a `LABEL prospector.verify-base=1`
  added to **both** stages of `Dockerfile.base`, so classic-builder
  intermediates carry it too.
- `docker builder prune -f --filter until=24h` — BuildKit cache, where the
  intermediate stages are not images at all. `until` is the filter key
  `builder prune` accepts. The 24h window protects the build that just finished
  and any concurrent one.

`collect` wraps all of it and returns `{"ok": bool, "keep": [...],
"reclaimed": [...], "error": ...}`. A generation counts as reclaimed only once
every one of its images is gone, so an image a live phase container still holds
keeps its clone too — that run reads both. A GC failure is logged and never
propagates; it must not be able to fail a pin.

### Call sites

`prepare_base` calls `collect` twice: once **before** `build_base_image`, once
after the pin is saved.

The before-call is not redundant. A full disk breaks the build, so GC that only
runs on success can never run again once the disk fills — the failure mode is
self-locking, and that is exactly what happened on 2026-08-10. Both calls are
the same function: before the build the pin is the old one, after it the new
one, and the old pin becomes the kept previous generation.

`verify_driver.py gc [--dry-run]` exposes the same path for manual reclaim.

## Daemon probe — delivered by #73

Telling a stopped daemon apart from a base this machine never prepared is
`verify_driver.daemon_available()`, which #73 landed on main while this work was
in review. Its treatment goes further than a message fix: `verify_pr` consults
the daemon before the image inventory means anything and parks through
`_no_daemon`, which does not spend the `WAITING_FOR_BASE_MAX_HOURS` budget —
that budget bounds a base an operator must prepare, and no amount of waiting
makes a stopped daemon answer. `next_queued` also holds parked base-waiters back
during an outage on one probe per scan, and `recover_orphans` re-queues a
restart that interrupted nothing.

Nothing here re-implements it.

## Refresh health in the Control tab

`maybe_refresh_base` records its outcome into the existing `verify_base`
registry: `refresh_ok`, `refresh_error`, and `refresh_failures` (consecutive,
reset to 0 on success).

These are written **after** `prepare_base` returns. `prepare_base` does a
full-replace `save_verify_base`, so anything written before it is dropped —
the same trap the existing `refresh_attempted_at` re-stamp at
`verify_worker.py:176` works around, and which
`test_verify_worker_refresh.py:76` already pins.

`autohunt_view.status()` gains a `base: VerifyBaseHealth` field: `base_sha`
(sha12), `tier`, `pinned_at`, `age_hours`, `stale`, `refresh_ok`,
`refresh_error`, `refresh_failures`.

**`stale` is `age_hours > 2 * REFRESH_AFTER_HOURS`** (48h). One missed daily
refresh is a hiccup; two consecutive means the lane is broken.

`ControlPanel.tsx` renders one line directly under the runner chips in the
Auto-hunt block, where `runner online` and `running #N` already live:
`base 4813ed3f0c6d · t1 · pinned 3h ago` in the healthy case, and a red chip
(`pin refresh failing · 3 attempts`, error as a title tooltip) when `stale` or
`refresh_failures > 0`.

## Documentation

`sandbox/README.md` documents the base lifecycle in "Setting up a verify
machine" (step 3, `prepare-base`) and says nothing about what happens to the
artifacts afterwards. It gains the retention policy: what `prepare-base`
reclaims and when, that a generation is a SHA, that the pinned SHA and one
previous generation survive, and the `verify_driver.py gc [--dry-run]` escape
hatch for an operator who needs to reclaim by hand.

## Tests

- `plan` is pure, so retention policy is tested with no Docker: the pinned SHA
  is never deleted; the previous generation is retained; a third-oldest
  generation is reclaimed; a clone with no image is handled; malformed or
  missing timestamps do not crash and do not endanger the pin.
- `collect`'s never-raise contract, with a `subprocess.run` that throws.
- `maybe_refresh_base` failure-counter behavior, alongside the existing refresh
  tests.

## Deliberately out of scope

- **No locking or in-use probing** between the app's `compile_preflight` threads
  and the worker's GC. The keep-newest rule covers the race without new
  cross-thread coordination.
- **No change to `WAITING_FOR_BASE_MAX_HOURS`, the retry cadence, or any part of
  the daemon-outage path.** #73 owns that.
