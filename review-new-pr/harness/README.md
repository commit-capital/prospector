# Layer 1 — Static Gate Harness

The minimum-viable per-PR review pipeline.

Three static gates, zero LLM cost, runs in GitHub Actions on every PR push.
Produces a structured verdict JSON. Layer 2 (a cross-PR review service)
consumes the verdicts; **this layer never posts or merges**.

## What's here

```
harness/
├── README.md                     ← this file
├── workflow.yml                  ← GitHub Actions workflow to ship to the target repo
├── gates/
│   ├── _common.py               ← PRContext + Verdict types, profile access
│   ├── secret_scan.py           ← detects new credential patterns in diff
│   ├── dep_diff.py              ← flags new package.json deps against allowlist
│   └── pr_template_check.py     ← validates the profile's PR-template sections
├── actions/                      ← entry-point scripts the workflow invokes
│   ├── run_single_gate.py
│   └── reduce_verdicts.py
├── tests/                        ← pytest suite
└── run_harness.py                ← local driver — runs gates over cached PRs
```

The harness is standalone on purpose: stdlib-only, run under a bare
`python3`, never importing `pipeline` — so the whole directory can be copied
into the target repository's CI unchanged. Repository policy (the required
PR-template sections) comes from the same profile JSON the pipeline uses
(`TRIAGE_PROFILE`), read directly with stdlib `json`.

## Local test — validate the harness against real PRs

The triage repo caches the open corpus's PR diffs (`pipeline/cache/diffs/`);
the local runner uses these.

```bash
cd review-new-pr/harness

# One specific PR
python3 run_harness.py --pr 4318

# Just one gate
python3 run_harness.py --pr 4318 --gate secret_scan

# Batch: every cached PR. Writes verdicts/<pr>.json + _aggregate.json
python3 run_harness.py --batch
```

### Calibration baseline (as of 2026-05-18)

Last batch run over the original deployment's 2,231-PR cached backlog:

| Outcome | Count | % |
|---|---|---|
| `pass` (all gates pass) | 662 | 30% |
| `needs_human_review` (any uncertain or low-sev fail) | 1,512 | 68% |
| `auto_reject` (any high/critical fail) | 15 | <1% |
| Parse errors (diff cache issue) | 42 | 2% |

Per-gate breakdown:

| Gate | pass | fail | uncertain |
|---|---|---|---|
| `secret_scan` | 2,180 | 0 | 9 |
| `dep_diff` | 2,033 | 0 | 156 |
| `pr_template_check` | 711 | 1,349 | 129 |

The 15 auto-rejects are all PRs with empty bodies — high-confidence
"please fill out the template" responses, never reaching human triage. No
false-positive auto-rejects across the entire backlog.

## Run the tests

```bash
uv run pytest review-new-pr/harness/tests -v
```

The suite covers happy-path detection, malicious payloads (real-shaped keys,
typosquats, prompt-injection bodies), and fixture downgrade for test files.

## Deploy to the target repository

1. Open a PR against the triaged repository that adds:
   ```
   .github/workflows/pr-review-harness.yml      ← from workflow.yml
   .github/harness/run_single_gate.py           ← from actions/run_single_gate.py
   .github/harness/reduce_verdicts.py           ← from actions/reduce_verdicts.py
   .github/harness/gates/                       ← entire gates/ directory
   ```

2. Verdicts publish as Actions artifacts (no orphan branch yet — that's
   Sub-project 2 in the design doc).

3. Layer 2 fetches verdicts via the Actions API.

## Security stance

What this harness can do:
- Read the PR diff and PR body
- Run pure-Python pattern detection (no contributor code execution)
- Write a verdict JSON artifact

What this harness **cannot** do:
- Post comments (no write token)
- Close PRs (no write token)
- Run contributor's `pnpm install` (gates are static; that's Sub-project 2's medium-cost gates)
- Access secrets (gates have no env-var access beyond `GITHUB_TOKEN` which is read-only per the workflow's `permissions:` block)

The reducer job that produces the final verdict checks out the BASE branch
(trusted), not the PR head. A malicious PR cannot tamper with its own verdict.

## What's NOT in v1 (per the design)

These come in later sub-projects:

- **Build/test gates** (typecheck, build, tests in sandboxed runner): Sub-project 1.5
- **LLM-based gates** (code quality, correctness, roadmap alignment): Sub-project 2
- **Orphan-branch verdict storage** for Layer 2 consumption: Sub-project 2
- **Layer 2 cross-PR review service**: Sub-projects 3–4
- **Training data capture**: Sub-project 5

Each is independently deliverable. v1 is the smallest reasonable opening
move — a single workflow file + three Python gates, reviewable in 10 minutes.

## Tunable knobs

If you want to adjust the harness, the things to change:

- **Secret patterns** — `gates/secret_scan.py` `SECRET_PATTERNS` list.
- **Allowlist** — `gates/dep_diff.py` is seeded from the package.json files of a local checkout named by `HARNESS_DEP_SEED_REPO` (unset → empty allowlist). For CI deployment, ship a static allowlist JSON generated from the upstream default branch.
- **PR template sections** — `harness.pr_template.required_sections` / `recommended_sections` in the repository profile (`TRIAGE_PROFILE`). Should mirror the target repo's PR template.
- **Overall verdict rules** — `run_harness.py` / `reduce_verdicts.py` `derive_overall()`. Currently: any high/critical fail → auto_reject; any other fail or uncertain → needs_human_review.

## Iteration loop

1. Edit a gate.
2. `pytest tests/test_<gate>.py` until the new behavior is captured.
3. `python3 run_harness.py --batch` and compare `verdicts/_aggregate.json` to the baseline above.
4. Spot-check the now-fail and now-pass PRs to confirm the change makes sense at the population level.
5. When the distribution looks right, ship.

Three gates, no LLM in the loop, validated against a 2,231-PR real backlog
with zero false-positive auto-rejects.
