# review-new-pr

Static-gate review harness for new PRs as they arrive on the triaged
repository — a standalone layer that runs in the target repo's own CI,
separate from the triage pipeline in this repo.

## Contents

| Path | Description |
|------|-------------|
| `harness/` | Gate validators and GitHub Actions workflow |
| `harness/gates/` | Individual check modules (dep diff, secret scan, template check) |
| `harness/actions/` | Harness runner actions |
| `harness/tests/` | Test suite for gates |
| `harness/workflow.yml` | GitHub Actions workflow definition |
| `harness/run_harness.py` | Main harness entry point |

## Running gates locally

```bash
cd review-new-pr/harness
python3 run_harness.py --pr <PR_NUMBER>
```

See `harness/README.md` for full setup and configuration docs.
