"""taxonomy reads the active profile's vocabulary; the generic default has none.

The suite runs under the fixture profile (root conftest sets TRIAGE_PROFILE);
the generic-default tests clear PROFILE_PATH per test. Together these run the
same consumer against two materially different profiles."""
from pipeline import settings, taxonomy


class TestFixtureProfile:
    def test_names_come_from_the_profile(self):
        assert taxonomy.subsystem_names() == [
            "execution-locks", "inbox", "auth", "ui", "cli", "widgets", "other"]

    def test_classify_matches_profile_terms(self):
        assert taxonomy.classify("clean up stale lock on checkout") == "execution-locks"
        assert taxonomy.classify("Widget Frobnicator crashes on load") == "widgets"

    def test_classify_searches_the_body_too(self):
        assert taxonomy.classify("small fix", "the badge count is wrong") == "inbox"

    def test_classify_falls_back_to_other(self):
        assert taxonomy.classify("completely unrelated words") == "other"


class TestGenericDefault:
    def test_vocabulary_is_just_other(self, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", "")
        assert taxonomy.subsystem_names() == ["other"]

    def test_everything_classifies_other(self, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", "")
        assert taxonomy.classify("clean up stale lock on checkout") == "other"
