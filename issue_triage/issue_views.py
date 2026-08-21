"""Generate the dev-facing hand-off docs FROM the issue store (never parsed back):
ISSUE-STATUS.md (snapshot + pain leaderboard) and SUMMARY.md (per-cluster table).
Mirrors pipeline/views.py for PRs. All output is advisory; closes run through the
app executor gated on confirmed curation.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from issue_triage.issue_store import IssueStore
from pipeline import settings

if TYPE_CHECKING:
    from issue_triage.issue_model import IssueCluster

ROOT = Path(__file__).resolve().parent
ISSUE_STATUS = ROOT.parent / "ISSUE-STATUS.md"
SUMMARY = ROOT / "SUMMARY.md"


def _ranked(store: IssueStore) -> list[IssueCluster]:
    cls = [c for c in store.all_issue_clusters().values() if c.members]
    cls.sort(key=lambda c: -(c.pain or 0.0))
    return cls


def _confirmed(c: IssueCluster) -> bool:
    return bool((c.curation or {}).get("confirmed"))


def _label(c: IssueCluster) -> str:
    return (c.curation or {}).get("label") or c.title or f"cluster {c.id}"


def status_md(store: IssueStore) -> str:
    issues = store.all_issues()
    cls = _ranked(store)
    confirmed = [c for c in cls if _confirmed(c)]
    dup_issues = sum(1 for i in issues.values() if i.disposition == "close-dup")
    repro_issues = sum(1 for i in issues.values() if i.disposition == "request-repro")
    lines = [
        f"# {settings.display_name()} Issue Triage — Status", "",
        "_Generated from the issue store. Suggested actions only; closes run through "
        f"the app executor as {settings.bot_login()}, gated on confirmed curation._", "",
        "| What we found | Count |", "|---|---|",
        f"| Open issues | {len(issues)} |",
        f"| Clusters | {len(cls)} |",
        f"| Confirmed dup clusters | {len(confirmed)} |",
        f"| Issues routed close-dup | {dup_issues} |",
        f"| Issues needing repro | {repro_issues} |", "",
        "## Pain leaderboard (top 25)", "",
        "| Rank | Cluster | Title | Canonical | Pain | Members | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, c in enumerate(cls[:25], 1):
        status = "✅ confirmed" if _confirmed(c) else ("⚠️ split" if c.needs_review else "candidate")
        canon = f"#{c.canonical}" if c.canonical else "—"
        pain = c.pain if c.pain is not None else "—"
        lines.append(f"| {i} | C{c.id:03d} | {_label(c)} | {canon} | {pain} | {len(c.members)} | {status} |")
    return "\n".join(lines) + "\n"


def summary_md(store: IssueStore) -> str:
    cls = _ranked(store)
    lines = ["# Issue clusters — summary", "",
             "_Per-cluster detail. The TL;DR snapshot is at the top of `ISSUE-STATUS.md`._", "",
             "| Cluster | Title | Canonical | Members | Pain | Subsystem | Status |",
             "|---|---|---|---|---|---|---|"]
    for c in cls:
        status = "confirmed" if _confirmed(c) else ("split" if c.needs_review else "candidate")
        canon = f"#{c.canonical}" if c.canonical else "—"
        pain = c.pain if c.pain is not None else "—"
        lines.append(f"| C{c.id:03d} | {_label(c)} | {canon} | {len(c.members)} | "
                     f"{pain} | {c.subsystem or '—'} | {status} |")
    return "\n".join(lines) + "\n"


def write(store: IssueStore | None = None) -> None:
    store = store or IssueStore()
    ISSUE_STATUS.write_text(status_md(store))
    SUMMARY.write_text(summary_md(store))
    print(f"rendered {ISSUE_STATUS.name}, {SUMMARY.name} from the issue store")


if __name__ == "__main__":
    write()
