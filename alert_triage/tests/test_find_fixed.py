"""The alert find-fixed wave's agent runner. run_agent is a subprocess boundary,
mocked here."""
from __future__ import annotations

import json
import os

from alert_triage import find_fixed


def test_the_batch_agent_reads_only_its_private_bundle_directory(monkeypatch):
    seen: dict = {}

    def fake_run(prompt, **kw):
        path = next(t for t in prompt.split() if "alert-find-fixed-" in t).rstrip(".,—")
        seen.update(kw, bundle=json.load(open(path)))
        return json.dumps({"verdicts": [{"id": 5, "verdict": "not-fixed"}]})

    monkeypatch.setattr(find_fixed.headless_agent, "run_agent", fake_run)
    entries = [{"id": 5, "source": "code-scanning", "number": 1}]
    assert find_fixed.run_batch_agent(entries) == [{"id": 5, "verdict": "not-fixed"}]
    assert seen["bundle"] == entries
    assert seen["read_root"] == [seen["cwd"]] and seen["allow_gh"] is True
    assert list(seen["env_allow"]) == [] and not os.path.exists(seen["cwd"])
