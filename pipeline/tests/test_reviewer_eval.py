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


@pytest.fixture
def pinned(tmp_path, monkeypatch) -> Path:
    """Intake pinned at prompt `p` (the cases' own), the code at prompt `cand`."""
    path = tmp_path / "pins.json"
    path.write_text(json.dumps({"intake": {"prompt_sha": "p", "model": "opus",
                                           "provisional": True, "score": None, "run": None}}))
    monkeypatch.setattr(re_, "configs", lambda: {"intake": {"prompt_sha": "cand", "model": "opus"}})
    return path


def _cases(store: Store, n_catch: int, n_pass: int) -> list[re_.Case]:
    return ([_labeled(store, "intake", "suspicious", "suspicious") for _ in range(n_catch)]
            + [_labeled(store, "intake", "clear", "clear") for _ in range(n_pass)])


def _gate(store, pins, candidate, **kw) -> re_.GateResult:
    return re_.gate(store, "intake", pins_path=pins, replay_candidate=candidate,
                    replay_pinned=lambda case: case.row.verdict, **kw)


def test_gate_refuses_a_new_under_call(tmp_path, pinned):
    store = Store(tmp_path / "s")
    cases = _cases(store, 10, 10)
    miss = cases[0].row.id
    result = _gate(store, pinned, lambda c: "clear" if c.row.id == miss else c.row.verdict)
    assert not result.passed and not result.provisional
    assert any(str(miss) in r for r in result.reasons)


def test_gate_refuses_more_over_calls(tmp_path, pinned):
    store = Store(tmp_path / "s")
    cases = _cases(store, 10, 10)
    strict = cases[-1].row.id
    result = _gate(store, pinned, lambda c: "suspicious" if c.row.id == strict else c.row.verdict)
    assert not result.passed and any("over" in r for r in result.reasons)


def test_gate_passes_and_repins(tmp_path, pinned):
    store = Store(tmp_path / "s")
    _cases(store, 10, 10)
    result = _gate(store, pinned, lambda c: c.row.verdict)
    assert result.passed and not result.provisional
    re_.write_pin("intake", result, pinned)
    pin = json.loads(pinned.read_text())["intake"]
    assert (pin["prompt_sha"], pin["provisional"], pin["score"]["right"]) == ("cand", False, 20)


def test_disagreements_get_k_samples(tmp_path, pinned):
    store = Store(tmp_path / "s")
    [case] = _cases(store, 1, 0)
    calls: list[int] = []

    def candidate(c: re_.Case) -> str:
        calls.append(c.row.id)
        return "clear"
    result = re_.gate(store, "intake", pins_path=pinned, k=3, replay_candidate=candidate,
                      replay_pinned=lambda c: "suspicious")
    assert len(calls) == 3
    assert result.candidate.under == 1


def test_provisional_passes_and_reports(tmp_path, pinned):
    store = Store(tmp_path / "s")
    _cases(store, 2, 2)
    result = _gate(store, pinned, lambda c: "clear")
    assert result.passed and result.provisional and result.reasons


def test_samples_are_kept_for_the_next_run(tmp_path, pinned):
    store = Store(tmp_path / "s")
    _cases(store, 1, 1)
    _gate(store, pinned, lambda c: c.row.verdict)
    _gate(store, pinned, lambda c: pytest.fail("the candidate's sample was kept"))


def test_a_pin_main_never_held_refuses(tmp_path, monkeypatch):
    store = Store(tmp_path / "s")
    [case] = _cases(store, 1, 0)
    monkeypatch.setattr(re_, "pinned_commit", lambda reviewer, pin: None)
    with pytest.raises(re_.PinUnreplayable, match="--init"):
        with re_.pinned_runner("intake", {"prompt_sha": "zzz", "model": "opus"}, store):
            pass


