import pytest



@pytest.fixture
def active_reviewers(monkeypatch):
    """Make exactly the given reviewer ids the ones seen on the repository today."""
    from pipeline import review_policy, storekit

    def pin(*ids: str) -> None:
        seen = {rid: {"last_observed_at": storekit.now(), "prs": 1} for rid in ids}
        monkeypatch.setattr(review_policy, "_load_seen", lambda: seen)
        review_policy.reset()
    return pin


@pytest.fixture(autouse=True)
def _greptile_profile(active_reviewers, monkeypatch):
    """This suite was written against the greptile deployment's 5/5 merge bar:
    Greptile is the one reviewer seen, unless a test names others."""
    active_reviewers("greptile")
    monkeypatch.delenv("TRIAGE_REVIEW_THRESHOLD", raising=False)


@pytest.fixture(autouse=True)
def _isolate_cluster_unit_dirs(tmp_path, monkeypatch):
    """The CLUSTER driver's unit/output staging dirs are module-level constants
    pointing at fixed /tmp paths shared by every process. Point them at this
    test's tmp_path so parallel workers (pytest-xdist) can't read each other's
    unit files back through the glob-based readers."""
    from pipeline import cluster_driver as cd

    for name in ("CLUSTER_UNIT_DIR", "CLUSTER_OUT_DIR",
                 "ASSIGN_OUT_DIR", "STRADDLE_UNIT_DIR"):
        d = tmp_path / name.lower().replace("_", "-")
        d.mkdir()
        monkeypatch.setattr(cd, name, d)
