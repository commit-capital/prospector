"""Condensed on-PR activity history — comments, reviews (each automated
reviewer's flagged), commits, and reopen/close/force-push/rename events — read live from
GitHub via one batched GraphQL query per PR. Powers the PR-detail history
panel so a reviewer can follow the back-and-forth without leaving the app."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from pipeline import gh
from pipeline import reviewers
from pipeline import settings
from pipeline import storekit
from prospector_app.backend import activity
from prospector_app.backend import data
from prospector_app.backend import pr_checks

if TYPE_CHECKING:
    from pipeline.model import Pr

SNIPPET_LEN = 200
_TIMELINE_TYPES = ("REOPENED_EVENT, CLOSED_EVENT, MERGED_EVENT, HEAD_REF_FORCE_PUSHED_EVENT, "
                   "RENAMED_TITLE_EVENT")

HistoryKind = Literal["comment", "review", "bot_review", "commit", "reopened", "closed", "merged",
                      "force_push", "renamed", "prospector_action"]


class Via(TypedDict):
    """The Prospector activity-log event behind a history entry."""
    verb: str                 # activity.verb: "re-triggered Greptile", "rebased", …
    operator: str | None      # the person who initiated it
    initiator: str | None     # "worker" when the automation acted on its own
    reason: str | None        # a close's reason (duplicate / stale / …)
    detail: str | None


class HistoryItem(TypedDict):
    kind: HistoryKind  # prospector_action: an activity event GitHub shows no entry for
    at: str | None  # always non-None on the list fetch_pr_history returns — items without one are dropped
    actor: str | None
    summary: str | None
    url: str | None
    state: str | None           # review state (approved/changes requested/…), review kinds only
    reviewer: str | None        # registry reviewer id, bot_review kind only
    score: int | None           # Greptile's confidence N/5, bot_review kind only
    sha: str | None             # the commit's oid, commit kind only
    via: Via | None             # set when Prospector made this entry


def _item(kind: HistoryKind, at: str | None, actor: str | None, summary: str | None,
          url: str | None = None, *, state: str | None = None, reviewer: str | None = None,
          score: int | None = None, sha: str | None = None) -> HistoryItem:
    return {"kind": kind, "at": at, "actor": actor, "summary": summary, "url": url,
            "state": state, "reviewer": reviewer, "score": score, "sha": sha, "via": None}


def _snippet(body: str | None) -> str | None:
    if not body:
        return None
    s = " ".join(body.split())
    return s[:SNIPPET_LEN] + ("…" if len(s) > SNIPPET_LEN else "")


def _query(n: int) -> str:
    fields = (
        "comments(last: 50) { nodes { author { login } createdAt url bodyText } } "
        "reviews(last: 50) { nodes { author { login } state submittedAt url bodyText } } "
        "commits(last: 100) { nodes { commit { oid committedDate message author { name } } } } "
        f"timelineItems(last: 50, itemTypes: [{_TIMELINE_TYPES}]) {{ nodes {{ __typename "
        "... on ReopenedEvent { createdAt actor { login } } "
        "... on ClosedEvent { createdAt actor { login } } "
        "... on MergedEvent { createdAt actor { login } } "
        "... on HeadRefForcePushedEvent { createdAt actor { login } } "
        "... on RenamedTitleEvent { createdAt actor { login } currentTitle previousTitle } "
        "} }"
    )
    return (f'query {{ repository(owner: "{settings.repo_owner()}", name: "{settings.repo_name()}") {{ '
            f"pullRequest(number: {int(n)}) {{ {fields} }} }} }}")


def _fetch_node(n: int) -> dict[str, Any] | None:
    body = gh.gh_graphql(_query(n), timeout=60)
    repo = ((body or {}).get("data") or {}).get("repository") or {}
    node = repo.get("pullRequest")
    return node if isinstance(node, dict) else None


def _comment_items(node: dict[str, Any]) -> list[HistoryItem]:
    out: list[HistoryItem] = []
    for c in ((node.get("comments") or {}).get("nodes")) or []:
        out.append(_item("comment", c.get("createdAt"), (c.get("author") or {}).get("login"),
                         _snippet(c.get("bodyText")), c.get("url")))
    return out


def _review_items(node: dict[str, Any]) -> list[HistoryItem]:
    out: list[HistoryItem] = []
    for r in ((node.get("reviews") or {}).get("nodes")) or []:
        login = (r.get("author") or {}).get("login")
        bot = reviewers.by_login(login)
        score = reviewers.parse_confidence_score(r.get("bodyText")) if bot is reviewers.GREPTILE else None
        state = (r.get("state") or "").replace("_", " ").lower() or None
        out.append(_item("bot_review" if bot is not None else "review", r.get("submittedAt"),
                         login, _snippet(r.get("bodyText")), r.get("url"), state=state,
                         reviewer=bot.id if bot is not None else None, score=score))
    return out


def _commit_items(node: dict[str, Any]) -> list[HistoryItem]:
    out: list[HistoryItem] = []
    for c in ((node.get("commits") or {}).get("nodes")) or []:
        commit = c.get("commit") or {}
        oid = commit.get("oid")
        message = (commit.get("message") or "").split("\n", 1)[0].strip() or None
        out.append(_item("commit", commit.get("committedDate"),
                         (commit.get("author") or {}).get("name"), message,
                         f"https://github.com/{settings.repo()}/commit/{oid}" if oid else None,
                         sha=oid))
    return out


def _timeline_items(node: dict[str, Any]) -> list[HistoryItem]:
    verb: dict[str, tuple[HistoryKind, str]] = {
        "ReopenedEvent": ("reopened", "reopened this PR"),
        "ClosedEvent": ("closed", "closed this PR"),
        "MergedEvent": ("merged", "merged this PR"),
        "HeadRefForcePushedEvent": ("force_push", "force-pushed the branch"),
    }
    out: list[HistoryItem] = []
    for t in ((node.get("timelineItems") or {}).get("nodes")) or []:
        typ = (t or {}).get("__typename")
        actor = (t.get("actor") or {}).get("login")
        if typ in verb:
            kind, summary = verb[typ]
            out.append(_item(kind, t.get("createdAt"), actor, summary))
        elif typ == "RenamedTitleEvent":
            prev, cur = t.get("previousTitle"), t.get("currentTitle")
            summary = f'renamed "{prev}" → "{cur}"' if prev and cur else "renamed this PR"
            out.append(_item("renamed", t.get("createdAt"), actor, summary))
    return out


def fetch_pr_history(n: int) -> list[HistoryItem]:
    """This PR's condensed activity history — comments, reviews (Greptile's
    scored ones flagged with their confidence score), commits, and
    reopen/close/force-push/rename events — oldest first. Empty when the PR
    can't be fetched from GitHub."""
    node = _fetch_node(n)
    if not node:
        return []
    items = (_comment_items(node) + _review_items(node)
             + _commit_items(node) + _timeline_items(node))
    items = [it for it in items if it["at"]]
    items.sort(key=lambda it: it["at"] or "")
    return items


