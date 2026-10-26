# Parallel issue-fix slots

## Why

The issue-fix worker (`prospector_app/backend/issue_fix_worker.py`) runs one
request at a time. Measured on the Studio's ledger, 2026-10-12 to 2026-10-26:

- 302 issues the fixability triage judged `go`; 250 never attempted.
- A run takes 90 minutes at the median, 223 at p90. Since 2026-10-20 runs filled
  133 of ~165 wall-clock hours, so one slot tops out near 12–16 runs a day, under
  the Studio's hunt budget of 20.
- Claude sat at 3% of its 5-hour window and under weekly pace; no issue-fix run
  waited on capacity.

The lane is bound by wall-clock time, not AI capacity. The goal is more issue-fix
PRs reaching "ready to merge" per day, at today's precision or better.

## Goals

- Run up to N issue-fix requests at once on one machine, N a setting.
- Never let two slots, or two machines, work the same request or the same issue.
- Let the factory use up to 90% of the Studio (the operator's call: the machine
  exists for these work queues).

## Non-goals

- Routing work to Codex by capacity or strength. That is the next project, and
  it matters only once slots can fill Claude's 5-hour window.
- Auto-scaling the slot count from load.
- Changing the other lanes' workers.

## Anti-goals

- Duplicated work: two slots running the same issue, or the hunter queueing past
  its daily budget because two threads counted at once.
- More machine faults from slots fighting over Docker, memory or base images.
- More wrong PRs. Slots change throughput only, never a gate or prompt.

## Design

### Shape

One process, one coordinator thread, N slot threads.

- **Slots** loop on `run_once`: pick, claim, run, repeat. Nothing else.
- **The coordinator** is today's `_drain` loop without `run_once`: orphan
  reclaim, reply polling, the ten-minute follow-up pass, triage and the hunt. It
  runs these on their own cadence, not only when the machine is idle, since with
  slots the machine is rarely idle.

Slots stay threads in one process because `recover_orphans` treats this host's
`running` claims made before the process started (`STARTED_AT`) as orphans.
Separate processes would fail each other's live runs on a restart.

### No duplicate work

Three rules carry this, and each needs a test.

1. **The claim is the only way to start work, and it is atomic.**
   `IssueStore.claim_fix_request` reads the issue with its write stamp and saves
   through `storekit.save_if`, a single `UPDATE … WHERE saved_at = <stamp>`. When
   two slots pick the same queued issue in the same instant, exactly one update
   lands; the other gets `None` and polls again. This already holds across
   machines; slots on one machine are just more writers.
2. **One in-flight request per issue.** A request lives on the issue row
   (`fix_request`), and `fix_review.queue` refuses while one is `queued` or
   `running` (`IN_FLIGHT`), so one issue can never carry two runs.
3. **One hunter per machine.** Only the coordinator calls `hunt` and `triage`,
   so the daily-budget count (`_hunted_today`) and the pick run single-threaded
   here.

`fix_review.queue` is a read-check-write, not a compare-and-swap, so two
writers (the hunter and an operator's click, or two machines' hunters) can both
pass the `IN_FLIGHT` check and the later save overwrites the earlier request.
Rule 1 still prevents a duplicate run, but a request can be silently replaced.
Make `queue` save through `save_issue_if` and report a lost race as not queued.
*(Builder's call, settled here: cheap, and slots make queue writes more
frequent.)*

Tests (in `prospector_app/backend/tests/`, against a scratch SQLite store):

- N threads racing `claim_fix_request` on one queued request: exactly one
  claim, and the row names that slot's host.
- N slots over M queued requests: every request runs once and no issue runs
  twice.
- Two concurrent `queue` calls on one issue: exactly one returns ok.
- The coordinator hunts with slots busy, and never past the budget.

### Hunting to keep slots fed

Today the hunt queues one `solve` only when nothing ran. With slots, the
coordinator queues while the count of queued `solve` requests is below its idle
slots, still within `TRIAGE_ISSUE_FIX_HUNT_BUDGET` (20 on the Studio) and while
`lane_health.capacity_open` holds. The budget stays the operator's cap on new
attempts a day; with slots it, not wall-clock time, becomes the binding limit,
and the operator raises it from Setup when wanted.

`hunt` reads `issue_data.issues()`, a snapshot up to 10 s old, and computes the
day's count once. A fill pass that queues several must read the queued count
and the day's hunted count from the store each time, or keep its own tally for
the pass, so it never overruns the budget. *(My call.)* The Studio is the only
machine running the issue-fix lane today, so the fill rule needs no per-host
filter; a two-machine test still covers the `queue` race.

