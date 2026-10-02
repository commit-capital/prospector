"""threat_evidence.capture: an immutable, payload-free record of a flagged head,
with its diffs gzip-compressed and hashed, from read-only GitHub reads."""
from __future__ import annotations

import gzip
import hashlib
import json

from pipeline import threat_evidence as te
from pipeline.store import Store
from pipeline.threat_evidence import LiveGitHub  # the real reads; conftest stubs te.LiveGitHub
from pipeline.storekit import EvidenceBlobs, EvidenceRecord

PAYLOAD = "global['!']='9-0008-2';var _$_1e42=(function(l,e){var x=String.fromCharCode(127);})"
HEAD = "e" * 40
PRIOR = "d" * 40
BASE = "b" * 40

HONEST = (
    "diff --git a/src/a.ts b/src/a.ts\nindex 1..2 100644\n--- a/src/a.ts\n+++ b/src/a.ts\n"
    "@@ -1 +1 @@\n-old\n+new\n")
INJECTED = (
    "diff --git a/cli/esbuild.config.mjs b/cli/esbuild.config.mjs\nindex 3..4 100644\n"
    "--- a/cli/esbuild.config.mjs\n+++ b/cli/esbuild.config.mjs\n@@ -1,2 +1,4 @@\n"
    "+import { createRequire } from 'module';\n+const require = createRequire(import.meta.url);\n"
    " export default {};\n-void main();\n+void main();" + " " * 300 + PAYLOAD + "\n")
FLAGGED_DIFF = (HONEST + INJECTED).encode()
PRIOR_DIFF = HONEST.encode()


class FakeGitHub:
    """GitHubReads answering for one flagged PR, recording every call."""

    def __init__(self, *, head_now: str = HEAD, compare_ok: bool = True,
                 listing: tuple[bytes, bool] | None = None, pushes: bool = True,
                 pull_ok: bool = True) -> None:
        self.head_now, self.compare_ok, self.listing = head_now, compare_ok, listing
        self.pushes, self.pull_ok = pushes, pull_ok
        self.calls: list[str] = []

    def pull(self, n: int) -> dict | None:
        self.calls.append(f"pull {n}")
        if not self.pull_ok:
            return None
        return {"number": n, "html_url": f"https://github.com/o/r/pull/{n}", "title": "fix: x",
                "body": "honest words", "state": "open", "created_at": "2026-08-22T22:27:22Z",
                "user": {"login": "mallory"}, "base": {"sha": BASE, "ref": "trunk"},
                "head": {"sha": self.head_now, "ref": "fix/x",
                         "repo": {"full_name": "mallory/r", "id": 42}}}

    def compare(self, base: str, head: str) -> dict | None:
        self.calls.append(f"compare {base[:1]}...{head[:1]}")
        return {"total_commits": 1, "commits": [{
            "sha": head, "commit": {
                "author": {"name": "Project Bot", "email": "noreply@x", "date": "2026-08-22T22:26:46Z"},
                "committer": {"name": "Project Bot", "email": "noreply@x", "date": "2026-08-22T22:26:46Z"},
                "verification": {"verified": False, "reason": "unsigned"}}}]}

    def compare_diff(self, base: str, head: str) -> bytes | None:
        self.calls.append(f"diff {base[:1]}...{head[:1]}")
        if not self.compare_ok:
            return None
        return {HEAD: FLAGGED_DIFF, PRIOR: PRIOR_DIFF}.get(head)

    def listing_diff(self, n: int) -> tuple[bytes, bool] | None:
        self.calls.append(f"listing {n}")
        return self.listing

    def force_pushes(self, n: int) -> list[te.ForcePush] | None:
        self.calls.append(f"pushes {n}")
        if not self.pushes:
            return []
        return [te.ForcePush(at="2026-09-28T16:44:31Z", actor="mallory", before=PRIOR, after=HEAD)]

    def user(self, login: str) -> dict | None:
        self.calls.append(f"user {login}")
        return {"login": login, "id": 293929990, "type": "User", "created_at": "2026-06-15T16:53:31Z"}

    def login(self) -> str | None:
        return "operator"


