"""Community response signals — how authors react after we triage their PR or
close their issue.

After we close / request-changes / comment as the configured bot, the author may
reply (argue back, ask why), reopen a PR we closed, or push new commits to fix
what we flagged. This module sweeps the PRs we've acted on and the issues we
closed, reads each one's upstream timeline *since our action*, and classifies
the response so the PR explorer can surface it as a sortable column +
filterable chips, and `author_returns` can follow the submissions an author
names in a comment.

Read-only upstream (one batched GraphQL request per ~20 items, the
freshness_live pattern). The classified signals are a derived cache — `scan`
rebuilds them from upstream — kept as each PR's and issue's `responses` section,
so one machine's sweep (`responses_sweep`) serves every app through the
snapshots. An operator's ack is human input that exists nowhere else, kept as
the PR's `response_ack` section, which the sweep never touches.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from prospector_app.backend import activity
from prospector_app.backend import author_returns
from prospector_app.backend import data
from prospector_app.backend import issue_data
from pipeline import gh
from pipeline import settings
from pipeline.storekit import now as _now
from pipeline.storekit import parse_ts as _dt

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue
    from pipeline.model import Pr

_log = logging.getLogger(__name__)

Kind = Literal["pr", "issue"]
KINDS: tuple[Kind, ...] = ("pr", "issue")

_CHUNK = 20
SNIPPET_LEN = 160

# Logins that are us, never a community response. Other automation is recognised
# from the actor's type rather than listed here (`_is_human`).
BOTS = {settings.bot_login()}


def _actor(event: dict, field: str) -> dict:
    """The actor behind a timeline event, as ``{login, __typename}``. GraphQL
    nests it under `field` (`author` on a comment, `actor` on a reopen); a flat
    ``{login: ...}`` event is read as its own actor."""
    nested = event.get(field)
    if isinstance(nested, dict):
        return nested
    return event if event.get("login") else {}


def _login(event: dict, field: str) -> str | None:
    return _actor(event, field).get("login")


def _is_human(actor: dict) -> bool:
    """Whether `actor` is a community member rather than us or other automation.

    GraphQL types every actor, so a GitHub App reads as `__typename` "Bot" —
    that is the check, and it holds for any app without naming one. A Bot's
    GraphQL login is bare (`coderabbitai`), while the REST `[bot]` suffix
    (`coderabbitai[bot]`) identifies the same account in payloads that carry no
    type, so both are recognised."""
    login = actor.get("login")
    if not login or login in BOTS:
        return False
    if actor.get("__typename") == "Bot":
        return False
    return not login.endswith("[bot]")


def _snippet(body: str | None) -> str | None:
    if not body:
        return None
    s = " ".join(body.split())
    return s[:SNIPPET_LEN] + ("…" if len(s) > SNIPPET_LEN else "")


def _mention_re(repo: str) -> re.Pattern[str]:
    """``#N`` or a PR/issue URL in `repo`; the number is group 1 or 2."""
    return re.compile(
        rf"(?<![\w/&])#(\d+)\b|github\.com/{re.escape(repo)}/(?:pull|issues)/(\d+)\b",
        re.IGNORECASE)


