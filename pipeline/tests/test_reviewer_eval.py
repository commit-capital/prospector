"""reviewer_eval: replaying a labeled reviewer run offline, scoring it, and
gating a reviewer's configuration on the result. Agents are canned."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pipeline import headless_agent, verdict_labels, verdict_log
from pipeline.evals import reviewer_eval as re_
from pipeline.store import Store

DIFF = """diff --git a/src/x.ts b/src/x.ts
--- a/src/x.ts
+++ b/src/x.ts
@@ -1 +1 @@
-export const x = 1;
+export const x = 2;
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path, monkeypatch) -> tuple[Path, str]:
    """A local repository standing in for TRIAGE_REPO, and its one commit."""
    repo = tmp_path / "origin"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "x.ts").write_text("export const x = 1;\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    monkeypatch.setattr(re_, "repo_url", lambda: str(repo))
    return repo, _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def evalrepo(tmp_path) -> re_.EvalRepo:
    return re_.EvalRepo(tmp_path / "evalrepo")


def _case(store: Store, reviewer: str, verdict: str, inputs: dict, *, kind: str = "issue",
          base: str | None = None, head: str | None = None, data: dict | None = None,
          expected: str | None = None) -> re_.Case:
    vid = verdict_log.record(reviewer, verdict_log.Subject(kind, 7), verdict=verdict,  # type: ignore[arg-type]
                             prompt_sha="p", model="opus", data=data or {"findings": []},
                             inputs=inputs, head_sha=head, base_sha=base, store=store)
    assert vid is not None
    verdict_labels.label(store, vid, expected=expected or verdict, by="op")
    [case] = [c for c in re_.load_cases(store, reviewer) if c.row.id == vid]
    return case


def test_replay_intake_reads_only_text(tmp_path, evalrepo):
    store = Store(tmp_path / "s")
    case = _case(store, "intake", "clear", {"title": "t", "body": "b", "notes": "",
                                            "blocked": "0"})
    assert re_.replay(case, evalrepo, store) == "clear"  # conftest stubs the reviewer


def test_replay_a_blocked_intake_runs_no_agent(tmp_path, evalrepo, monkeypatch):
    store = Store(tmp_path / "s")
    case = _case(store, "intake", "malicious", {"title": "t", "body": "b", "notes": "",
                                                "blocked": "1"})
    monkeypatch.setattr(headless_agent, "run_agent", lambda *a, **k: pytest.fail("agent ran"))
    assert re_.replay(case, evalrepo, store) == "malicious"


def test_replay_fixability_reads_the_base_tree(tmp_path, origin, evalrepo, monkeypatch):
    _, sha = origin
    store = Store(tmp_path / "s")
    answer = {"verdict": "go", "kind": "defect", "code": ["src/x.ts"], "readings": [],
              "question": "", "fixability": 80, "reason": "r"}
    monkeypatch.setattr(headless_agent, "run_agent", lambda prompt, **kw: json.dumps(answer))
    case = _case(store, "fixability", "go", {"title": "t", "body": "b", "replies": ""}, base=sha)
    assert re_.replay(case, evalrepo, store) == "go"


def test_replay_fix_review_applies_the_worktree_patch(tmp_path, origin, evalrepo, monkeypatch):
    _, sha = origin
    store = Store(tmp_path / "s")
    seen: list[str] = []

    def fake(prompt: str, **kw) -> str:
        seen.append((Path(kw["cwd"]) / "src" / "x.ts").read_text())
        return json.dumps({"verdict": "unsafe", "reason": "r"})
    monkeypatch.setattr(headless_agent, "run_agent", fake)
    case = _case(store, "fix-review", "unsafe",
                 {"title": "t", "body": "b", "root_cause": "rc", "test_paths": "",
                  "evidence": "", "history": "", "patch": DIFF, "worktree_patch": DIFF,
                  "lane": "cross", "inventory_veto": "1"}, base=sha)
    assert case.fidelity == "full"
    assert re_.replay(case, evalrepo, store) == "unsafe"
    assert seen and all(text == "export const x = 2;\n" for text in seen)


def test_replay_trust_boundary(tmp_path, origin, evalrepo):
    _, sha = origin
    store = Store(tmp_path / "s")
    case = _case(store, "trust-boundary", "pass",
                 {"title": "t", "body": "b", "patch": DIFF, "worktree_patch": DIFF,
                  "lane": "solo"}, base=sha)
    assert re_.replay(case, evalrepo, store) == "pass"  # conftest stubs the reviewer


def test_replay_design(tmp_path, origin, evalrepo, monkeypatch):
    _, sha = origin
    store = Store(tmp_path / "s")
    store.save_diffs_many([("h1", 7, DIFF)])
    monkeypatch.setattr(headless_agent, "run_agent",
                        lambda prompt, **kw: json.dumps({"summary": "s", "findings": []}))
    case = _case(store, "design", "pass", {"title": "t", "coverage": "", "docs": ""},
                 kind="pr", base=sha, head="h1")
    assert re_.replay(case, evalrepo, store) == "pass"


def test_replay_security(tmp_path, evalrepo, monkeypatch):
    from pipeline import diff_cache
    store = Store(tmp_path / "s")
    store.save_diffs_many([("h2", 7, DIFF)])
    monkeypatch.setattr(diff_cache, "DIFFS", tmp_path / "diffs")

    def fake(prompt: str, **kw) -> str:
        return "```json\n" + json.dumps({"lens_summary": "s", "findings": []}) + "\n```"
    monkeypatch.setattr(headless_agent, "run_agent", fake)
    case = _case(store, "security", "GREEN", {"title": "t", "bot_evidence": "[]"},
                 kind="pr", head="h2")
    assert case.fidelity == "partial"
    assert re_.replay(case, evalrepo, store) == "GREEN"


def test_partial_cases_are_marked(tmp_path):
    store = Store(tmp_path / "s")
    old = _case(store, "fix-review", "safe", {"title": "t", "body": "b", "patch": DIFF})
    assert old.fidelity == "partial" and old.why_partial


def test_unreplayable_sha(tmp_path, origin, evalrepo):
    with pytest.raises(re_.Unreplayable):
        with evalrepo.tree("0" * 40):
            pass


def test_worktrees_are_removed_on_error(origin, evalrepo):
    _, sha = origin
    with pytest.raises(RuntimeError):
        with evalrepo.tree(sha) as tree:
            assert (tree / "src" / "x.ts").exists()
            raise RuntimeError("boom")
    assert _git(evalrepo.root, "worktree", "list").count("\n") == 0


def test_replay_never_records(tmp_path, evalrepo):
    store = Store(tmp_path / "s")
    case = _case(store, "intake", "clear", {"title": "t", "body": "b", "notes": "",
                                            "blocked": "0"})
    before = len(store.review_verdicts())
    re_.replay(case, evalrepo, store)
    assert len(store.review_verdicts()) == before


def _labeled(store: Store, reviewer: str, verdict: str, expected: str) -> re_.Case:
    return _case(store, reviewer, verdict, {"title": "t", "body": "b", "notes": "",
                                            "blocked": "0"}, expected=expected)


def test_production_row_is_a_sample_of_its_own_config(tmp_path):
    store = Store(tmp_path / "s")
    case = _labeled(store, "intake", "clear", "clear")
    re_.add_sample(store, case, "q", "opus", "suspicious")
    ledger = re_.ledger_samples(store)
    assert re_.samples(case, "p", "opus", ledger) == ["clear"]
    assert re_.samples(case, "q", "opus", ledger) == ["suspicious"]


def test_majority_breaks_ties_to_the_severe_side():
    assert re_.majority("intake", ["clear", "suspicious"]) == "suspicious"
    assert re_.majority("intake", ["clear", "clear", "malicious"]) == "clear"
    assert re_.majority("intake", ["failed", "clear"]) == "clear"
    assert re_.majority("intake", ["failed"]) == "failed"
    assert re_.majority("intake", []) is None


def test_score_counts(tmp_path):
    store = Store(tmp_path / "s")
    right = _labeled(store, "intake", "suspicious", "suspicious")
    under = _labeled(store, "intake", "clear", "malicious")
    over = _labeled(store, "intake", "malicious", "clear")
    lost = _labeled(store, "intake", "clear", "clear")
    results = {right.row.id: "suspicious", under.row.id: "clear", over.row.id: "suspicious",
               lost.row.id: "failed"}
    s = re_.score(results, [right, under, over, lost])
    assert (s.cases, s.right, s.under, s.over, s.failed) == (4, 1, 1, 1, 1)
    assert (s.must_catch, s.should_pass) == (2, 2)


def test_blocking_has_one_owner():
    from prospector_app.backend import reviewer_ratchet
    assert "BLOCKING" not in vars(reviewer_ratchet)
    assert reviewer_ratchet.MIN_CASES is verdict_labels.MIN_CASES
