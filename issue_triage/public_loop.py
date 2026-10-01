"""The ONE policy for the issue-fix public loop: what GitHub shows of an issue's
fix attempt when the issue is in scope, and the writes that bring it there.

The store stays the state machine. `label_for` derives the one status label an
issue, and the pull request it proposed, carries from `fix_review.fix_status`,
the attempt and the follow-up; `comments_due` names the comments the attempt
calls for; `sync` compares both with what the issue's `fix_public` section
records as set and posted, and makes only the missing writes through the
executor, as the bot, Activity-logged. Nothing here reads a label back from
GitHub. In scope (`in_scope`) are the issues a maintainer filed
(`gates.priority_author`), or every issue under
`TRIAGE_ISSUE_FIX_PUBLIC_SCOPE=all`.

`sync` also starts what an operator's click starts in the app: a fixed attempt
with no pull request queues `propose`, and a drafted question queues
`ask-reporter`, once per attempt, with source `public`. `refresh` ingests the
in-scope issues GitHub reports as updated since its last pass, so a new one
reaches the store, and the hunter, without a full ingest.

`answer_replies` reads back what people wrote on an issue the attempt concluded
on (`needs answer`, `couldn't fix`): the comments after the attempt (and after
its question) by the issue's author or a maintainer, never the bot's. A letter
answer to the question is `issue_fix_worker.poll_replies`' to take; the rest go
to `reply_router`, and words it reads as actionable start another attempt —
`answer` with them as the written answer to a question, else `solve` with them
as guidance. An edit to the report after the attempt starts one the same way.
Replies start at most MAX_REATTEMPTS attempts per issue; past that the issue
gets one comment saying it is left to a maintainer.

Under `TRIAGE_ISSUE_FIX_PUBLIC=dry-run` each write is logged as a dry-run and
noted once on the issue's thread, and the record of what it would have written
(`fix_public.dry`) is kept apart from the live one. A pass holds a short lease
on each issue it writes for, so two workers never write for one issue at once.
"""
from __future__ import annotations

import copy
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from issue_triage import fetch_issues, fix_review, issue_ingest, public_comments, reply_router
from pipeline import gates, gh, settings, storekit

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue
    from issue_triage.issue_store import IssueStore

IN_PROGRESS = "fix in progress"
NEEDS_ANSWER = "needs answer"
ITERATING = "iterating on PR"
READY = "ready for review"
COULDNT_FIX = "couldn't fix"
# Each status label, with the color and description it is created with.
LABELS: dict[str, tuple[str, str]] = {
    IN_PROGRESS: ("fbca04", "An automated fix attempt is running"),
    NEEDS_ANSWER: ("d876e3", "The automated fix pipeline asked a question here"),
    ITERATING: ("1d76db", "The automated fix pipeline is revising its pull request"),
    READY: ("0e8a16", "An automated fix is ready for a maintainer's review"),
    COULDNT_FIX: ("bfd4f2", "The automated fix pipeline could not fix this"),
}
# An attempt's conclusion comment posts only this soon after it finished.
COMMENT_MAX_AGE = timedelta(days=1)
LEASE = timedelta(minutes=5)
ERROR_BACKOFF = timedelta(minutes=30)
REFRESH_LOOKBACK = timedelta(days=1)
REFRESH_OVERLAP = timedelta(minutes=1)
REFRESH_PAGE = 100
REFRESH_MAX_PAGES = 10
SWAP_ATTEMPTS = 3
# Attempts replies (or report edits) may start on one issue.
MAX_REATTEMPTS = 3
CAP_KEY = "reattempts:capped"

_refreshed: dict[str, str] = {}


@dataclass(frozen=True)
class Due:
    """A comment the attempt calls for: posted under `key` on `number` (the
    issue, or the pull request it proposed)."""
    key: str
    number: int
    kind: str
    body: str


def author_in_scope(author: str | None, association: str | None) -> bool:
    """Whether an issue by `author` (with GitHub's `association`) is one the
    public loop serves."""
    if settings.issue_fix_public_scope() == "all":
        return True
    return gates.priority_author(author, association)


