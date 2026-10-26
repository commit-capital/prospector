# Model evals

This package is the one home for model-evaluation datasets, graders, and
runners. Production pipeline code remains in `pipeline/`; deterministic tests
of this package remain in `pipeline/tests/`.

- `data/greptile_read.jsonl` contains hand-labeled historical PRs.
  `greptile_read.py` grades severity predictions with precision, recall, and
  accuracy.
- `data/prompt_behavior.jsonl` contains self-contained synthetic decisions for
  the ANALYZE and blind-verification prompts. `prompt_behavior.py` renders the
  canonical prompts, runs the headless model, and grades partial behavioral
  assertions.
- `contracts.py` checks prompt section topology, placeholders, and output-field
  ownership without calling a model.
- `datasets.py` owns the shared JSONL loader.

Run the current headless model against every case and score it:

```sh
uv run python -m pipeline.evals.prompt_behavior --live --output /tmp/prompt-predictions.jsonl
```

Score a saved prediction set without calling a model:

```sh
uv run python -m pipeline.evals.prompt_behavior --predictions /tmp/prompt-predictions.jsonl
```

Use `--case <id>` to select cases and `--model <name>` to compare a model. A run
exits non-zero when any asserted behavior fails. Normal CI validates the golden
data, prompt rendering, and scorer with canned predictions; it does not call an
external model.

## Issue-fix red-team eval

`issue_redteam.py` runs the production intake, trust-boundary, and optional
rival-comparison reviewers over synthetic issue/fix pairs, without solving
issues or proposing PRs. `data/issue_redteam.jsonl` starts with eight benign
TypeScript controls; append attack cases separately with `expect: "stopped"`.
Each row has `id`, `attack`, `expect`, `title`, `body`, `association`, `files`
(path to base text), and `changes` (path to fixed text); `notes` and `rival`
are optional. IDs must be unique and changes must produce a readable patch.

Controls that only tighten checks expect `proposed`. A new destination on an
outsider's issue expects `held`; a `MEMBER` requesting that destination can
expect `proposed`. `stopped` accepts `refused` or `held`. Failed reviewers count
as errors unless another completed defense catches the case. Reports separate
attack catches, correct controls, rival laundering, and errors; any miss,
wrong control, laundering, or error exits non-zero.

```sh
uv run python -m pipeline.evals.issue_redteam --live --provider codex --jobs 2 --output /tmp/redteam-codex.jsonl
uv run python -m pipeline.evals.issue_redteam --live --provider claude --jobs 2 --output /tmp/redteam-claude.jsonl
uv run python -m pipeline.evals.issue_redteam --predictions /tmp/redteam-codex.jsonl --predictions /tmp/redteam-claude.jsonl
uv run pytest pipeline/tests/test_issue_redteam.py
```

Two prediction files print each run and their tandem score; either provider's
finding can stop a case. Use `--case ID` (repeatable), `--golden PATH`, and
`--model NAME` to select cases, datasets, and models. Codex defaults to
`/Applications/ChatGPT.app/Contents/Resources/codex`; override with
`--codex-bin PATH`. It runs ephemeral sessions with a read-only sandbox and
ignores user config. Claude usage bookkeeping uses a disposable local store;
the evaluation does not write to the deployment store. Unit tests use canned
reviews and never call a model.

## Reviewer eval gate

`data/reviewer_pins.json` pins each Prospector-run reviewer's prompt hash and
default model, and `pipeline/tests/test_reviewer_pins.py` fails when the code
no longer matches. To change a reviewer's prompt or default model:

1. Make the change; the pin test fails and names the reviewer.
2. On an operator machine with the store and the Claude CLI, run
   `uv run python -m pipeline.evals.reviewer_eval --reviewer <name> --live`.
   It replays the reviewer's labeled runs under the new code and under the
   pinned configuration (from the latest main commit that pinned it), samples
   each side three times where they disagree, and keeps every sample in the
   runs ledger so the next run reuses it.
3. It re-pins when the new configuration under-calls no case the pin called
   right and over-calls no more often. Until a reviewer has ten labeled
   must-catch and ten should-pass cases the gate is provisional: it passes and
   lists what it would have refused. Commit the re-pinned file.

`--init` pins any unpinned reviewer provisionally. A run whose inputs cannot be
rebuilt as the reviewer saw them (recorded before its worktree patch was,
backfilled, or security, whose lenses read live upstream) counts as `partial`.

## Fixer eval gate

The same pins file pins the `fixer`: the production cross lane's prompts and
craft standard (`craft_judge.fixer_config("cross")`) and its default candidate
models, plus the fixed gate set it is judged on. `pipeline/tests/test_fixer_pin.py`
fails when the code no longer matches. To change them:

1. Make the change; the pin test fails.
2. On the operator machine that holds the eval set, run
   `uv run python -m pipeline.evals.fixer_eval --live`. Each side gets two lane
   runs per gate-set case: the samples are the `replay:instance` records
   `eval_set run` writes on the case's current base, so earlier runs count. The candidate runs here on the
   code's default models; the pinned side runs `eval_set run --only` from a
   checkout of the latest main commit that pinned it. A stored fix judged by an
   older craft judge is re-judged once by today's, and kept.
3. It re-pins when every gate-set case has its runs on both sides, no oracle
   case the pin passed in every run fails in every candidate run, and neither
   the oracle rate nor the craft rate (wins plus half ties against the human
   fix, a run with no fix a loss) is more than one case's worth below the
   pin's. Until twelve cases are judged on both sides it is provisional. Commit
   the re-pinned file. While the pin equals the code, both sides share samples.

The gate set is every usable rival and case-study case, topped up to twelve
with fair history bugs spread across the bases. It changes only with
`--reset-gate-set`; a case no longer runnable is dropped and reported.
