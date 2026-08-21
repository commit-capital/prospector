import json

from pipeline import greptile_read_driver as drv
from pipeline.store import Store
from pipeline.testsupport import greptile_entry


def _pr(st, n, score, head="h1", summary=None, findings=()):
    pr = st.create_pr(n, {"title": "t", "state": "open", "head_sha": head})
    entry = greptile_entry(score, head)
    entry["summary"] = summary
    entry["findings"] = [{"body": b, "severity": None, "resolved": False, "outdated": False}
                         for b in findings]
    pr.set_reviews({"greptile": entry})
    return pr


def test_candidates_only_sub5_without_current_review(tmp_path):
    st = Store(tmp_path)
    _pr(st, 1, 5)                    # 5/5 -> excluded
    _pr(st, 2, 3)                    # sub-5, no review -> candidate
    p3 = _pr(st, 3, 4)
    p3.set_greptile_review({"severity": "nits", "findings": [], "summary": "s"})  # current -> excluded
    assert drv.candidates(st) == [2]


def test_candidates_empty_when_no_review_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "none")
    st = Store(tmp_path)
    _pr(st, 2, 3)                    # sub-5 would be a candidate under greptile
    assert drv.candidates(st) == []


def test_reread_before_reselects_current_but_superseded_verdicts(tmp_path):
    st = Store(tmp_path)
    p1 = _pr(st, 1, 3)
    p1.set_greptile_review({"severity": "nits", "findings": [], "summary": "s"})  # current head
    old = st.load_pr(1).greptile_review["checked_at"]

    # a cutoff after the verdict's stamp re-selects it despite head-freshness;
    # a cutoff before it leaves the current verdict alone.
    assert drv.candidates(st) == []                          # freshness alone excludes it
    assert drv.candidates(st, reread_before="2099-01-01T00:00:00+00:00") == [1]
    assert drv.candidates(st, reread_before=old) == []       # not stamped strictly before itself


def test_write_batches_stamps_clean_when_no_findings(tmp_path):
    st = Store(tmp_path)
    _pr(st, 2, 3)
    res = drv.write_batches(st)
    assert res["clean"] == 1 and res["batched"] == 0
    assert st.load_pr(2).greptile_review["severity"] == "clean"


def test_write_batches_bundles_the_stored_entry(tmp_path, monkeypatch):
    st = Store(tmp_path)
    monkeypatch.setattr(drv, "BATCH_DIR", tmp_path / "batches")
    _pr(st, 2, 3, summary="Confidence Score: 3/5 — the retry loop never exits",
        findings=["Unbounded retry here."])
    res = drv.write_batches(st)
    assert res["clean"] == 0 and res["batched"] == 1
    batch = json.loads((tmp_path / "batches" / "batch-000.json").read_text())
    item = batch["items"][0]
    assert item["pr"] == 2 and item["reviews"] == ["Confidence Score: 3/5 — the retry loop never exits"]
    assert item["comments"] == ["Unbounded retry here."]


def test_commit_greptile_dir_writes_section(tmp_path):
    st = Store(tmp_path)
    _pr(st, 2, 3)
    out = tmp_path / "out"
    out.mkdir()
    (out / "batch-000.json").write_text(json.dumps([{
        "pr": 2, "head_sha": "h1", "severity": "defects",
        "findings": [{"headline": "h", "class": "substantive", "why": "w"}], "summary": "s"}]))
    written, errors = drv.commit_greptile_dir(st, out)
    assert written == 1 and errors == []
    assert st.load_pr(2).greptile_review["severity"] == "defects"
