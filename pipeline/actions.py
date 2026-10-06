"""The ONE action-items model — operator / first-party worklist items the
triage discovers that are NOT a per-PR merge/close disposition.

Examples: rotate a leaked credential (from threat-scan), salvage a good fix
out of a rejected PR into a clean first-party PR (from analyze), notify
upstream of a security issue. These are durable, cross-cutting tasks with a
done/open lifecycle — so they live in the action-items registry (owned by store.py),
not in a per-PR section. The app surfaces them as a flat, checkable worklist.

Each item has a STABLE id ("<kind>:<pr>") so re-running a scan upserts rather
than duplicates, and a human-set status survives re-emission of the evidence
it judged.
"""
from __future__ import annotations

import re

# kind → human label. Closed vocabulary; validated on make_item.
KINDS = {
    "rotate-secret": "Potential secret leaked",
    "salvage-fix": "Salvage fix into first-party PR",
    "notify-upstream": "Notify upstream",
    "block-actor": "Block / review actor",
    "review": "Needs operator review",
}
STATUSES = {"open", "done", "dismissed"}

# A rotate-secret item's evidence is "<path>: <added-line excerpt>". These read
# the two halves for the shapes of a test fixture rather than a live
# credential: a test/fixture/example path, or a marker word in the excerpt.
_FIXTURE_PATH = re.compile(
    r"(?:^|/)(?:tests?|__tests__|testing|specs?|fixtures?|testdata|mocks?|"
    r"examples?|samples?)(?:/|$)|(?:^|/)(?:test_|conftest\.)|_test\.|\.(?:spec|test)\.",
    re.IGNORECASE)
_FIXTURE_TEXT = re.compile(
    r"fixture|dummy|fake|placeholder|pretend|example|sample|leakmarker|"
    r"not[_-]?a[_-]?real|redacted|changeme", re.IGNORECASE)


def likely_fixture(evidence: str) -> bool:
    """Whether a rotate-secret item's evidence reads as a test fixture rather
    than a live credential."""
    if not evidence:
        return False
    path, sep, excerpt = evidence.partition(":")
    if not sep:
        return bool(_FIXTURE_TEXT.search(evidence))
    return bool(_FIXTURE_PATH.search(path.strip()) or _FIXTURE_TEXT.search(excerpt))


def is_fixture(item: dict) -> bool:
    """Whether a rotate-secret item reads as a test fixture; an item stored
    before fixture marking existed is judged from its evidence."""
    if "fixture" in item:
        return bool(item["fixture"])
    return likely_fixture(item.get("evidence") or "")


def make_item(kind: str, *, pr: int, summary: str, created: str,
              evidence: str = "", detail: str = "",
              fixture: bool | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind {kind!r} not in {sorted(KINDS)}")
    item = {
        "id": f"{kind}:{int(pr)}",
        "kind": kind,
        "pr": int(pr),
        "summary": summary,
        "evidence": evidence,
        "detail": detail,
        "status": "open",
        "created": created,
    }
    if fixture is not None:
        item["fixture"] = fixture
    return item


def empty_registry() -> dict:
    return {"items": []}


def _by_id(reg: dict) -> dict[str, dict]:
    return {it["id"]: it for it in reg.get("items", [])}


def upsert(reg: dict, item: dict) -> dict:
    """Add the item, or refresh an existing one of the same id, keeping its
    created date and, for the same or no evidence, its status. New evidence is
    a new finding: the item reopens, since a human status judged other
    evidence, and records the day it was found."""
    items = reg.setdefault("items", [])
    existing = _by_id(reg).get(item["id"])
    if existing is None:
        items.append(item)
    else:
        if item["evidence"] and item["evidence"] != existing.get("evidence"):
            existing["evidence_found"] = item["created"]
            existing["status"] = "open"
        existing["summary"] = item["summary"]
        existing["evidence"] = item["evidence"] or existing.get("evidence", "")
        existing["detail"] = item["detail"] or existing.get("detail", "")
        if "fixture" in item:
            existing["fixture"] = item["fixture"]
    items.sort(key=lambda i: (i["status"] != "open", i["kind"], i["pr"]))
    return reg


def found(item: dict) -> str | None:
    """The day the item's current evidence was first found."""
    return item.get("evidence_found") or item.get("created")


def withdraw(reg: dict, item_id: str) -> dict:
    """Drop the item while it is open: the finding that raised it is gone. A
    handled item stays."""
    reg["items"] = [it for it in reg.get("items", [])
                    if it["id"] != item_id or it["status"] != "open"]
    return reg


def dismiss(reg: dict, item_id: str, evidence: str) -> dict:
    """Dismiss the item when it is open, as judged on `evidence`, which a
    re-emission must differ from to reopen it."""
    it = _by_id(reg).get(item_id)
    if it is not None:
        it["evidence"] = evidence
        if it["status"] == "open":
            it["status"] = "dismissed"
    return reg


def set_status(reg: dict, item_id: str, status: str) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status {status!r} not in {sorted(STATUSES)}")
    it = _by_id(reg).get(item_id)
    if it is None:
        raise KeyError(item_id)
    it["status"] = status
    return reg