# The GitHub entry kinds each activity kind appears as. A close posts its
# comment and then closes, so one event can tag one entry of each kind.
_SHOWS_AS: dict[str, tuple[HistoryKind, ...]] = {
    "close": ("comment", "closed"),
    "reopen": ("reopened",),
    "merge": ("merged",),
    "comment": ("comment",),
    "review-retrigger": ("comment",),
    "review": ("review",),
    "line-comment": ("review",),
    "resubmit": ("commit", "force_push"),
}
# How far a GitHub entry's time may sit from its activity event's.
MATCH_WINDOW_S = 600


def _is_bot(login: str | None) -> bool:
    bot = settings.bot_login().removesuffix("[bot]")
    return bool(bot) and login is not None and login.removesuffix("[bot]") == bot


def _via(ev: dict) -> Via:
    return {"verb": ev.get("verb") or str(ev.get("kind") or ""), "operator": ev.get("operator"),
            "initiator": ev.get("initiator"), "reason": ev.get("reason"),
            "detail": ev.get("detail")}


def _gap(a: str | None, b: str | None) -> float | None:
    ta, tb = storekit.parse_ts(a), storekit.parse_ts(b)
    return abs((ta - tb).total_seconds()) if ta and tb else None


def _match(items: list[HistoryItem], ev: dict, taken: set[int]) -> list[int]:
    """The indexes of the untaken `items` that `ev` made: the entry at its
    captured `event_url`, a push's head commit by SHA, and otherwise the
    nearest entry of each kind it shows as, within MATCH_WINDOW_S and — for
    bot writes — authored by the bot."""
    hits: list[int] = []
    url = ev.get("event_url")
    if url:
        hits += [i for i, it in enumerate(items) if i not in taken and it["url"] == url][:1]
    for kind in _SHOWS_AS.get(str(ev.get("kind")), ()):
        if any(items[i]["kind"] == kind for i in hits):
            continue
        best: tuple[float, int] | None = None
        for i, it in enumerate(items):
            if i in taken or i in hits or it["kind"] != kind:
                continue
            if kind == "commit":
                if it["sha"] and it["sha"] == ev.get("to_sha"):
                    best = (0.0, i)
                continue
            gap = _gap(it["at"], ev.get("at"))
            if gap is None or gap > MATCH_WINDOW_S or (kind != "force_push" and not _is_bot(it["actor"])):
                continue
            if best is None or gap < best[0]:
                best = (gap, i)
        if best is not None:
            hits.append(best[1])
    return hits


