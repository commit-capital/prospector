"""design_review: the Prospector-run reviewer's agent, its answer validation
and its hunt pool."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import coverage, design_review, headless_agent, profile, settings
from pipeline.model import Pr


def test_mode_defaults_to_advisory(monkeypatch):
    monkeypatch.delenv("TRIAGE_DESIGN_REVIEW", raising=False)
    assert settings.design_review_mode() == "advisory"


def test_mode_reads_choices_and_falls_back(monkeypatch):
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "Gate")
    assert settings.design_review_mode() == "gate"
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "bogus")
    assert settings.design_review_mode() == "advisory"

HEAD = "h" * 40
DIFF = """diff --git a/src/new.ts b/src/new.ts
--- a/src/new.ts
+++ b/src/new.ts
@@ -0,0 +1,2 @@
+export const pad = (s: string) => s.padStart(2, "0");
"""


def _finding(**over) -> dict:
    f = {"category": "reuse", "severity": "major", "path": "src/new.ts", "line": 1,
         "title": "duplicates pad", "body": "src/util.ts already exports pad",
         "evidence_path": "src/util.ts"}
    f.update(over)
    return f


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    root = tmp_path / "clone"
    (root / "src").mkdir(parents=True)
    (root / "src" / "util.ts").write_text("export const pad = () => 1;\n")
    (root / "AGENTS.md").write_text("Reuse helpers in src/util.ts.\n")
    (tmp_path / "outside.ts").write_text("export const pad = () => 1;\n")
    return root


@pytest.fixture
def diff(tmp_path: Path) -> Path:
    p = tmp_path / f"{HEAD}.diff"
    p.write_text(DIFF)
    return p


def test_validate_keeps_an_anchored_finding(clone):
    summary, kept = design_review.validate({"summary": "ok", "findings": [_finding()]},
                                           touched={"src/new.ts"}, clone=clone)
    assert summary == "ok"
    assert [f["evidence_path"] for f in kept] == ["src/util.ts"]
    assert kept[0]["resolved"] is False and kept[0]["outdated"] is False
    assert kept[0]["severity"] == "major" and kept[0]["category"] == "reuse"


@pytest.mark.parametrize("bad", [
    {"category": "style"},
    {"severity": "fatal"},
    {"path": "elsewhere.ts"},
    {"evidence_path": "src/missing.ts"},
    {"evidence_path": "../outside.ts"},
    {"evidence_path": None},
    {"title": ""},
])
def test_validate_drops_what_it_cannot_anchor(clone, bad):
    _, kept = design_review.validate({"summary": "", "findings": [_finding(**bad)]},
                                     touched={"src/new.ts"}, clone=clone)
    assert kept == []


def test_validate_needs_no_evidence_outside_reuse(clone):
    f = _finding(category="test", evidence_path=None, line="7")
    _, kept = design_review.validate({"findings": [f, "junk"]}, touched={"src/new.ts"}, clone=clone)
    assert [(k["category"], k["line"]) for k in kept] == [("test", None)]


def test_review_hands_the_agent_coverage_and_stores_it(monkeypatch, clone, diff):
    seen: dict = {}

    def fake(prompt: str, **kw) -> str:
        seen["prompt"] = prompt
        return json.dumps({"summary": "s", "findings": []})
    monkeypatch.setattr(headless_agent, "run_agent", fake)
    rec = coverage.record(coverage.MEASURED, base_sha="b" * 40, tests=["src/new.test.ts"],
                          wanted={"src/new.ts": [1]},
                          results={"src/new.ts": {"measured": True, "executable": [1], "hit": []}})
    entry = design_review.review(clone, diff, pr=7, title="t", head_sha=HEAD, base_sha="b" * 40,
                                 coverage_record=rec)
    assert "coverage 0% (0/1 changed lines)" in seen["prompt"]
    assert "__COVERAGE__" not in seen["prompt"]
    assert entry["extra"]["coverage"] == rec


def test_review_runs_locked_down(monkeypatch, clone, diff):
    seen: dict = {}

    def fake(prompt: str, **kw) -> str:
        seen.update(kw, prompt=prompt)
        return json.dumps({"summary": "one dup", "findings": [_finding()]})
    monkeypatch.setattr(headless_agent, "run_agent", fake)
    entry = design_review.review(clone, diff, pr=7, title="t", head_sha=HEAD, base_sha="b" * 40)
    assert seen["allow_gh"] is False and seen["edit_root"] is None and seen["env_allow"] == ()
    assert set(seen["read_root"]) == {str(clone.resolve()), str(diff.resolve())}
    assert seen["cwd"] == str(clone.resolve())
    assert "Reuse helpers in src/util.ts." in seen["prompt"]
    assert str(diff.resolve()) in seen["prompt"]
    assert entry["kind"] == "review" and entry["reviewed_sha"] == HEAD
    assert entry["summary"] == "one dup" and len(entry["findings"]) == 1
    assert entry["extra"]["base_sha"] == "b" * 40


@pytest.mark.parametrize("answer", ["no json here", '{"findings": "nope"}'])
def test_review_raises_on_an_unusable_answer(monkeypatch, clone, diff, answer):
    monkeypatch.setattr(headless_agent, "run_agent", lambda prompt, **kw: answer)
    with pytest.raises(design_review.DesignReviewFailed):
        design_review.review(clone, diff, pr=7, title="t", head_sha=HEAD, base_sha="b")


def test_review_raises_when_the_agent_does_not_finish(monkeypatch, clone, diff):
    def boom(prompt: str, **kw) -> str:
        raise RuntimeError("exit 1")
    monkeypatch.setattr(headless_agent, "run_agent", boom)
    with pytest.raises(design_review.DesignReviewFailed):
        design_review.review(clone, diff, pr=7, title="t", head_sha=HEAD, base_sha="b")


def test_review_lets_an_outage_through(monkeypatch, clone, diff):
    def down(prompt: str, **kw) -> str:
        raise headless_agent.AgentUnavailable("not logged in")
    monkeypatch.setattr(headless_agent, "run_agent", down)
    with pytest.raises(headless_agent.AgentUnavailable):
        design_review.review(clone, diff, pr=7, title="t", head_sha=HEAD, base_sha="b")


def _pr(**over) -> Pr:
    rec = {"pr": 1, "meta": {"state": "open", "head_sha": HEAD},
           "threat": {"verdict": "clear", "signatures": [], "against_head_sha": HEAD,
                      "checked_at": "2026-10-13T00:00:00+00:00"}}
    rec.update(over)
    return Pr(None, rec)


def test_huntable_wants_a_clear_open_unreviewed_head():
    assert design_review.huntable(_pr()) is True


@pytest.mark.parametrize("over", [
    {"meta": {"state": "closed", "head_sha": HEAD}},
    {"threat": {"verdict": "suspicious", "signatures": ["x"], "against_head_sha": HEAD}},
    {"threat": {"verdict": "clear", "signatures": [], "against_head_sha": "o" * 40}},
    {"threat": {"verdict": "clear", "signatures": [], "against_head_sha": HEAD,
                "detail": {"exempt": "dependency-bump"}}},
    {"reviews": {"design": {"kind": "review", "reviewed_sha": HEAD, "findings": [],
                            "checks": []}}},
    {"meta": {"state": "open", "head_sha": HEAD, "draft": True}},
    {"analysis": {"disposition": "close-dup", "rationale": "r", "against_head_sha": HEAD,
                  "checked_at": "2026-10-13T00:00:00+00:00"}},
])
def test_huntable_refuses(over):
    assert design_review.huntable(_pr(**over)) is False


def test_huntable_re_reviews_a_moved_head():
    old = {"design": {"kind": "review", "reviewed_sha": "o" * 40, "findings": [], "checks": []}}
    assert design_review.huntable(_pr(reviews=old)) is True


def test_profile_accepts_design_as_a_fixable_gate():
    assert "design" in profile.AUTOFIX_GATES