FLAG = te.Flag(pr=11987, head_sha=HEAD, author="mallory",
               signatures=["obfuscated-self-decoder"], scanned_at="2026-10-02T16:03:50+00:00")


def _only(store: Store) -> tuple[EvidenceRecord, EvidenceBlobs]:
    [rec] = store.threat_evidence(pr=FLAG.pr)
    blobs = store.threat_evidence_blobs(rec.id)
    assert blobs is not None
    return rec, blobs


def _gunzip(blob: bytes | None) -> bytes:
    assert blob is not None
    return gzip.decompress(blob)


def test_complete_capture_stores_exact_bytes_and_hashes(tmp_path):
    store = Store(tmp_path)
    assert te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path) == "captured"
    rec, blobs = _only(store)
    assert rec.complete and rec.head_sha == HEAD and rec.author == "mallory"
    assert _gunzip(blobs.diff_gz) == FLAGGED_DIFF
    assert _gunzip(blobs.prior_gz) == PRIOR_DIFF
    art = rec.data["artifacts"]
    assert art["diff"]["sha256"] == hashlib.sha256(FLAGGED_DIFF).hexdigest()
    assert art["diff"]["source"] == "compare" and art["diff"]["complete"] is True
    assert art["prior"]["sha256"] == hashlib.sha256(PRIOR_DIFF).hexdigest()
    assert art["prior"]["before_sha"] == PRIOR
    assert rec.data["actor"]["id"] == 293929990
    assert rec.data["commits"][0]["committer"]["date"] == "2026-08-22T22:26:46Z"
    assert rec.data["force_pushes"][0]["before"] == PRIOR
    assert rec.data["provenance"]["captured_by"] == "operator"
    assert rec.data["base_sha"] == BASE and rec.data["head_repo"]["id"] == 42


def test_record_holds_match_locations_not_payload(tmp_path):
    store = Store(tmp_path)
    te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path)
    rec, _ = _only(store)
    dumped = json.dumps(rec.data)
    assert "fromCharCode" not in dumped and "createRequire" not in dumped
    lines = FLAGGED_DIFF.decode().split("\n")
    assert rec.data["detection"]["matches"]
    for m in rec.data["detection"]["matches"]:
        assert m["file"] == "cli/esbuild.config.mjs"
        assert lines[m["diff_line"] - 1].startswith("+")
    assert "obfuscated-self-decoder" in rec.data["detection"]["full_diff_signatures"]


def test_full_diff_signatures_include_a_payload_after_a_form_feed(tmp_path):
    store = Store(tmp_path)
    gh = FakeGitHub(pushes=False)
    hidden = (HONEST + INJECTED.replace(" " * 300, "\x0c")).encode()
    gh.compare_diff = lambda base, head: hidden  # type: ignore[method-assign]
    te.capture(store, FLAG, github=gh, diffs_dir=tmp_path)
    rec, _ = _only(store)
    assert "obfuscated-self-decoder" in rec.data["detection"]["full_diff_signatures"]


def test_existing_complete_capture_makes_no_github_call(tmp_path):
    store = Store(tmp_path)
    te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path)
    again = FakeGitHub()
    assert te.capture(store, FLAG, github=again, diffs_dir=tmp_path) == "already"
    assert again.calls == []


def test_head_moved_fetches_flagged_sha_and_skips_listing(tmp_path):
    store = Store(tmp_path)
    gh = FakeGitHub(head_now="f" * 40, compare_ok=False, listing=(b"diff --git a/z b/z\n", True))
    (tmp_path / f"{HEAD}.diff").write_bytes(b"diff --git a/cached b/cached\n")
    assert te.capture(store, FLAG, github=gh, diffs_dir=tmp_path) == "partial"
    assert "listing 11987" not in gh.calls
    assert "diff b...e" in gh.calls
    rec, blobs = _only(store)
    assert rec.data["artifacts"]["diff"]["source"] == "scan-cache"
    assert _gunzip(blobs.diff_gz) == b"diff --git a/cached b/cached\n"


def test_listing_fallback_when_compare_refused(tmp_path):
    store = Store(tmp_path)
    gh = FakeGitHub(compare_ok=False, listing=(b"diff --git a/z b/z\n+x\n", True))
    assert te.capture(store, FLAG, github=gh, diffs_dir=tmp_path) == "captured"
    rec, _ = _only(store)
    assert rec.data["artifacts"]["diff"]["source"] == "per-file-listing"