def classify(action_at: str, action_kind: str, *, state: str,
             comments: list[dict], reopens: list[dict], commit_dates: list[str],
             cross_refs: list[dict] | None = None,
             pr_author: str | None = None,
             bots: set[str] | None = None,
             repo: str | None = None) -> dict | None:
    """Pure classifier: given when/what we last did and a PR's timeline, return
    the response signals seen *after* our action, or None if there were none.

    - replied: a human issue comment after we acted (latest one's author/snippet kept)
    - reopened: a human reopen event after we acted, or state is open while our
      last action was a close (a state flip we may not have an event for)
    - new_commits: a commit pushed after we acted
    - resubmitted: a new PR in this repository, by the same author, cross-referenced
      on this closed PR after our action (author opened a fresh PR as a redo)
    - follow_ups: the PRs and issues the author opened after our action that
      cross-reference this one, and the numbers they name in their own comments
      since (not a signal by itself; such a comment is a reply)
    """
    _ = bots  # bot filtering is applied via _is_human (module-level BOTS)
    cut = _dt(action_at)
    if cut is None:
        return None

    # replied — newest human comment strictly after our action
    human = [c for c in comments
             if _is_human(_actor(c, "author")) and (_dt(c.get("createdAt")) or cut) > cut]
    human.sort(key=lambda c: c.get("createdAt") or "")
    latest = human[-1] if human else None
    replied = latest is not None
    reply_login = _login(latest, "author") if latest else None
    reply_at = latest.get("createdAt") if latest else None

    # reopened — a human reopen event after our action, or an unmistakable state flip
    reopen_after = [r for r in reopens
                    if _is_human(_actor(r, "actor")) and (_dt(r.get("createdAt")) or cut) > cut]
    reopen_at = max((d for r in reopen_after if (d := r.get("createdAt")) is not None), default=None)
    reopened = bool(reopen_after) or (state == "open" and action_kind in ("close", "issue-close"))

    # new_commits — any commit pushed after our action
    new_commit_dates = [d for d in commit_dates if (_dt(d) or cut) > cut]
    commit_at = max(new_commit_dates, default=None)
    new_commits = bool(new_commit_dates)

    # The author's own PRs and issues in this repository, opened after our action
    # and cross-referenced here since (GitHub records one for each mention in
    # their title, body or comments). A cross-ref from another repository (e.g.
    # the author's fork of an unrelated project) carries a bare, non-comparable
    # number and is never one of these.
    this_repo = repo or f"{settings.repo_owner()}/{settings.repo_name()}"
    own_refs = [
        (ref, source) for ref in cross_refs or []
        if pr_author and (_dt(ref.get("createdAt")) or cut) > cut
        and (source := ref.get("source") or {}).get("__typename") in ("PullRequest", "Issue")
        and (source.get("repository") or {}).get("nameWithOwner") == this_repo
        and (source.get("author") or {}).get("login") == pr_author
        and source.get("number") and (_dt(source.get("createdAt")) or cut) > cut]
    resubmission = next(((ref, source) for ref, source in own_refs
                         if source["__typename"] == "PullRequest"), None)
    resubmitted = resubmission is not None
    resubmitted_pr = int(resubmission[1]["number"]) if resubmission else None
    resubmitted_at = resubmission[0].get("createdAt") if resubmission else None

    # The author's comments since name a submission its own body may never link
    # ("opened a new PR at #N").
    follow_ups = sorted({int(source["number"]) for _, source in own_refs} | {
        int(m.group(1) or m.group(2))
        for c in human if pr_author and _login(c, "author") == pr_author
        for m in _mention_re(this_repo).finditer(c.get("bodyText") or "")})

    if not (replied or reopened or new_commits or resubmitted):
        return None

    last = max((t for t in (reply_at, reopen_at, commit_at, resubmitted_at) if t), default=None)
    return {
        "reopened": reopened,
        "new_commits": new_commits,
        "replied": replied,
        "resubmitted": resubmitted,
        "resubmitted_pr": resubmitted_pr,
        "follow_ups": follow_ups,
        "last_response_at": last,
        "snippet": _snippet(latest.get("bodyText")) if latest else None,
        "by": reply_login or (_login(reopen_after[-1], "actor") if reopen_after else None),
    }


def _acted_baseline(events: list[dict] | None = None) -> dict[Kind, dict[int, dict]]:
    """The cutoff a response is measured against, per kind: each PR's latest
    LANDED action — reopen too, we measure against whatever we last did — and
    each issue whose latest landed state action is our close, with who made it
    and a close's reason. A dry-run never counts (is_landed)."""
    events = activity.all_events() if events is None else events
    prs: dict[int, dict] = {}
    issues: dict[int, dict] = {}
    for ev in events:  # newest-first; first landed hit per item wins
        if not activity.is_landed(ev):
            continue
        kind = ev.get("kind")
        action = {"at": ev.get("at"), "kind": kind, "by": ev.get("operator")}
        if kind in ("issue-close", "issue-reopen") and ev.get("issue") is not None:
            issues.setdefault(int(ev["issue"]), {
                **action, "reason": activity.issue_close_reason(ev) if kind == "issue-close" else None})
        elif ev.get("pr") is not None:
            prs.setdefault(int(ev["pr"]), {**action, "reason": ev.get("reason")})
    return {"pr": prs,
            "issue": {n: b for n, b in issues.items() if b["kind"] == "issue-close"}}


