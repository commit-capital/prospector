"""The ONE policy for closing an issue with no operator: one the fixability
triage (`fixability`) judges `not-in-repo`, `external` or `already-fixed`.

`run` closes at most BATCH issues a pass and DAILY_CAP a UTC day. An issue
qualifies (`candidate`) while it is open, in the public loop's scope
(`public_loop.in_scope`), not a maintainer's (`gates.priority_author`), with no
fix request, attempt or chosen pull request, and never closed by Prospector
before. Its current triage is a `skip` of one of CLOSE_KINDS. Before the close
the triage's word is checked (`evidence`). An `already-fixed` issue needs a
test the host runs, passing twice on the pinned default branch and read as
checking what the report asks (`fixed_test`). A `not-in-repo` or `external`
issue needs a second, independent read on the reviewer model that also finds
the code is not here or the cause outside. A check that fails is recorded
against the triage as `close_check`, so the issue waits for a new triage. One
that holds is recorded too, so a failed write retries without re-running it.
A check or write that fails for a reason not the issue's (a sandbox, GitHub)
is retried up to MAX_FAILS times on one triage, and a live pass that cannot
mint the bot's token closes nothing.

The executor (`executor.auto_close_issue`) re-checks GitHub live, posts
`public_comments.closed_out` and closes the issue as the bot, Activity-logged:
`not planned` for a report about code that is not here, `completed` for one
already fixed. The comment asks the author to reply if Prospector is wrong.
GitHub stops an outsider reopening an issue a bot closed, so `watch` reads the
replies on the issues it closed, once each is updated (`public_loop.refresh`
re-ingests them). The author's reply marks the close `disputed`, which the
Issues page reads as `close-disputed` for an operator to settle. An issue
someone reopens is noted and never closed by Prospector again.

Both follow `settings.issue_fix_public` per issue (`public_loop.issue_mode`):
`off` closes nothing, and `dry-run` records under `fix_public.dry` and notes
each close on the issue's thread without writing.
"""
from __future__ import annotations

import traceback
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pipeline import bot_token
from issue_triage import fixability, fixed_test, public_comments, public_loop
from pipeline import gates, gh, headless_agent, prove, settings, storekit, verify_driver

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue
    from issue_triage.issue_store import IssueStore

CLOSE_KINDS = ("not-in-repo", "external", "already-fixed")
# The GitHub state_reason each kind closes with.
REASONS = {"not-in-repo": "not_planned", "external": "not_planned",
           "already-fixed": "completed"}
BATCH = 2
DAILY_CAP = 10
# Checks or writes that may fail, for reasons not the issue's, on one triage.
MAX_FAILS = 3
# Longer than a check takes (`fixed_test`'s agents and two sandbox runs).
LEASE = timedelta(hours=1)
KEY = "closed-out"


def _note(store: IssueStore, n: int, text: str) -> None:
    store.edit_issue(n).append_fix_thread({"at": storekit.now(), "by": "close-out",
                                           "kind": "note", "text": text})


def _stamp(issue: Issue) -> str:
    """The issue's triage, named for `close_check` by when it was judged and
    the content it was judged against."""
    tri = issue.section("fix_triage") or {}
    return f"{tri.get('checked_at')} {tri.get('against_updated_at')}"


def candidate(issue: Issue, mode: str) -> str | None:
    """The triage kind issue `issue` may be closed as under `mode`, or None."""
    if (issue.state != "open" or not public_loop.in_scope(issue)
            or gates.priority_author(issue.author, issue.author_association)
            or issue.fix_request or issue.fix_run or issue.fix_choice
            or issue.fix_public.get("closed")
            or public_loop.mode_view(issue.fix_public, mode).get("closed")):
        return None
    tri = fixability.current(issue)
    if not tri or tri.get("verdict") != "skip" or tri.get("kind") not in CLOSE_KINDS:
        return None
    check = public_loop.mode_view(issue.fix_public, mode).get("close_check") or {}
    if check.get("triage") == _stamp(issue) and (
            ("ok" in check and not check["ok"]) or int(check.get("fails") or 0) >= MAX_FAILS):
        return None
    return tri["kind"]


