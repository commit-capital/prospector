"""The ONE action-items model — operator / first-party worklist items the
triage discovers that are NOT a per-PR merge/close disposition.

Examples: rotate a leaked credential (from threat-scan), salvage a good fix
out of a rejected PR into a clean first-party PR (from analyze), notify
upstream of a security issue. These are durable, cross-cutting tasks with a
done/open lifecycle — so they live in the action-items registry (owned by store.py),
not in a per-PR section. The app surfaces them as a flat, checkable worklist.

Each item has a STABLE id ("<kind>:<pr>") so re-running a scan upserts rather
than duplicates, and a human-set status survives re-emission.
"""
from __future__ import annotations

# kind → human label. Closed vocabulary; validated on make_item.
KINDS = {
    "rotate-secret": "Potential secret leaked",
    "salvage-fix": "Salvage fix into first-party PR",
    "notify-upstream": "Notify upstream",
    "block-actor": "Block / review actor",
    "review": "Needs operator review",
}
STATUSES = {"open", "done", "dismissed"}


def make_item(kind: str, *, pr: int, summary: str, created: str,
              evidence: str = "", detail: str = "") -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind {kind!r} not in {sorted(KINDS)}")
    return {
        "id": f"{kind}:{int(pr)}",
        "kind": kind,
        "pr": int(pr),
        "summary": summary,
        "evidence": evidence,
        "detail": detail,
        "status": "open",
        "created": created,
    }


def empty_registry() -> dict:
    return {"items": []}


def _by_id(reg: dict) -> dict[str, dict]:
    return {it["id"]: it for it in reg.get("items", [])}


def upsert(reg: dict, item: dict) -> dict:
    """Add the item, or refresh an existing one of the same id WITHOUT
    resetting its human-set status or original created date (so a re-scan
    never reopens a done/dismissed item or rewrites when it was first found)."""
    items = reg.setdefault("items", [])
    existing = _by_id(reg).get(item["id"])
    if existing is None:
        items.append(item)
    else:
        existing["summary"] = item["summary"]
        existing["evidence"] = item["evidence"] or existing.get("evidence", "")
        existing["detail"] = item["detail"] or existing.get("detail", "")
        # status and created are intentionally preserved
    items.sort(key=lambda i: (i["status"] != "open", i["kind"], i["pr"]))
    return reg


def set_status(reg: dict, item_id: str, status: str) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status {status!r} not in {sorted(STATUSES)}")
    it = _by_id(reg).get(item_id)
    if it is None:
        raise KeyError(item_id)
    it["status"] = status
    return reg