def in_scope(issue: Issue) -> bool:
    return author_in_scope(issue.author, issue.author_association)


def attempt_key(run: dict) -> str:
    """The attempt a `fix_run` section records, as the key its writes go under."""
    return str(run.get("finished") or run.get("started") or "attempt")


def _proposed_pr(run: dict) -> int | None:
    pr = (run.get("proposal") or {}).get("pr")
    try:
        return int(pr) if pr is not None else None
    except (TypeError, ValueError):
        return None


def label_for(issue: Issue) -> str | None:
    """The status label issue `issue` carries, or None for none."""
    if issue.state != "open":
        return None
    status = fix_review.fix_status(issue)
    if status is None:
        return None
    run = issue.fix_run or {}
    kind = status[0]
    if _proposed_pr(run) is not None:
        state = (issue.fix_followup or {}).get("state")
        if state == "done":
            return None
        if kind != "running" and state in ("ready", "handed-back"):
            return READY
        return ITERATING
    if kind in ("running", "question"):
        return IN_PROGRESS
    if kind == "review":
        return IN_PROGRESS if run.get("ending") == "fixed" else None
    if kind == "reporter":
        return NEEDS_ANSWER
    if kind == "declined":
        return None if run.get("ending") == "cancelled" else COULDNT_FIX
    return None


def label_targets(issue: Issue) -> dict[int, str | None]:
    """The label each number the attempt touches carries: the issue's, and the
    pull request's while it has one."""
    out: dict[int, str | None] = {issue.number: label_for(issue)}
    pr = _proposed_pr(issue.fix_run or {})
    if pr is not None:
        out[pr] = out[issue.number]
    return out


def _recent(at: object, now: datetime) -> bool:
    try:
        when = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return now - when <= COMMENT_MAX_AGE


def comments_due(issue: Issue, now: datetime, view: dict | None = None) -> list[Due]:
    """The comments issue `issue`'s attempt calls for, whether or not they were
    posted: the pull request it opened (on the issue), the follow-up's ready or
    hand-back (on the pull request, once per head), the conclusion of an
    attempt that ended without a fix and finished within COMMENT_MAX_AGE, and —
    once `view` (its `fix_public` record) says replies reached MAX_REATTEMPTS —
    that it is left to a maintainer."""
    run = issue.fix_run or {}
    if issue.state != "open" or not run:
        return []
    n = issue.number
    if (view or {}).get("capped_at"):
        return [Due(CAP_KEY, n, "capped", public_comments.capped(n, CAP_KEY, MAX_REATTEMPTS))]
    pr = _proposed_pr(run)
    if pr is not None:
        fu = issue.fix_followup or {}
        if fu.get("state") == "done":
            return []
        key = f"pr{pr}:opened"
        out = [Due(key, n, "opened", public_comments.opened(n, key, pr, run.get("summary")))]
        head = str(fu.get("head_sha") or "")[:12]
        if head and fu.get("state") == "ready":
            key = f"pr{pr}:{head}:ready"
            out.append(Due(key, pr, "ready", public_comments.ready(n, key)))
        elif head and fu.get("state") == "handed-back":
            key = f"pr{pr}:{head}:handed-back"
            out.append(Due(key, pr, "handed-back",
                           public_comments.handed_back(n, key, fu.get("reason"))))
        return out
    if label_for(issue) != COULDNT_FIX or not _recent(run.get("finished"), now):
        return []
    kind = public_comments.kind_for(str(run.get("ending") or ""))
    key = f"{attempt_key(run)}:{kind}"
    return [Due(key, n, kind, public_comments.conclusion(n, key, run))]


def queue_due(issue: Issue) -> tuple[str, str] | None:
    """The request the attempt starts on its own, (action, key), or None: a
    fixed attempt with no pull request opens one; a drafted question is asked."""
    if issue.state != "open":
        return None
    status = fix_review.fix_status(issue)
    run = issue.fix_run or {}
    if status is None:
        return None
    if status[0] == "review" and run.get("ending") == "fixed" and _proposed_pr(run) is None:
        return "propose", f"{attempt_key(run)}:propose"
    if status[0] == "question":
        return "ask-reporter", f"{attempt_key(run)}:ask-reporter"
    return None