def test_listing_missing_a_patch_is_partial(tmp_path):
    store = Store(tmp_path)
    gh = FakeGitHub(compare_ok=False, listing=(b"diff --git a/z b/z\n# no patch\n", False))
    assert te.capture(store, FLAG, github=gh, diffs_dir=tmp_path) == "partial"


def test_second_partial_is_not_written_but_complete_is(tmp_path):
    store = Store(tmp_path)
    (tmp_path / f"{HEAD}.diff").write_bytes(b"diff --git a/c b/c\n")
    assert te.capture(store, FLAG, github=FakeGitHub(compare_ok=False), diffs_dir=tmp_path) == "partial"
    assert te.capture(store, FLAG, github=FakeGitHub(compare_ok=False), diffs_dir=tmp_path) == "failed"
    assert te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path) == "captured"
    assert [r.complete for r in store.threat_evidence(pr=FLAG.pr)] == [True, False]


def test_nothing_fetched_writes_nothing(tmp_path):
    store = Store(tmp_path)
    gh = FakeGitHub(pull_ok=False, compare_ok=False)
    gh.compare = lambda base, head: None  # type: ignore[method-assign]
    assert te.capture(store, FLAG, github=gh, diffs_dir=tmp_path) == "failed"
    assert store.threat_evidence(pr=FLAG.pr) == []


def test_oversized_diff_is_truncated_and_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(te, "MAX_DIFF_BYTES", 100)
    store = Store(tmp_path)
    assert te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path) == "partial"
    rec, blobs = _only(store)
    assert len(_gunzip(blobs.diff_gz)) == 100
    assert rec.data["artifacts"]["diff"]["truncated"] is True


def test_no_force_push_means_no_prior(tmp_path):
    store = Store(tmp_path)
    assert te.capture(store, FLAG, github=FakeGitHub(pushes=False), diffs_dir=tmp_path) == "captured"
    rec, blobs = _only(store)
    assert rec.data["artifacts"]["prior"] is None and blobs.prior_gz is None


def _cache_heads(store: Store, *rows: tuple[str, str, str]) -> None:
    """Shared diff-cache rows for PR 11987: (head, fetched_at, body)."""
    from pipeline import schema
    with store.engine.begin() as conn:
        for head, at, body in rows:
            conn.execute(schema.diffs.insert().values(head_sha=head, pr=FLAG.pr, body=body,
                                                      fetched_at=at))


def test_prior_found_in_the_diff_cache_when_github_shows_no_force_push(tmp_path):
    store = Store(tmp_path)
    _cache_heads(store, (PRIOR, "2026-08-27T20:21:54+00:00", HONEST),
                 (HEAD, "2026-10-02T16:11:35+00:00", HONEST + INJECTED))
    assert te.capture(store, FLAG, github=FakeGitHub(pushes=False), diffs_dir=tmp_path) == "captured"
    rec, blobs = _only(store)
    prior = rec.data["artifacts"]["prior"]
    assert prior["before_sha"] == PRIOR and prior["found_by"] == "diff-cache"
    assert prior["source"] == "compare" and prior["complete"] is True
    assert _gunzip(blobs.prior_gz) == PRIOR_DIFF
    readme = dict(te.bundle_files(rec, blobs))["README.md"].decode()
    assert "newest earlier head Prospector fetched" in readme
    assert f"{PRIOR}:refs/evidence/" in readme


def test_prior_falls_back_to_the_cached_body_when_github_refuses_it(tmp_path):
    store = Store(tmp_path)
    _cache_heads(store, (PRIOR, "2026-08-27T20:21:54+00:00", HONEST),
                 (HEAD, "2026-10-02T16:11:35+00:00", HONEST + INJECTED))
    gh = FakeGitHub(pushes=False)
    gh.compare_diff = lambda base, head: FLAGGED_DIFF if head == HEAD else None  # type: ignore[method-assign]
    te.capture(store, FLAG, github=gh, diffs_dir=tmp_path)
    rec, blobs = _only(store)
    prior = rec.data["artifacts"]["prior"]
    assert prior["source"] == "diff-cache" and prior["before_sha"] == PRIOR
    assert _gunzip(blobs.prior_gz) == HONEST.encode()
    assert any(PRIOR[:12] in e for e in rec.data["errors"])