def evidence(issue: Issue, kind: str, base: prove.PinnedBase) -> dict:
    """Whether the triage's `kind` holds up for issue `issue` over `base`:
    {"ok", "why", ...}. An agent outage or a declined prompt, and a sandbox
    that cannot run, propagate."""
    title, body = issue.title or "", issue.body or ""
    if kind == "already-fixed":
        proof = fixed_test.prove_fixed(
            issue.number, base, title=title, body=body,
            workdir=verify_driver.SCRATCH / "issue-close" / str(issue.number),
            label=f"close-{issue.number}")
        return {"ok": proof["status"] == "shown", "why": proof["why"], "tests": proof["paths"],
                "asserts": proof["asserts"], "base_sha": base.sha}
    try:
        second = fixability.review(base.clone, title=title, body=body,
                                   model=settings.issue_fix_review_model())
    except (headless_agent.AgentUnavailable, headless_agent.AgentDeclined):
        raise
    except (RuntimeError, ValueError) as e:
        return {"ok": False, "why": f"the second read did not finish: {e}"}
    agrees = second["verdict"] == "skip" and second["kind"] in ("not-in-repo", "external")
    return {"ok": agrees, "base_sha": base.sha,
            "why": f"a second read finds {second['verdict']} / {second['kind']}: "
                   f"{second['reason'][:300]}"}


def _failed(store: IssueStore, n: int, mode: str, check: dict, why: str) -> None:
    """Count a check or write that failed for a reason not the issue's; past
    MAX_FAILS on one triage the issue waits for a new one."""
    fails = int(check.get("fails") or 0) + 1
    public_loop.record_view(store, n, mode, close_check={**check, "fails": fails,
                                                         "last_error": why[:300]})
    _note(store, n, f"Closing this failed ({why[:300]}); "
          + ("leaving it until it is judged again." if fails >= MAX_FAILS else "retrying later."))


def close_one(store: IssueStore, issue: Issue, *, mode: str, base: prove.PinnedBase,
              now: datetime) -> bool:
    """Check and close issue `issue` under `mode`. Whether it acted."""
    from prospector_app.backend import executor

    n = issue.number
    leased = public_loop.take_lease(store, n, settings.worker_id(), now, LEASE)
    if leased is None:
        return False
    try:
        kind = candidate(leased, mode)
        tri = fixability.current(leased)
        if kind is None or tri is None:
            return False
        stamp = _stamp(leased)
        check = public_loop.mode_view(leased.fix_public, mode).get("close_check") or {}
        if check.get("triage") != stamp:
            check = {"triage": stamp}
        if not check.get("ok"):
            try:
                found = evidence(leased, kind, base)
            except headless_agent.AgentUnavailable:
                raise
            except headless_agent.AgentDeclined as e:
                found = {"ok": False, "why": f"the agent declined: {e}"}
            except Exception as e:
                _failed(store, n, mode, check, f"the check could not run: {e}")
                return True
            check = {**check, **found, "at": storekit.now()}
            public_loop.record_view(store, n, mode, close_check=check)
            if not check["ok"]:
                _note(store, n, f"Did not close this as {kind}: {check['why']}")
                return True
        body = public_comments.closed_out(n, KEY, kind, tri.get("reason"),
                                          list(check.get("tests") or []))
        problems = public_comments.problems(body)
        if problems:
            public_loop.record_view(store, n, mode, close_check={**check, "ok": False,
                                                                 "why": "; ".join(problems)})
            _note(store, n, f"Did not post the close comment: {'; '.join(problems)}.")
            return True
        res = executor.auto_close_issue(n, kind=kind, reason=REASONS[kind], body=body,
                                        marker=public_comments.marker(n, KEY), dry_run=mode != "live")
        status = res.get("status")
        if status == "executed" or (status == "dry-run" and mode != "live"):
            public_loop.record_view(store, n, mode, closed={
                "kind": kind, "at": storekit.now(), "triage": stamp,
                "evidence": {k: check.get(k) for k in ("why", "tests", "asserts", "base_sha")
                             if check.get(k)}})
            _note(store, n, f"Dry run: would close this as {kind}." if status == "dry-run"
                  else f"Closed this as {kind}: {check['why']}")
        elif status == "blocked":
            public_loop.record_view(store, n, mode, close_check={
                **check, "ok": False, "why": str(res.get("detail") or "blocked")})
            _note(store, n, f"Did not close this as {kind}: {res.get('detail')}.")
        else:
            _failed(store, n, mode, check, str(res.get("detail") or status))
        return True
    finally:
        public_loop.release(store, n)


