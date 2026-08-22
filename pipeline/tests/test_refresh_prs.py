import json

import pytest

from pipeline import ingest
from pipeline.review_fetch import PrFeed
from pipeline.testsupport import set_section
from pipeline.freshness import SECTION_SCHEMA_VERSION
from pipeline.store import Store


@pytest.fixture(autouse=True)
def _offline_feed(monkeypatch):
    """Tests opt into a feed explicitly; the default stays offline."""
    monkeypatch.setattr(ingest.review_fetch, "fetch_feeds", lambda numbers: {})


def _seed(store: Store, n: int, sha: str):
    """A PR fully stamped at `sha`: meta + summary/analysis/security current."""
    ingest.upsert_pr(store, {"number": n, "title": "x", "user": {"login": "a"},
                             "state": "open", "head": {"sha": sha},
                             "base": {"ref": "master"}, "html_url": "u"},
                     ci_override="passing", reviews_override=_reviews(5, sha))
    sec_payloads = {
        "summary": {"one_liner": "x", "schema_version": SECTION_SCHEMA_VERSION["summary"]},
        "analysis": {"disposition": "merge", "rationale": "x"},
        "security": {"verdict": "GREEN"},
    }
    for sec in ("summary", "analysis", "security"):
        set_section(store, n, sec, sec_payloads[sec], against_head_sha=sha)


def _reviews(score: int, sha: str) -> dict:
    return {"greptile": {"kind": "review", "score": score, "reviewed_sha": sha,
                         "findings": [], "checks": [], "extra": {}}, "pr_updated_at": "t"}


def _feed(n: int, sha: str, score: int | None) -> PrFeed:
    runs = [{"app": "github-actions", "name": "Build", "status": "completed",
             "conclusion": "success", "title": None, "summary": None, "url": None}]
    if score is not None:
        runs.append({"app": "greptile-apps", "name": "Greptile Review", "status": "completed",
                     "conclusion": "failure", "title": f"Confidence {score}/5", "summary": "",
                     "url": "u"})
    return PrFeed(pr=n, head_sha=sha, updated_at="t2", check_runs=runs)


def _fake_gh(sha: str):
    return {"number": 7, "title": "x", "user": {"login": "a"}, "state": "open",
            "head": {"sha": sha}, "base": {"ref": "master"}, "html_url": "u"}


def _open_gh(n: int, sha: str):
    return {"number": n, "title": "x", "user": {"login": "a"}, "state": "open",
            "head": {"sha": sha}, "base": {"ref": "master"}, "html_url": "u"}


def test_greptile_score_overridden_from_the_feed(tmp_path, monkeypatch):
    """A stored score can lag Greptile's on-PR verdict; the feed's verdict at
    the head wins, along with the reviewed SHA (#431 — the false sub-5
    merge-block)."""
    store = Store(str(tmp_path))
    ingest.upsert_pr(store, _fake_gh("c1c162ed"), reviews_override=_reviews(4, "older"))

    class R:
        returncode = 0
        stdout = json.dumps(_fake_gh("c1c162ed"))
    monkeypatch.setattr(ingest.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(ingest.review_fetch, "fetch_feeds",
                        lambda numbers: {7: _feed(7, "c1c162ed", 5)})

    ingest.refresh_prs(store, [7])

    rec = store.load_pr(7)
    assert rec.greptile == 5
    assert rec.greptile_reviewed_sha == "c1c162ed"
    assert rec.ci == "passing"


def test_greptile_score_kept_when_feed_unavailable(tmp_path, monkeypatch):
    """No feed (GitHub unreachable) → the stored entry stands rather than being
    wiped, and CI falls back to the REST verdict."""
    store = Store(str(tmp_path))
    ingest.upsert_pr(store, _fake_gh("sha1"), reviews_override=_reviews(4, "sha1"))

    class R:
        returncode = 0
        stdout = json.dumps(_fake_gh("sha1"))
    monkeypatch.setattr(ingest.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(ingest.review_fetch, "fetch_feeds", lambda numbers: {})
    monkeypatch.setattr(ingest, "gh_ci_status", lambda sha: "passing")

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
    monkeypatch.setattr(ingest.review_fetch, "fetch_feeds",
                        lambda numbers: {7: _feed(7, "new_sha", 5)})
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
    monkeypatch.setattr(ingest.review_fetch, "fetch_feeds", lambda numbers: {})
    monkeypatch.setattr(ingest, "gh_ci_status", lambda sha: "passing")

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
    monkeypatch.setattr(ingest.review_fetch, "fetch_feeds", lambda numbers: {})
    monkeypatch.setattr(ingest, "gh_ci_status", lambda sha: None)

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
