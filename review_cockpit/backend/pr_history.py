"""Condensed on-PR activity history — comments, reviews (Greptile's scored ones
flagged), commits, and reopen/close/force-push/rename events — read live from
GitHub via one batched GraphQL query per PR. Powers the PR-detail history
panel so a reviewer can follow the back-and-forth without leaving the cockpit."""
from __future__ import annotations

import json
from typing import Any, TypedDict

from pipeline.greptile import parse_confidence_score
from pipeline.settings import REPO, REPO_NAME, REPO_OWNER
from review_cockpit.backend.safety_guard import run

SNIPPET_LEN = 200
_TIMELINE_TYPES = "REOPENED_EVENT, CLOSED_EVENT, HEAD_REF_FORCE_PUSHED_EVENT, RENAMED_TITLE_EVENT"


class HistoryItem(TypedDict):
    kind: str  # comment | review | greptile_review | commit | reopened | closed | force_push | renamed
    at: str | None  # always non-None on the list fetch_pr_history returns — items without one are dropped
    actor: str | None
    summary: str | None
    url: str | None
    state: str | None           # review state (approved/changes requested/…), review kinds only
    greptile_score: int | None  # confidence N/5, greptile_review kind only


def _snippet(body: str | None) -> str | None:
    if not body:
        return None
    s = " ".join(body.split())
    return s[:SNIPPET_LEN] + ("…" if len(s) > SNIPPET_LEN else "")


def _is_greptile(login: str | None) -> bool:
    return "greptile" in (login or "").lower()


def _query(n: int) -> str:
    fields = (
        "comments(last: 50) { nodes { author { login } createdAt url bodyText } } "
        "reviews(last: 50) { nodes { author { login } state submittedAt url bodyText } } "
        "commits(last: 100) { nodes { commit { oid committedDate message author { name } } } } "
        f"timelineItems(last: 50, itemTypes: [{_TIMELINE_TYPES}]) {{ nodes {{ __typename "
        "... on ReopenedEvent { createdAt actor { login } } "
        "... on ClosedEvent { createdAt actor { login } } "
        "... on HeadRefForcePushedEvent { createdAt actor { login } } "
        "... on RenamedTitleEvent { createdAt actor { login } currentTitle previousTitle } "
        "} }"
    )
    return (f'query {{ repository(owner: "{REPO_OWNER}", name: "{REPO_NAME}") {{ '
            f"pullRequest(number: {int(n)}) {{ {fields} }} }} }}")


def _fetch_node(n: int) -> dict[str, Any] | None:
    r = run(["gh", "api", "graphql", "-f", f"query={_query(n)}"], timeout=60)
    if r.returncode != 0:
        return None
    try:
        repo = (json.loads(r.stdout).get("data") or {}).get("repository") or {}
    except (ValueError, TypeError):
        return None
    node = repo.get("pullRequest")
    return node if isinstance(node, dict) else None


def _comment_items(node: dict[str, Any]) -> list[HistoryItem]:
    out: list[HistoryItem] = []
    for c in ((node.get("comments") or {}).get("nodes")) or []:
        out.append({
            "kind": "comment", "at": c.get("createdAt"),
            "actor": (c.get("author") or {}).get("login"),
            "summary": _snippet(c.get("bodyText")), "url": c.get("url"),
            "state": None, "greptile_score": None,
        })
    return out


def _review_items(node: dict[str, Any]) -> list[HistoryItem]:
    out: list[HistoryItem] = []
    for r in ((node.get("reviews") or {}).get("nodes")) or []:
        login = (r.get("author") or {}).get("login")
        greptile = _is_greptile(login)
        score = parse_confidence_score(r.get("bodyText")) if greptile else None
        state = (r.get("state") or "").replace("_", " ").lower() or None
        out.append({
            "kind": "greptile_review" if greptile else "review",
            "at": r.get("submittedAt"), "actor": login,
            "summary": _snippet(r.get("bodyText")), "url": r.get("url"),
            "state": state, "greptile_score": score,
        })
    return out


def _commit_items(node: dict[str, Any]) -> list[HistoryItem]:
    out: list[HistoryItem] = []
    for c in ((node.get("commits") or {}).get("nodes")) or []:
        commit = c.get("commit") or {}
        oid = commit.get("oid")
        message = (commit.get("message") or "").split("\n", 1)[0].strip() or None
        out.append({
            "kind": "commit", "at": commit.get("committedDate"),
            "actor": (commit.get("author") or {}).get("name"), "summary": message,
            "url": f"https://github.com/{REPO}/commit/{oid}" if oid else None,
            "state": None, "greptile_score": None,
        })
    return out


def _timeline_items(node: dict[str, Any]) -> list[HistoryItem]:
    verb = {
        "ReopenedEvent": ("reopened", "reopened this PR"),
        "ClosedEvent": ("closed", "closed this PR"),
        "HeadRefForcePushedEvent": ("force_push", "force-pushed the branch"),
    }
    out: list[HistoryItem] = []
    for t in ((node.get("timelineItems") or {}).get("nodes")) or []:
        typ = (t or {}).get("__typename")
        actor = (t.get("actor") or {}).get("login")
        if typ in verb:
            kind, summary = verb[typ]
            out.append({"kind": kind, "at": t.get("createdAt"), "actor": actor,
                        "summary": summary, "url": None, "state": None, "greptile_score": None})
        elif typ == "RenamedTitleEvent":
            prev, cur = t.get("previousTitle"), t.get("currentTitle")
            summary = f'renamed "{prev}" → "{cur}"' if prev and cur else "renamed this PR"
            out.append({"kind": "renamed", "at": t.get("createdAt"), "actor": actor,
                        "summary": summary, "url": None, "state": None, "greptile_score": None})
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
