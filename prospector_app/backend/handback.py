"""Handing a hunted autofix back to its PR's author: the ONE policy for what
GitHub hears when the automation could not clear its own work.

A request the hunter queued whose change the agents judged and turned down — a
reviewer's rejection, a continuation that declined, a compile, lint or
related-tests failure, an agent that would not guess at a conflict — ends
`refused`, and the author is asked to take it over: one bot comment per head
naming what is wrong. A PR whose author is not a maintainer and has not pushed,
commented or reviewed on it in STALE_DAYS is closed with a note instead. A
refusal on policy (risk tier 0, a withheld path) or a machine fault is never
handed back; those park or retry for an operator.

`TRIAGE_FIX_HANDBACK` (`settings.fix_handback`) is `live`, `dry-run` (each write
is logged to Activity as a dry-run and none is made) or `off`.

    uv run python -m prospector_app.backend.handback

lists what a live hand-back would do with every request it covers now.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pipeline import gates, gh, settings, storekit
from prospector_app.backend import executor, models

if TYPE_CHECKING:
    from pipeline.model import Pr

STALE_DAYS = 30

# What a hand-back asks of the author: conflicts with the base, a build the
# updated branch fails, or review findings the agent could not fix.
KINDS = ("conflicts", "build", "fix")

# Sandbox exits that are the harness's fault, never the change's.
_HARNESS_EXITS = (gates.SENTINEL_PATCH_CONFLICT, gates.SENTINEL_PATCH_UNREADABLE,
                  gates.SENTINEL_NO_PLAN, gates.SENTINEL_SETUP_FAIL)

# A resolve bar stamped with no `judged` field is read by its reason.
_JUDGED_REASONS = ("the behavior-lens reviewer", "the history-lens reviewer",
                   "the continuation agent declined", "the continuation changed nothing",
                   "the continuation was not trusted", "the continuation did not clear",
                   "the compile preflight did not clear",
                   "the related-tests sandbox run did not pass")


def marker(head: str) -> str:
    return f"<!-- prospector:handback:{head[:12]} -->"


def evidence_judged(stamp: dict) -> bool:
    """Whether a resolve auto-review stamp holds a judgment against the change:
    a reviewer's considered rejection, a compile that failed, or related tests
    that ran and failed."""
    if any(r.get("verdict") != "safe" and not r.get("failed")
           for r in stamp.get("reviews") or [] if isinstance(r, dict)):
        return True
    if ((stamp.get("compile_preflight") or {}).get("exit") == gates.SENTINEL_TEST_FAIL):
        return True
    run = (stamp.get("tests") or {}).get("run") or {}
    exit_code = run.get("exit")
    return (not (run.get("error") or run.get("refused")) and exit_code is not None
            and exit_code != gates.SENTINEL_PASS and exit_code not in _HARNESS_EXITS)


def parked_judged(result: dict) -> bool:
    """Whether a parked resolve's stamped bar is a judgment the author should
    hear, rather than a policy hold or a machine fault an operator should."""
    bar = (result.get("auto_review") or {}).get("bar") or {}
    if bar.get("ok"):
        return False
    if "judged" in bar:
        return bool(bar["judged"])
    return str(bar.get("reason") or "").startswith(_JUDGED_REASONS)


def covered(req: dict) -> bool:
    """Whether a request is the hunter's to hand back. An operator's request
    stays with the operator, and so does a fix from the CI the bot itself
    broke."""
    from prospector_app.backend import fix_queue
    return (req.get("source") in fix_queue.AUTO_SOURCES
            and (req.get("objection") or {}).get("kind") != "ci")


def active(req: dict) -> bool:
    return settings.fix_handback() != "off" and covered(req)


_ACTIVITY_QUERY = """
query {
  repository(owner: "%s", name: "%s") {
    pullRequest(number: %d) {
      state headRefOid createdAt author { login }
      commits(last: 50) { nodes { commit { committedDate authors(first: 5) { nodes { user { login } } } } } }
      comments(last: 100) { nodes { createdAt author { login } } }
      reviews(last: 50) { nodes { submittedAt author { login } } }
    }
  }
}"""


def _when(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def live_state(n: int) -> dict | None:
    """The PR's live {state, head, author, last_active} — last_active the
    newest of its opening and its author's own commits, comments and reviews —
    or None when GitHub did not answer."""
    try:
        data = gh.gh_graphql_data(_ACTIVITY_QUERY % (settings.repo_owner(), settings.repo_name(),
                                                     int(n)), timeout=30)
    except RuntimeError:
        return None
    pr = ((data.get("repository") or {}).get("pullRequest")) or {}
    author = (pr.get("author") or {}).get("login")
    times = [_when(pr.get("createdAt"))]
    for node in (pr.get("commits") or {}).get("nodes") or []:
        commit = node.get("commit") or {}
        logins = {((a.get("user") or {}).get("login"))
                  for a in (commit.get("authors") or {}).get("nodes") or []}
        if author in logins:
            times.append(_when(commit.get("committedDate")))
    for kind, stamp in (("comments", "createdAt"), ("reviews", "submittedAt")):
        for node in (pr.get(kind) or {}).get("nodes") or []:
            if (node.get("author") or {}).get("login") == author:
                times.append(_when(node.get(stamp)))
    known = [t for t in times if t is not None]
    return {"state": str(pr.get("state") or "").lower(), "head": pr.get("headRefOid"),
            "author": author, "last_active": max(known) if known else None}


def stale(rec: Pr, last_active: datetime | None, now: datetime | None = None) -> bool:
    """Whether a handed-back PR closes instead of waiting on its author: a
    non-maintainer silent on it for STALE_DAYS. An unknown last activity
    never closes."""
    if last_active is None or gates.priority_author(rec.author, rec.author_association):
        return False
    return (now or datetime.now(timezone.utc)) - last_active > timedelta(days=STALE_DAYS)


def _problem(kind: str, base: str, paths: list[str]) -> str:
    if kind == "conflicts":
        shown = "".join(f"\n- `{p}`" for p in paths[:10])
        more = f"\n- …and {len(paths) - 10} more" if len(paths) > 10 else ""
        return f"merge conflicts with `{base}`" + (f" in:{shown}{more}" if shown else ".")
    if kind == "build":
        return f"a build failure once it is brought up to date with `{base}`."
    return "issues raised in automated review (see the review comments above)."


def comment_body(kind: str, author: str | None, paths: list[str], head: str) -> str:
    base = settings.default_branch()
    who = f"@{author} " if author else ""
    problem = _problem(kind, base, paths)
    ask = (f"Could you merge or rebase onto `{base}` and push?" if kind == "conflicts"
           else f"Could you update onto `{base}` and fix the build?" if kind == "build"
           else "Could you look at the review feedback and push an update?")
    return (f"Hi {who}— this PR has {problem}\n\n"
            f"Our automation tried to fix this but couldn't produce a change it trusts, "
            f"so it needs you. {ask} Once you push, it's picked up again automatically.\n\n"
            f"{marker(head)}")


def close_body(kind: str, author: str | None, paths: list[str]) -> str:
    base = settings.default_branch()
    who = f"@{author}" if author else "there"
    return (f"Hi {who}, thanks for this PR. It has {_problem(kind, base, paths)}\n\n"
            f"Our automation couldn't fix that on its own, and there hasn't been activity "
            f"from you here in over {STALE_DAYS} days, so we're closing it rather than "
            f"leaving it open with no path forward. If you're still interested, bring the "
            f"branch up to date with `{base}` and comment here or open a new PR — we'd be "
            f"glad to take another look.")


def plan(rec: Pr, head: str) -> dict:
    """What a hand-back of PR `rec` pinned at `head` does now: `close`,
    `comment`, or `skip` with the reason, read from the live PR."""
    live = live_state(rec.number)
    if live is None:
        return {"do": "skip", "why": "GitHub did not answer"}
    if live["state"] != "open":
        return {"do": "skip", "why": f"the PR is {live['state']}"}
    if head and live["head"] != head:
        return {"do": "skip", "why": "the author pushed since; the hunter tries the new head"}
    last = live["last_active"]
    return {"do": "close" if stale(rec, last) else "comment", "author": live["author"],
            "last_active": last.isoformat() if last else None}


def deliver(rec: Pr, req: dict, kind: str, paths: list[str]) -> dict:
    """Hand PR `rec`'s refused request `req` back to its author on GitHub, as
    the bot, under `settings.fix_handback()`. Returns the outcome the request
    records: what was planned and the executor's status."""
    head = str(req.get("against_head_sha") or rec.head_sha or "")
    step = plan(rec, head)
    outcome = {**step, "kind": kind, "head_sha": head, "at": storekit.now(),
               "mode": settings.fix_handback()}
    if step["do"] == "skip":
        return outcome
    dry_run = settings.fix_handback() != "live"
    if step["do"] == "close":
        res = executor.execute_pr(
            rec.number, models.CloseAction(action="CLOSE_STALE",
                                           comment=close_body(kind, step["author"], paths),
                                           reason="autofix hand-back: author silent"),
            dry_run=dry_run)
    else:
        res = executor.hand_back_comment(rec.number,
                                         comment_body(kind, step["author"], paths, head),
                                         marker=marker(head), dry_run=dry_run)
    return {**outcome, "status": res.get("status"), "detail": res.get("detail")}


