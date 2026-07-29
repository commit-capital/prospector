"""Community response signals — how authors react after we triage their PR.

After we close / request-changes / comment as the configured bot, the author may
reply (argue back, ask why), reopen a PR we closed, or push new commits to fix
what we flagged. This module sweeps the PRs we've acted on, reads each one's
upstream timeline *since our action*, and classifies the response so the PR
explorer can surface it as a sortable column + filterable chips.

Read-only upstream (one batched GraphQL request per ~20 PRs, the freshness_live
pattern). The classified signals are a derived cache — `scan` rebuilds them from
upstream — so they live in a cockpit-local registry. Operator acks are human
input that exists nowhere else, so they live in the shared store's
`response_acks` registry and are visible to every operator.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from review_cockpit.backend import activity
from review_cockpit.backend import data
from review_cockpit.backend.safety_guard import run
from pipeline.settings import BOT_LOGIN, REPO_NAME, REPO_OWNER

_log = logging.getLogger(__name__)

REG = Path(__file__).resolve().parents[1] / "approved-actions" / "responses.json"
_CHUNK = 20
SNIPPET_LEN = 160

# Logins that are us, never a community response. Other automation is recognised
# from the actor's type rather than listed here (`_is_human`).
BOTS = {BOT_LOGIN}


def _dt(ts: str | None) -> datetime | None:
    """Parse an ISO timestamp to an aware UTC datetime. Activity-log times are
    UTC-aware ('+00:00') and GitHub times are UTC 'Z', while legacy activity rows
    may be naive local. Normalizing all to UTC is the only correct way to compare
    'did this happen after we acted'."""
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return d.astimezone(timezone.utc) if d.tzinfo else d.astimezone(timezone.utc)


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
    reopened = bool(reopen_after) or (state == "open" and action_kind == "close")

    # new_commits — any commit pushed after our action
    new_commit_dates = [d for d in commit_dates if (_dt(d) or cut) > cut]
    commit_at = max(new_commit_dates, default=None)
    new_commits = bool(new_commit_dates)

    # resubmitted — a cross-reference on this closed PR pointing to a different PR
    # in this repository, by the same author, created after our action (author
    # opened a fresh PR as redo). A cross-ref from another repository (e.g. the
    # author's fork of an unrelated project) carries a bare, non-comparable PR
    # number and is never a resubmission of this PR.
    this_repo = repo or f"{REPO_OWNER}/{REPO_NAME}"
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

    if not (replied or reopened or new_commits or resubmitted):
        return None

    last = max((t for t in (reply_at, reopen_at, commit_at, resubmitted_at) if t), default=None)
    return {
        "reopened": reopened,
        "new_commits": new_commits,
        "replied": replied,
        "resubmitted": resubmitted,
        "resubmitted_pr": resubmitted_pr,
        "last_response_at": last,
        "snippet": _snippet(latest.get("bodyText")) if latest else None,
        "by": reply_login or (_login(reopen_after[-1], "actor") if reopen_after else None),
    }


def _acted_baseline() -> dict[int, dict]:
    """The latest LANDED action per PR (the cutoff a response is measured against).
    A dry-run never counts (is_landed). Reopen counts as our latest action too —
    we measure responses against whatever we last did."""
    out: dict[int, dict] = {}
    for ev in activity.all_events():  # newest-first; first landed hit per PR wins
        pr = ev.get("pr")
        if pr is None or not activity.is_landed(ev):
            continue
        pr = int(pr)
        if pr not in out:
            out[pr] = {"at": ev.get("at"), "kind": ev.get("kind")}
    return out


def _query(prs: list[int]) -> str:
    # Every actor carries __typename so a GitHub App is recognised as a Bot:
    # a Bot's login here is bare, with no REST-style `[bot]` suffix to read it
    # off (`_is_human`).
    fields = ("number state headRefOid "
              "author { login __typename } "
              "comments(last: 20) { nodes { author { login __typename } createdAt url bodyText } } "
              "timelineItems(last: 20, itemTypes: [REOPENED_EVENT, CROSS_REFERENCED_EVENT]) { "
              "nodes { __typename "
              "... on ReopenedEvent { createdAt actor { login __typename } } "
              "... on CrossReferencedEvent { createdAt "
              "source { __typename "
              "... on PullRequest { number createdAt author { login } "
              "repository { nameWithOwner } } } } } } "
              "commits(last: 10) { nodes { commit { committedDate oid } } }")
    aliases = " ".join(f"p{i}: pullRequest(number: {int(n)}) {{ {fields} }}"
                       for i, n in enumerate(prs))
    return f'query {{ repository(owner: "{REPO_OWNER}", name: "{REPO_NAME}") {{ {aliases} }} }}'


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


def _fetch(prs: list[int]) -> tuple[dict[int, dict], list[int]]:
    """Batched live timeline node per PR via GraphQL. Returns ``(nodes, failed)``.

    ``failed`` lists every PR this sweep could not read: the whole chunk when the
    call yields nothing usable (a `gh` timeout, or a response with no
    ``data.repository``), and — when `gh` reports errors — the PRs whose alias
    came back empty without a NOT_FOUND to explain it. `scan` leaves those PRs'
    prior registry entries untouched, so "couldn't check" is never recorded as
    "no response".

    One PR deleted from the repository makes `gh` exit non-zero for its whole
    chunk while GitHub still returns every other alias in the same body, so the
    body is read whatever the exit status and each alias judged on its own:
    resolved is data, and NOT_FOUND is a PR that is gone — absent from both
    return values, the same answer as an empty alias in a clean response."""
    out: dict[int, dict] = {}
    failed: list[int] = []
    for i in range(0, len(prs), _CHUNK):
        chunk = prs[i:i + _CHUNK]
        try:
            r = run(["gh", "api", "graphql", "-f", f"query={_query(chunk)}"], timeout=90)
        except subprocess.TimeoutExpired:
            _log.warning("responses._fetch: gh graphql timed out; skipping %d PRs (%s)",
                        len(chunk), _span(chunk))
            failed.extend(chunk)
            continue
        try:
            body = json.loads(r.stdout)
            repo = (body.get("data") or {}).get("repository")
        except (ValueError, TypeError, AttributeError):
            body, repo = {}, None
        # An empty `repository` is a real answer (its aliases resolved to
        # nothing); only its absence means the call yielded nothing to read.
        if not isinstance(repo, dict):
            _log.warning("responses._fetch: gh graphql returned no data (rc=%d) for %d PRs "
                        "(%s): %s", r.returncode, len(chunk), _span(chunk),
                        (r.stderr or "").strip()[:300])
            failed.extend(chunk)
            continue
        gone = _not_found_aliases(body)
        unread = [n for j, n in enumerate(chunk)
                  if not repo.get(f"p{j}") and f"p{j}" not in gone] if r.returncode != 0 else []
        if r.returncode != 0:
            _log.warning("responses._fetch: gh graphql reported errors (rc=%d) for %d PRs (%s); "
                        "%d resolved, %d gone from the repo, %d unread: %s",
                        r.returncode, len(chunk), _span(chunk),
                        sum(1 for j in range(len(chunk)) if repo.get(f"p{j}")),
                        len(gone), len(unread), (r.stderr or "").strip()[:200])
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


def scan(prs: list[int] | None = None) -> dict:
    """Sweep the PRs we've acted on, classify each one's response, and rewrite the
    registry. Returns {checked, with_response, prs, failed}. Recomputes every
    successfully-fetched PR from scratch (the registry is current truth for
    those) but preserves each PR's first `detected_at`, and — for any PR whose
    chunk failed to fetch this sweep (`failed`) — carries its prior entry
    forward unchanged rather than treating "couldn't check" as "no response"."""
    baseline = _acted_baseline()
    targets = [int(n) for n in (prs if prs is not None else baseline.keys()) if int(n) in baseline]
    nodes, failed = _fetch(targets)
    failed_set = set(failed)
    prior = load()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    reg: dict[int, dict] = {}
    for n in targets:
        if n in failed_set:
            if n in prior:
                reg[n] = prior[n]
            continue
        node = nodes.get(n)
        if not node:
            continue
        sig = _node_signals(node, baseline[n])
        if not sig:
            continue
        sig["pr"] = n
        sig["our_action"] = {"kind": baseline[n]["kind"], "at": baseline[n]["at"]}
        sig["detected_at"] = (prior.get(n) or {}).get("detected_at") or now
        reg[n] = sig
    _save(reg)
    return {"checked": len(targets), "with_response": len(reg), "prs": sorted(reg),
            "failed": sorted(failed_set)}


# --- registry persistence (mtime-cached read) ------------------------------
_cache: tuple[float, dict[int, dict]] | None = None


def _save(reg: dict[int, dict]) -> None:
    global _cache
    REG.parent.mkdir(parents=True, exist_ok=True)
    REG.write_text(json.dumps({str(k): v for k, v in sorted(reg.items())}, indent=2))
    _cache = None  # invalidate


def load() -> dict[int, dict]:
    """The response registry, {pr: signals}. Cached on the file's mtime so a
    per-row lookup in list_prs doesn't re-read/parse the file 50 times."""
    global _cache
    if not REG.exists():
        return {}
    mtime = REG.stat().st_mtime
    if _cache and _cache[0] == mtime:
        return _cache[1]
    try:
        raw = json.loads(REG.read_text())
    except (ValueError, OSError):
        return {}
    reg = {int(k): v for k, v in raw.items()}
    _cache = (mtime, reg)
    return reg


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
# from every cockpit's queue until a newer response supersedes it.
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
    rec = {"at": at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "by": by or activity.operator()["name"]}
    store = data.store()
    reg = store.load_response_acks()
    reg.setdefault("acks", {})[str(int(pr))] = rec
    store.save_response_acks(reg)
    _acks_cache = None
    return rec
