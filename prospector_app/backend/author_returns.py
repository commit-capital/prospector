"""Authors who came back after we closed their PR or issue.

Two signals, read from the store's PR and issue snapshots with no network:

- ``reopened``: our latest landed state action on the item is a close, and the
  store sees it open again. The store's state is live-maintained — our close
  writes ``closed``, the live sweep and the issue ingest write an upstream
  reopen back to ``open`` — so an ``open`` here is a reopen observed.
- ``follow-up``: the item is still closed, and its author has since opened a
  PR or issue that links it — a ``#N`` or a URL of the item in the new one's
  title or body, or a PR's cross-reference or the author's own comment on the
  closed item naming it, both read by the responses sweep (``named``).
  That is an author answering our close (too broad, not scoped) with a
  submission that targets what we asked for.

Either one is a reason to look at the author's work again. The responses
sweep records them (`record`) on each open item that answers a close, as its
`author_return` section; the lists and the Activity tab read that section.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict

from pipeline import settings
from pipeline.storekit import parse_ts
from prospector_app.backend import activity

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue
    from pipeline.model import Pr

Kind = Literal["pr", "issue"]
KINDS: tuple[Kind, ...] = ("pr", "issue")


class Item(Protocol):
    @property
    def state(self) -> str | None: ...
    @property
    def author(self) -> str | None: ...
    @property
    def title(self) -> str | None: ...
    @property
    def url(self) -> str | None: ...
    @property
    def created_at(self) -> str | None: ...
    @property
    def body(self) -> str | None: ...


class Ref(TypedDict):
    kind: Kind
    number: int
    title: str | None
    url: str | None
    created_at: str | None


class AuthorReturn(Ref):
    author: str | None
    closed_at: str | None
    closed_by: str | None
    reason: str | None
    signal: Literal["reopened", "follow-up"]
    follow_ups: list[Ref]
    last_at: str | None


def _closed_by_us(events: list[dict]) -> dict[tuple[Kind, int], dict]:
    """Each PR and issue whose latest landed state action is our close, keyed
    by kind and number, from a newest-first event list."""
    latest: dict[tuple[Kind, int], dict] = {}
    for ev in events:
        if not activity.is_landed(ev):
            continue
        kind = ev.get("kind")
        if kind in ("close", "merge", "reopen") and ev.get("pr") is not None:
            latest.setdefault(("pr", int(ev["pr"])), ev)
        elif kind in ("issue-close", "issue-reopen") and ev.get("issue") is not None:
            latest.setdefault(("issue", int(ev["issue"])), ev)
    return {k: ev for k, ev in latest.items() if ev.get("kind") in ("close", "issue-close")}


def _mention(number: int, repo: str) -> re.Pattern[str]:
    """``#N`` or a URL of PR/issue N in `repo`, not as the prefix of a longer number."""
    url = rf"github\.com/{re.escape(repo)}/(?:pull|issues)/{number}"
    return re.compile(rf"(?:(?<![\w/])#{number}|{url})(?!\d)", re.IGNORECASE)


def _ref(kind: Kind, number: int, item: Item) -> Ref:
    return {"kind": kind, "number": number, "title": item.title,
            "url": item.url, "created_at": item.created_at}


