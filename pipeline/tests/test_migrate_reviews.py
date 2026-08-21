from pipeline import migrate_reviews, store_edit
from pipeline.store import Store

NOW = "2026-06-10T00:00:00+00:00"


def _legacy(n: int, score: int | None, sha: str | None) -> dict:
    sig = {"ci": "passing", "checked_at": NOW, "against_head_sha": "h"}
    if score is not None:
        sig["greptile"] = score
    if sha is not None:
        sig["greptile_reviewed_sha"] = sha
    return {"pr": n, "meta": {"title": "t", "state": "closed", "head_sha": "h"}, "signals": sig}


def test_lifts_legacy_signals_into_reviews():
    out = migrate_reviews.lift_greptile_signals(_legacy(1, 4, "old"))
    assert out is not None
    assert "greptile" not in out["signals"] and "greptile_reviewed_sha" not in out["signals"]
    entry = out["reviews"]["greptile"]
    assert entry["kind"] == "review" and entry["score"] == 4 and entry["reviewed_sha"] == "old"
    assert entry["observed_at"] == NOW
    assert out["reviews"]["against_head_sha"] == "h" and out["reviews"]["checked_at"] == NOW


def test_untouched_when_nothing_to_lift():
    assert migrate_reviews.lift_greptile_signals(
        {"pr": 1, "meta": {"title": "t", "state": "open", "head_sha": "h"}, "signals": {"ci": "passing"}}) is None


def test_keeps_an_existing_entry_and_still_drops_the_legacy_keys():
    rec = _legacy(1, 4, "old")
    rec["reviews"] = {"greptile": {"kind": "review", "score": 5, "reviewed_sha": "h"},
                      "checked_at": NOW, "against_head_sha": "h"}
    out = migrate_reviews.lift_greptile_signals(rec)
    assert out["reviews"]["greptile"]["score"] == 5
    assert "greptile" not in out["signals"]


def test_through_store_edit_is_idempotent(tmp_path):
    st = Store(tmp_path)
    st.save_pr(_legacy(1, 3, "h"))
    st.save_pr({"pr": 2, "meta": {"title": "t", "state": "open", "head_sha": "h"}})
    report = store_edit.plan_edit(st, migrate_reviews.lift_greptile_signals, "prs")
    assert sorted(report.changed) == [1]
    store_edit.apply_edit(st, report, "lift_greptile_signals", backup_dir=tmp_path / "bk")
    assert st.load_pr(1).greptile == 3 and st.load_pr(1).greptile_reviewed_sha == "h"
    again = store_edit.plan_edit(st, migrate_reviews.lift_greptile_signals, "prs")
    assert not again.changed
    assert st.runs()[-1].raw["action"] == "store-edit"
