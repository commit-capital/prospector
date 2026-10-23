"""Every reviewer's prompt and default model match its pin. A change to either
needs a live eval run that re-pins (pipeline/evals/README.md); no model is
called here."""
from __future__ import annotations

import json

from pipeline.evals import reviewer_eval as re_


def test_every_reviewer_matches_its_pin():
    pins = json.loads(re_.PINS.read_text())
    for reviewer, config in re_.configs().items():
        pin = pins.get(reviewer) or {}
        assert (pin.get("prompt_sha"), pin.get("model")) == (config["prompt_sha"], config["model"]), (
            f"{reviewer}: code is {config['prompt_sha']}/{config['model']}, pinned "
            f"{pin.get('prompt_sha')}/{pin.get('model')}. Run: uv run python -m "
            f"pipeline.evals.reviewer_eval --reviewer {reviewer} --live")


def test_init_writes_only_missing_pins(tmp_path):
    path = tmp_path / "pins.json"
    path.write_text(json.dumps({"intake": {"prompt_sha": "old", "model": "m",
                                           "provisional": False, "score": None, "run": None}}))
    re_.init_pins(path)
    pins = json.loads(path.read_text())
    assert pins["intake"]["prompt_sha"] == "old"
    assert set(pins) == set(re_.configs())
    assert pins["design"]["provisional"] is True and pins["design"]["score"] is None