def author_returns(prs: Mapping[int, Item], issues: Mapping[int, Item],
                   events: list[dict] | None = None, *,
                   pr_bodies: Callable[[list[int]], dict[int, str | None]] | None = None,
                   named: Mapping[tuple[Kind, int], Collection[int]] | None = None,
                   repo: str | None = None) -> list[AuthorReturn]:
    """Every closed-by-us PR or issue its author came back to, newest
    response first. `pr_bodies` reads PR descriptions the snapshot leaves out
    (each PR's own `body` without it); `named` maps a closed (kind, number) to
    the numbers the responses sweep saw its author link from it."""
    events = activity.all_events() if events is None else events
    repo = repo or settings.repo()
    named = named or {}
    stores: dict[Kind, Mapping[int, Item]] = {"pr": prs, "issue": issues}
    # The open submissions a follow-up can be, per author.
    open_by_author: dict[str, list[tuple[Kind, int, Item]]] = {}
    for kind, items in stores.items():
        for n, item in items.items():
            if item.state == "open" and item.author:
                open_by_author.setdefault(item.author.lower(), []).append((kind, n, item))

    # Each closed item with the author's submissions opened after our close.
    closed: list[tuple[Kind, int, Item, dict, list[tuple[Kind, int, Item]]]] = []
    for (kind, n), ev in _closed_by_us(events).items():
        item = stores[kind].get(n)
        if item is None:
            continue
        cut = parse_ts(ev.get("at"))
        later: list[tuple[Kind, int, Item]] = [] if item.state == "open" or cut is None else [
            (f_kind, f_n, f_item)
            for f_kind, f_n, f_item in open_by_author.get((item.author or "").lower(), [])
            if (f_kind, f_n) != (kind, n)
            and (created := parse_ts(f_item.created_at)) is not None and created > cut]
        closed.append((kind, n, item, ev, later))
    wanted = sorted({f_n for *_, later in closed for f_kind, f_n, _ in later if f_kind == "pr"})
    bodies = pr_bodies(wanted) if pr_bodies and wanted else {}

    def text(kind: Kind, n: int, item: Item) -> str:
        body = bodies.get(n) if kind == "pr" and pr_bodies else item.body
        return f"{item.title or ''}\n{body or ''}"

    out: list[AuthorReturn] = []
    for kind, n, item, ev, later in closed:
        follow_ups: list[Ref] = []
        if item.state == "open":
            signal: Literal["reopened", "follow-up"] = "reopened"
        else:
            signal = "follow-up"
            pattern = _mention(n, repo)
            names = named.get((kind, n), ())
            follow_ups = sorted(
                (_ref(f_kind, f_n, f_item) for f_kind, f_n, f_item in later
                 if f_n in names
                 or pattern.search(text(f_kind, f_n, f_item))),
                key=lambda r: r["created_at"] or "", reverse=True)
            if not follow_ups:
                continue
        out.append({
            **_ref(kind, n, item),
            "author": item.author,
            "closed_at": ev.get("at"),
            "closed_by": ev.get("operator"),
            "reason": activity.issue_close_reason(ev) if kind == "issue" else ev.get("reason"),
            "signal": signal,
            "follow_ups": follow_ups,
            "last_at": follow_ups[0]["created_at"] if follow_ups else ev.get("at"),
        })
    return sorted(out, key=lambda r: r["last_at"] or "", reverse=True)


class Link(TypedDict):
    """One close an open item answers, kept on that item's `author_return`
    section, newest first."""
    signal: Literal["reopened", "follow-up"]
    closed: Ref
    author: str | None
    reason: str | None
    closed_at: str | None
    closed_by: str | None


def compute() -> list[AuthorReturn]:
    """`author_returns` over the app's snapshots and stored responses."""
    from prospector_app.backend import data, issue_data, responses
    named: dict[tuple[Kind, int], set[int]] = {
        (kind, n): {int(x) for x in [sig.get("resubmitted_pr"), *(sig.get("mentions") or [])] if x}
        for kind, sigs in responses.recorded().items() for n, sig in sigs.items()}
    return author_returns(data.prs(), issue_data.issues(),
                          pr_bodies=data.pr_bodies, named=named)


def links(returns: list[AuthorReturn]) -> dict[Kind, dict[int, list[Link]]]:
    """Each open item's links to the closes it answers, in `returns` order
    (newest response first)."""
    out: dict[Kind, dict[int, list[Link]]] = {"pr": {}, "issue": {}}
    for r in returns:
        link: Link = {"signal": r["signal"], "closed": _ref_of(r), "author": r["author"],
                      "reason": r["reason"], "closed_at": r["closed_at"],
                      "closed_by": r["closed_by"]}
        targets: list[Ref] = [r] if r["signal"] == "reopened" else r["follow_ups"]
        for t in targets:
            out[t["kind"]].setdefault(t["number"], []).append(link)
    return out


def _ref_of(r: AuthorReturn) -> Ref:
    return {"kind": r["kind"], "number": r["number"], "title": r["title"],
            "url": r["url"], "created_at": r["created_at"]}


def record() -> None:
    """Write each open item's `author_return` section where it moved."""
    from prospector_app.backend import data, issue_data, responses
    fresh: dict[Kind, dict[int, dict]] = {
        kind: {n: {"closes": closes} for n, closes in by_n.items()}
        for kind, by_n in links(compute()).items()}
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
