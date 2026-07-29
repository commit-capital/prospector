"""Generated views over the store. Markdown is OUTPUT here, never input —
nothing in the pipeline parses these files back.

Usage: uv run python views.py   (writes pipeline/STATUS.md)
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pipeline import gates, storekit
from pipeline.store import Store


def status_md(store: Store) -> str:
    prs = store.all_prs()
    clusters = store.all_clusters()
    open_prs = {n: r for n, r in prs.items() if r.state == "open"}
    draft_count = sum(1 for r in open_prs.values() if r.draft)

    lines = [
        "# Pipeline status",
        "",
        f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} — do not edit; "
        f"regenerate with `pipeline/views.py`._",
        "",
        f"- Open PRs in store: **{len(open_prs)}** ({draft_count} draft)",
        f"- Clusters: **{len(clusters)}**",
    ]

    runs = store.runs()
    phase_runs = [r for r in runs if isinstance(r, storekit.PhaseRun)]
    if phase_runs:
        lines.append("\n## Last runs\n")
        seen: dict[str, storekit.PhaseRun] = {}
        for r in phase_runs:
            seen[r.phase] = r
        for phase, r in seen.items():
            lines.append(f"- **{phase}**: finished {r.finished} — {r.raw.get('stats')}")

    edits = [r for r in runs if isinstance(r, storekit.StoreEdit)]
    if edits:
        lines.append("\n## Store edits\n")
        for e in edits[-5:]:
            lines.append(f"- **{e.transform}** on `{e.table}` at {e.ts}: "
                         f"{len(e.changed)} changed, {len(e.deleted)} deleted "
                         f"of {e.examined} examined")

    if clusters:
        states = Counter(gates.cluster_state(c, prs) for c in clusters.values())
        lines.append("\n## Cluster states\n")
        for s, n in states.most_common():
            lines.append(f"- {s}: {n}")
        lines.append("\n| # | Root problem | PRs | State | Outcome |")
        lines.append("|---|---|---|---|---|")
        for cid in sorted(clusters):
            c = clusters[cid]
            lines.append(f"| {cid} | {(c.root_problem or '')[:80]} | {len(c.prs)} "
                         f"| {gates.cluster_state(c, prs)} | {c.outcome or '—'} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    store = Store()
    out = Path(__file__).resolve().parent / "STATUS.md"
    out.write_text(status_md(store))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
