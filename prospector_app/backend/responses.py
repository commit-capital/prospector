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
rebuilds them from upstream — kept in the shared store's `responses` registry,
keyed by kind (PRs and issues share GitHub's number space but are read apart),
so one machine's sweep (`responses_sweep`) serves every app. Operator acks are
human input that exists nowhere else, in the store's `response_acks` registry.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Literal

from prospector_app.backend import activity
from prospector_app.backend import data
from pipeline import gh
from pipeline import settings
from pipeline.storekit import now as _now
from pipeline.storekit import parse_ts as _dt

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
    - mentions: the PR/issue numbers the author names in their own comments
      after our action (not a signal by itself; the author's comment is a reply)
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

    # resubmitted — a cross-reference on this closed PR pointing to a different PR
    # in this repository, by the same author, created after our action (author
    # opened a fresh PR as redo). A cross-ref from another repository (e.g. the
    # author's fork of an unrelated project) carries a bare, non-comparable PR
    # number and is never a resubmission of this PR.
    this_repo = repo or f"{settings.repo_owner()}/{settings.repo_name()}"
    resubmitted = False
    resubmitted_pr: int | None = None
    resubmitted_at: str | None = None
    if cross_refs and pr_author:
        for ref in cross_refs:
            if (_dt(ref.get("createdAt")) or cut) <= cut:
                continue
            source = ref.get("source") or {}
            if source.get("__typename") != "PullRequest":
                continue
            if (source.get("repository") or {}).get("nameWithOwner") != this_repo:
                continue
            ref_author = (source.get("author") or {}).get("login")
            ref_number = source.get("number")
            ref_created_at = source.get("createdAt")
            if (ref_author == pr_author and ref_number
                    and ref_created_at and (_dt(ref_created_at) or cut) > cut):
                resubmitted = True
                resubmitted_pr = int(ref_number)
                resubmitted_at = ref.get("createdAt")
                break

    # mentions — the PRs or issues the author names in their own comments after
    # our action ("opened a new PR at #N"), the link a resubmission's own body
    # may never carry.
    mentions = sorted({
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
        "mentions": mentions,
        "last_response_at": last,
        "snippet": _snippet(latest.get("bodyText")) if latest else None,
        "by": reply_login or (_login(reopen_after[-1], "actor") if reopen_after else None),
    }


def _acted_baseline(events: list[dict] | None = None) -> dict[Kind, dict[int, dict]]:
    """The cutoff a response is measured against, per kind: each PR's latest
    LANDED action — reopen too, we measure against whatever we last did — and
    each issue whose latest landed state action is our close. A dry-run never
    counts (is_landed)."""
    events = activity.all_events() if events is None else events
    prs: dict[int, dict] = {}
    issues: dict[int, dict] = {}
    for ev in events:  # newest-first; first landed hit per item wins
        if not activity.is_landed(ev):
            continue
        kind = ev.get("kind")
        if kind in ("issue-close", "issue-reopen") and ev.get("issue") is not None:
            issues.setdefault(int(ev["issue"]), {"at": ev.get("at"), "kind": kind})
        elif ev.get("pr") is not None:
            prs.setdefault(int(ev["pr"]), {"at": ev.get("at"), "kind": kind})
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
    NOT_FOUND to explain it. `scan` leaves those PRs' prior registry entries
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
    """The fresh registry entries for `targets` of `kind`, and the ones that
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
        sig["our_action"] = {"kind": baseline[n]["kind"], "at": baseline[n]["at"]}
        sig["detected_at"] = (prior.get(n) or {}).get("detected_at") or now
        out[n] = sig
    return out, sorted(failed_set)


def scan(prs: list[int] | None = None, issues: list[int] | None = None) -> dict:
    """Sweep the PRs we've acted on and the issues we closed, classify each
    one's response, and rewrite the registry. With `prs` or `issues`, sweep
    just those and keep every other entry. Returns {checked, with_response,
    prs, issues, failed, failed_issues}.

    Every fetched item is recomputed from scratch (the registry is current
    truth for it) but keeps its first `detected_at`; an item whose fetch failed
    (`failed`, `failed_issues`) carries its prior entry forward unchanged
    rather than treating "couldn't check" as "no response"."""
    baseline = _acted_baseline()
    partial = prs is not None or issues is not None
    wanted: dict[Kind, list[int] | None] = {"pr": prs, "issue": issues}
    prior = load_all()
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
        kept = {n: v for n, v in prior[kind].items() if n not in targets} if partial else {}
        reg[kind] = {**kept, **fresh}
    _save(reg)
    return {"checked": checked, "with_response": len(reg["pr"]) + len(reg["issue"]),
            "prs": sorted(reg["pr"]), "issues": sorted(reg["issue"]),
            "failed": failed["pr"], "failed_issues": failed["issue"]}


# --- registry persistence (shared store, TTL-cached read) -------------------
# `for_pr` runs once per row of a list request while the store is a network
# round-trip; the TTL bounds how long another machine's sweep takes to appear.
_REG_TTL_SEC = 30.0
_cache: tuple[float, dict[Kind, dict[int, dict]]] | None = None


def _save(reg: dict[Kind, dict[int, dict]]) -> None:
    global _cache
    data.store().save_responses(
        {kind: {str(k): v for k, v in sorted(reg[kind].items())} for kind in KINDS})
    _cache = (time.monotonic(), reg)


def load_all() -> dict[Kind, dict[int, dict]]:
    """The response registry, {kind: {number: signals}}. Fails soft: an
    unreadable store reads as no responses, cached like a read."""
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < _REG_TTL_SEC:
        return _cache[1]
    try:
        raw = data.store().load_responses()
    except Exception:
        _log.warning("responses.load_all: store read failed", exc_info=True)
        raw = {}
    reg: dict[Kind, dict[int, dict]] = {
        kind: {int(k): v for k, v in (raw.get(kind) or {}).items()} for kind in KINDS}
    _cache = (now, reg)
    return reg


def load() -> dict[int, dict]:
    """The PRs' response signals, {pr: signals}."""
    return load_all()["pr"]


def for_pr(n: int) -> dict | None:
    """The response signal for PR `n`, or None when there's been no response
    since our action. `ack` carries {at, by} once an operator has marked the
    signal seen, and is None when no ack covers it — a response newer than the
    ack's `at` supersedes the ack."""
    sig = load().get(int(n))
    if sig is None:
        return None
    rec = load_acks().get(int(n))
    acked = _dt((rec or {}).get("at"))
    last = _dt(sig.get("last_response_at"))
    return {**sig, "ack": rec if (acked and last and acked >= last) else None}


# --- acknowledgment persistence (shared store, TTL-cached read) -------------
# "I've seen every response on this PR as of this instant" (#537), recorded in
# the store's `response_acks` registry so one operator's ack clears the signal
# from every instance's queue until a newer response supersedes it.
_ACK_TTL_SEC = 5.0
_acks_cache: tuple[float, dict[int, dict[str, str]]] | None = None


def load_acks() -> dict[int, dict[str, str]]:
    """Every operator's acks, {pr: {at, by}}. Cached for _ACK_TTL_SEC because
    `for_pr` runs once per row of a list request while the store is a network
    round-trip; the TTL bounds how long another operator's ack takes to appear.
    Fails soft — an unreadable store shows signals unacked rather than failing
    the request. A failure is cached too, so a request over ~3,000 rows costs one
    store attempt per TTL window rather than one per row."""
    global _acks_cache
    now = time.monotonic()
    if _acks_cache and now - _acks_cache[0] < _ACK_TTL_SEC:
        return _acks_cache[1]
    try:
        raw = (data.store().load_response_acks() or {}).get("acks") or {}
    except Exception:
        _log.warning("responses.load_acks: store read failed", exc_info=True)
        _acks_cache = (now, {})
        return {}
    acks = {int(k): v for k, v in raw.items()}
    _acks_cache = (now, acks)
    return acks


def ack(pr: int, *, at: str | None = None, by: str | None = None) -> dict[str, str]:
    """Record that `by` (the current operator by default) has seen PR `pr`'s
    responses as of `at` (now by default). Returns the record stored."""
    global _acks_cache
    rec = {"at": at or _now(),
           "by": by or activity.operator()["name"]}
    store = data.store()
    reg = store.load_response_acks()
    reg.setdefault("acks", {})[str(int(pr))] = rec
    store.save_response_acks(reg)
    _acks_cache = None
    return rec
