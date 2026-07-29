"""Headless batch ANALYZE: batching, verdict filtering, and store commits, with
the agent stubbed — no claude subprocess, no GitHub."""
import json
import threading

from issue_triage import analyze_issues
from issue_triage import issue_analyze_driver
from issue_triage import issue_store

META = {"title": "t", "body": "b", "state": "open", "updated_at": "T1"}


def _seed(tmp_path, n_issues: int) -> issue_store.IssueStore:
    st = issue_store.IssueStore(tmp_path)
    for n in range(1, n_issues + 1):
        st.create_issue(n, META)
    return st


def _fenced(verdicts: list[dict]) -> str:
    return "```json\n" + json.dumps({"verdicts": verdicts}) + "\n```"


def test_analyze_batch_applies_only_valid_in_batch_verdicts(tmp_path, monkeypatch):
    """A verdict outside the batch or with an unknown disposition is dropped;
    the rest commit with a fresh analysis stamp."""
    st = _seed(tmp_path, 2)
    monkeypatch.setattr(analyze_issues.headless_agent, "run_agent",
                        lambda prompt, **kw: _fenced([
                            {"issue": 1, "disposition": "needs-human", "rationale": "r1"},
                            {"issue": 2, "disposition": "not-a-thing", "rationale": "r2"},
                            {"issue": 99, "disposition": "needs-human", "rationale": "hallucinated"},
                        ]))
    applied = analyze_issues.analyze_batch(st, [1, 2])
    assert applied == 1
    assert st.load_issue(1).disposition == "needs-human"
    assert st.load_issue(2).disposition is None
    assert st.load_issue(99) is None


def test_analyze_batch_prompt_carries_bundle_path(tmp_path, monkeypatch):
    """The agent prompt embeds a readable bundle file holding exactly the batch."""
    st = _seed(tmp_path, 3)
    seen = {}

    def fake_run(prompt, **kw):
        seen["prompt"] = prompt
        return _fenced([])

    monkeypatch.setattr(analyze_issues.headless_agent, "run_agent", fake_run)
    analyze_issues.analyze_batch(st, [1, 3])
    assert "__BUNDLE_PATH__" not in seen["prompt"]
    path = next(tok for tok in seen["prompt"].split() if "issue-analyze-" in tok)
    bundle = json.loads(open(path.rstrip(".,—")).read())
    assert [e["number"] for e in bundle] == [1, 3]


def test_main_batches_and_reports(tmp_path, monkeypatch, capsys):
    """--limit caps the run, --batch splits it into several concurrent agent
    calls, and a failing batch doesn't stop the rest. The failure keys off the
    batch's content (issue #1), not call order, so it's deterministic under the
    thread pool."""
    _seed(tmp_path, 5)
    calls = []

    def fake_run(prompt, **kw):
        calls.append(prompt)
        path = next(tok for tok in prompt.split() if "issue-analyze-" in tok)
        bundle = json.loads(open(path.rstrip(".,—")).read())
        if any(e["number"] == 1 for e in bundle):   # the [1, 2] batch always fails
            raise RuntimeError("boom")
        return _fenced([{"issue": e["number"], "disposition": "needs-human", "rationale": "r"}
                        for e in bundle])

    monkeypatch.setattr(analyze_issues.headless_agent, "run_agent", fake_run)
    rc = analyze_issues.main(["--limit", "4", "--batch", "2", "--store", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert len(calls) == 2                      # 4 issues in batches of 2
    assert "failed, continuing: boom" in out
    st = issue_store.IssueStore(tmp_path)
    assert st.load_issue(1).disposition is None  # its batch raised
    assert st.load_issue(2).disposition is None
    assert st.load_issue(3).disposition == "needs-human"
    assert st.load_issue(4).disposition == "needs-human"
    assert st.load_issue(5).disposition is None  # beyond --limit
    assert len(issue_analyze_driver.pending(st)) == 3  # batch-1 pair + #5


def test_main_records_run_with_attempted_count(tmp_path, monkeypatch):
    """Beyond apply_verdicts' per-batch "analyze" entries, main() appends one
    whole-run summary carrying a real `started` and `stats.attempted`, so a
    seconds-per-issue rate can be computed from it
    (pipeline_status._seconds_per_unit)."""
    _seed(tmp_path, 4)
    monkeypatch.setattr(analyze_issues.headless_agent, "run_agent",
                        lambda prompt, **kw: _fenced([
                            {"issue": e, "disposition": "needs-human", "rationale": "r"}
                            for e in range(1, 5)]))
    rc = analyze_issues.main(["--limit", "4", "--batch", "4", "--store", str(tmp_path)])
    assert rc == 0
    st = issue_store.IssueStore(tmp_path)
    summaries = [r for r in st.runs()
                if r.phase == "analyze" and r.raw.get("stats", {}).get("attempted")]
    assert len(summaries) == 1
    assert summaries[0].started is not None
    assert summaries[0].raw["stats"]["attempted"] == 4


def test_main_runs_batches_concurrently(tmp_path, monkeypatch):
    """With --concurrency > 1 the batches' agents overlap in time rather than
    running strictly one after another."""
    _seed(tmp_path, 6)
    barrier = threading.Barrier(3, timeout=5)     # 3 batches must all be in-flight at once

    def fake_run(prompt, **kw):
        barrier.wait()                            # deadlocks (TimeoutError) unless run in parallel
        path = next(tok for tok in prompt.split() if "issue-analyze-" in tok)
        bundle = json.loads(open(path.rstrip(".,—")).read())
        return _fenced([{"issue": e["number"], "disposition": "needs-human", "rationale": "r"}
                        for e in bundle])

    monkeypatch.setattr(analyze_issues.headless_agent, "run_agent", fake_run)
    rc = analyze_issues.main(
        ["--limit", "6", "--batch", "2", "--concurrency", "3", "--store", str(tmp_path)])
    assert rc == 0
    st = issue_store.IssueStore(tmp_path)
    assert all(st.load_issue(n).disposition == "needs-human" for n in range(1, 7))


def test_main_nothing_pending(tmp_path, monkeypatch, capsys):
    st = _seed(tmp_path, 1)
    issue_analyze_driver.apply_verdicts(st, [
        {"issue": 1, "disposition": "needs-human", "rationale": "r"}])
    monkeypatch.setattr(analyze_issues.headless_agent, "run_agent",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no agent call")))
    rc = analyze_issues.main(["--store", str(tmp_path)])
    assert rc == 0
    assert "nothing pending" in capsys.readouterr().out
