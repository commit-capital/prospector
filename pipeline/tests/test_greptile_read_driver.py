import json
import unittest.mock as mock

from pipeline import greptile_read_driver as drv
from pipeline import settings
from pipeline.store import Store


def _pr(st, n, score, head="h1"):
    pr = st.create_pr(n, {"title": "t", "state": "open", "head_sha": head})
    pr.set_signals({"greptile": score})
    return pr


def test_candidates_only_sub5_without_current_review(tmp_path):
    st = Store(tmp_path)
    _pr(st, 1, 5)                    # 5/5 -> excluded
    _pr(st, 2, 3)                    # sub-5, no review -> candidate
    p3 = _pr(st, 3, 4)
    p3.set_greptile_review({"severity": "nits", "findings": [], "summary": "s"})  # current -> excluded
    assert drv.candidates(st) == [2]


def test_candidates_empty_when_no_review_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "REVIEW_PROVIDER", "none")
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
    with mock.patch.object(drv, "fetch_greptile_review_data", return_value=("h1", [], [])):
        res = drv.write_batches(st)
    assert res["clean"] == 1 and res["batched"] == 0
    assert st.load_pr(2).greptile_review["severity"] == "clean"


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
