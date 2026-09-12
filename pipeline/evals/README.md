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