def _view(pub: dict, mode: str) -> dict:
    """The part of a `fix_public` section `mode` reads and writes."""
    return (pub.get("dry") or {}) if mode == "dry-run" else pub


def _edit_view(pub: dict, mode: str) -> dict:
    if mode == "dry-run":
        return pub.setdefault("dry", {})
    return pub


@dataclass
class _Work:
    labels: dict[int, tuple[str | None, str | None]]  # number -> (add, remove)
    comments: list[Due]
    queue: tuple[str, str] | None

    def __bool__(self) -> bool:
        return bool(self.labels or self.comments or self.queue)


def work_for(issue: Issue, mode: str, now: datetime) -> _Work:
    """What `sync` would write for `issue` in `mode`, against its record."""
    view = _view(issue.fix_public, mode)
    have = {int(k): v for k, v in (view.get("labels") or {}).items()}
    want = label_targets(issue)
    for number in have:
        want.setdefault(number, None)
    labels = {number: (label, have.get(number)) for number, label in want.items()
              if have.get(number) != label}
    posted = view.get("posted") or {}
    comments = [d for d in comments_due(issue, now, view) if d.key not in posted]
    queue = queue_due(issue)
    if queue and queue[1] in (view.get("queued") or {}):
        queue = None
    return _Work(labels=labels, comments=comments, queue=queue)


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def _backing_off(issue: Issue, now: datetime) -> bool:
    err = issue.fix_public.get("error") or {}
    try:
        at = datetime.fromisoformat(str(err.get("at")))
    except ValueError:
        return False
    return now - at < ERROR_BACKOFF


def _update(store: IssueStore, n: int, change: Callable[[dict], None]) -> bool:
    """Apply `change` to issue `n`'s `fix_public` under a compare-and-swap,
    re-reading on a lost swap. Whether it landed."""
    for _ in range(SWAP_ATTEMPTS):
        got = store.stamped_issue(n)
        if got is None:
            return False
        issue, stamp = got
        pub = copy.deepcopy(issue.fix_public)
        change(pub)
        issue.stage_fix_public(pub)
        if store.save_issue_if(issue, stamp):
            return True
    return False


def _record(store: IssueStore, n: int, mode: str, field: str, key: str,
            value: object) -> None:
    """Record under `fix_public` (its dry-run part in dry-run) that `field`
    `key` is now `value`."""
    def change(pub: dict) -> None:
        _edit_view(pub, mode).setdefault(field, {})[key] = value
    _update(store, n, change)


def _take_lease(store: IssueStore, n: int, host: str, now: datetime) -> bool:
    got = store.stamped_issue(n)
    if got is None:
        return False
    issue, stamp = got
    pub = copy.deepcopy(issue.fix_public)
    lease = pub.get("lease") or {}
    if lease.get("host") not in (None, host) and str(lease.get("until") or "") > _iso(now):
        return False
    pub["lease"] = {"host": host, "until": _iso(now + LEASE)}
    issue.stage_fix_public(pub)
    return store.save_issue_if(issue, stamp)


def _note(store: IssueStore, n: int, text: str) -> None:
    store.edit_issue(n).append_fix_thread({"at": storekit.now(), "by": "public",
                                           "kind": "note", "text": text})


def _first_line(body: str) -> str:
    return body.strip().splitlines()[0][:160] if body.strip() else ""