def tag_prospector(items: list[HistoryItem], events: list[dict]) -> list[HistoryItem]:
    """`items` (GitHub's side) with each entry Prospector made tagged `via` its
    activity event (`activity.for_pr` rows, oldest first), plus one
    prospector_action entry for each event GitHub shows no entry for — a
    description rewrite, a CI re-run. Oldest first."""
    out = [HistoryItem(**it) for it in items]
    taken: set[int] = set()
    extra: list[HistoryItem] = []
    for ev in events:
        hits = _match(out, ev, taken)
        for i in hits:
            out[i]["via"] = _via(ev)
        taken.update(hits)
        if not hits and ev.get("at"):
            row = _item("prospector_action", ev.get("at"), ev.get("identity") or settings.bot_login(),
                        None, ev.get("event_url"))
            row["via"] = _via(ev)
            extra.append(row)
    return sorted(out + extra, key=_order)


def _order(it: HistoryItem) -> datetime:
    return storekit.parse_ts(it["at"]) or datetime.min.replace(tzinfo=timezone.utc)


Tone = Literal["pass", "warn", "fail", "na"]


class Fact(TypedDict):
    label: str
    tone: Tone


class CommitSummary(TypedDict):
    """What the pipeline concluded about one commit, placed after that
    commit's stretch of the timeline."""
    kind: Literal["prospector"]
    sha: str
    head: bool              # the PR's current head
    facts: list[Fact]
    unrecorded: list[str]   # stages that keep no per-commit record of an older commit


TimelineItem = HistoryItem | CommitSummary

# Stages whose result the store holds for the latest judged commit only and
# whose ledger rows name no head, so an older commit's result is unknown.
_HEAD_ONLY = {"security": "security review", "verify": "validation run", "ci": "CI"}


# The checks a summary always states; any other check is stated only when it
# is not passing.
_CORE_CHECKS = ("security", "verify", "review", "ci")
_SHORT = {"mergeable": "Merge conflicts", "tests": "No tests"}


def _check_fact(row: dict) -> Fact | None:
    """A check row as one short fact — its verdict without the row's advice —
    or None for a passing check outside _CORE_CHECKS."""
    key, parts = row["key"], str(row["detail"]).split(" · ")
    if key not in _CORE_CHECKS and row["status"] == "pass":
        return None
    if key == "review":  # one part per reviewer
        label = " · ".join(parts)
    elif key == "security":
        found = parts[1] if len(parts) > 1 and not parts[1].startswith("0 ") else None
        label = f"Security review {parts[0]}" + (f" · {found}" if found else "")
    elif key == "verify":
        label = f"Validation: {parts[0].split(' — ')[0]}"
    elif key == "ci":
        label = f"CI {parts[0]}"
    elif key == "drift":
        label = f"{parts[0].capitalize()} on {settings.default_branch()}"
    else:
        label = _SHORT.get(key) or f"{row['name']}: {parts[0]}"
    return {"label": label, "tone": row["status"]}