def _pins_repo(tmp_path: Path, monkeypatch) -> tuple[Path, list[str]]:
    """A repository whose pins file pins intake to AAA, then (after an unrelated
    commit) to BBB, with origin/main at the last commit."""
    repo = tmp_path / "code"
    pins = repo / "pipeline" / "evals" / "data" / "reviewer_pins.json"
    pins.parent.mkdir(parents=True)
    _git_init = ["-c", "user.email=t@t", "-c", "user.name=t"]
    _git(repo, "init", "-q")
    shas = []
    for sha, extra in (("AAA", None), ("AAA", "x"), ("BBB", None)):
        pins.write_text(json.dumps({"intake": {"prompt_sha": sha, "model": "opus"}}))
        if extra:
            (repo / "other.txt").write_text(extra)
        _git(repo, *_git_init, "add", ".")
        _git(repo, *_git_init, "commit", "-qm", f"pin {sha}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.setattr(re_, "REPO_ROOT", repo)
    monkeypatch.setattr(re_, "PINS", pins)
    return repo, shas


def test_pinned_commit_is_the_newest_main_commit_holding_the_pin(tmp_path, monkeypatch):
    _, shas = _pins_repo(tmp_path, monkeypatch)
    assert re_.pinned_commit("intake", {"prompt_sha": "BBB", "model": "opus"}) == shas[2]
    assert re_.pinned_commit("intake", {"prompt_sha": "AAA", "model": "opus"}) == shas[1]
    assert re_.pinned_commit("intake", {"prompt_sha": "AAA", "model": "sonnet"}) is None


def test_pinned_replay_reads_the_parents_store(tmp_path, monkeypatch):
    store = Store(tmp_path / "s")
    [case] = _cases(store, 1, 0)
    seen: dict = {}

    def run(argv, **kw):
        if argv[:2] == ["uv", "run"]:
            seen["env"] = kw.get("env") or {}
            return subprocess.CompletedProcess(argv, 0, stdout="suspicious\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(re_.subprocess, "run", run)
    with re_.pinned_runner("intake", {"prompt_sha": "p", "model": "opus"}, store,
                           commit="c0ffee") as replay_pinned:
        assert replay_pinned(case) == "suspicious"
    assert seen["env"]["TRIAGE_STORE_URL"] == store.engine.url.render_as_string(hide_password=False)


def test_an_unreplayable_pinned_case_is_skipped(tmp_path, monkeypatch):
    store = Store(tmp_path / "s")
    [case] = _cases(store, 1, 0)

    def run(argv, **kw):
        if argv[:2] == ["uv", "run"]:
            return subprocess.CompletedProcess(argv, re_.UNREPLAYABLE_EXIT, stdout="",
                                               stderr="no base")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(re_.subprocess, "run", run)
    with re_.pinned_runner("intake", {"prompt_sha": "p", "model": "opus"}, store,
                           commit="c0ffee") as replay_pinned:
        with pytest.raises(re_.Unreplayable):
            replay_pinned(case)


def test_the_gate_skips_unreplayable_cases(tmp_path, pinned):
    store = Store(tmp_path / "s")
    _cases(store, 1, 1)

    def candidate(c: re_.Case) -> str:
        raise re_.Unreplayable("no base")
    result = _gate(store, pinned, candidate)
    assert result.candidate.unreplayable == 2 and result.passed


def test_a_candidate_that_fails_where_the_pin_was_right_is_refused(tmp_path, pinned):
    store = Store(tmp_path / "s")
    _cases(store, 10, 10)
    result = _gate(store, pinned, lambda c: "failed")
    assert not result.passed and any("failed" in r for r in result.reasons)


def test_a_patch_that_no_longer_applies_is_unreplayable(tmp_path, origin, evalrepo):
    _, sha = origin
    store = Store(tmp_path / "s")
    bad = DIFF.replace("-export const x = 1;", "-export const y = 9;")
    case = _case(store, "trust-boundary", "pass",
                 {"title": "t", "body": "b", "patch": bad, "worktree_patch": bad,
                  "lane": "solo"}, base=sha)
    with pytest.raises(re_.Unreplayable):
        re_.replay(case, evalrepo, store)


def test_provisional_counts_only_replayed_cases(tmp_path, pinned):
    store = Store(tmp_path / "s")
    cases = _cases(store, 12, 10)
    lost = {c.row.id for c in cases[:3]}

    def candidate(c: re_.Case) -> str:
        if c.row.id in lost:
            raise re_.Unreplayable("gone")
        return c.row.verdict
    assert _gate(store, pinned, candidate).provisional