def run(store: IssueStore, issues: dict[int, Issue], *, may_run: Callable[[], bool],
        mode: str | None = None, now: datetime | None = None) -> list[int]:
    """Check and close up to BATCH of `issues`, the newest first, within
    DAILY_CAP a UTC day for each of live and dry-run, each while `may_run` says
    the AI capacity is open. A live pass that cannot mint the bot's token
    closes nothing. The issues acted on. An agent outage propagates."""
    from pipeline.store import Store

    mode = mode or settings.issue_fix_public()
    if mode == "off":
        return []
    picks = [(n, i, m) for n, i in sorted(issues.items(), reverse=True)
             if candidate(i, m := public_loop.issue_mode(i, mode))]
    if not picks:
        return []
    midnight = storekit.utc_midnight().isoformat(timespec="seconds")
    room = {m: DAILY_CAP - store.closed_out_since(midnight, dry=m == "dry-run")
            for m in {m for *_, m in picks}}
    kept = []
    for pick in picks:
        room[pick[2]] -= 1
        if room[pick[2]] >= 0:
            kept.append(pick)
    picks = kept[:BATCH]
    if any(m == "live" for *_, m in picks) and not bot_token.available():
        picks = [p for p in picks if p[2] != "live"]
    if not picks:
        return []
    try:
        base = prove.pinned(Store())
    except prove.NoBase:
        return []
    acted = []
    for n, issue, m in picks:
        if not may_run():
            break
        try:
            if close_one(store, issue, mode=m, base=base, now=now or datetime.now(timezone.utc)):
                acted.append(n)
        except headless_agent.AgentUnavailable:
            raise
        except Exception:
            traceback.print_exc()
    return acted


def watch(store: IssueStore) -> int:
    """Read the replies on each issue Prospector closed since it was last
    updated: the author's marks the close `disputed`, and a reopened issue
    is noted. How many issues it acted on."""
    if settings.issue_fix_public() == "off":
        return 0
    acted = 0
    for issue in store.fix_issues().values():
        closed = issue.fix_public.get("closed")
        if (not closed or closed.get("disputed") or closed.get("reopened")
                or (closed.get("replies_read_at") or "") >= (issue.updated_at or "~")):
            continue
        n = issue.number
        if issue.state == "open":
            live = gh.gh_json(f"repos/{settings.repo()}/issues/{n}")
            if live is None or live.get("state") != "open":
                continue
            public_loop.record_view(store, n, "live", closed={**closed, "reopened": storekit.now()})
            _note(store, n, "This issue was reopened after Prospector closed it, and is not "
                            "closed by Prospector again.")
            acted += 1
            continue
        at = storekit.parse_ts(closed.get("at"))
        replies = public_loop.read_replies(issue, at) if at else []
        if replies is None:
            continue
        mark = {**closed, "replies_read_at": issue.updated_at or ""}
        authors = [r for r in replies if not r.maintainer]
        if authors:
            mark["disputed"] = {"at": authors[-1].at, "by": authors[-1].login,
                                "reply": authors[-1].body[:1000]}
            _note(store, n, f"{authors[-1].login} replied after Prospector closed this as "
                            f"{closed.get('kind')}; it needs an operator to decide.")
        elif replies:
            _note(store, n, "A maintainer replied after Prospector closed this.")
        public_loop.record_view(store, n, "live", closed=mark)
        acted += bool(replies)
    return acted