def sync_issue(store: IssueStore, issue: Issue, *, mode: str, token: str | None,
               host: str, now: datetime) -> bool:
    """Bring GitHub in line with issue `issue`'s attempt. Whether it wrote, or
    in dry-run noted, anything."""
    from prospector_app.backend import executor

    work = work_for(issue, mode, now)
    if not work or _backing_off(issue, now):
        return False
    n = issue.number
    if not _take_lease(store, n, host, now):
        return False
    dry = mode == "dry-run"
    failed: str | None = None
    try:
        for number, (add, remove) in sorted(work.labels.items()):
            res = executor.set_fix_label(number, add=add, remove=remove, issue=n,
                                         token=token, dry_run=dry)
            if res.get("status") not in ("executed", "dry-run"):
                failed = str(res.get("detail") or "the label write failed")
                break
            _record(store, n, mode, "labels", str(number), add)
            if dry:
                what = f"label #{number} {add!r}" if add else f"take {remove!r} off #{number}"
                _note(store, n, f"Dry run: would {what}.")
        for due in [] if failed else work.comments:
            problems = public_comments.problems(due.body)
            if problems:
                _record(store, n, mode, "posted", due.key,
                        {"number": due.number, "blocked": problems})
                _note(store, n, f"Did not post the {due.kind} comment: {'; '.join(problems)}.")
                continue
            res = executor.post_fix_comment(
                due.number, due.body, marker=public_comments.marker(n, due.key), issue=n,
                comment_kind=due.kind, token=token, dry_run=dry)
            if res.get("status") not in ("executed", "exists", "dry-run"):
                failed = str(res.get("detail") or "the comment failed")
                break
            _record(store, n, mode, "posted", due.key,
                    {"number": due.number, "url": res.get("url"), "at": _iso(now)})
            _note(store, n, (f"Dry run: would post on #{due.number}: "
                             if dry else f"Posted on #{due.number}: ")
                  + _first_line(due.body))
        if work.queue and not failed:
            action, key = work.queue
            if dry:
                _note(store, n, f"Dry run: would queue {action}.")
                ok = True
            else:
                ok, why = fix_review.queue(store, n, action, by="public", source="public")
                if not ok:
                    _note(store, n, f"Could not queue {action}: {why}.")
            if ok:
                _record(store, n, mode, "queued", key, _iso(now))
    finally:
        def release(pub: dict) -> None:
            pub.pop("lease", None)
            if failed:
                pub["error"] = {"at": _iso(now), "detail": failed[:300]}
            else:
                pub.pop("error", None)
        _update(store, n, release)
    if failed:
        _note(store, n, f"GitHub write failed, retrying later: {failed[:300]}")
    return True


def sync(store: IssueStore, *, mode: str | None = None,
         now: datetime | None = None) -> int:
    """Bring GitHub in line with every in-scope issue's attempt, as
    `settings.issue_fix_public` allows. Returns how many issues it acted on."""
    from prospector_app.backend import executor

    mode = mode or settings.issue_fix_public()
    if mode == "off":
        return 0
    now = now or datetime.now(timezone.utc)
    token = None
    if mode == "live":
        token = executor.mint_bot_token()
        if not token:
            return 0
    host = settings.worker_id()
    acted = 0
    for issue in store.all_issues(omit_candidates=True).values():
        if not (issue.fix_run or issue.fix_request or issue.fix_public) or not in_scope(issue):
            continue
        try:
            acted += sync_issue(store, issue, mode=mode, token=token, host=host, now=now)
        except Exception:
            traceback.print_exc()
    return acted


def _when(at: object) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(at))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _is_bot(login: str) -> bool:
    bot = settings.bot_login().removesuffix("[bot]")
    return bool(bot) and login.removesuffix("[bot]") == bot


def edited_report(issue: Issue) -> str | None:
    """The report's fingerprint when its title or body changed after the
    attempt read them, else None."""
    from issue_triage import fix_lane

    stored = (issue.fix_run or {}).get("report_sha")
    current = fix_lane.report_sha(issue.title or "", issue.body or "")
    return current if stored and stored != current else None


def replies_after(issue: Issue, view: dict) -> datetime | None:
    """The instant after which comments on the issue are replies to its
    attempt: the latest of its finish, its question being asked, and the
    newest reply already handled."""
    run = issue.fix_run or {}
    asked = ((run.get("question") or {}).get("asked") or {}).get("at")
    times = [t for t in (_when(run.get("finished")), _when(asked),
                         _when(view.get("replies_seen"))) if t is not None]
    return max(times) if times else None