def test_a_head_cached_after_the_flagged_one_is_not_its_prior(tmp_path):
    store = Store(tmp_path)
    _cache_heads(store, (HEAD, "2026-10-02T16:11:35+00:00", HONEST + INJECTED),
                 ("f" * 40, "2026-10-03T00:00:00+00:00", HONEST))
    te.capture(store, FLAG, github=FakeGitHub(pushes=False), diffs_dir=tmp_path)
    rec, _ = _only(store)
    assert rec.data["artifacts"]["prior"] is None


def test_github_force_push_wins_over_the_diff_cache(tmp_path):
    store = Store(tmp_path)
    _cache_heads(store, ("c" * 40, "2026-08-01T00:00:00+00:00", HONEST))
    te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path)
    rec, _ = _only(store)
    assert rec.data["artifacts"]["prior"]["before_sha"] == PRIOR
    assert rec.data["artifacts"]["prior"]["found_by"] == "force-push"


def test_non_utf8_diff_round_trips(tmp_path):
    store = Store(tmp_path)
    gh = FakeGitHub(pushes=False)
    raw = FLAGGED_DIFF + b"+\xff\xfe binary-ish\n"
    gh.compare_diff = lambda base, head: raw  # type: ignore[method-assign]
    te.capture(store, FLAG, github=gh, diffs_dir=tmp_path)
    _, blobs = _only(store)
    assert _gunzip(blobs.diff_gz) == raw


def test_written_captures_append_a_ledger_run(tmp_path):
    store = Store(tmp_path)
    te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path)
    te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path)  # already: no run
    runs = [r for r in store.runs() if r.phase == "threat-evidence:capture"]
    assert len(runs) == 1 and runs[0].raw["status"] == "captured" and runs[0].raw["pr"] == 11987


# ---------------------------------------------------------------------------
# Export, the zip bundle, and verification
# ---------------------------------------------------------------------------
def _captured(tmp_path) -> tuple[Store, EvidenceRecord, EvidenceBlobs]:
    store = Store(tmp_path)
    te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path)
    rec, blobs = _only(store)
    return store, rec, blobs


def test_force_push_changes_keeps_only_changed_blocks():
    changed, removed = te.force_push_changes(FLAGGED_DIFF, PRIOR_DIFF)
    assert changed == INJECTED.encode() and removed == []
    changed, removed = te.force_push_changes(HONEST.encode(), (HONEST + INJECTED).encode())
    assert changed == b"" and removed == ["cli/esbuild.config.mjs"]


def test_force_push_changes_keeps_non_utf8_bytes_exact():
    raw = INJECTED.encode() + b"+\xff\xfe\n"
    changed, _ = te.force_push_changes(HONEST.encode() + raw, PRIOR_DIFF)
    assert changed == raw


def test_bundle_files_and_sha256sums(tmp_path):
    _, rec, blobs = _captured(tmp_path)
    files = dict(te.bundle_files(rec, blobs))
    head7 = HEAD[:7]
    assert files[f"pr-11987-{head7}.diff"] == FLAGGED_DIFF
    assert files[f"pr-11987-{PRIOR[:7]}.prior.diff"] == PRIOR_DIFF
    assert files["force-push-changes.diff"] == INJECTED.encode()
    assert json.loads(files["record.json"])["head_sha"] == HEAD
    sums = files["SHA256SUMS"].decode().splitlines()
    assert f"{hashlib.sha256(FLAGGED_DIFF).hexdigest()}  pr-11987-{head7}.diff" in sums
    assert len(sums) == len(files) - 1
    readme = files["README.md"].decode()
    assert "git fetch" in readme and HEAD in readme and "Do not" in readme
    assert "fromCharCode" not in readme


def test_bundle_refuses_a_hash_mismatch(tmp_path):
    import pytest
    _, rec, blobs = _captured(tmp_path)
    bad = EvidenceBlobs(diff_gz=gzip.compress(b"tampered"), prior_gz=blobs.prior_gz)
    with pytest.raises(te.IntegrityError):
        te.bundle_files(rec, bad)


