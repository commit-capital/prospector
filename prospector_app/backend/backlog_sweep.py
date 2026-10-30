"""A one-time sweep of the open PRs whose authors finished their part long ago
but which drifted behind the default branch while waiting on us.

The population is open PRs quiet for QUIET_DAYS (neither the PR nor its head
commit has moved) that either sit at the Greptile bar on their head with CI not
failing, or carry an author push Greptile never re-reviewed. For each, the sweep
first tries to bring the PR current through the autofix queue, and asks the
author to rebase when that cannot work:

- `clean`: git replays the head onto the base without conflict (or, for a
  history with merge commits, merges the base in cleanly), so a `rebase` (or
  `update`) request is queued.
- `resolvable`: it conflicts, within what an agent resolve is allowed and
  likely to handle, so a `rebase` is queued; its pause escalates to `resolve`.
- `author`: an agent may not or likely cannot resolve it, so the author is
  asked. A queued request that ends on its conflicts lands here too.
- `close`: the author was asked CLOSE_AFTER_DAYS ago and has neither pushed
  nor been active on the PR since, so it closes as won't-fix with a note that
  it can come back once it is current.
- `ineligible`: `gates.fix_eligibility` keeps the bot off the branch.

The classification is local git only: the base and every PR head are fetched
into a gitignored bare repository and tested with `git merge-tree`, pushing
nothing. Every action is one runs-ledger entry (PHASE), the per-PR record that a
head was already queued, asked or closed, so the sweep does each at most once.

    uv run prospector backlog-sweep                      # dry run: the plan for every PR
    uv run prospector backlog-sweep --live --limit 10    # act on at most 10 PRs
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from pipeline import gates, review_policy, reviewers, settings, storekit
from prospector_app.backend import data, executor, fix_queue, handback, models

if TYPE_CHECKING:
    from pipeline.model import Pr

PHASE = "backlog-sweep"
QUIET_DAYS = 60
# How long an author has to answer the rebase ask before the PR closes.
CLOSE_AFTER_DAYS = 14
# The most conflicted files a queued agent resolve is expected to handle; more
# reads as a drift only the author can untangle.
RESOLVE_MAX_PATHS = 5
REPO_DIR = settings.REPO_ROOT / "prospector_app" / "cache" / "backlog-sweep.git"
FETCH_CHUNK = 200

Bucket = Literal["clean", "resolvable", "author", "close", "ineligible", "waiting", "done", "skip"]
Kind = Literal["greptile-bar", "unreviewed-push"]


@dataclass
class Probe:
    """What local git says about one PR head against the current base."""
    head_date: str
    current: bool
    merge_commits: bool
    rebase_clean: bool
    conflicts: list[str]
    changed: list[str]


@dataclass
class Plan:
    pr: int
    bucket: Bucket
    why: str
    action: str | None = None  # the request to queue, for clean and resolvable
    ask: bool = False
    close: bool = False
    conflicts: list[str] = field(default_factory=list)
    quiet_since: str | None = None
    kind: Kind | None = None
    retry: bool = False  # the queued request ended on the machine, not the PR


def population_kind(rec: Pr, now: datetime, *, acted: bool = False) -> Kind | None:
    """Why `rec` belongs to the backlog, from stored facts, or None. A PR the
    sweep already acted on at its head stays in, however recently its own
    comment moved it. The head commit's own date is checked later, from git."""
    updated = _when(rec.updated_at)
    if rec.state != "open" or rec.draft or updated is None:
        return None
    if not acted and now - updated < timedelta(days=QUIET_DAYS):
        return None
    reviewed = rec.greptile_reviewed_sha
    if reviewed and reviewed != rec.head_sha:
        return "unreviewed-push"
    if (reviewed == rec.head_sha and rec.ci != "failing"
            and review_policy.own_bar(rec, reviewers.GREPTILE).status == reviewers.PASS):
        return "greptile-bar"
    return None


def _when(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


# The replayed commits stay in the local cache, so their identity is a placeholder.
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "backlog-sweep",
            "GIT_AUTHOR_EMAIL": "backlog-sweep@localhost", "GIT_COMMITTER_NAME": "backlog-sweep",
            "GIT_COMMITTER_EMAIL": "backlog-sweep@localhost"}


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(REPO_DIR), *args], capture_output=True, text=True,
                          env={**os.environ, **_GIT_ENV}, timeout=600)


def _base_ref() -> str:
    return f"refs/heads/{settings.default_branch()}"


