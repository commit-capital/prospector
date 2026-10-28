"""The fixer's prompts, standard and default models match its pin. A change to
any of them needs a live gate run that re-pins (pipeline/evals/README.md); no
model is called here."""
from __future__ import annotations

import json

from pipeline.evals import fixer_eval
from pipeline.evals import reviewer_eval as re_


def test_the_fixer_matches_its_pin():
    pin = json.loads(re_.PINS.read_text()).get(fixer_eval.PIN) or {}
    config = fixer_eval.config()
    assert (pin.get("prompt_sha"), pin.get("model")) == (config["prompt_sha"], config["model"]), (
        f"fixer: code is {config['prompt_sha']}/{config['model']}, pinned "
        f"{pin.get('prompt_sha')}/{pin.get('model')}. Run: uv run python -m "
        f"pipeline.evals.fixer_eval --live")


def test_the_pin_ignores_the_environment(monkeypatch):
    before = fixer_eval.config()
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_MODELS", "haiku")
    assert fixer_eval.config() == before and before["model"] == "opus,sonnet,opus"


def test_init_adds_only_a_missing_fixer_pin(tmp_path):
    path = tmp_path / "pins.json"
    path.write_text(json.dumps({"intake": {"prompt_sha": "x", "model": "m"}}))
    fixer_eval.init_pin(path)
    pins = json.loads(path.read_text())
    assert pins["intake"] == {"prompt_sha": "x", "model": "m"}
    assert pins["fixer"] == {**fixer_eval.config(), "provisional": True, "gate_set": None,
                             "score": None, "run": None}
    pins["fixer"]["prompt_sha"] = "old"
    path.write_text(json.dumps(pins))
    fixer_eval.init_pin(path)
    assert json.loads(path.read_text())["fixer"]["prompt_sha"] == "old"
