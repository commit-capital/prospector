"""What the app says about supply-chain threats: the open PRs the threat scan
flagged, the incident log and actor blocklist it keeps in the store's `threats`
registry, and the leaked credentials still waiting to be rotated — loud for a
maintainer's leak (`gates.maintainer_leak`), quiet for a contributor's or a
test fixture.

`summarize` feeds the red banner every page shows (through the system-health
poll) and `detail` the Security tab's Threats view; both are pure over plain
data, and `summary` / `current_detail` gather the live inputs.
"""
from __future__ import annotations

import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, TypedDict

from pipeline import actions, gates
from prospector_app.backend import data

if TYPE_CHECKING:
    from pipeline.model import Pr


class FlaggedPr(TypedDict):
    pr: int
    title: str | None
    author: str | None
    url: str | None
    verdict: str
    signatures: list[str]
    noticed: str | None


class ThreatSummary(TypedDict):
    malicious: list[FlaggedPr]
    suspicious: int
    # The PRs holding a credential a maintainer leaked, lowest first.
    secrets: list[int]


class Secret(TypedDict):
    id: str
    pr: int
    summary: str | None
    evidence: str | None
    created: str | None
    author: str | None
    maintainer: bool
    fixture: bool


class Incident(TypedDict):
    pr: int
    author: str | None
    head_sha: str | None
    signatures: list[str]
    noticed: str | None
    state: str | None
    title: str | None


class BlockedActor(TypedDict):
    login: str
    reason: str | None
    added: str | None
    incidents: list[int]
    open_prs: list[int]


class ThreatDetail(TypedDict):
    # True while the PR snapshot's first load runs; every list is then empty.
    loading: bool
    flagged: list[FlaggedPr]
    incidents: list[Incident]
    actors: list[BlockedActor]
    # The maintainers' leaks the banner counts, then every other open
    # rotate-secret item: a contributor's, or one reading as a test fixture.
    secrets: list[Secret]
    quiet_secrets: list[Secret]


# How long the registries behind the banner are held: every open page polls
# it, and a PR's open/closed state comes from the snapshot, not from these.
_REGISTRY_TTL_SECONDS = 60.0
_registry_cache: tuple[float, list[dict], list[dict]] | None = None


def _flagged(pr: Pr, noticed: dict[int, str]) -> FlaggedPr:
    return {"pr": pr.number, "title": pr.title, "author": pr.author, "url": pr.url,
            "verdict": str(pr.threat_verdict), "signatures": list(pr.threat_signatures),
            "noticed": noticed.get(pr.number)}


def _noticed(incidents: list[dict]) -> dict[int, str]:
    return {int(i["pr"]): str(i["noticed"]) for i in incidents
            if i.get("pr") is not None and i.get("noticed")}


def _item_pr(item: dict, by_number: dict[int, Pr]) -> Pr | None:
    n = item.get("pr")
    return by_number.get(n) if isinstance(n, int) else None


def _secret(item: dict, pr: Pr | None) -> Secret:
    return {"id": str(item.get("id")), "pr": int(item["pr"]), "summary": item.get("summary"),
            "evidence": item.get("evidence"), "created": item.get("created"),
            "author": pr.author if pr else None,
            "maintainer": pr is not None and gates.priority_author(pr.author, pr.author_association),
            "fixture": actions.is_fixture(item)}


def summarize(prs: Iterable[Pr], incidents: list[dict], items: list[dict]) -> ThreatSummary:
    """The banner's answer: every open PR flagged malicious, lowest number
    first; how many open PRs read suspicious; and the PRs holding a credential
    a maintainer leaked."""
    by_number = {pr.number: pr for pr in prs}
    noticed = _noticed(incidents)
    malicious: list[FlaggedPr] = []
    suspicious = 0
    for pr in by_number.values():
        if pr.state != "open":
            continue
        if pr.threat_verdict == "malicious":
            malicious.append(_flagged(pr, noticed))
        elif pr.threat_verdict == "suspicious":
            suspicious += 1
    malicious.sort(key=lambda f: f["pr"])
    secrets = sorted({int(it["pr"]) for it in items
                      if gates.maintainer_leak(it, _item_pr(it, by_number))})
    return {"malicious": malicious, "suspicious": suspicious, "secrets": secrets}


