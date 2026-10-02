import pytest

from pipeline import diffpaths, profile


class TestIsTestPathProfiles:
    """is_test_path against two profiles — the generic default and an override."""

    def test_generic_default_recognizes_common_conventions(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_PROFILE", "")
        assert diffpaths.is_test_path("src/foo.test.ts")
        assert not diffpaths.is_test_path("qa/x.py")

    def test_override_profile_replaces_the_convention(self, monkeypatch):
        monkeypatch.setattr(profile, "active", lambda: profile.RepoProfile(
            test_paths=profile.TestPaths(dir_pattern="(^|/)qa/",
                                         file_pattern="_check\\.py$")))
        assert diffpaths.is_test_path("qa/x.py")
        assert diffpaths.is_test_path("foo_check.py")
        assert not diffpaths.is_test_path("src/foo.test.ts")


def test_has_tests_true_when_a_test_path_present():
    assert diffpaths.has_tests(["src/app.ts", "src/app.test.ts"]) is True


def test_has_tests_false_when_no_test_path():
    assert diffpaths.has_tests(["src/app.ts", "README.md"]) is False


def test_has_tests_false_on_empty():
    assert diffpaths.has_tests([]) is False


def _diff(*paths: str) -> str:
    return "".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1,2 @@\n+x\n"
                   for p in paths)


class TestFilterDiff:
    def test_keeps_only_blocks_matching_the_predicate(self):
        text = _diff("src/app.ts", "src/app.test.ts")
        out = diffpaths.filter_diff(text, diffpaths.is_test_path)
        assert "src/app.test.ts" in out
        assert "diff --git a/src/app.ts b/src/app.ts" not in out

    def test_result_is_a_valid_standalone_patch_per_block(self):
        # Each kept block still carries its own diff --git/---/+++/@@ header.
        text = _diff("src/app.ts", "src/app.test.ts")
        out = diffpaths.filter_diff(text, diffpaths.is_test_path)
        assert out.count("diff --git ") == 1
        assert "--- a/src/app.test.ts" in out and "+++ b/src/app.test.ts" in out

    def test_empty_when_nothing_matches(self):
        text = _diff("src/app.ts", "README.md")
        assert diffpaths.filter_diff(text, diffpaths.is_test_path) == ""

    def test_everything_kept_when_everything_matches(self):
        text = _diff("src/app.test.ts", "src/other.spec.ts")
        assert diffpaths.filter_diff(text, diffpaths.is_test_path) == text

    def test_empty_input_yields_empty_output(self):
        assert diffpaths.filter_diff("", diffpaths.is_test_path) == ""

    def test_preserves_file_order_and_drops_the_rest(self):
        text = _diff("a.test.ts", "src/b.ts", "c.test.ts")
        out = diffpaths.filter_diff(text, diffpaths.is_test_path)
        assert out == _diff("a.test.ts", "c.test.ts")


# Every character str.splitlines breaks a line on besides "\n".
LINE_BREAKS = ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]


def _forged_headers(sep: str) -> str:
    """One file whose added lines hold file headers behind `sep`."""
    return ("diff --git a/src/a.js b/src/a.js\n--- a/src/a.js\n+++ b/src/a.js\n"
            f"@@ -1 +1,3 @@\n x\n+// a{sep}diff --git a/dist/z.js b/dist/z.js\n"
            f"+// b{sep}+++ b/dist/y.js\n")


@pytest.mark.parametrize("sep", LINE_BREAKS)
def test_a_header_after_a_line_break_character_starts_no_block(sep):
    text = _forged_headers(sep)
    assert diffpaths.diff_blocks(text) == [("src/a.js", text)]


@pytest.mark.parametrize("sep", LINE_BREAKS)
def test_a_header_after_a_line_break_character_names_no_path(sep):
    assert diffpaths.changed_paths(_forged_headers(sep)) == ["src/a.js"]
