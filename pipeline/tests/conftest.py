import pytest

from pipeline import settings


@pytest.fixture(autouse=True)
def _greptile_profile(monkeypatch):
    """This suite was written against the greptile deployment's 5/5 merge bar. Pin
    the greptile review profile so every legacy test asserts that bar regardless of
    the `none` default; tests exercising the no-provider path override explicitly."""
    monkeypatch.setattr(settings, "REVIEW_PROVIDER", "greptile")
    monkeypatch.setattr(settings, "REVIEW_THRESHOLD", None)


@pytest.fixture(autouse=True)
def _isolate_cluster_unit_dirs(tmp_path, monkeypatch):
    """The CLUSTER driver's unit/output staging dirs are module-level constants
    pointing at fixed /tmp paths shared by every process. Point them at this
    test's tmp_path so parallel workers (pytest-xdist) can't read each other's
    unit files back through the glob-based readers."""
    from pipeline import cluster_driver as cd

    for name in ("CLUSTER_UNIT_DIR", "CLUSTER_OUT_DIR", "ASSIGN_UNIT_DIR",
                 "ASSIGN_OUT_DIR", "STRADDLE_UNIT_DIR"):
        d = tmp_path / name.lower().replace("_", "-")
        d.mkdir()
        monkeypatch.setattr(cd, name, d)
