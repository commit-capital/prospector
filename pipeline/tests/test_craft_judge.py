"""craft_judge: a fix judged blind against a reference fix, in both orders, by
the craft standard the fixer was given; and the cheap flags beside it. The
agent is canned."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pipeline import authoring, headless_agent
from pipeline.evals import craft_judge as cj

OURS = "diff --git a/src/a.ts b/src/a.ts\n+const OURS_MARK = 1;\n"
REF = "diff --git a/src/a.ts b/src/a.ts\n+const REF_MARK = 1;\n"


def _agent(monkeypatch, answer) -> list[str]:
    """`answer(prompt)` decides; returns the prompts seen."""
    prompts: list[str] = []

    def run(prompt: str, **kw) -> str:
        prompts.append(prompt)
        return answer(prompt)
    monkeypatch.setattr(headless_agent, "run_agent", run)
    return prompts


def _first(prompt: str) -> str:
    """Which mark is Fix A in this prompt."""
    a = prompt.index("Fix A")
    return "ours" if prompt.find("OURS_MARK", a) < prompt.find("REF_MARK", a) else "ref"


def _prefers(side: str, points: list[int] | None = None):
    def answer(prompt: str) -> str:
        better = "A" if _first(prompt) == side else "B"
        return json.dumps({"better": better, "reason": f"{side} reads better",
                           "points": points or [2]})
    return answer


def test_points_are_house_style_s_bullets():
    assert len(cj.POINTS) == authoring.HOUSE_STYLE.count("\n- ")
    assert all(p and not p.startswith("- ") for p in cj.POINTS)


@pytest.mark.parametrize("side,result", [("ours", "win"), ("ref", "loss")])
def test_agreement_in_both_orders_is_a_win_or_loss(tmp_path, monkeypatch, side, result):
    prompts = _agent(monkeypatch, _prefers(side))
    craft = cj.judge(tmp_path, OURS, REF, title="t", body="b")
    assert len(prompts) == 2 and {_first(p) for p in prompts} == {"ours", "ref"}
    assert craft.result == result and craft.points == [2] and craft.error is None
    assert craft.judge_sha == cj.JUDGE_SHA


def test_disagreeing_orders_are_a_tie(tmp_path, monkeypatch):
    _agent(monkeypatch, lambda prompt: json.dumps({"better": "A", "reason": "r", "points": []}))
    assert cj.judge(tmp_path, OURS, REF, title="t", body="b").result == "tie"


def test_the_prompt_never_names_ours_or_the_merged_fix(tmp_path, monkeypatch):
    prompts = _agent(monkeypatch, _prefers("ours"))
    cj.judge(tmp_path, OURS, REF, title="t", body="b")
    for prompt in prompts:
        text = prompt.replace("OURS_MARK", "").replace("REF_MARK", "")
        assert not re.search(r"\b(merged|human|ours|our|reference)\b", text, re.IGNORECASE)


def test_off_range_points_are_dropped(tmp_path, monkeypatch):
    _agent(monkeypatch, _prefers("ref", points=[0, 1, 99]))
    assert cj.judge(tmp_path, OURS, REF, title="t", body="b").points == [1]


def test_a_garbled_answer_is_an_error_not_a_crash(tmp_path, monkeypatch):
    _agent(monkeypatch, lambda prompt: "no json here")
    craft = cj.judge(tmp_path, OURS, REF, title="t", body="b")
    assert craft.result is None and craft.error


def test_an_empty_reference_is_not_judged(tmp_path, monkeypatch):
    prompts = _agent(monkeypatch, _prefers("ours"))
    craft = cj.judge(tmp_path, OURS, "", title="t", body="b")
    assert craft.result is None and craft.error and prompts == []


def _added(path: str, *lines: str) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,0 +1,{len(lines)} @@\n{body}")


def test_flags_foreign_text(tmp_path):
    sql = _added("src/q.ts", "const q = \"SELECT id FROM users WHERE name = '\" + n + \"'\";")
    assert cj.flags(sql, tmp_path) == ["foreign-text"]
    plain = _added("src/q.ts", "const re = /^[a-z]+$/;", "throw new Error(\"not found\");")
    assert cj.flags(plain, tmp_path) == []


def test_flags_duplicated_block(tmp_path):
    block = ["const a = load(x);", "if (!a) return null;", "const b = a.items.map(f);",
             "return b.filter(Boolean);"]
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sibling.ts").write_text("\n".join(["// other", *block, ""]))
    assert cj.flags(_added("src/new.ts", *block), tmp_path) == ["duplicated-block"]
    fresh = ["const w = 1;", "const v = w + 2;", "log(v);", "return v * 3;"]
    assert cj.flags(_added("src/new.ts", *fresh), tmp_path) == []


def test_fixer_config_changes_with_the_standard(monkeypatch):
    before = cj.fixer_config("cross")
    monkeypatch.setattr(authoring, "HOUSE_STYLE", authoring.HOUSE_STYLE + "\n- one more rule")
    after = cj.fixer_config("cross")
    assert before["prompt_sha"] != after["prompt_sha"] and before["models"] == after["models"]
    assert cj.fixer_config("staged")["prompt_sha"] != before["prompt_sha"]


def test_the_judge_reads_only_the_tree(tmp_path, monkeypatch):
    seen: dict = {}

    def run(prompt: str, **kw) -> str:
        seen.update(kw)
        return json.dumps({"better": "tie", "reason": "r", "points": []})
    monkeypatch.setattr(headless_agent, "run_agent", run)
    cj.judge(tmp_path, OURS, REF, title="t", body="b")
    assert seen["allow_gh"] is False and seen.get("edit_root") is None
    assert [str(Path(p)) for p in seen["read_root"]] == [str(tmp_path)]


def test_score_craft_judges_over_the_pre_fix_tree(tmp_path, monkeypatch):
    base = tmp_path / "base"
    (base / "src").mkdir(parents=True)
    (base / "src" / "a.ts").write_text("export const a = 1;\n")
    pre = ("diff --git a/src/a.ts b/src/a.ts\n--- a/src/a.ts\n+++ b/src/a.ts\n"
           "@@ -1 +1 @@\n-export const a = 1;\n+export const a = 0;\n")
    seen: dict = {}

    def judge(worktree, ours, reference, *, title, body):
        seen["text"] = (Path(worktree) / "src" / "a.ts").read_text()
        seen["tree"] = Path(worktree)
        return cj.Craft("win", reasons=["r"], points=[1])
    monkeypatch.setattr(cj, "judge", judge)
    out = cj.score_craft(base, pre, OURS, REF, title="t", body="b")
    assert seen["text"] == "export const a = 0;\n" and not seen["tree"].exists()
    assert out["craft"]["result"] == "win" and out["flags"] == []
