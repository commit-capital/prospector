"""Move-to-top: an operator's priority on a waiting request, one above the
highest its queue holds, so repeated clicks stack in click order. Every queue
order sorts by it first (verify_worker.next_queued, fix_worker._oldest,
fix_review.queued_issues)."""
from __future__ import annotations

from typing import Literal

from prospector_app.backend import data

Kind = Literal["verify", "fix", "issue-fix"]


def _top_priority(sections: list[dict | None]) -> float:
    return max((float(s.get("priority") or 0) for s in sections if s), default=0.0)


def top(kind: Kind, n: int) -> dict:
    if kind == "issue-fix":
        from issue_triage.issue_store import IssueStore
        store = IssueStore()
        queued = store.issues_matching(("fix_request", "status"), ["queued"])
        priority = _top_priority([i.fix_request for i in queued.values()]) + 1
        ok = store.prioritize_fix_request(n, priority)
    else:
        section = "verify_request" if kind == "verify" else "fix_request"
        priority = _top_priority([rec.raw.get(section) for rec in data.prs().values()]) + 1
        st = data.store()
        ok = (st.prioritize_verify_request(n, priority) if kind == "verify"
              else st.prioritize_fix_request(n, priority))
        data.refresh()
    if not ok:
        raise ValueError(f"#{n} is not waiting in the {kind} queue")
    return {"kind": kind, "n": n, "priority": priority}
