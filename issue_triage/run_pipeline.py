"""Entry point for the store-backed issue pipeline. Delegates to issue_pipeline
(INGEST -> CLUSTER); the agentic ANALYZE phase runs in parallel batches via
analyze_issues.py.

  python issue_triage/run_pipeline.py              # full run incl. live fetch
  python issue_triage/run_pipeline.py --skip-fetch # reuse the existing store
"""
from issue_triage import issue_pipeline


def main():
    issue_pipeline.main()


if __name__ == "__main__":
    main()
