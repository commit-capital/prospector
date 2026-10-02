"""threat_refresh: a worker machine threat-scans every head the store records
without a verdict, on a cadence, and books each pass in the runs ledger."""
from __future__ import annotations

import pytest

from pipeline import diff_cache, storekit
from pipeline import store as S
from pipeline.tests.test_threats import CLEAN_DIFF, PAYLOAD_DIFF
from prospector_app.backend import data, threat_refresh


def _pr(n: int, head: str, *, author: str = "alice", stamped: str | None = None,
        updated: str = "2026-09-28T16:44:00+00:00") -> dict:
    rec = {"pr": n,
           "meta": {"title": f"pr {n}", "author": author, "state": "open", "draft": False,
                    "head_sha": head, "updated_at": updated,
                    "checked_at": "2026-09-28T16:44:00+00:00"}}
    if stamped is not None:
        rec["threat"] = {"verdict": "clear", "signatures": [], "detail": {},
                         "checked_at": "2026-09-27T00:00:00+00:00",
                         "against_head_sha": stamped}
    return rec


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setattr(threat_refresh, "_left", {})
    diffs = tmp_path / "diffs"
    diffs.mkdir()
    monkeypatch.setattr(diff_cache, "DIFFS", diffs)
    data.refresh()
    return st


def _github(monkeypatch, diffs: dict[str, str]) -> list[str]:
    """GitHub serves `diffs` by head; returns the heads fetched."""
    fetched: list[str] = []

    def fetch(pr, head, diffs_dir=None, store=None):
        fetched.append(head)
        if head not in diffs:
            return False
        (diffs_dir / f"{head}.diff").write_text(diffs[head])
        return True
    monkeypatch.setattr(diff_cache, "fetch_diff", fetch)
    return fetched


def _passes(st: S.Store) -> list[dict]:
    return [r.raw for r in st.runs()
            if isinstance(r, storekit.PhaseRun) and r.phase == threat_refresh.PHASE]


def test_a_force_pushed_payload_is_flagged_by_the_next_pass(store, monkeypatch):
    store.save_pr(_pr(11987, "evil", author="zach", stamped="before"))
    store.save_pr(_pr(11988, "fine", stamped="fine"))
    data.refresh()
    fetched = _github(monkeypatch, {"evil": PAYLOAD_DIFF})

    assert threat_refresh.scan_new_heads() == [11987]

    assert fetched == ["evil"]
    after = store.load_pr(11987)
    assert after.threat_verdict == "malicious"
    assert after.section("threat")["against_head_sha"] == "evil"
    assert "zach" in store.load_threats()["actors"]
    [booked] = _passes(store)
    assert booked["prs"] == [11987] and booked["malicious_prs"] == [11987]
    assert booked["stats"]["restamped"] == 1 and booked["stats"]["fetched"] == 1


def test_a_pass_with_every_head_scanned_does_nothing(store, monkeypatch):
    store.save_pr(_pr(1, "h1", stamped="h1"))
    data.refresh()
    _github(monkeypatch, {})
    monkeypatch.setattr(threat_refresh.threat_scan, "scan",
                        lambda *a, **k: pytest.fail("scanned with nothing new"))
    assert threat_refresh.scan_new_heads() == []
    assert _passes(store) == []


def test_a_head_left_without_a_verdict_waits_before_a_retry(store, monkeypatch):
    store.save_pr(_pr(1, "h1"))
    data.refresh()
    fetched = _github(monkeypatch, {})
    assert threat_refresh.scan_new_heads() == [1]
    assert threat_refresh.scan_new_heads() == []
    assert fetched == ["h1"]

    _github(monkeypatch, {"h1": CLEAN_DIFF})
    threat_refresh._left[(1, "h1")] -= threat_refresh.RETRY_SECONDS
    assert threat_refresh.scan_new_heads() == [1]
    assert store.load_pr(1).threat_verdict == "clear"


def test_a_new_head_is_tried_at_once_whatever_its_predecessor_waits_on(store, monkeypatch):
    store.save_pr(_pr(1, "h1"))
    data.refresh()
    _github(monkeypatch, {"h2": PAYLOAD_DIFF})
    threat_refresh.scan_new_heads()
    store.save_pr(_pr(1, "h2"))
    data.refresh()
    assert threat_refresh.scan_new_heads() == [1]
    assert store.load_pr(1).threat_verdict == "malicious"


def test_each_pass_is_bounded_most_recently_updated_first(store, monkeypatch):
    for n in range(1, 5):
        store.save_pr(_pr(n, f"h{n}", updated=f"2026-09-2{n}T00:00:00+00:00"))
    data.refresh()
    _github(monkeypatch, {f"h{n}": CLEAN_DIFF for n in range(1, 5)})
    assert threat_refresh.scan_new_heads(limit=2) == [4, 3]
    assert threat_refresh.scan_new_heads(limit=2) == [2, 1]


def test_an_actor_a_pass_blocks_is_flagged_on_their_other_prs(store, monkeypatch):
    store.save_pr(_pr(1, "evil", author="zach"))
    store.save_pr(_pr(2, "quiet", author="zach", stamped="quiet"))
    data.refresh()
    _github(monkeypatch, {"evil": PAYLOAD_DIFF})
    assert threat_refresh.scan_new_heads() == [1]
    assert threat_refresh.scan_new_heads() == [2]
    after = store.load_pr(2)
    assert after.threat_verdict == "malicious"
    assert "blocked-actor" in after.threat_signatures


def test_a_stale_refresh_wakes_the_pass(store, monkeypatch):
    from prospector_app.backend import stale_refresh
    woken: list[bool] = []
    monkeypatch.setattr(stale_refresh, "stale_merge_candidates", lambda: [1])
    monkeypatch.setattr(stale_refresh.ingest, "refresh_prs", lambda st, numbers: [])
    monkeypatch.setattr(threat_refresh, "wake", lambda: woken.append(True))
    stale_refresh.refresh_stale()
    assert woken == [True]


def test_start_only_on_a_worker_machine(monkeypatch):
    monkeypatch.delenv("TRIAGE_VERIFY_WORKER", raising=False)
    monkeypatch.delenv("TRIAGE_FIX_WORKER", raising=False)
    assert threat_refresh.start() is False
