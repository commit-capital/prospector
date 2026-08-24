"""Per-PR SECURITY review: the gate-free review engine, 3-lens + refute
orchestration, coverage hold, and the disposition-aware RED flip — all with
headless claude mocked out."""
import json
import threading

from pipeline import security_review as rs
from pipeline.testsupport import set_section
from pipeline.store import Store
from pipeline.testsupport import reviews_section

NOW = "2026-06-10T00:00:00+00:00"


def _eligible_pr(store, n=100, head="h1", cluster=1):
    """A clean merge candidate — the only thing security_eligible accepts."""
    store.save_pr({
        "pr": n,
        "meta": {"title": f"t{n}", "author": "a", "state": "open", "draft": False,
                 "head_sha": head, "checked_at": NOW},
        "signals": {"ci": "passing", "mergeable": True,
                    "checked_at": NOW, "against_head_sha": head},
        "reviews": reviews_section(head, NOW),
        "drift": {"state": "applicable", "checked_at": NOW, "against_head_sha": head},
        "analysis": {"disposition": "merge", "rationale": "best",
                     "checked_at": NOW, "against_head_sha": head},
        "cluster": {"ids": [cluster], "checked_at": NOW, "against_head_sha": head},
    })
    store.save_cluster({"id": cluster, "root_problem": "p", "prs": [n], "outcome": "merge-ready"})


def _fenced(obj):
    return "```json\n" + json.dumps(obj) + "\n```"


def _mock_agents(monkeypatch, *, lens_findings, verify_results=None):
    """Route review vs verify prompts to canned JSON. lens_findings is returned by
    every lens; verify_results is returned by the (single) verify chunk."""
    def fake(prompt, *a, **k):
        if prompt.startswith("Pre-merge security review"):
            return _fenced({"lens_summary": "s", "findings": lens_findings})
        if prompt.startswith("Adversarially verify"):
            return _fenced({"results": verify_results or []})
        raise AssertionError(f"unexpected prompt: {prompt[:40]}")
    monkeypatch.setattr(rs.headless_agent, "run_agent", fake)
    monkeypatch.setattr(rs.diff_cache, "fetch_diff", lambda *a, **k: True)