### Machine resources

- **Docker VM.** Large sandbox phases already queue on host locks
  (`verify_driver.large_slots`, one flock per slot), so extra issue-fix slots
  wait for a phase rather than fail. The slot count it allows comes from the VM's
  memory: today 32 GiB gives 2. The Studio has 16 cores and 64 GiB. For the 90%
  budget the operator resizes Colima to 12 CPUs and 48 GiB, which gives 4 large
  slots and leaves 16 GiB on the host for the slots' agent CLIs, the per-run
  copy of the base clone (`lane_tree.materialize`) and the app. A one-time manual
  step, not code:

  ```bash
  colima stop && colima start --cpu 12 --memory 48
  ```

  The worker logs a warning at start when `TRIAGE_ISSUE_FIX_SLOTS` exceeds
  `large_slots()`; extra slots would only queue on the locks. *(My call.)*
- **Work directories** are per issue (`propose.result_dir(n)`), and rule 2
  keeps an issue to one run, so slots never share one.
- **Shared verify scratch.** Slots do share files under the verify scratch:
  `suite-config.json` (`verify_driver.write_suite_config`) and the per-tree
  `suite-exclude/` and `suite-baseline/` files (`prove`), which every issue on
  the pin shares. Today they are written in place with `write_text`, so a slot
  can mount a half-written file. Write each to a temp file and rename it into
  place. A bind-mounted file keeps the inode it was mounted with, so a running
  container reads a whole old version and the next run reads the whole new one.
  Which version a run sees stays as today; no gate logic changes. *(My call.)*
- **Base images.** The issue-fix lane never builds a base: `prove.pinned` and
  `prove.held` raise `NoBase` when the image is missing. The risk is
  `verify_gc` (run from `prepare_base`) removing a pin a long-running slot still
  uses. The builder confirms it cannot, and makes it slot-safe if it can.

### Settings and heartbeat

- New `TRIAGE_ISSUE_FIX_SLOTS` (int, `machine` group, default 1, editable in
  Setup), registered in `pipeline/settings_registry.py`. The Studio starts at 3,
  then 4 after the VM resize.
- The heartbeat gains `current_issues` (a list). `current_issue` stays, holding
  the first, so older readers and other machines keep working without a store
  schema bump. Update `machines.py`, `machine_activity.py`,
  `control_overview.py` and `frontend/src/api.ts` to show every issue in flight.
  *(My call.)*
- `state["current"]` becomes a per-slot set behind a lock.

### Restarts

Pulling main restarts the Studio's workers, and with N slots a restart cuts off
up to N runs. An interrupted run is the machine's fault, not the issue's
(operator's decision):

- `recover_orphans` requeues each request it finds orphaned (`status` back to
  `queued`, `attempts` unchanged) instead of ending it `failed`, and notes the
  interruption in the issue's thread. A non-`solve` request keeps needing its
  run's host, as today.
- An interrupted run never counts toward `HUNT_MAX_ATTEMPTS` or the day's hunt
  budget.
- Shutdown does not wait for in-flight runs; `SHUTDOWN_TIMEOUT` stays as is.

Test: a request left `running` by an earlier process comes back `queued` with
its attempt count unchanged, and runs again.

### Health and capacity

Unchanged. Each slot checks the capacity gate at its own claim. An
`AgentUnavailable` in any slot trips the agent lanes (`lane_health`), after which
no slot picks. `worker_health` trips a lane after `TRIP_AFTER` consecutive
machine faults, so a shared cause (Docker down, memory exhausted) failing
several slots at once trips the lane sooner than one slot would. That is the
behavior wanted: a burst like that is a machine problem. `worker_health` stays
out of scope, and the rollout expects it. *(My call.)*

## Rollout

1. Merge with the default of 1 slot, so behavior is unchanged everywhere.
2. Set `TRIAGE_ISSUE_FIX_SLOTS=3` on the Studio and watch for a day: machine
   faults by kind, run duration (contention shows as a longer p50), and endings.
3. Resize Colima, set 4, watch again.

Success: issue-fix `fixed` endings per day up 2–3× over the 2026-10-20…26
baseline (about 3–6 a day), with no rise in the fault rate or in wrong PRs.

## Left to the builder

- How slot threads start and stop inside the existing worker lifecycle
  (`start`, `stop`, `_drained`, the beat thread).
- The coordinator's cadences, as long as reclaim, replies and follow-ups keep
  their current intervals.
- Whether the Health & queues view shows slots per machine.
