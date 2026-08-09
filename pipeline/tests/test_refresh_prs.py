import json

from pipeline import greptile
from pipeline import ingest
from pipeline.testsupport import set_section
from pipeline.freshness import SECTION_SCHEMA_VERSION
from pipeline.store import Store


def _seed(store: Store, n: int, sha: str):
    """A PR fully stamped at `sha`: meta + summary/analysis/security current."""
    ingest.upsert_pr(store, {"number": n, "title": "x", "user": {"login": "a"},
                             "state": "open", "head": {"sha": sha},
                             "base": {"ref": "master"}, "html_url": "u"},
                     ci_override="passing", greptile_override=5)
    sec_payloads = {
        "summary": {"one_liner": "x", "schema_version": SECTION_SCHEMA_VERSION["summary"]},
        "analysis": {"disposition": "merge", "rationale": "x"},
        "security": {"verdict": "GREEN"},
    }
    for sec in ("summary", "analysis", "security"):
        set_section(store, n, sec, sec_payloads[sec], against_head_sha=sha)


def _fake_gh(sha: str):
    return {"number": 7, "title": "x", "user": {"login": "a"}, "state": "open",
            "head": {"sha": sha}, "base": {"ref": "master"}, "html_url": "u"}


def _open_gh(n: int, sha: str):
    return {"number": n, "title": "x", "user": {"login": "a"}, "state": "open",
            "head": {"sha": sha}, "base": {"ref": "master"}, "html_url": "u"}


def test_prs_missing_greptile_data_selects_scored_missing_sha(tmp_path):
    store = Store(str(tmp_path))
    # scored, no reviewed SHA → needs backfill
    ingest.upsert_pr(store, _open_gh(1, "h1"), greptile_override=5)
    # scored, SHA already stored → skip
    ingest.upsert_pr(store, _open_gh(2, "h2"), greptile_override=4,
                     greptile_reviewed_sha="h2")
    # no Greptile score → skip (nothing to fetch)
    ingest.upsert_pr(store, _open_gh(4, "h4"), ci_override="passing")
    assert greptile.prs_missing_greptile_data(store) == [1]