def test_green_when_no_findings(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _eligible_pr(store)
    _mock_agents(monkeypatch, lens_findings=[])
    assert rs.run(store, 100) == 0
    assert store.load_pr(100).section("security")["verdict"] == "GREEN"
    assert "trigger" not in store.runs()[-1].raw


def test_run_entry_carries_trigger_and_timestamps(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _eligible_pr(store)
    _mock_agents(monkeypatch, lens_findings=[])
    assert rs.run(store, 100, trigger="autohunt") == 0
    entry = store.runs()[-1]
    assert entry.phase == "security:review-one"
    assert entry.raw["trigger"] == "autohunt"
    assert entry.started and entry.finished


def test_red_finding_flips_to_needs_human(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _eligible_pr(store)
    finding = {"severity": "red", "category": "authz", "title": "priv-esc",
               "location": "f.py", "detail": "d", "confidence": "high"}
    _mock_agents(monkeypatch, lens_findings=[finding],
                 verify_results=[{"index": 0, "upheld": True,
                                  "adjusted_severity": "red", "reasoning": "confirmed"}])
    assert rs.run(store, 100) == 0
    rec = store.load_pr(100)
    assert rec.section("security")["verdict"] == "RED"
    assert rec.disposition == "needs-human"
    # Cluster reopened for re-analysis.
    assert store.load_cluster(1).outcome is None


def test_refuted_finding_yields_green(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _eligible_pr(store)
    finding = {"severity": "red", "category": "authz", "title": "maybe", "detail": "d"}
    _mock_agents(monkeypatch, lens_findings=[finding],
                 verify_results=[{"index": 0, "upheld": False,
                                  "adjusted_severity": "not-an-issue", "reasoning": "fp"}])
    assert rs.run(store, 100) == 0
    assert store.load_pr(100).section("security")["verdict"] == "GREEN"


def test_lens_failure_holds_as_incomplete(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _eligible_pr(store)
    monkeypatch.setattr(rs.diff_cache, "fetch_diff", lambda *a, **k: True)

    def fake(prompt, *a, **k):
        raise RuntimeError("claude exited 1")
    monkeypatch.setattr(rs.headless_agent, "run_agent", fake)

    # No lens ran → GREEN is downgraded to INCOMPLETE → held, no section written.
    assert rs.run(store, 100) == 0
    assert store.load_pr(100).section("security") is None


def test_verify_failure_with_flagged_findings_holds_as_incomplete(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _eligible_pr(store)
    monkeypatch.setattr(rs.diff_cache, "fetch_diff", lambda *a, **k: True)
    finding = {"severity": "red", "category": "authz", "title": "maybe", "detail": "d"}

    def fake(prompt, *a, **k):
        if prompt.startswith("Pre-merge security review"):
            return _fenced({"lens_summary": "s", "findings": [finding]})
        raise RuntimeError("claude exited 1: 403 Unable to verify organization membership")
    monkeypatch.setattr(rs.headless_agent, "run_agent", fake)

    # Every lens ran and flagged a finding, but the verify chunk failed — the
    # unverified findings must not read as refuted, so no GREEN is trusted:
    # the verdict is held and the PR stays eligible to re-run.
    assert rs.run(store, 100) == 0
    assert store.load_pr(100).section("security") is None


def test_confirmed_red_stands_when_a_later_verify_chunk_fails(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _eligible_pr(store)
    monkeypatch.setattr(rs.diff_cache, "fetch_diff", lambda *a, **k: True)
    findings = [{"severity": "red", "category": "authz", "title": f"t{i}", "detail": "d"}
                for i in range(rs.VERIFY_CHUNK_SIZE + 1)]

    def fake(prompt, *a, **k):
        if prompt.startswith("Pre-merge security review"):
            # Only the security lens flags; the flagged set spans two chunks.
            return _fenced({"lens_summary": "s",
                            "findings": findings if "via the security lens" in prompt else []})
        if '"index": 0' in prompt:
            return _fenced({"results": [{"index": 0, "upheld": True,
                                         "adjusted_severity": "red",
                                         "reasoning": "confirmed"}]})
        raise RuntimeError("claude exited 1")
    monkeypatch.setattr(rs.headless_agent, "run_agent", fake)

    # The second verify chunk failed, but the first ran and confirmed a red —
    # a confirmed finding stands regardless of verify completeness.
    assert rs.run(store, 100) == 0
    assert store.load_pr(100).section("security")["verdict"] == "RED"


def test_non_merge_pr_is_reviewed_not_refused(tmp_path, monkeypatch):
    # The engine is gate-free: a close-fixed PR is still reviewed on demand.
    store = Store(str(tmp_path))
    _eligible_pr(store)
    set_section(store, 100, "analysis", {"disposition": "close-fixed", "rationale": "r",
                                         "upstream_pr": 1}, against_head_sha="h1")
    _mock_agents(monkeypatch, lens_findings=[])
    assert rs.run(store, 100) == 0
    assert store.load_pr(100).section("security")["verdict"] == "GREEN"


def test_red_on_non_merge_pr_records_without_flipping_disposition(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _eligible_pr(store)
    set_section(store, 100, "analysis", {"disposition": "request-changes", "rationale": "r",
                                         "asks": ["rebase"]}, against_head_sha="h1")
    finding = {"severity": "red", "category": "authz", "title": "priv-esc", "detail": "d"}
    _mock_agents(monkeypatch, lens_findings=[finding],
                 verify_results=[{"index": 0, "upheld": True,
                                  "adjusted_severity": "red", "reasoning": "confirmed"}])
    assert rs.run(store, 100) == 0
    rec = store.load_pr(100)
    assert rec.section("security")["verdict"] == "RED"
    # RED only un-routes a merge; a request-changes PR keeps its disposition.
    assert rec.section("analysis")["disposition"] == "request-changes"
    assert store.load_cluster(1).outcome == "merge-ready"  # cluster untouched


def test_unknown_pr_returns_error(tmp_path):
    store = Store(str(tmp_path))
    assert rs.run(store, 999) == 1


def test_concurrent_lens_progress_prints_lens_by_lens(tmp_path, monkeypatch, capsys):
    """The 3 lenses run concurrently, but their progress must print grouped by
    lens in LENSES order — byte-identical to a serial run. Adversarial timing:
    every lens interleaves its emits (two barriers), and the live lens (security)
    is forced to finish LAST so the others are fully buffered then flushed."""
    store = Store(str(tmp_path))
    _eligible_pr(store)
    monkeypatch.setattr(rs.diff_cache, "fetch_diff", lambda *a, **k: True)

    lines = {"security": ["/sec/a.py", "/sec/b.py"],
             "correctness": ["/cor/a.py", "/cor/b.py"],
             "scope": ["/scope/a.py", "/scope/b.py"]}
    first, second = threading.Barrier(3), threading.Barrier(3)  # force interleaving
    others_returned = threading.Semaphore(0)                    # security exits last

    def fake(prompt, *a, on_event=None, **k):
        key = next(k2 for k2 in lines if f"via the {k2} lens" in prompt)
        a_path, b_path = lines[key]
        on_event(("tool", "Read", {"file_path": a_path}))
        first.wait(timeout=10)   # every lens has emitted its first line
        on_event(("tool", "Read", {"file_path": b_path}))
        second.wait(timeout=10)  # …and its second, maximally interleaved
        if key == "security":
            others_returned.acquire(timeout=10)
            others_returned.acquire(timeout=10)
        else:
            others_returned.release()
        return _fenced({"lens_summary": "s", "findings": []})

    monkeypatch.setattr(rs.headless_agent, "run_agent", fake)
    assert rs.run(store, 100) == 0

    out = capsys.readouterr().out
    expected = "\n".join(
        f"  · {lens['key']} lens\n" + "\n".join(f"    · Read: {p}"
                                                for p in lines[lens["key"]])
        for lens in rs.LENSES)
    assert expected in out
