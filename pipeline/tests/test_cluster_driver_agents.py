"""The headless summarize agent's reach. run_agent is a subprocess boundary,
mocked here."""
from __future__ import annotations

import json
import os

from pipeline import cluster_driver, headless_agent


def test_the_summarize_agent_reads_only_its_batch_and_the_listed_diffs(monkeypatch, tmp_path):
    seen: dict = {}

    def fake_run(prompt, **kw):
        path = next(t for t in prompt.split() if "summarize-" in t).rstrip(".,—")
        seen.update(kw, batch=json.load(open(path)), prompt=prompt)
        return '{"items": []}'

    monkeypatch.setattr(headless_agent, "run_agent", fake_run)
    diff = tmp_path / "abc.diff"
    cluster_driver.run_summarize_agent(
        [{"pr": 1, "head_sha": "abc", "title": "t", "diff_path": str(diff)}])
    resolved = os.path.realpath(diff)
    assert seen["read_root"] == [seen["cwd"], resolved]
    assert seen["batch"][0]["diff_path"] == resolved
    assert seen["allow_gh"] is False and list(seen["env_allow"]) == []
    assert "__REPO__" not in seen["prompt"] and "__BATCH_PATH__" not in seen["prompt"]