def detail(prs: Iterable[Pr], registry: dict, items: list[dict]) -> ThreatDetail:
    """The Threats view: open flagged PRs (malicious first), the incident log
    newest first with each PR's state, the blocked actors with their open PRs,
    the maintainers' leaks the banner counts, and every other open
    rotate-secret item, live-looking ones first."""
    by_number = {pr.number: pr for pr in prs}
    incidents_raw: list[dict] = list(registry.get("incidents") or [])
    noticed = _noticed(incidents_raw)
    open_prs = [pr for pr in by_number.values() if pr.state == "open"]
    flagged = [_flagged(pr, noticed) for pr in open_prs
               if pr.threat_verdict in ("malicious", "suspicious")]
    flagged.sort(key=lambda f: (f["verdict"] != "malicious", f["pr"]))

    incidents: list[Incident] = []
    for inc in incidents_raw:
        n = int(inc["pr"])
        pr = by_number.get(n)
        incidents.append({"pr": n, "author": inc.get("author"), "head_sha": inc.get("head_sha"),
                          "signatures": list(inc.get("signatures") or []),
                          "noticed": inc.get("noticed"),
                          "state": pr.state if pr else None,
                          "title": pr.title if pr else None})
    incidents.sort(key=lambda i: (str(i["noticed"] or ""), i["pr"]), reverse=True)

    open_by_author: dict[str, list[int]] = {}
    for pr in open_prs:
        if pr.author:
            open_by_author.setdefault(pr.author, []).append(pr.number)
    actors: list[BlockedActor] = [
        {"login": login, "reason": entry.get("reason"), "added": entry.get("added"),
         "incidents": list(entry.get("incidents") or []),
         "open_prs": sorted(open_by_author.get(login, []))}
        for login, entry in sorted((registry.get("actors") or {}).items())]
    secrets: list[Secret] = []
    quiet: list[Secret] = []
    for it in items:
        if it.get("kind") != "rotate-secret" or it.get("status") != "open":
            continue
        pr = _item_pr(it, by_number)
        (secrets if gates.maintainer_leak(it, pr) else quiet).append(_secret(it, pr))
    secrets.sort(key=lambda s: s["pr"])
    quiet.sort(key=lambda s: (s["fixture"], s["pr"]))
    return {"loading": False, "flagged": flagged, "incidents": incidents, "actors": actors,
            "secrets": secrets, "quiet_secrets": quiet}


def _registries() -> tuple[list[dict], list[dict]]:
    """The incident log and the action items, held for _REGISTRY_TTL_SECONDS."""
    global _registry_cache
    now = time.monotonic()
    if _registry_cache and now - _registry_cache[0] < _REGISTRY_TTL_SECONDS:
        return _registry_cache[1], _registry_cache[2]
    incidents = list(data.store().load_threats().get("incidents") or [])
    items = data.action_items()
    _registry_cache = (now, incidents, items)
    return incidents, items


def summary() -> ThreatSummary | None:
    """The banner's answer, or None while the PR snapshot's first load runs —
    every page polls this, and none may hold a request on that load."""
    if data.snapshot_loading():
        return None
    incidents, items = _registries()
    return summarize(data.prs().values(), incidents, items)


def current_detail() -> ThreatDetail:
    if data.snapshot_loading():
        return {"loading": True, "flagged": [], "incidents": [], "actors": [], "secrets": [],
                "quiet_secrets": []}
    return detail(data.prs().values(), data.store().load_threats(), data.action_items())