def fetch(prs: list[int]) -> None:
    """Fetch the base branch and every PR's head into REPO_DIR, read-only."""
    if not REPO_DIR.exists():
        REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", str(REPO_DIR)], check=True)
    url = f"https://github.com/{settings.repo()}.git"
    refspecs = [f"+{_base_ref()}:{_base_ref()}"] + [f"+refs/pull/{n}/head:refs/pr/{n}" for n in prs]
    for i in range(0, len(refspecs), FETCH_CHUNK):
        r = _git("fetch", "-q", "--no-tags", url, *refspecs[i:i + FETCH_CHUNK])
        if r.returncode != 0:
            raise RuntimeError(f"git fetch failed: {r.stderr.strip()}")


def probe(n: int, head: str | None) -> Probe | str:
    """Local git's view of PR `n` at the stored `head`, or why it has none."""
    fetched = _git("rev-parse", "--verify", "-q", f"refs/pr/{n}").stdout.strip()
    if not fetched:
        return "its head was not fetched"
    if head is None or fetched != head:
        return "its head moved since the last ingest"
    base = _base_ref()
    span = f"{base}..{head}"
    current = _git("merge-base", "--is-ancestor", base, head).returncode == 0
    merges = _git("rev-list", "--merges", "--count", span).stdout.strip() not in ("", "0")
    merged = _git("merge-tree", "--write-tree", "--name-only", "--no-messages", base, head)
    if merged.returncode not in (0, 1):
        return f"git merge-tree failed: {merged.stderr.strip()}"
    conflicts = merged.stdout.split("\n")[1:] if merged.returncode == 1 else []
    rebase_clean = not merges and not conflicts and _replays(base, span)
    return Probe(head_date=_git("log", "-1", "--format=%cI", head).stdout.strip(),
                 current=current, merge_commits=merges, rebase_clean=rebase_clean,
                 conflicts=[p for p in conflicts if p],
                 changed=[p for p in _git("diff", "--name-only", f"{base}...{head}")
                          .stdout.split("\n") if p])


def _replays(base: str, span: str) -> bool:
    """Whether each commit in `span` cherry-picks onto `base` in turn without
    conflict, as a rebase would."""
    onto = _git("rev-parse", base).stdout.strip()
    for commit in _git("rev-list", "--reverse", span).stdout.split():
        picked = _git("merge-tree", "--write-tree", "--no-messages",
                      f"--merge-base={commit}^", onto, commit)
        if picked.returncode != 0:
            return False
        onto = _git("commit-tree", picked.stdout.split()[0], "-p", onto, "-m", "replay").stdout.strip()
    return True


def classify(rec: Pr, pr_probe: Probe) -> Plan:
    """The bucket local git and the autofix gates put PR `rec` in."""
    n = rec.number
    held = gates.stacked_block(rec) or ("a dependency bump" if gates.bump_exempt(rec) else None)
    if held:
        return Plan(n, "ineligible", held)
    ok, why = gates.fix_eligibility(rec, "rebase", pr_probe.changed)
    if not ok:
        return Plan(n, "ineligible", why)
    if pr_probe.current:
        return Plan(n, "skip", "already contains the base branch")
    if pr_probe.rebase_clean:
        return Plan(n, "clean", "rebases cleanly", action="rebase")
    if pr_probe.merge_commits:
        if not pr_probe.conflicts:
            return Plan(n, "clean", "merges the base in cleanly", action="update")
        return Plan(n, "author", "conflicts, and its merge commits keep the bot from rebasing it",
                    ask=True, conflicts=pr_probe.conflicts)
    paths = pr_probe.conflicts or pr_probe.changed
    ok, why = gates.fix_eligibility(rec, "resolve", paths)
    if not ok:
        return Plan(n, "author", f"an agent resolve is withheld: {why}", ask=True,
                    conflicts=pr_probe.conflicts)
    if len(pr_probe.conflicts) > RESOLVE_MAX_PATHS:
        return Plan(n, "author", f"conflicts in {len(pr_probe.conflicts)} files", ask=True,
                    conflicts=pr_probe.conflicts)
    return Plan(n, "resolvable", f"conflicts in {len(pr_probe.conflicts) or 'its own'} "
                                 f"file(s) an agent may resolve", action="rebase",
                conflicts=pr_probe.conflicts)


