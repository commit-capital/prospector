"""The activity log's foreign-repo guard. An event records a bare PR/issue number
and no repo, so a process pointed at a scratch repo — an end-to-end test driving the
real executor — must not be able to append rows indistinguishable from real ones."""
import pytest

from pipeline import schema
from pipeline import settings
from pipeline import storekit
from prospector_app.backend import activity


def _engine(tmp_path):
    eng = storekit.get_engine(f"sqlite:///{tmp_path}/store.db")
    schema.METADATA.create_all(eng)
    return eng


def test_first_write_stamps_the_store(tmp_path):
    eng = _engine(tmp_path)
    assert storekit.stamped_repo(eng) is None
    storekit.assert_repo(eng)
    assert storekit.stamped_repo(eng) == settings.repo()


def test_matching_repo_is_allowed(tmp_path):
    eng = _engine(tmp_path)
    storekit.assert_repo(eng)   # stamps
    storekit.assert_repo(eng)   # and passes on the next call


def test_foreign_repo_is_refused(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    storekit.assert_repo(eng)   # stamped with the configured repo
    monkeypatch.setenv("TRIAGE_REPO", "other-owner/other-repo")
    with pytest.raises(storekit.ForeignRepoError, match="refusing to write"):
        storekit.assert_repo(eng)


def test_escape_hatch_downgrades_to_a_warning(tmp_path, monkeypatch, capsys):
    eng = _engine(tmp_path)
    storekit.assert_repo(eng)
    monkeypatch.setenv("TRIAGE_REPO", "other-owner/other-repo")
    monkeypatch.setattr(settings, "STORE_ALLOW_FOREIGN_REPO", True)
    storekit._FOREIGN_WARNED.clear()
    storekit.assert_repo(eng)   # does not raise
    assert "refusing to write" in capsys.readouterr().err


def test_record_refuses_a_foreign_repo_write(tmp_path, monkeypatch):
    """The guard is reached through activity.record, not just called directly."""
    eng = _engine(tmp_path)
    storekit.assert_repo(eng)
    monkeypatch.setattr(activity, "_engine", lambda: eng)
    monkeypatch.setattr(activity, "operator", lambda: {"name": "T", "email": None})
    activity.record("issue-close", issue=10, action="CLOSE_ISSUE_DUP", status="executed")

    monkeypatch.setenv("TRIAGE_REPO", "other-owner/other-repo")
    with pytest.raises(storekit.ForeignRepoError):
        activity.record("issue-close", issue=389, action="CLOSE_ISSUE_DUP", status="executed")