def test_export_writes_read_only_files_outside_git(tmp_path):
    _, rec, blobs = _captured(tmp_path / "s")
    out = tmp_path / "out"
    paths = te.export(rec, blobs, out)
    assert {p.name for p in paths} >= {"README.md", "record.json", "SHA256SUMS"}
    for p in paths:
        assert not p.stat().st_mode & 0o222


def test_export_refuses_inside_a_git_work_tree(tmp_path):
    import subprocess

    import pytest
    _, rec, blobs = _captured(tmp_path / "s")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(te.ExportRefused):
        te.export(rec, blobs, repo / "evidence" / "pr")
    assert not (repo / "evidence").exists()


def test_bundle_zip_holds_the_same_files(tmp_path):
    import io
    import zipfile
    _, rec, blobs = _captured(tmp_path)
    z = zipfile.ZipFile(io.BytesIO(te.bundle_zip(rec, blobs)))
    root = te.bundle_name(rec)
    files = te.bundle_files(rec, blobs)
    assert sorted(z.namelist()) == sorted(f"{root}/{n}" for n, _ in files)
    assert z.read(f"{root}/SHA256SUMS") == dict(files)["SHA256SUMS"]


def test_summary_has_no_payload_and_no_blobs(tmp_path):
    _, rec, _ = _captured(tmp_path)
    s = te.summary(rec)
    assert s["id"] == rec.id and s["complete"] is True and s["captured_by"] == "operator"
    dumped = json.dumps(s)
    assert "diff_gz" not in dumped and "fromCharCode" not in dumped
    assert s["force_pushes"][0]["before"] == PRIOR
    assert s["signatures"] == ["obfuscated-self-decoder"]


def test_log_export_appends_a_ledger_run(tmp_path):
    store, rec, _ = _captured(tmp_path)
    te.log_export(store, rec, via="app", operator="Alex Example")
    [run] = [r for r in store.runs() if r.phase == "threat-evidence:export"]
    assert run.raw["capture_id"] == rec.id and run.raw["operator"] == "Alex Example"
    assert run.raw["via"] == "app"


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------
def _seed_flagged(store: Store, verdict: str = "malicious") -> None:
    store.save_pr({"pr": 11987, "meta": {"title": "t", "author": "mallory", "state": "open",
                   "draft": False, "head_sha": HEAD, "checked_at": "2026-10-02T00:00:00+00:00"}})
    store.edit_pr(11987).set_threat({"verdict": verdict, "signatures": ["obfuscated-self-decoder"],
                                     "detail": {}})


def test_cli_capture_refuses_a_pr_not_flagged(tmp_path, monkeypatch, capsys):
    import pytest
    store = Store(tmp_path)
    _seed_flagged(store, "clear")
    monkeypatch.setattr(te, "LiveGitHub", lambda: pytest.fail("no GitHub read"))
    assert te.main(["capture", "--pr", "11987", "--store", str(tmp_path)]) == 1
    assert "not flagged malicious" in capsys.readouterr().out


def test_cli_capture_one_flagged_pr(tmp_path, monkeypatch):
    store = Store(tmp_path)
    _seed_flagged(store)
    monkeypatch.setattr(te, "LiveGitHub", FakeGitHub)
    assert te.main(["capture", "--pr", "11987", "--store", str(tmp_path)]) == 0
    [rec] = store.threat_evidence(pr=11987)
    assert rec.head_sha == HEAD and rec.data["detection"]["signatures"] == ["obfuscated-self-decoder"]


def test_cli_backfill_captures_registry_incidents_at_their_head(tmp_path, monkeypatch):
    from pipeline import threats
    store = Store(tmp_path)
    reg = store.load_threats()
    threats.record_incident(reg, 11987, "mallory", HEAD, ["obfuscated-self-decoder"],
                            noticed="2026-10-02")
    threats.record_incident(reg, 5, "mallory", None, ["blocked-actor"], noticed="2026-10-02")
    store.save_threats(reg)
    monkeypatch.setattr(te, "LiveGitHub", FakeGitHub)
    assert te.main(["capture", "--backfill", "--store", str(tmp_path)]) == 0
    assert [r.head_sha for r in store.threat_evidence(pr=11987)] == [HEAD]
    assert store.threat_evidence(pr=5) == []