def ended_on_conflicts(req: dict[str, object], head: str | None) -> bool:
    """Whether the request at `head` ended without bringing the PR current
    because of its conflicts: a refusal naming them, a resolve an operator
    discarded, or a parked resolve the agents judged."""
    if req.get("against_head_sha") != head:
        return False
    result = req.get("result")
    result = result if isinstance(result, dict) else {}
    status, action = req.get("status"), req.get("action")
    if status == "refused" and action in ("rebase", "resolve"):
        return bool(result.get("conflict_paths")) or action == "resolve"
    if action == "resolve" and status == "cancelled":
        return not result.get("reclaimed")
    return (action == "resolve" and status == "awaiting-review"
            and handback.parked_judged(result))


def ended_on_the_machine(req: dict[str, object], head: str | None) -> bool:
    """Whether the request at `head` ended without a verdict on the PR: the
    worker failed, or refused it with no conflicts to name."""
    if req.get("against_head_sha") != head or ended_on_conflicts(req, head):
        return False
    return req.get("status") == "failed" or (
        req.get("status") == "refused" and req.get("action") in ("rebase", "update"))


def plan_one(rec: Pr, steps: dict[str, str], pr_probe: Probe | str) -> Plan:
    """PR `rec`'s next step, given the sweep `steps` already taken at its head
    and when each was taken."""
    n = rec.number
    req = rec.fix_request or {}
    if "closed" in steps:
        return Plan(n, "done", "closed after the author did not answer")
    if "asked" in steps:
        asked = _when(steps["asked"]) or datetime.now(timezone.utc)
        if datetime.now(timezone.utc) - asked < timedelta(days=CLOSE_AFTER_DAYS):
            return Plan(n, "waiting", f"the author was asked on {steps['asked'][:10]}")
        return Plan(n, "close", f"the author has not answered in {CLOSE_AFTER_DAYS} days",
                    close=True)
    if "queued" in steps:
        if ended_on_conflicts(req, rec.head_sha):
            paths = [str(p) for p in (req.get("result") or {}).get("conflict_paths") or []]
            return Plan(n, "author", "the queued request could not resolve its conflicts",
                        ask=True, conflicts=paths)
        if req.get("status") in fix_queue.IN_FLIGHT:
            return Plan(n, "waiting", f"its {req.get('action')} request is {req.get('status')}")
        if "requeued" not in steps and ended_on_the_machine(req, rec.head_sha):
            if isinstance(pr_probe, str):
                return Plan(n, "skip", pr_probe)
            plan = classify(rec, pr_probe)
            if plan.action is not None:
                plan.retry = True
                plan.why = f"its queued {req.get('action')} ended {req.get('status')}; once more"
            return plan
        return Plan(n, "done", f"its queued {req.get('action')} ended {req.get('status')}")
    if isinstance(pr_probe, str):
        return Plan(n, "skip", pr_probe)
    if datetime.now(timezone.utc) - (_when(pr_probe.head_date) or datetime.now(timezone.utc)) \
            < timedelta(days=QUIET_DAYS):
        return Plan(n, "skip", "its head commit is recent")
    if req.get("status") in fix_queue.IN_FLIGHT:
        return Plan(n, "waiting", f"a {req.get('action')} request is already {req.get('status')}")
    return classify(rec, pr_probe)


def _steps_by_pr(prs: list[int] | None = None) -> dict[int, dict[str, dict[str, str]]]:
    """The sweep steps already taken (on `prs`, or on every PR), as
    {pr: {head_sha: {step: when}}}. One row per action, and each head is acted
    on at most four times, so this stays as small as the backlog."""
    out: dict[int, dict[str, dict[str, str]]] = {}
    for run in data.store().runs(phase=PHASE, prs=prs):
        if not isinstance(run, storekit.PhaseRun) or not isinstance(run.raw.get("pr"), int):
            continue
        stats = run.raw.get("stats") or {}
        out.setdefault(int(run.raw["pr"]), {}).setdefault(
            str(stats.get("head_sha")), {})[str(stats.get("step"))] = str(run.finished)
    return out


def plans() -> list[Plan]:
    """The sweep's plan for every PR in the population, the most recently
    active first: their authors are likeliest to answer, and their drift the
    smallest."""
    now = datetime.now(timezone.utc)
    taken = _steps_by_pr()
    members: list[tuple[Pr, Kind]] = []
    for rec in data.prs().values():
        kind = population_kind(rec, now, acted=str(rec.head_sha) in taken.get(rec.number, {}))
        if kind is not None:
            members.append((rec, kind))
    members.sort(key=lambda m: m[0].updated_at or "", reverse=True)
    fetch([rec.number for rec, _ in members])
    out: list[Plan] = []
    for rec, kind in members:
        steps = taken.get(rec.number, {}).get(str(rec.head_sha), {})
        plan = plan_one(rec, steps, probe(rec.number, rec.head_sha))
        plan.kind, plan.quiet_since = kind, rec.updated_at
        out.append(plan)
    return out


