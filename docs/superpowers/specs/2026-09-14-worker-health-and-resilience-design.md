# Worker health, circuit breaking, and cross-machine recovery

Date: 2026-09-14. Approved in chat.

## Problem

The verify and autofix workers record every outcome per PR and nothing per
machine. A worker whose sandbox cannot start, whose agent CLI is
unauthenticated, or whose base pin stopped refreshing keeps draining the queue
and converts it to `failed`, `refused`, or an in-memory skip set, while the
Control tab shows one chip per PR. Work claimed by a worker that goes offline
stays claimed. Host-keyed records use `socket.gethostname()`, which on macOS
follows the network, so one laptop has appeared under three names and its kept
worktrees are orphaned.

## A. Stable worker identity

- `settings.worker_id()` returns `TRIAGE_WORKER_ID` when set, else the
  hostname. Every stamp, claim, pin, and heartbeat uses it.
- `worker_control.WRITABLE` admits `TRIAGE_WORKER_ID` (a single token of
  letters, digits, `.`, `_`, `-`). `setup-worker-machine.sh` writes it from
  the machine's LocalHostName.
- `pipeline/rename_worker.py` folds one worker name into another across PR
  records (`fix_request`, `verify_request`, `security_run`, and nested `host`
  stamps under them) and the `verify_base`, `verify_worker`, and `fix_worker`
  registries, keeping the newest record where both names hold one. Dry run by
  default; `--live` snapshots pre-images through `store_edit`.

## D. Worker health and escalation

- `pipeline/worker_health.py` is the ONE policy: per worker, per lane
  (`security`, `verify`, `fix`), a record of consecutive system-fault
  endings, the last failure, the last success, and a `tripped` stamp. The
  record lives in a `worker_health` registry keyed by worker id.
- A lane trips on three consecutive system-fault endings, on an agent
  outage (`headless_agent.AgentUnavailable`: auth failure or missing CLI),
  on three consecutive pin-refresh failures (verify), or on a failed sandbox
  self-test. A tripped lane picks no work. Every fifteen minutes the worker
  runs that lane's self-test and re-opens on a pass; the Control tab offers
  Resume.
- On trip: a `worker:trip` ledger entry; a banner on the Control tab; an
  issue on `PROSPECTOR_FEEDBACK_REPO` as the operator, labeled
  `worker-health`, titled by worker, lane, and failure signature, with the
  last three reasons and the log tail, deduplicated per signature for seven
  days.
- Any live backend that sees an enabled worker's heartbeat stale for over an
  hour files one offline issue the same way.
- Worker stdout is mirrored to a rotating log under the verify scratch
  directory.
- An agent outage ends a fix-worker action `failed` (cooldown retry), never
  `refused`. The security lane does not add the PR to its skip set on an
  outage; the skip set gains timestamps and reasons and expires entries after
  six hours.

## C. Retry and reclaim

- The verify request fault map moves into `gates.py`. The hunter re-queues a
  system-fault `error` request when the head moved, when a different worker
  is picking, or when six hours passed on the same worker, up to three
  attempts per head. `refused-safety` and `cancelled` stay terminal.
- A `running` verify or fix request whose worker's heartbeat is stale is
  reclaimable by any worker: verify at a resumable step re-queues, otherwise
  it becomes an interrupted error; fix becomes `failed` with the reason, and
  the hunter re-arms that head.
- A parked `resolve` whose worker has been offline over 24 hours and that no
  reviewer rejected is cancelled with that reason and its head re-armed so
  another worker re-authors it. Reviewer-rejected resolves stay parked.

## Order

A, then D, then C: retry without a breaker is what produced the backlog.