def _ledger_fact(run: storekit.PhaseRun) -> Fact | None:
    """The fact a per-PR runs-ledger row states about the head it names."""
    stats = run.raw.get("stats") or {}
    if run.phase == "design-review:run":
        if stats.get("error"):
            return {"label": "Design review failed", "tone": "warn"}
        counts = stats.get("findings") or {}
        found = ", ".join(f"{n} {sev}" for sev, n in counts.items() if n)
        return {"label": f"Design review: {found or 'no findings'}", "tone": "warn" if found else "pass"}
    if run.phase == "fix:single":
        status = str(stats.get("status") or "")
        tone: Tone = "pass" if status == "pushed" else "fail" if status == "failed" else "na"
        return {"label": f"Fix ({stats.get('action') or 'fix'}): {status or 'recorded'}", "tone": tone}
    if run.phase == "threat-evidence:capture":
        return {"label": f"Threat evidence {run.raw.get('status') or 'captured'}", "tone": "na"}
    if run.phase == "threat-scan:clear":
        return {"label": f"Flagged key cleared by {run.raw.get('by') or 'an operator'}", "tone": "pass"}
    return None


def _run_head(run: storekit.PhaseRun) -> str | None:
    stats = run.raw.get("stats")
    head = (stats.get("head_sha") if isinstance(stats, dict) else None) or run.raw.get("head_sha")
    return head if isinstance(head, str) else None


_DISPOSITION_LABEL = {"merge": "Recommends merge", "request-changes": "Changes requested",
                      "needs-human": "Needs a human"}


def commit_summaries(items: list[HistoryItem], rec: Pr | None,
                     runs: list[storekit.RunRecord]) -> list[TimelineItem]:
    """`items` with one CommitSummary after each commit's stretch (before the
    next commit, or the head's before a merge or close): the store's current
    check rows grouped by the commit each judged, its analysis for that
    commit, and the runs-ledger rows about this PR naming that head. An older
    commit with nothing recorded gets no row; the head always gets one."""
    if rec is None:
        return list(items)
    facts: dict[str, list[Fact]] = {}
    keys: dict[str, set[str]] = {}
    for sha, rows in pr_checks.checks_by_commit(rec).items():
        facts[sha] = [f for r in rows if (f := _check_fact(r))]
        keys[sha] = {r["key"] for r in rows}
    analysis = rec.section("analysis")
    if analysis and analysis.get("against_head_sha") and rec.disposition:
        d = rec.disposition
        facts.setdefault(analysis["against_head_sha"], []).append(
            {"label": _DISPOSITION_LABEL.get(d, f"Recommends {d}"),
             "tone": "pass" if d == "merge" else "warn"})
    for run in runs:
        if not isinstance(run, storekit.PhaseRun) or run.raw.get("pr") != rec.n:
            continue
        head, fact = _run_head(run), _ledger_fact(run)
        if head and fact and not (run.phase == "design-review:run" and "design" in keys.get(head, set())):
            facts.setdefault(head, []).append(fact)

    out: list[TimelineItem] = []
    pending: CommitSummary | None = None
    for it in items:
        ends = it["kind"] == "commit" or (pending is not None and pending["head"]
                                          and it["kind"] in ("merged", "closed"))
        if pending is not None and ends:
            out.append(pending)
            pending = None
        out.append(it)
        sha = it["sha"]
        if it["kind"] == "commit" and sha and (sha in facts or sha == rec.head_sha):
            head = sha == rec.head_sha
            pending = {"kind": "prospector", "sha": sha, "head": head, "facts": facts.get(sha, []),
                       "unrecorded": [] if head else [
                           label for key, label in _HEAD_ONLY.items() if key not in keys.get(sha, set())]}
    if pending is not None:
        out.append(pending)
    return out


def timeline(n: int) -> list[TimelineItem]:
    """PR `n`'s single timeline, oldest first: GitHub's history with
    Prospector's own writes tagged, its activity events GitHub shows nothing
    for, and a Prospector summary after each commit."""
    items = tag_prospector(fetch_pr_history(n), list(reversed(activity.for_pr(n))))
    return commit_summaries(items, data.prs().get(int(n)), data.runs())