# Every actor carries __typename so a GitHub App is recognised as a Bot: a
# Bot's login here is bare, with no REST-style `[bot]` suffix to read it off
# (`_is_human`).
_COMMON = ("number state "
           "author { login __typename } "
           "comments(last: 20) { nodes { author { login __typename } createdAt url bodyText } } "
           "timelineItems(last: 20, itemTypes: [REOPENED_EVENT, CROSS_REFERENCED_EVENT]) { "
           "nodes { __typename "
           "... on ReopenedEvent { createdAt actor { login __typename } } "
           "... on CrossReferencedEvent { createdAt "
           "source { __typename "
           "... on PullRequest { number createdAt author { login } "
           "repository { nameWithOwner } } "
           "... on Issue { number createdAt author { login } "
           "repository { nameWithOwner } } } } } } ")
_FIELDS: dict[Kind, tuple[str, str]] = {
    "pr": ("pullRequest", _COMMON + "headRefOid commits(last: 10) { nodes { commit { committedDate oid } } }"),
    "issue": ("issue", _COMMON),
}


def _query(numbers: list[int], kind: Kind = "pr") -> str:
    field, fields = _FIELDS[kind]
    aliases = " ".join(f"p{i}: {field}(number: {int(n)}) {{ {fields} }}"
                       for i, n in enumerate(numbers))
    return f'query {{ repository(owner: "{settings.repo_owner()}", name: "{settings.repo_name()}") {{ {aliases} }} }}'


def _span(chunk: list[int]) -> str:
    """A chunk named for a log line. Chunks follow the order we acted, not PR
    number, so the numbers are listed rather than shown as a range."""
    head = ", ".join(str(n) for n in chunk[:4])
    return head if len(chunk) <= 4 else f"{head}, +{len(chunk) - 4} more"


def _not_found_aliases(body: dict) -> set[str]:
    """The aliases GitHub reports as NOT_FOUND, read off the errors array's
    ``path`` (``["repository", "p3"]`` -> ``p3``). A NOT_FOUND alias names a PR
    that is gone from the repository, which is an answer about that PR — not a
    failure to reach it."""
    out: set[str] = set()
    for err in body.get("errors") or []:
        if not isinstance(err, dict) or err.get("type") != "NOT_FOUND":
            continue
        path = err.get("path") or []
        if path and isinstance(path[-1], str):
            out.add(path[-1])
    return out


def _fetch(prs: list[int], kind: Kind = "pr") -> tuple[dict[int, dict], list[int]]:
    """Batched live timeline node per PR (or issue) via GraphQL. Returns
    ``(nodes, failed)``.

    ``failed`` lists every PR this sweep could not read: the whole chunk when the
    call yields nothing usable (a `gh` timeout, or a response with no
    ``data.repository``; `gh.gh_graphql` logs why), and — when the response
    carries GraphQL errors — the PRs whose alias came back empty without a
    NOT_FOUND to explain it. `scan` leaves those PRs' stored signals
    untouched, so "couldn't check" is never recorded as "no response".

    One PR deleted from the repository puts an error in its whole chunk's
    response while GitHub still returns every other alias in the same body, so
    each alias is judged on its own: resolved is data, and NOT_FOUND is a PR
    that is gone — absent from both return values, the same answer as an empty
    alias in a clean response."""
    out: dict[int, dict] = {}
    failed: list[int] = []
    for i in range(0, len(prs), _CHUNK):
        chunk = prs[i:i + _CHUNK]
        body = gh.gh_graphql(_query(chunk, kind), timeout=90) or {}
        repo = (body.get("data") or {}).get("repository")
        # An empty `repository` is a real answer (its aliases resolved to
        # nothing); only its absence means the call yielded nothing to read.
        if not isinstance(repo, dict):
            _log.warning("responses._fetch: gh graphql returned no data for %d PRs (%s)",
                         len(chunk), _span(chunk))
            failed.extend(chunk)
            continue
        errors = body.get("errors") or []
        gone = _not_found_aliases(body)
        unread = [n for j, n in enumerate(chunk)
                  if not repo.get(f"p{j}") and f"p{j}" not in gone] if errors else []
        if errors:
            messages = "; ".join(str(e.get("message") or e.get("type") or e)
                                 for e in errors if isinstance(e, dict))
            _log.warning("responses._fetch: gh graphql reported errors for %d PRs (%s); "
                         "%d resolved, %d gone from the repo, %d unread: %s",
                         len(chunk), _span(chunk),
                         sum(1 for j in range(len(chunk)) if repo.get(f"p{j}")),
                         len(gone), len(unread), messages[:200])
        failed.extend(unread)
        for j, n in enumerate(chunk):
            node = repo.get(f"p{j}")
            if node:
                out[int(n)] = node
    return out, failed