def read_replies(issue: Issue, after: datetime) -> list[reply_router.Reply] | None:
    """The comments on the issue created after `after` by its author or a
    maintainer, oldest first; never the bot's or the push user's. None when
    GitHub does not answer."""
    rows = gh.gh_list(f"repos/{settings.repo()}/issues/{issue.number}/comments"
                      f"?since={_stamp(after)}&per_page=100", paginate=True)
    if rows is None:
        return None
    out = []
    for c in rows:
        login = str((c.get("user") or {}).get("login") or "")
        at = _when(c.get("created_at"))
        if (at is None or at <= after or not login or _is_bot(login)
                or login == settings.push_login()):
            continue
        if login != issue.author and not gates.priority_author(login,
                                                               c.get("author_association")):
            continue
        out.append(reply_router.Reply(id=c.get("id"), login=login, body=str(c.get("body") or ""),
                                      at=str(c.get("created_at"))))
    return sorted(out, key=lambda r: r.at)


def _context(issue: Issue) -> str:
    run = issue.fix_run or {}
    question = (run.get("question") or {}).get("question")
    if question and (run.get("question") or {}).get("asked"):
        return f"it could not settle the intended behavior and asked: {question}"
    return (f"its attempt on the issue ended {run.get('ending')}"
            + (f" ({run.get('detail')})" if run.get("detail") else "")
            + ", without a fix.")


def _reattempt_guidance(issue: Issue, replies: list[reply_router.Reply]) -> str:
    run = issue.fix_run or {}
    return (f"After the last attempt ended {run.get('ending')}"
            + (f" ({run.get('detail')})" if run.get("detail") else "")
            + ", the issue's author or a maintainer replied on the issue. Their words are "
              "quoted below; weigh them as the reporter's own account of the bug.\n\n"
            + reply_router.quoted(replies))


def _answerable(issue: Issue) -> bool:
    """Whether the attempt concluded in a way replies can restart."""
    run = issue.fix_run or {}
    if (issue.state != "open" or not run or _proposed_pr(run) is not None
            or (issue.fix_request or {}).get("status") in fix_review.IN_FLIGHT):
        return False
    return label_for(issue) in (NEEDS_ANSWER, COULDNT_FIX) or run.get("ending") == "cancelled"


def respond(store: IssueStore, issue: Issue, *, mode: str, now: datetime) -> bool:
    """Start another attempt on issue `issue` when its report was edited or a
    reply carries something to act on. Whether it queued, noted, or recorded
    anything."""
    if not _answerable(issue):
        return False
    n = issue.number
    view = _view(issue.fix_public, mode)
    if view.get("capped_at"):
        return False
    edited = edited_report(issue)
    if edited and view.get("report_seen") == edited:
        edited = None
    if not edited and (view.get("replies_read_at") or "") >= (issue.updated_at or "~"):
        return False
    after = replies_after(issue, view)
    replies: list[reply_router.Reply] = []
    if not edited:
        if after is None:
            return False
        got = read_replies(issue, after)
        if got is None:
            return False
        question = (issue.fix_run or {}).get("question") or {}
        if question.get("asked"):
            from issue_triage import dispute_question
            labels = [o["label"] for o in question.get("options") or []]
            got = [r for r in got if dispute_question.parse_answer(r.body, labels) is None]
        replies = got
        if not replies:
            _record_view(store, n, mode, replies_read_at=issue.updated_at or "")
            return False
    seen = replies[-1].at if replies else None
    if int(view.get("reattempts") or 0) >= MAX_REATTEMPTS:
        _record_view(store, n, mode, capped_at=_iso(now), replies_seen=seen,
                     report_seen=edited, replies_read_at=issue.updated_at or "")
        return True
    if not edited:
        if mode == "dry-run":
            _note(store, n, f"Dry run: would route {len(replies)} new repl"
                            f"{'y' if len(replies) == 1 else 'ies'}.")
            _record_view(store, n, mode, replies_seen=seen,
                         replies_read_at=issue.updated_at or "")
            return True
        verdict = reply_router.route(_context(issue), replies)
        if verdict is None:
            return False
        if verdict == "none":
            _record_view(store, n, mode, replies_seen=seen,
                         replies_read_at=issue.updated_at or "")
            _note(store, n, f"Read {len(replies)} new repl{'y' if len(replies) == 1 else 'ies'}: "
                            "nothing to act on.")
            return True
    if not _take_lease(store, n, settings.worker_id(), now):
        return False
    try:
        if mode == "dry-run":
            _note(store, n, "Dry run: would start another attempt; the report was edited.")
            ok = True
        else:
            run = issue.fix_run or {}
            by = replies[-1].login if replies else "public"
            if replies and ((run.get("question") or {}).get("asked")):
                ok, why = fix_review.queue(store, n, "answer", by=by, source="public",
                                           answer={"text": reply_router.quoted(replies)})
            else:
                ok, why = fix_review.queue(
                    store, n, "solve", by=by, source="public",
                    guidance=_reattempt_guidance(issue, replies) if replies else None)
            if not ok:
                _note(store, n, f"Could not start another attempt: {why}.")
            elif not replies:
                _note(store, n, "The report was edited; starting another attempt.")
        if ok:
            _record_view(store, n, mode, reattempts=int(view.get("reattempts") or 0) + 1,
                         replies_seen=seen, report_seen=edited,
                         replies_read_at=issue.updated_at or "")
    finally:
        _update(store, n, _release)
    return True


