"""Authors who came back after we closed their PR or issue, read off the
responses sweep's signals on the closed item:

- ``reopened``: the author reopened it, and the store sees it open.
- ``follow-up``: it is still closed, and an open PR or issue of the author's,
  opened after our close, cross-references it or is named in their comment on
  it (the sweep's ``follow_ups``).

Either one is a reason to look at the author's work again. The sweep records
each as the open item's `author_return` section (`record`); the lists and the
Activity tab read that section.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, TypedDict

from pipeline.storekit import parse_ts
from prospector_app.backend import activity

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue
    from pipeline.model import Pr

Kind = Literal["pr", "issue"]


class Ref(TypedDict):
    kind: Kind
    number: int
    title: str | None
    url: str | None
    created_at: str | None


class Link(TypedDict):
    """One close an open item answers, kept on that item's `author_return`
    section, newest close first."""
    signal: Literal["reopened", "follow-up"]
    closed: Ref
    author: str | None
    reason: str | None
    closed_at: str | None
    closed_by: str | None


class AuthorReturn(Ref):
    author: str | None
    closed_at: str | None
    closed_by: str | None
    reason: str | None
    signal: Literal["reopened", "follow-up"]
    follow_ups: list[Ref]
    last_at: str | None


def _ref(kind: Kind, number: int, item: Pr | Issue) -> Ref:
    return {"kind": kind, "number": number, "title": item.title,
            "url": item.url, "created_at": item.created_at}


def links(signals: Mapping[Kind, Mapping[int, dict]], prs: Mapping[int, Pr],
          issues: Mapping[int, Issue]) -> dict[Kind, dict[int, list[Link]]]:
    """Each open item's links to the closes it answers, from the closed items'
    response `signals`."""
    stores: dict[Kind, Mapping[int, Pr] | Mapping[int, Issue]] = {"pr": prs, "issue": issues}
    closes: list[tuple[Kind, int, dict]] = sorted(
        ((kind, n, sig) for kind, sigs in signals.items() for n, sig in sigs.items()
         if (sig.get("our_action") or {}).get("kind") in ("close", "issue-close")),
        key=lambda c: c[2]["our_action"].get("at") or "", reverse=True)
    out: dict[Kind, dict[int, list[Link]]] = {"pr": {}, "issue": {}}
    for kind, n, sig in closes:
        item = stores[kind].get(n)
        if item is None:
            continue
        action = sig["our_action"]
        link: Link = {"signal": "follow-up", "closed": _ref(kind, n, item), "author": item.author,
                      "reason": action.get("reason"), "closed_at": action.get("at"),
                      "closed_by": action.get("by")}
        if item.state == "open":
            if sig.get("reopened"):
                out[kind].setdefault(n, []).append({**link, "signal": "reopened"})
            continue
        cut = parse_ts(action.get("at"))
        for m in sig.get("follow_ups") or []:
            f_kind: Kind = "pr" if m in prs else "issue"
            f = stores[f_kind].get(m)
            created = parse_ts(f.created_at) if f is not None else None
            if (f is not None and f.state == "open" and created is not None and cut is not None
                    and created > cut and (f.author or "").lower() == (item.author or "").lower()):
                out[f_kind].setdefault(m, []).append(link)
    return out


def record(signals: Mapping[Kind, Mapping[int, dict]]) -> None:
    """Write each open item's `author_return` section where it moved."""
    from prospector_app.backend import data, issue_data, responses
    fresh: dict[Kind, dict[int, dict]] = {
        kind: {n: {"closes": closes} for n, closes in by_n.items()}
        for kind, by_n in links(signals, data.prs(), issue_data.issues()).items()}
    stored: dict[Kind, dict[int, dict]] = {
        "pr": _sections(data.prs()), "issue": _sections(issue_data.issues())}
    responses.save_sections("author_return", stored, fresh)


def _sections(recs: Mapping[int, Pr] | Mapping[int, Issue]) -> dict[int, dict]:
    return {n: sec for n, rec in recs.items() if (sec := rec.section("author_return"))}


def latest(rec: Pr | Issue) -> Link | None:
    """The newest close open item `rec` answers, or None."""
    closes = (rec.section("author_return") or {}).get("closes") or []
    return closes[0] if closes else None


def recorded(prs: Mapping[int, Pr], issues: Mapping[int, Issue],
             scope: activity.ActivityScope) -> list[AuthorReturn]:
    """Every close an author came back to, newest response first, read off the
    stored links: a close counts when its item is in `scope`'s PRs (an author
    scope holds no issues) and, under an operator scope, that operator made it."""
    stores: dict[Kind, Mapping[int, Pr] | Mapping[int, Issue]] = {"pr": prs, "issue": issues}
    grouped: dict[tuple[Kind, int], AuthorReturn] = {}
    for kind, items in stores.items():
        for n, item in items.items():
            for link in (item.section("author_return") or {}).get("closes") or []:
                closed = link["closed"]
                if scope.pr_numbers is not None and (
                        closed["kind"] != "pr" or closed["number"] not in scope.pr_numbers):
                    continue
                if scope.operator and (link.get("closed_by") or "—") != scope.operator:
                    continue
                r = grouped.setdefault((closed["kind"], closed["number"]), AuthorReturn(
                    kind=closed["kind"], number=closed["number"], title=closed["title"],
                    url=closed["url"], created_at=closed["created_at"], author=link["author"],
                    closed_at=link["closed_at"], closed_by=link.get("closed_by"),
                    reason=link["reason"], signal=link["signal"], follow_ups=[],
                    last_at=link["closed_at"]))
                if link["signal"] == "follow-up":
                    r["follow_ups"].append(_ref(kind, n, item))
    for r in grouped.values():
        r["follow_ups"].sort(key=lambda f: f["created_at"] or "", reverse=True)
        if r["follow_ups"]:
            r["last_at"] = r["follow_ups"][0]["created_at"]
    return sorted(grouped.values(), key=lambda r: r["last_at"] or "", reverse=True)
