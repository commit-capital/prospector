"""threat_evidence.capture: an immutable, payload-free record of a flagged head,
with its diffs gzip-compressed and hashed, from read-only GitHub reads."""
from __future__ import annotations

import gzip
import hashlib
import json

from pipeline import threat_evidence as te
from pipeline.store import Store
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
                "author": {"name": "Paperclip", "email": "noreply@x", "date": "2026-08-22T22:26:46Z"},
                "committer": {"name": "Paperclip", "email": "noreply@x", "date": "2026-08-22T22:26:46Z"},
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