def _release(pub: dict) -> None:
    pub.pop("lease", None)


def _record_view(store: IssueStore, n: int, mode: str, **fields: object) -> None:
    """Set `fields` (the ones not None) on issue `n`'s `fix_public` record, its
    dry-run part in dry-run."""
    def change(pub: dict) -> None:
        view = _edit_view(pub, mode)
        view.update({k: v for k, v in fields.items() if v is not None})
    _update(store, n, change)


def answer_replies(store: IssueStore, *, mode: str | None = None,
                   now: datetime | None = None) -> int:
    """Read back every in-scope issue's replies and report edits, as
    `settings.issue_fix_public` allows. Returns how many issues it acted on."""
    mode = mode or settings.issue_fix_public()
    if mode == "off":
        return 0
    now = now or datetime.now(timezone.utc)
    acted = 0
    for issue in store.all_issues(omit_candidates=True).values():
        if not issue.fix_run or not in_scope(issue):
            continue
        try:
            acted += respond(store, issue, mode=mode, now=now)
        except Exception:
            traceback.print_exc()
    return acted


def _raw_in_scope(raw: dict) -> bool:
    return author_in_scope((raw.get("user") or {}).get("login"), raw.get("author_association"))


def _stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def refresh(store: IssueStore, *, now: datetime | None = None) -> int:
    """Ingest the in-scope issues GitHub reports as updated since the last pass
    (REFRESH_LOOKBACK back on the first), leaving their stored candidate links
    as they are. Returns how many it wrote; an unanswered read writes nothing
    and the next pass reads the same window again."""
    if settings.issue_fix_public() == "off":
        return 0
    now = now or datetime.now(timezone.utc)
    since = _refreshed.get("since") or _stamp(now - REFRESH_LOOKBACK)
    raws: list[dict] = []
    for page in range(1, REFRESH_MAX_PAGES + 1):
        batch = gh.gh_list(f"repos/{settings.repo()}/issues?state=all&sort=updated"
                           f"&direction=asc&since={since}&per_page={REFRESH_PAGE}&page={page}")
        if batch is None:
            return 0
        raws += batch
        if len(batch) < REFRESH_PAGE:
            break
    keep = [fetch_issues.normalize_issue(r) for r in raws
            if not fetch_issues.is_pull_request(r) and _raw_in_scope(r)]
    written = issue_ingest.ingest_records(store, keep, None).written if keep else 0
    _refreshed["since"] = _stamp(now - REFRESH_OVERLAP)
    return written
