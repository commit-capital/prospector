"""#195: a CLOSE_DUP / CLOSE_FIXED fired without explicit references must still
cite the canonical PR (or upstream fix) the store already knows. A bulk close
that carried no canonical used to fall back to the neutral 'duplicate during
triage' wording, dropping the link to the PR it's a dup of — even though the
PR's stored analysis named one. The executor now backfills those references
from the store before composing the closing comment."""
import types

from prospector_app.backend import data
from prospector_app.backend import executor
from prospector_app.backend import models


def _rec(**kw):
    fields = {"canonical": None, "upstream_pr": None,
              "upstream_commit": None, "upstream_date": None}
    fields.update(kw)
    return types.SimpleNamespace(**fields)


def _setup(monkeypatch, rec):
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {3711: [221]})
    monkeypatch.setattr(data, "prs", lambda: {3711: rec})
    monkeypatch.setattr(executor, "_pr_live", lambda n: None)  # preflight fail-open
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)


def test_close_dup_backfills_stored_canonical(monkeypatch):
    """The exact #195 case: a bulk CLOSE_DUP carrying canonical=None still links
    the canonical PR the store knows."""
    _setup(monkeypatch, _rec(canonical=4416))
    res = executor.execute_pr(3711, models.CloseAction(action="CLOSE_DUP", canonical=None),
                              token=None, dry_run=True)
    assert res["status"] == "dry-run"
    assert "#4416" in res["detail"]


def test_explicit_canonical_wins_over_stored(monkeypatch):
    """An operator-supplied canonical is never overridden by the stored one."""
    _setup(monkeypatch, _rec(canonical=4416))
    res = executor.execute_pr(3711, models.CloseAction(action="CLOSE_DUP", canonical=9999),
                              token=None, dry_run=True)
    assert "#9999" in res["detail"] and "#4416" not in res["detail"]


def test_explicit_comment_wins(monkeypatch):
    """An operator-edited comment is posted verbatim, not regenerated."""
    _setup(monkeypatch, _rec(canonical=4416))
    res = executor.execute_pr(3711, models.CloseAction(action="CLOSE_DUP", comment="custom dup note"),
                              token=None, dry_run=True)
    assert "custom dup note" in res["detail"]


def test_no_stored_canonical_stays_neutral(monkeypatch):
    """Nothing to cite (the store has no canonical) → the honest neutral close,
    no fabricated reference."""
    _setup(monkeypatch, _rec(canonical=None))
    res = executor.execute_pr(3711, models.CloseAction(action="CLOSE_DUP", canonical=None),
                              token=None, dry_run=True)
    assert "4416" not in res["detail"] and "during triage" in res["detail"].lower()


def test_close_fixed_backfills_upstream(monkeypatch):
    """CLOSE_FIXED likewise recovers the stored upstream reference."""
    _setup(monkeypatch, _rec(upstream_pr=4416))
    res = executor.execute_pr(3711, models.CloseAction(action="CLOSE_FIXED"), token=None, dry_run=True)
    assert "#4416" in res["detail"]