def test_backfill_greptile_data_stamps_and_tolerates_failures(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    ingest.upsert_pr(store, _open_gh(1, "h1"), greptile_override=4)
    ingest.upsert_pr(store, _open_gh(2, "h2"), greptile_override=5)
    # PR 2 stands in for a 404 / no-Greptile-review PR — verdict is (None, None), no raise
    monkeypatch.setattr(greptile, "fetch_greptile_verdict",
                        lambda n: (5, "rev1") if n == 1 else (None, None))

    stats = greptile.backfill_greptile_data(store)

    assert stats == {"candidates": 2, "stamped": 1, "skipped": 1}
    assert store.load_pr(1).greptile_reviewed_sha == "rev1"
    assert store.load_pr(1).greptile == 5  # stored 4 corrected to Greptile's own
    assert store.load_pr(2).greptile_reviewed_sha is None
    # per-PR persistence makes it resumable: PR 1 is no longer a candidate
    assert greptile.prs_missing_greptile_data(store) == [2]


def test_greptile_score_overridden_from_greptile_verdict(tmp_path, monkeypatch):
    """A stored greptileScore can lag Greptile's on-PR verdict; when Greptile's
    own score (scraped from the issue summary) differs, it wins, along with the
    reviewed SHA (#431 — the false sub-5 merge-block)."""
    store = Store(str(tmp_path))
    ingest.upsert_pr(store, _fake_gh("c1c162ed"), greptile_override=4)

    class R:
        returncode = 0
        stdout = json.dumps(_fake_gh("c1c162ed"))
    monkeypatch.setattr(ingest.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(ingest, "gh_ci_status", lambda sha: "passing")
    monkeypatch.setattr(greptile, "fetch_greptile_verdict", lambda n: (5, "c1c162ed"))

    ingest.refresh_prs(store, [7])

    rec = store.load_pr(7)
    assert rec.greptile == 5
    assert rec.greptile_reviewed_sha == "c1c162ed"


def test_greptile_score_kept_when_verdict_absent(tmp_path, monkeypatch):
    """No Greptile verdict available (never reviewed / unreachable) → the
    stored score stands rather than being wiped."""
    store = Store(str(tmp_path))
    ingest.upsert_pr(store, _fake_gh("sha1"), greptile_override=4)

    class R:
        returncode = 0
        stdout = json.dumps(_fake_gh("sha1"))
    monkeypatch.setattr(ingest.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(ingest, "gh_ci_status", lambda sha: "passing")
    monkeypatch.setattr(greptile, "fetch_greptile_verdict", lambda n: (None, None))

    ingest.refresh_prs(store, [7])

    assert store.load_pr(7).greptile == 4


def test_moved_head_marks_summary_analysis_security_stale(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _seed(store, 7, "old_sha")
    from pipeline.freshness import is_current
    rec = store.load_pr(7)
    assert all(is_current(rec, s) for s in ("summary", "analysis", "security"))

    class R:  # fake CompletedProcess
        returncode = 0
        stdout = json.dumps(_fake_gh("new_sha"))
    monkeypatch.setattr(ingest.subprocess, "run", lambda *a, **k: R())

    out = ingest.refresh_prs(store, [7])

    assert out == [{"pr": 7, "moved": True, "old_sha": "old_sha", "new_sha": "new_sha"}]
    rec = store.load_pr(7)
    assert rec.head_sha == "new_sha"
    assert not is_current(rec, "summary")
    assert not is_current(rec, "analysis")
    assert not is_current(rec, "security")  # malware-in-interim guard


def test_moved_head_recomputes_diff_signals_before_restamping(tmp_path, monkeypatch):
    """#668: old-head diff facts must not inherit the new signals stamp."""
    store = Store(str(tmp_path))
    _seed(store, 7, "old_sha")
    store.load_pr(7).record_live_state(
        diffstat={"additions": 4, "deletions": 0, "changed_files": 1},
        has_tests=False)
    moved = dict(_fake_gh("new_sha"), additions=86, deletions=1, changed_files=2)
    monkeypatch.setattr(ingest, "fetch_pr", lambda n: moved)
    monkeypatch.setattr(ingest, "gh_ci_status", lambda sha: "passing")
    monkeypatch.setattr(greptile, "fetch_greptile_verdict", lambda n: (5, "new_sha"))
    from pipeline import diff_cache
    monkeypatch.setattr(
        diff_cache, "fetch_diff_paths",
        lambda pr, sha, *a, **k: ["src/app.ts", "src/app.test.ts"])

    ingest.refresh_prs(store, [7])

    sig = store.load_pr(7).section("signals")
    assert sig["against_head_sha"] == "new_sha"
    assert sig["diffstat"] == {"additions": 86, "deletions": 1, "changed_files": 2}
    assert sig["has_tests"] is True


def test_github_ci_overrides_stored_verdict(tmp_path, monkeypatch):
    """A stored ciStatus can lag GitHub; when GitHub has a verdict for the
    current head it wins (the #4416 false merge-block)."""
    store = Store(str(tmp_path))
    ingest.upsert_pr(store, _fake_gh("sha1"), ci_override="failing")

    class R:
        returncode = 0
        stdout = json.dumps(_fake_gh("sha1"))
    monkeypatch.setattr(ingest.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(ingest, "gh_ci_status", lambda sha: "passing")
    monkeypatch.setattr(greptile, "fetch_greptile_verdict", lambda n: (None, None))

    ingest.refresh_prs(store, [7])

    assert store.load_pr(7).ci == "passing"


def test_keeps_stored_ci_when_github_has_no_verdict(tmp_path, monkeypatch):
    """GitHub unreachable or has no checks for the head → keep the stored value."""
    store = Store(str(tmp_path))
    ingest.upsert_pr(store, _fake_gh("sha1"), ci_override="failing")

    class R:
        returncode = 0
        stdout = json.dumps(_fake_gh("sha1"))
    monkeypatch.setattr(ingest.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(ingest, "gh_ci_status", lambda sha: None)
    monkeypatch.setattr(greptile, "fetch_greptile_verdict", lambda n: (None, None))

    ingest.refresh_prs(store, [7])

    assert store.load_pr(7).ci == "failing"


def test_unchanged_head_keeps_sections_current(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    _seed(store, 7, "same_sha")

    class R:
        returncode = 0
        stdout = json.dumps(_fake_gh("same_sha"))
    monkeypatch.setattr(ingest.subprocess, "run", lambda *a, **k: R())

    out = ingest.refresh_prs(store, [7])

    assert out == [{"pr": 7, "moved": False, "old_sha": "same_sha", "new_sha": "same_sha"}]
    from pipeline.freshness import is_current
    assert is_current(store.load_pr(7), "summary")
