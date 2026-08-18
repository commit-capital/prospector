"""Headless find-fixed runner: batching, verdict filtering, store commits — the
agent stubbed, no claude subprocess, no GitHub."""
import json

from issue_triage import find_fixed, issue_fixed_driver, issue_store

META = {"title": "t", "body": "b", "state": "open", "updated_at": "T1"}
CAUSAL_EVIDENCE = {
    "reported_origin": "src/service.ts:produceState",
    "fix_hunk": "src/service.ts:produceState",
    "relationship": "The hunk changes the reported producer directly.",
    "before": "The issue inputs produced the invalid state.",
    "after": "The issue inputs leave the state unchanged.",
    "current_path_check": "The producer no longer writes the invalid state.",
}


def _seed(tmp_path, n: int) -> issue_store.IssueStore:
    st = issue_store.IssueStore(tmp_path)
    for i in range(1, n + 1):
        st.create_issue(i, META)
    return st


def _fenced(verdicts: list[dict]) -> str:
    return "```json\n" + json.dumps({"verdicts": verdicts}) + "\n```"


def test_batch_applies_only_valid_in_batch_verdicts(tmp_path, monkeypatch):
    st = _seed(tmp_path, 2)
    monkeypatch.setattr(find_fixed.headless_agent, "run_agent",
                        lambda prompt, **kw: _fenced([
                            {"issue": 1, "status": "not-fixed", "rationale": "still broken"},
                            {"issue": 2, "status": "bogus", "rationale": "x"},
                            {"issue": 99, "status": "not-fixed", "rationale": "hallucinated"},
                        ]))
    applied = find_fixed.scan_batch(st, [1, 2])
    assert applied == 1
    assert st.load_issue(1).fix_scan["status"] == "not-fixed"
    assert st.load_issue(2).fix_scan is None


def test_batch_runs_agent_with_gh_enabled(tmp_path, monkeypatch):
    st = _seed(tmp_path, 1)
    seen = {}

    def fake_run(prompt, **kw):
        seen.update(kw)
        return _fenced([{"issue": 1, "status": "not-fixed", "rationale": "r"}])

    monkeypatch.setattr(find_fixed.headless_agent, "run_agent", fake_run)
    find_fixed.scan_batch(st, [1])
    assert seen["allow_gh"] is True


def test_batch_drops_fixed_verdict_without_causal_evidence(tmp_path, monkeypatch):
    st = _seed(tmp_path, 1)
    monkeypatch.setattr(find_fixed.headless_agent, "run_agent",
                        lambda prompt, **kw: _fenced([
                            {"issue": 1, "status": "fixed", "fixed_by": 42,
                             "rationale": "#42 says it fixes this"}]))
    assert find_fixed.scan_batch(st, [1]) == 0
    assert st.load_issue(1).fix_scan is None


def test_main_retries_a_failed_batch(tmp_path, monkeypatch, capsys):
    """A batch that fails once is retried, so a transient agent failure doesn't
    silently leave its issues on a stale fix-scan."""
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)
    attempts = []

    def fake_run(prompt, **kw):
        attempts.append(prompt)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return _fenced([{"issue": 5, "status": "not-fixed", "rationale": "still broken"}])

    monkeypatch.setattr(find_fixed.headless_agent, "run_agent", fake_run)
    find_fixed.main(["--store", str(tmp_path)])
    out = capsys.readouterr().out
    assert len(attempts) == 2
    assert "retrying 1 failed batch" in out
    assert st.load_issue(5).fix_scan["status"] == "not-fixed"
    assert issue_fixed_driver.candidates(st) == []


def test_main_names_issues_left_unscanned(tmp_path, monkeypatch, capsys):
    """An issue the agent skips inside an otherwise-successful batch is named,
    not folded into the applied count."""
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)
    st.create_issue(6, META)
    monkeypatch.setattr(find_fixed.headless_agent, "run_agent",
                        lambda prompt, **kw: _fenced(
                            [{"issue": 5, "status": "not-fixed", "rationale": "r"}]))
    find_fixed.main(["--batch", "2", "--store", str(tmp_path)])
    out = capsys.readouterr().out
    assert "1 of this run's issues have no current fix-scan: #6" in out


def test_main_checks_merged_explicit_fixer_with_agent(tmp_path, monkeypatch):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.set_links([{"pr": 42, "how": "explicit", "title": "fix"}])
    monkeypatch.setattr(find_fixed.headless_agent, "run_agent",
                        lambda prompt, **kw: _fenced([
                            {"issue": 5, "status": "fixed", "fixed_by": 42,
                             "rationale": "#42 changes the reported producer.",
                             "causal_evidence": CAUSAL_EVIDENCE}]))
    find_fixed.main(["--store", str(tmp_path)])
    got = st.load_issue(5)
    assert got.disposition == "close-fixed"
    assert got.fixed_by == 42
    assert issue_fixed_driver.candidates(st) == []
