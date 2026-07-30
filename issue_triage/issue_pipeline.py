"""Run the deterministic issue phases in order against the store:
INGEST -> CLUSTER. Each phase stamps freshness; re-running only recomputes what an
issue's moved updated_at has staled.

The agentic ANALYZE phase is not run here — it runs in parallel batches via
`analyze_issues.py` (the app's issue-analyze job or the CLI). This
orchestrator prints how many issues are pending analysis when it finishes.

  python issue_triage/issue_pipeline.py            # full: live fetch + deterministic phases
  python issue_triage/issue_pipeline.py --skip-fetch
"""
from __future__ import annotations

import sys

from issue_triage import issue_analyze_driver
from issue_triage import issue_cluster_driver
from issue_triage import issue_ingest
from issue_triage.issue_store import IssueStore


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    store = IssueStore()
    if "--skip-fetch" not in argv:
        issue_ingest.main()
    n_clusters = issue_cluster_driver.run(store)
    pend = issue_analyze_driver.pending(store)
    print(f"deterministic phases complete: {len(store.all_issues())} issues, "
          f"{n_clusters} clusters; {len(pend)} pending agentic ANALYZE "
          f"(run analyze_issues.py --limit N)")


if __name__ == "__main__":
    main()
