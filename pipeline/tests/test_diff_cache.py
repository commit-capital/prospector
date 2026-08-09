"""diff_cache.py — the machine-local diff cache and its bounded, read-only
GitHub fetch (shared by the CLUSTER wave and the threat scan's fetch step)."""
import pytest

from pipeline import diff_cache
from pipeline.store import Store
from pipeline.wire import DiffManifestItem


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


DIFF_A = "diff --git a/src/a.ts b/src/a.ts\n+alpha\n"
DIFF_B = "diff --git a/src/b.ts b/src/b.ts\n+beta\n"


def _no_github(monkeypatch):
    """Make any subprocess call fail the test — the path under test must not
    reach GitHub."""
    def boom(*a, **k):
        raise AssertionError("unexpected GitHub call")
    monkeypatch.setattr(diff_cache.subprocess, "run", boom)


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "store")


class TestStoreDiffRows:
    """Store accessors for the shared `diffs` table: immutable rows keyed by
    head SHA."""

    def test_save_and_load_roundtrip(self, store):
        store.save_diffs_many([("aaa1", 7, DIFF_A)])
        assert store.load_diff("aaa1") == DIFF_A
        assert store.load_diff("zzz9") is None

    def test_existing_head_is_left_untouched(self, store):
        store.save_diffs_many([("aaa1", 7, DIFF_A)])
        store.save_diffs_many([("aaa1", 7, "changed body")])
        assert store.load_diff("aaa1") == DIFF_A

    def test_bulk_load_returns_only_present(self, store):
        store.save_diffs_many([("aaa1", 7, DIFF_A), ("bbb2", 8, DIFF_B)])
        assert store.load_diffs(["aaa1", "bbb2", "zzz9"]) == {"aaa1": DIFF_A, "bbb2": DIFF_B}
        assert store.load_diffs([]) == {}

    def test_rejects_empty_head_or_body(self, store):
        from pipeline.storekit import ValidationError
        with pytest.raises(ValidationError):
            store.save_diffs_many([("", 7, DIFF_A)])
        with pytest.raises(ValidationError):
            store.save_diffs_many([("aaa1", 7, "")])


class TestReadThroughFetch:
    """fetch_diff consults the local file, then the shared store, then GitHub —
    writing back at each level so the next reader up the chain hits."""

    def test_store_hit_materializes_local_file_without_github(
            self, tmp_path, monkeypatch, store):
        monkeypatch.setattr(diff_cache, "DIFFS", tmp_path / "diffs")
        _no_github(monkeypatch)
        store.save_diffs_many([("aaa1", 7, DIFF_A)])
        paths = diff_cache.fetch_diff_paths(7, "aaa1", store=store)
        assert paths == ["src/a.ts"]
        assert (tmp_path / "diffs" / "aaa1.diff").read_text() == DIFF_A

    def test_github_fetch_writes_back_to_store(self, tmp_path, monkeypatch, store):
        monkeypatch.setattr(diff_cache, "DIFFS", tmp_path / "diffs")

        class R:
            returncode, stdout, stderr = 0, DIFF_A, ""

        monkeypatch.setattr(diff_cache.subprocess, "run", lambda *a, **k: R())
        assert diff_cache.fetch_diff(7, "aaa1", store=store) is True
        assert store.load_diff("aaa1") == DIFF_A

    def test_store_failure_degrades_to_github(self, tmp_path, monkeypatch, store):
        monkeypatch.setattr(diff_cache, "DIFFS", tmp_path / "diffs")

        class R:
            returncode, stdout, stderr = 0, DIFF_A, ""

        monkeypatch.setattr(diff_cache.subprocess, "run", lambda *a, **k: R())
        monkeypatch.setattr(store, "load_diff",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
        monkeypatch.setattr(store, "save_diffs_many",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
        assert diff_cache.fetch_diff(7, "aaa1", store=store) is True
        assert (tmp_path / "diffs" / "aaa1.diff").read_text() == DIFF_A

    def test_local_file_wins_without_touching_store_or_github(
            self, tmp_path, monkeypatch, store):
        d = tmp_path / "diffs"
        d.mkdir(parents=True)
        (d / "aaa1.diff").write_text(DIFF_A)
        monkeypatch.setattr(diff_cache, "DIFFS", d)
        _no_github(monkeypatch)
        monkeypatch.setattr(store, "load_diff",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected store read")))
        assert diff_cache.fetch_diff_paths(7, "aaa1", store=store) == ["src/a.ts"]


class TestBulkFetch:
    def test_fetch_diffs_pulls_store_hits_and_writes_fresh_fetches_through(
            self, tmp_path, monkeypatch, store):
        monkeypatch.setattr(diff_cache, "DIFFS", tmp_path / "diffs")
        store.save_diffs_many([("aaa1", 7, DIFF_A)])

        def run(cmd, **kw):
            class R:
                returncode, stdout, stderr = 0, DIFF_B, ""
            assert cmd[:3] == ["gh", "pr", "diff"] and cmd[3] == "8"
            return R()

        monkeypatch.setattr(diff_cache.subprocess, "run", run)
        manifest = [DiffManifestItem(pr=7, head_sha="aaa1", title=None, diff_path=""),
                    DiffManifestItem(pr=8, head_sha="bbb2", title=None, diff_path="")]
        ok, bad = diff_cache.fetch_diffs(manifest, store=store)
        assert (ok, bad) == (2, 0)
        assert (tmp_path / "diffs" / "aaa1.diff").read_text() == DIFF_A
        assert (tmp_path / "diffs" / "bbb2.diff").read_text() == DIFF_B
        assert store.load_diff("bbb2") == DIFF_B