def _node_signals(node: dict, base: dict) -> dict | None:
    timeline_nodes: list[dict] = ((node.get("timelineItems") or {}).get("nodes")) or []
    reopens = [n for n in timeline_nodes if (n or {}).get("__typename") == "ReopenedEvent"]
    cross_refs = [n for n in timeline_nodes if (n or {}).get("__typename") == "CrossReferencedEvent"]
    comments = ((node.get("comments") or {}).get("nodes")) or []
    commit_dates = [((c or {}).get("commit") or {}).get("committedDate")
                    for c in ((node.get("commits") or {}).get("nodes")) or []]
    commit_dates = [d for d in commit_dates if d]
    pr_author: str | None = (node.get("author") or {}).get("login") or None
    return classify(base["at"], base["kind"], state=(node.get("state") or "").lower(),
                    comments=comments, reopens=reopens, commit_dates=commit_dates,
                    cross_refs=cross_refs, pr_author=pr_author)


def _sweep(kind: Kind, baseline: dict[int, dict], targets: list[int],
           prior: dict[int, dict], now: str) -> tuple[dict[int, dict], list[int]]:
    """The fresh signals for `targets` of `kind`, and the ones that
    could not be read — each of those keeps its prior entry."""
    nodes, failed = _fetch(targets, kind)
    failed_set = set(failed)
    out: dict[int, dict] = {}
    for n in targets:
        if n in failed_set:
            if n in prior:
                out[n] = prior[n]
            continue
        node = nodes.get(n)
        if not node:
            continue
        sig = _node_signals(node, baseline[n])
        if not sig:
            continue
        sig[kind] = n
        sig["our_action"] = baseline[n]
        sig["detected_at"] = (prior.get(n) or {}).get("detected_at") or now
        out[n] = sig
    return out, sorted(failed_set)


def changed_since(since: str, baseline: dict[Kind, dict[int, dict]]) -> dict[Kind, list[int]]:
    """The swept items whose response may have moved since `since`: each one
    GitHub lists as updated, each we acted on since, and each whose author
    has a PR in that listing — a resubmission's cross-reference leaves the
    closed item's update time alone. Raises RuntimeError when GitHub does not
    answer the listing."""
    listed = gh.gh_list(f"repos/{settings.repo()}/issues?state=all&since={since}&per_page=100",
                        paginate=True)
    if listed is None:
        raise RuntimeError(f"GitHub did not list the items updated since {since}")
    updated: dict[Kind, set[int]] = {"pr": set(), "issue": set()}
    authors: set[str] = set()
    for raw in listed:
        kind: Kind = "pr" if "pull_request" in raw else "issue"
        updated[kind].add(int(raw["number"]))
        if kind == "pr" and (login := (raw.get("user") or {}).get("login")):
            authors.add(login.lower())
    cut = _dt(since)
    records: dict[Kind, Mapping[int, Pr] | Mapping[int, Issue]] = {
        "pr": data.prs(), "issue": issue_data.issues()}
    out: dict[Kind, list[int]] = {}
    for kind in KINDS:
        recs = records[kind]
        out[kind] = sorted(
            n for n, base in baseline[kind].items()
            if n in updated[kind]
            or ((at := _dt(base.get("at"))) is not None and cut is not None and at >= cut)
            or ((rec := recs.get(n)) is not None and (rec.author or "").lower() in authors))
    return out