def summary(outcome: dict) -> str:
    """One line for the refusal reason naming what the hand-back did."""
    if outcome.get("do") == "skip":
        return f"Not handed back: {outcome.get('why')}."
    verb = "Closed as stale" if outcome.get("do") == "close" else "Handed back to the author"
    status = outcome.get("status")
    if status == "dry-run":
        return f"{verb} (dry-run)."
    if status == "error":
        return f"{verb} failed: {outcome.get('detail')}."
    return f"{verb}."


def candidates() -> list[tuple[Pr, dict, str, list[str]]]:
    """Every request a live hand-back covers now: a hunted parked resolve
    whose bar is a judgment, and a hunted refusal at the current head whose
    hand-back was only a dry-run. Each with its kind and paths."""
    from prospector_app.backend import data
    out: list[tuple[Pr, dict, str, list[str]]] = []
    for rec in data.prs().values():
        req = rec.fix_request or {}
        if rec.state != "open" or not covered(req):
            continue
        result = req.get("result") or {}
        paths = [str(p) for p in result.get("conflict_paths") or []]
        if (req.get("status") == "awaiting-review" and req.get("action") == "resolve"
                and parked_judged(result)):
            out.append((rec, req, "conflicts", paths))
            continue
        prior = result.get("handed_back") or {}
        if (req.get("status") == "refused" and prior.get("status") == "dry-run"
                and prior.get("head_sha") == rec.head_sha):
            out.append((rec, req, str(prior.get("kind") or "conflicts"), paths))
    return out


def main() -> None:
    rows = candidates()
    closes = comments = 0
    for rec, req, kind, _ in sorted(rows, key=lambda r: r[0].number):
        step = plan(rec, str(req.get("against_head_sha") or rec.head_sha or ""))
        closes += step["do"] == "close"
        comments += step["do"] == "comment"
        maint = " maintainer" if gates.priority_author(rec.author, rec.author_association) else ""
        print(f"#{rec.number:<6} {step['do']:<8} {kind:<9} @{rec.author}{maint} "
              f"last active {(step.get('last_active') or '?')[:10]}  {step.get('why') or ''}")
    print(f"\n{len(rows)} covered: {closes} close, {comments} comment, "
          f"{len(rows) - closes - comments} skip. Mode: {settings.fix_handback()}.")


if __name__ == "__main__":
    main()