def ask_rebase(rec: Pr, conflicts: list[str]) -> dict[str, object]:
    """Ask `rec`'s author, as the bot, to rebase its head, once per head, and
    stamp the ask on the PR (`rebase_ask`) for `gates.rebase_feedback`."""
    head = str(rec.head_sha or "")
    res = executor.hand_back_comment(rec.number, handback.rebase_ask_body(rec.author, conflicts, head),
                                     marker=handback.rebase_ask_marker(head), dry_run=False)
    if (res.get("status") in ("executed", "exists")
            and (rec.section("rebase_ask") or {}).get("head_sha") != head):
        data.store().set_pr_section(rec.number, "rebase_ask",
                                    {"head_sha": head, "at": storekit.now(),
                                     "url": res.get("event_url")})
    return res


def act(plan: Plan, rec: Pr) -> str:
    """Carry out `plan` live and record it in the ledger. Returns the outcome."""
    head = str(rec.head_sha or "")
    if plan.close:
        live = handback.live_state(rec.number)
        if live is None or live["state"] != "open" or live["head"] != head:
            return "not closed: GitHub did not answer, or the PR changed"
        asked = _when(_steps_by_pr([rec.number]).get(rec.number, {}).get(head, {}).get("asked"))
        if asked is None or (live["last_active"] is not None and live["last_active"] > asked):
            return "not closed: the author was active after the ask; a person decides"
        res = executor.execute_pr(
            rec.number, models.CloseAction(action="CLOSE_WONTFIX",
                                           comment=handback.rebase_close_body(rec.author),
                                           reason="backlog sweep: no answer to the rebase ask"),
            dry_run=False)
        status = str(res.get("status"))
        if status != "executed":
            return f"close failed: {res.get('detail') or status}"
        step = "closed"
    elif plan.ask:
        res = ask_rebase(rec, plan.conflicts)
        status = str(res.get("status"))
        if status not in ("executed", "exists"):
            return f"ask failed: {res.get('detail')}"
        step = "asked"
    else:
        assert plan.action is not None
        try:
            fix_queue.queue_pr(rec.number, plan.action)
        except ValueError as e:
            return f"not queued: {e}"
        status = step = "requeued" if plan.retry else "queued"
    now = storekit.now()
    data.store().append_run({"phase": PHASE, "pr": rec.number, "started": now, "finished": now,
                             "trigger": "operator",
                             "stats": {"step": step, "bucket": plan.bucket, "action": plan.action,
                                       "head_sha": head, "status": status}})
    return step if status != "exists" else "already asked"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="prospector backlog-sweep",
        description="Bring the stale, author-finished PR backlog current with the base branch, "
                    "or ask its authors to rebase. Dry run unless --live.")
    p.add_argument("--live", action="store_true",
                   help="queue requests, post asks and close unanswered PRs")
    p.add_argument("--limit", type=int, default=0,
                   help="with --live, the most PRs to act on in this run (required)")
    p.add_argument("--json", help="also write every PR's plan to this file")
    args = p.parse_args(argv)
    if args.live and args.limit <= 0:
        p.error("--live needs --limit N, the most PRs to act on in this run")
    every = plans()
    counts: dict[str, int] = {}
    for plan in every:
        counts[plan.bucket] = counts.get(plan.bucket, 0) + 1
    if args.json:
        with open(args.json, "w") as f:
            json.dump([asdict(plan) for plan in every], f, indent=1)
    if not args.live:
        for plan in every:
            print(f"#{plan.pr:<6} {plan.bucket:<10} {plan.action or ('ask' if plan.ask else 'close' if plan.close else '-'):<7} "
                  f"{plan.kind:<15} quiet since {(plan.quiet_since or '?')[:10]}  {plan.why}")
        print(json.dumps({"population": len(every), **counts}))
        return 0
    left = args.limit
    prs = data.prs()
    for plan in every:
        if left <= 0:
            break
        if plan.action is None and not plan.ask and not plan.close:
            continue
        outcome = act(plan, prs[plan.pr])
        print(f"#{plan.pr} {plan.bucket}: {outcome}")
        left -= outcome in ("queued", "requeued", "asked", "closed")
    return 0
