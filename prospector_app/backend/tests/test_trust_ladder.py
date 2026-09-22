"""The trust ladder derives each action type's rung on read: promotion is
earned by the window's rates, and a reversal spike demotes with no human
action, because nothing is stored."""
from __future__ import annotations

from pipeline import storekit
from prospector_app.backend import trust_ladder


def _decision(pr, decision, *, at, suggested=None, dry_run=False):
    rec = {"at": at, "pr": pr, "decision": decision, "dry_run": dry_run,
           "features": {}}
    if suggested is not None:
        rec["features"]["disposition"] = suggested
    return rec


def _ending(pr, action, status):
    return storekit.parse_run({"phase": "fix:single", "pr": pr, "started": None,
                               "finished": "2026-09-22T00:00:00",
                               "stats": {"action": action, "status": status}})


def test_rung_climbs_and_fails_closed_on_thin_windows():
    R = trust_ladder.Rate
    assert trust_ladder.rung(R(0, 0), R(0, 0)) == "shadow"
    assert trust_ladder.rung(R(9, 9), R(0, 0)) == "shadow"
    assert trust_ladder.rung(R(8, 10), R(0, 0)) == "one-click"
    assert trust_ladder.rung(R(29, 30), R(0, 0)) == "auto+undo"
    assert trust_ladder.rung(R(100, 100), R(0, 0)) == "auto"


def test_reversal_spike_demotes():
    R = trust_ladder.Rate
    assert trust_ladder.rung(R(30, 30), R(0, 20)) == "auto+undo"
    assert trust_ladder.rung(R(30, 30), R(5, 20)) == "one-click"
    assert trust_ladder.rung(R(30, 30), R(10, 20)) == "shadow"


def test_upstream_agreement_buckets_by_the_suggested_disposition():
    rows = [
        _decision(1, "CLOSE_DUP", at="t1", suggested="close-dup"),
        _decision(2, "MERGE", at="t2", suggested="close-dup"),
        _decision(3, "MERGE", at="t3", suggested="merge"),
        # A comment is not a ruling on the pick.
        _decision(4, "REVIEW:comment", at="t4", suggested="merge"),
        # A capture without a recorded suggestion carries no agreement evidence.
        _decision(5, "CLOSE_DUP", at="t5"),
        # A dry-run capture is a preview.
        _decision(6, "CLOSE_DUP", at="t6", suggested="close-dup", dry_run=True),
    ]
    rates = trust_ladder.upstream_rates(rows)
    agreement, _ = rates["close-dup"]
    assert (agreement.hits, agreement.n) == (1, 2)
    m_agree, m_rev = rates["merge"]
    assert (m_agree.hits, m_agree.n) == (1, 1)
    assert m_rev.n == 0


def test_a_close_later_reopened_counts_as_a_reversal():
    rows = [
        _decision(1, "CLOSE_DUP", at="2026-01-01T00:00:00", suggested="close-dup"),
        _decision(1, "REOPEN", at="2026-01-02T00:00:00"),
        _decision(2, "CLOSE_DUP", at="2026-01-03T00:00:00", suggested="close-dup"),
    ]
    _, reversal = trust_ladder.upstream_rates(rows)["close-dup"]
    assert (reversal.hits, reversal.n) == (1, 2)


def test_a_reopen_before_the_close_does_not_reverse_it():
    rows = [
        _decision(1, "REOPEN", at="2026-01-01T00:00:00"),
        _decision(1, "CLOSE_FIXED", at="2026-01-02T00:00:00", suggested="close-fixed"),
    ]
    _, reversal = trust_ladder.upstream_rates(rows)["close-fixed"]
    assert (reversal.hits, reversal.n) == (0, 1)


def test_ledger_rates_read_verdict_endings_only():
    runs = [
        _ending(1, "update", "pushed"),
        _ending(2, "update", "cancelled"),
        _ending(3, "update", "refused"),
        _ending(4, "update", "awaiting-review"),
        _ending(5, "update", "failed"),
        _ending(6, "fix", "pushed"),
    ]
    rates = trust_ladder.ledger_rates(runs)
    agreement, reversal = rates["update"]
    assert (agreement.hits, agreement.n) == (1, 2)
    assert (reversal.hits, reversal.n) == (1, 3)
    f_agree, _ = rates["fix"]
    assert (f_agree.hits, f_agree.n) == (1, 1)


def test_window_reads_only_the_most_recent_events():
    old = [_ending(n, "update", "cancelled") for n in range(trust_ladder.WINDOW)]
    new = [_ending(n, "update", "pushed") for n in range(trust_ladder.WINDOW)]
    agreement, _ = trust_ladder.ledger_rates(old + new)["update"]
    assert (agreement.hits, agreement.n) == (trust_ladder.WINDOW, trust_ladder.WINDOW)


def test_ladder_covers_every_type_and_fails_closed_to_shadow(monkeypatch):
    from prospector_app.backend import data
    from prospector_app.backend import training
    monkeypatch.setattr(data, "runs", lambda: [_ending(1, "rebase", "pushed")])
    monkeypatch.setattr(training, "decisions", lambda: [])
    out = trust_ladder.ladder()
    assert out["rungs"] == ["shadow", "one-click", "auto+undo", "auto"]
    by_id = {t["id"]: t for t in out["types"]}
    assert set(by_id) == set(trust_ladder.ACTION_TYPES)
    assert by_id["rebase"]["agreement"] == {"hits": 1, "n": 1, "rate": 1.0}
    assert by_id["rebase"]["rung"] == "shadow"
    assert by_id["merge"]["agreement"]["n"] == 0
    assert set(out["bars"]) == {"one-click", "auto+undo", "auto"}
