"""Authors who came back after we closed their PR or issue.

Two signals, read from the store and the responses registry with no network:

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

Either one is a reason to look at the author's work again.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Collection, Mapping
from typing import Literal, Protocol, TypedDict

from pipeline import settings
from pipeline.storekit import parse_ts
from prospector_app.backend import activity

Kind = Literal["pr", "issue"]


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
            "reason": activity.issue_close_reason(ev) if kind == "issue" else ev.get("reason"),
            "signal": signal,
            "follow_ups": follow_ups,
            "last_at": follow_ups[0]["created_at"] if follow_ups else ev.get("at"),
        })
    return sorted(out, key=lambda r: r["last_at"] or "", reverse=True)


class OpenReturn(TypedDict):
    signal: Literal["reopened", "follow-up"]
    closed: Ref
    reason: str | None
    closed_at: str | None


def current(prs: Mapping[int, Item] | None = None,
            events: list[dict] | None = None) -> list[AuthorReturn]:
    """`author_returns` over the app's snapshots and the responses registry."""
    from prospector_app.backend import data, issue_data, responses
    named: dict[tuple[Kind, int], set[int]] = {
        (kind, n): {int(x) for x in [sig.get("resubmitted_pr"), *(sig.get("mentions") or [])] if x}
        for kind, sigs in responses.load_all().items() for n, sig in sigs.items()}
    return author_returns(data.prs() if prs is None else prs, issue_data.issues(), events,
                          pr_bodies=data.pr_bodies, named=named)


_TTL_SEC = 10.0
_open_cache: tuple[float, dict[Kind, dict[int, OpenReturn]]] | None = None


def open_returns() -> dict[Kind, dict[int, OpenReturn]]:
    """The open PRs and issues that are an author's return — reopened, or a
    follow-up to a close — keyed by kind and number, for the list filters.
    Cached for _TTL_SEC: a PR list request reads it once per row."""
    global _open_cache
    now = time.monotonic()
    if _open_cache and now - _open_cache[0] < _TTL_SEC:
        return _open_cache[1]
    out: dict[Kind, dict[int, OpenReturn]] = {"pr": {}, "issue": {}}
    for r in reversed(current()):  # oldest first, so the newest close wins a shared follow-up
        closed: Ref = {"kind": r["kind"], "number": r["number"], "title": r["title"],
                       "url": r["url"], "created_at": r["created_at"]}
        if r["signal"] == "reopened":
            out[r["kind"]][r["number"]] = {"signal": "reopened", "closed": closed,
                                           "reason": r["reason"], "closed_at": r["closed_at"]}
        for f in r["follow_ups"]:
            out[f["kind"]][f["number"]] = {"signal": "follow-up", "closed": closed,
                                           "reason": r["reason"], "closed_at": r["closed_at"]}
    _open_cache = (now, out)
    return out
