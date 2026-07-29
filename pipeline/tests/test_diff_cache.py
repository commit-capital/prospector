"""diff_cache.py — the machine-local diff cache and its bounded, read-only
GitHub fetch (shared by the CLUSTER wave and the threat scan's fetch step)."""
from pipeline import diff_cache


class TestFetchDiffFallback:
    """GitHub refuses .diff for PRs over 20k lines (HTTP 406); fetch_diff
    falls back to synthesizing one from the per-file listing."""

    def _fake_run(self, files_jsonl):
        def run(cmd, **kw):
            class R:
                pass
            r = R()
            if cmd[:3] == ["gh", "pr", "diff"]:
                r.returncode, r.stdout, r.stderr = 1, "", "HTTP 406: diff exceeded the maximum number of lines"
            else:
                r.returncode, r.stdout, r.stderr = 0, files_jsonl, ""
            return r
        return run

    def test_too_large_diff_synthesized_from_files_api(self, tmp_path, monkeypatch):
        monkeypatch.setattr(diff_cache, "DIFFS", tmp_path)
        files = (
            '{"filename": "src/agent.ts", "status": "modified", "additions": 5, "deletions": 2, "patch": "@@ -1 +1 @@\\n-old\\n+new"}\n'
            '{"filename": "package-lock.json", "status": "modified", "additions": 30000, "deletions": 29000}\n'
        )
        monkeypatch.setattr(diff_cache.subprocess, "run", self._fake_run(files))
        assert diff_cache.fetch_diff(688, "deadbeef") is True
        text = (tmp_path / "deadbeef.diff").read_text()
        assert "diff --git a/src/agent.ts b/src/agent.ts" in text
        assert "+new" in text
        assert "package-lock.json" in text  # patch-less file still listed

    def test_fallback_failure_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(diff_cache, "DIFFS", tmp_path)
        def run(cmd, **kw):
            class R:
                returncode, stdout, stderr = 1, "", "boom"
            return R()
        monkeypatch.setattr(diff_cache.subprocess, "run", run)
        assert diff_cache.fetch_diff(688, "deadbeef") is False
        assert not (tmp_path / "deadbeef.diff").exists()

    def test_paths_are_derived_before_diff_cache_is_capped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(diff_cache, "DIFFS", tmp_path)
        monkeypatch.setattr(diff_cache, "MAX_DIFF_BYTES", 60)
        text = (
            "diff --git a/src/app.ts b/src/app.ts\n"
            + ("+source\n" * 20)
            + "diff --git a/src/app.test.ts b/src/app.test.ts\n"
            + "+test\n"
        )

        class R:
            returncode, stdout, stderr = 0, text, ""

        monkeypatch.setattr(diff_cache.subprocess, "run", lambda *a, **k: R())

        paths = diff_cache.fetch_diff_paths(688, "deadbeef")

        assert paths == ["src/app.ts", "src/app.test.ts"]
        assert (tmp_path / "deadbeef.diff").stat().st_size == 60

    def test_explicit_diffs_dir_overrides_the_canonical_cache(self, tmp_path, monkeypatch):
        text = "diff --git a/src/app.ts b/src/app.ts\n+x\n"

        class R:
            returncode, stdout, stderr = 0, text, ""

        monkeypatch.setattr(diff_cache.subprocess, "run", lambda *a, **k: R())
        alt = tmp_path / "alt"
        assert diff_cache.fetch_diff(9, "cafe", alt) is True
        assert (alt / "cafe.diff").read_text() == text