def scan(prs: list[int] | None = None, issues: list[int] | None = None, *,
         since: str | None = None) -> dict:
    """Sweep the PRs we've acted on and the issues we closed, classify each
    one's response, and write each changed `responses` section. With `prs` or
    `issues`, sweep just those, and with `since` just the items
    `changed_since` names; either way every other section stays. Returns
    {checked, with_response, prs, issues, failed, failed_issues}.

    Every fetched item is recomputed from scratch (its section is current
    truth for it) but keeps its first `detected_at`; an item whose fetch failed
    (`failed`, `failed_issues`) keeps its section unchanged rather than
    treating "couldn't check" as "no response"."""
    baseline = _acted_baseline()
    data.refresh()
    issue_data.refresh()
    wanted: dict[Kind, list[int] | None] = (
        {"pr": prs, "issue": issues} if since is None
        else {**changed_since(since, baseline)})
    partial = any(names is not None for names in wanted.values())
    prior = recorded()
    now = _now()
    reg: dict[Kind, dict[int, dict]] = {"pr": {}, "issue": {}}
    failed: dict[Kind, list[int]] = {"pr": [], "issue": []}
    checked = 0
    for kind in KINDS:
        names = wanted[kind]
        if partial and names is None:
            reg[kind] = dict(prior[kind])
            continue
        base = baseline[kind]
        targets = [int(n) for n in (names if names is not None else base) if int(n) in base]
        checked += len(targets)
        fresh, failed[kind] = _sweep(kind, base, targets, prior[kind], now)
        kept = ({n: v for n, v in prior[kind].items() if n not in targets and n in base}
                if partial else {})
        reg[kind] = {**kept, **fresh}
    save_sections("responses", prior, reg)
    author_returns.record(reg)
    return {"checked": checked, "with_response": len(reg["pr"]) + len(reg["issue"]),
            "prs": sorted(reg["pr"]), "issues": sorted(reg["issue"]),
            "failed": failed["pr"], "failed_issues": failed["issue"]}


def _sections(recs: Mapping[int, Pr] | Mapping[int, Issue]) -> dict[int, dict]:
    return {n: sig for n, rec in recs.items() if (sig := rec.section("responses"))}


def recorded() -> dict[Kind, dict[int, dict]]:
    """Every stored response signal, {kind: {number: signals}}."""
    return {"pr": _sections(data.prs()), "issue": _sections(issue_data.issues())}


def save_sections(name: str, prior: Mapping[Kind, Mapping[int, dict]],
                  fresh: Mapping[Kind, Mapping[int, dict]]) -> None:
    """Save section `name` on each PR and issue where it moved from `prior` to
    `fresh` (absent from `fresh` removes it), then bring both snapshots
    current. Nothing moved touches nothing."""
    moved = [(kind, n, fresh[kind].get(n)) for kind in KINDS
             for n in sorted(set(prior[kind]) | set(fresh[kind]))
             if fresh[kind].get(n) != prior[kind].get(n)]
    if not moved:
        return
    store, issue_store = data.store(), issue_data.store()
    with store.batch(), issue_store.batch():
        for kind, n, section in moved:
            if kind == "pr":
                store.set_pr_section(n, name, section)
            else:
                issue_store.set_issue_section(n, name, section)
    data.refresh()
    issue_data.refresh()


def signal(rec: Pr) -> dict | None:
    """PR `rec`'s response signal, or None when there's been no response
    since our action. `ack` carries {at, by} once an operator has marked the
    signal seen, and is None when no ack covers it — a response newer than the
    ack's `at` supersedes the ack."""
    sig = rec.section("responses")
    if sig is None:
        return None
    ack = rec.section("response_ack")
    acked = _dt((ack or {}).get("at"))
    last = _dt(sig.get("last_response_at"))
    return {**sig, "ack": ack if (acked and last and acked >= last) else None}


def ack(pr: int, *, at: str | None = None, by: str | None = None) -> dict[str, str]:
    """Record that `by` (the current operator by default) has seen PR `pr`'s
    responses as of `at` (now by default). Returns the record stored; raises
    KeyError when the PR is not in the store."""
    rec = {"at": at or _now(),
           "by": by or activity.operator()["name"]}
    if not data.store().set_pr_section(int(pr), "response_ack", rec):
        raise KeyError(f"PR {pr} is not in the store")
    data.refresh()
    return rec