def test_cli_list_export_and_verify(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / "s")
    te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path)
    monkeypatch.setattr(te.gh, "operator_login", lambda **k: "tester")
    assert te.main(["list", "--store", str(tmp_path / "s")]) == 0
    assert "#11987" in capsys.readouterr().out
    out = tmp_path / "out"
    assert te.main(["export", "--pr", "11987", "--out", str(out),
                    "--store", str(tmp_path / "s")]) == 0
    assert (out / "SHA256SUMS").exists()
    [run] = [r for r in store.runs() if r.phase == "threat-evidence:export"]
    assert run.raw["via"] == "cli" and run.raw["operator"] == "tester"
    capsys.readouterr()
    assert te.main(["verify", "--store", str(tmp_path / "s")]) == 0
    assert "1 ok, 0 mismatched" in capsys.readouterr().out


def test_cli_export_with_no_capture_fails(tmp_path, capsys):
    Store(tmp_path)
    assert te.main(["export", "--pr", "1", "--out", str(tmp_path / "o"),
                    "--store", str(tmp_path)]) == 1
    assert "no evidence" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Review fixes
# ---------------------------------------------------------------------------
def test_cli_capture_takes_the_flagged_head_when_the_stored_head_moved(tmp_path, monkeypatch):
    store = Store(tmp_path)
    _seed_flagged(store)
    rec = store.load_pr(11987)
    assert rec is not None
    raw = rec.raw
    raw["meta"]["head_sha"] = "a" * 40      # INGEST recorded a newer head since the flag
    store.save_pr(raw)
    monkeypatch.setattr(te, "LiveGitHub", FakeGitHub)
    assert te.main(["capture", "--pr", "11987", "--store", str(tmp_path)]) == 0
    assert [r.head_sha for r in store.threat_evidence(pr=11987)] == [HEAD]


def test_uncaptured_names_flagged_heads_without_a_complete_capture(tmp_path):
    store = Store(tmp_path)
    _seed_flagged(store)
    store.save_pr({"pr": 2, "meta": {"title": "t", "author": "a", "state": "open",
                                     "draft": False, "head_sha": "c" * 40,
                                     "checked_at": "2026-10-02T00:00:00+00:00"}})
    prs = store.all_prs()
    assert [(f.pr, f.head_sha) for f in te.uncaptured(store, prs)] == [(11987, HEAD)]
    te.capture(store, te.uncaptured(store, prs)[0], github=FakeGitHub(), diffs_dir=tmp_path)
    assert te.uncaptured(store, prs) == []
    assert store.threat_evidence_heads() == {(11987, HEAD)}


def _listing(monkeypatch, files: list[dict]) -> None:
    from pipeline import gh
    monkeypatch.setattr(gh, "pr_files", lambda n, **k: files)


def test_listing_with_a_truncated_patch_is_not_whole(monkeypatch):
    _listing(monkeypatch, [{"filename": "a.ts", "status": "modified", "additions": 5,
                            "deletions": 0, "patch": "@@ -0,0 +1,1 @@\n+one"}])
    listing = LiveGitHub().listing_diff(1)
    assert listing is not None and listing[1] is False


def test_listing_at_githubs_file_cap_is_not_whole(monkeypatch):
    from pipeline import diff_cache
    _listing(monkeypatch, [{"filename": f"f{i}.ts", "status": "added", "additions": 1,
                            "deletions": 0, "patch": "@@ -0,0 +1,1 @@\n+x"}
                           for i in range(diff_cache.LISTING_MAX_FILES)])
    listing = LiveGitHub().listing_diff(1)
    assert listing is not None and listing[1] is False


def test_whole_listing_is_whole(monkeypatch):
    _listing(monkeypatch, [{"filename": "a.ts", "status": "modified", "additions": 1,
                            "deletions": 0, "patch": "@@ -0,0 +1,1 @@\n+one"}])
    listing = LiveGitHub().listing_diff(1)
    assert listing is not None and listing[1] is True and b"+one" in listing[0]


