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


def test_a_pass_with_nothing_to_scan_is_timed_in_the_ledger(tmp_path, monkeypatch):
    from alert_triage.alert_store import AlertStore
    from pipeline import storekit
    monkeypatch.setattr(find_fixed.config, "mint_token", lambda: None)
    assert find_fixed.main(["--store", str(tmp_path)]) == 0
    passes = [r for r in AlertStore(tmp_path).runs()
              if isinstance(r, storekit.PhaseRun) and r.phase == "alert-find-fixed"]
    assert len(passes) == 1 and passes[0].started and passes[0].finished


def test_main_stops_at_the_first_usage_limit_hit(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timezone

    from alert_triage.alert_model import Alert
    from alert_triage.alert_store import AlertStore, alert_id
    from pipeline import headless_agent, storekit
    store = AlertStore(tmp_path)
    for n in range(1, 4):
        Alert(store, {"id": alert_id("code-scanning", n)}).apply_facts({
            "source": "code-scanning", "number": n, "state": "open", "raw_state": "open",
            "severity": "medium", "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-02T00:00:00Z",
            "html_url": f"https://github.com/o/r/security/code-scanning/{n}"})
    reset = datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)
    calls: list[list[int]] = []

    def fake_batch(entries):
        calls.append([e["number"] for e in entries])
        raise headless_agent.CapacityExhausted("usage limit reached", resets_at=reset)

    monkeypatch.setattr(find_fixed.config, "mint_token", lambda: None)
    monkeypatch.setattr(find_fixed, "run_batch_agent", fake_batch)
    rc = find_fixed.main(["--batch", "1", "--concurrency", "1", "--store", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert len(calls) == 1
    assert (f"AI usage limit reached — resets at {reset.astimezone():%H:%M} "
            "(2 batch(es) not started); stopping.") in out
    passes = [r for r in AlertStore(tmp_path).runs()
              if isinstance(r, storekit.PhaseRun) and r.phase == "alert-find-fixed"
              and r.raw.get("stats")]
    assert passes[0].raw["stats"]["failed_batches"] == 3