def test_listing_is_refused_when_the_head_moves_during_the_read(tmp_path):
    store = Store(tmp_path)
    gh = FakeGitHub(compare_ok=False, listing=(b"diff --git a/z b/z\n+x\n", True))
    heads = iter([HEAD, "f" * 40])
    real_pull = gh.pull
    gh.pull = lambda n: {**(real_pull(n) or {}), "head": {"sha": next(heads)}}  # type: ignore[method-assign]
    assert te.capture(store, FLAG, github=gh, diffs_dir=tmp_path) == "partial"
    rec, _ = _only(store)
    assert rec.data["artifacts"]["diff"] is None
    assert any("moved" in e for e in rec.data["errors"])


def test_prior_is_never_a_head_cached_after_the_flag_when_the_flagged_head_is_uncached(tmp_path):
    store = Store(tmp_path)
    flag = te.Flag(pr=FLAG.pr, head_sha=HEAD, author="mallory", signatures=[],
                   scanned_at="2026-10-02T16:03:50+00:00")
    _cache_heads(store, ("f" * 40, "2026-10-03T00:00:00+00:00", HONEST))   # after the flag
    te.capture(store, flag, github=FakeGitHub(pushes=False), diffs_dir=tmp_path)
    rec, _ = _only(store)
    assert rec.data["artifacts"]["prior"] is None


def test_prior_before_the_flag_is_found_when_the_flagged_head_is_uncached(tmp_path):
    store = Store(tmp_path)
    flag = te.Flag(pr=FLAG.pr, head_sha=HEAD, author="mallory", signatures=[],
                   scanned_at="2026-10-02T16:03:50+00:00")
    _cache_heads(store, (PRIOR, "2026-08-27T20:21:54+00:00", HONEST))
    te.capture(store, flag, github=FakeGitHub(pushes=False), diffs_dir=tmp_path)
    rec, _ = _only(store)
    assert rec.data["artifacts"]["prior"]["before_sha"] == PRIOR


def test_flagged_diff_falls_back_to_the_shared_diff_cache(tmp_path):
    store = Store(tmp_path)
    _cache_heads(store, (HEAD, "2026-10-02T16:11:35+00:00", "diff --git a/c b/c\n# omitted\n"))
    gh = FakeGitHub(head_now="f" * 40, compare_ok=False, pushes=False)
    assert te.capture(store, FLAG, github=gh, diffs_dir=tmp_path / "empty") == "partial"
    rec, blobs = _only(store)
    assert rec.data["artifacts"]["diff"]["source"] == "diff-cache"
    assert _gunzip(blobs.diff_gz) == b"diff --git a/c b/c\n# omitted\n"


def test_force_push_changes_ignores_a_header_hidden_after_a_form_feed():
    gone = ("diff --git a/src/b.ts b/src/b.ts\nindex 5..6 100644\n--- a/src/b.ts\n"
            "+++ b/src/b.ts\n@@ -1 +1 @@\n-b\n+bb\n")
    smuggled = INJECTED.replace(" " * 300, "\x0cdiff --git a/src/b.ts b/src/b.ts\x0c")
    changed, removed = te.force_push_changes((HONEST + smuggled).encode(),
                                             (HONEST + gone).encode())
    assert changed == smuggled.encode()
    assert removed == ["src/b.ts"]      # the push dropped b.ts; the hidden header is no file


def test_readme_renders_attacker_text_inert(tmp_path):
    store = Store(tmp_path)
    gh = FakeGitHub()
    real_pull = gh.pull
    title = "fix ![x](https://attacker.example/x.png) <img src=//a.example/y> `tick`"
    gh.pull = lambda n: {**(real_pull(n) or {}), "title": title}  # type: ignore[method-assign]
    te.capture(store, FLAG, github=gh, diffs_dir=tmp_path)
    rec, blobs = _only(store)
    readme = dict(te.bundle_files(rec, blobs))["README.md"].decode()
    line = next(ln for ln in readme.splitlines() if ln.startswith("- Title:"))
    assert line == "- Title: `` " + title + " ``"
    import re
    outside_code = re.sub(r"(`+).*?\1", "", readme)
    assert "attacker.example" not in outside_code and "<img" not in outside_code
