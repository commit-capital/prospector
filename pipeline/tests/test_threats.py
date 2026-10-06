"""threats.py — the ONE threat-detection policy: signatures, actor blocklist,
the scan driver, and the gate's fail-closed consumption of a malicious flag."""
import random
import re
import signal

import pytest

from pipeline import actions
from pipeline import diff_cache
from pipeline import gates
from pipeline import notify
from pipeline import storekit
from pipeline import threats
from pipeline import threat_scan
from pipeline.model import Pr
from pipeline.store import Store
from pipeline.testsupport import greptile_entry, reviews_section


# A trimmed-down version of the real payload seen in the tarun-khatri PRs:
# the createRequire smuggle + the obfuscated self-decoder appended to a build
# config, on top of the legitimate esbuild export.
PAYLOAD_DIFF = """\
diff --git a/cli/esbuild.config.mjs b/cli/esbuild.config.mjs
--- a/cli/esbuild.config.mjs
+++ b/cli/esbuild.config.mjs
@@ -1,5 +1,8 @@
+import { createRequire } from 'module';
+const require = createRequire(import.meta.url);
 export default {
   entryPoints: ["src/index.ts"],
-};
+};                       global['!']='9-0008-2';var _$_1e42=(function(l,e){var x=String.fromCharCode(127);})("rmcej%otb%",2857687);global[_$_1e42[0]]= require;
"""

CLEAN_DIFF = """\
diff --git a/cli/esbuild.config.mjs b/cli/esbuild.config.mjs
--- a/cli/esbuild.config.mjs
+++ b/cli/esbuild.config.mjs
@@ -1,4 +1,4 @@
 export default {
-  target: "node18",
+  target: "node20",
 };
"""

# Every character str.splitlines breaks a line on besides "\n".
LINE_BREAKS = ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]


class TestScanDiff:
    def test_obfuscated_payload_is_malicious(self):
        r = threats.scan_diff(PAYLOAD_DIFF)
        assert r["verdict"] == "malicious"
        assert "obfuscated-self-decoder" in r["signatures"]
        assert "capability-smuggle" in r["signatures"]
        assert "build-config-require-injection" in r["signatures"]

    def test_clean_diff_is_clear(self):
        assert threats.scan_diff(CLEAN_DIFF)["verdict"] == "clear"

    def test_createRequire_outside_build_config_not_flagged(self):
        diff = ("--- a/src/util.ts\n+++ b/src/util.ts\n"
                "@@ -1 +1,2 @@\n+import { createRequire } from 'module';\n")
        # createRequire in a normal source file is legitimate — not a build config
        assert threats.scan_diff(diff)["verdict"] == "clear"

    def test_named_createRequire_in_build_config_not_flagged(self):
        # a descriptively-named binding resolving an in-repo package is ordinary
        # monorepo build tooling — the smuggle shape rebinds `require` itself
        diff = ("--- a/cli/esbuild.config.mjs\n+++ b/cli/esbuild.config.mjs\n"
                "@@ -1 +1,2 @@\n"
                '+const requireFromDb = createRequire(resolve(repoRoot, "packages/db/package.json"));\n')
        r = threats.scan_diff(diff)
        assert "build-config-require-injection" not in r["signatures"]
        assert r["verdict"] == "clear"

    def test_require_shadow_in_build_config_flagged(self):
        diff = ("--- a/cli/esbuild.config.mjs\n+++ b/cli/esbuild.config.mjs\n"
                "@@ -1 +1,2 @@\n"
                "+const require = createRequire(import.meta.url);\n")
        r = threats.scan_diff(diff)
        assert "build-config-require-injection" in r["signatures"]
        assert r["verdict"] == "malicious"

    def test_inline_createRequire_invoke_in_build_config_flagged(self):
        # capability used without ever naming a binding — same RCE surface
        diff = ("--- a/rollup.config.js\n+++ b/rollup.config.js\n"
                "@@ -1 +1,2 @@\n"
                "+const fs = createRequire(import.meta.url)('node:fs');\n")
        r = threats.scan_diff(diff)
        assert "build-config-require-injection" in r["signatures"]

    def test_eol_churn_alone_is_suspicious_not_malicious(self):
        r = threats.scan_diff(CLEAN_DIFF, additions=20000, deletions=20010)
        assert r["verdict"] == "suspicious"
        assert r["signatures"] == ["eol-churn-camouflage"]

    def test_payload_with_churn_still_malicious(self):
        r = threats.scan_diff(PAYLOAD_DIFF, additions=20000, deletions=20010)
        assert r["verdict"] == "malicious"

    def test_fromcharcode_127_marker(self):
        diff = ("--- a/x.js\n+++ b/x.js\n@@ -1 +1 @@\n"
                "+var x=String.fromCharCode(127);\n")
        assert threats.scan_diff(diff)["verdict"] == "malicious"

    def test_eol_churn_derived_from_diff_when_diffstat_absent(self):
        # No additions/deletions passed (sig.diffstat absent at threat-scan time):
        # scan_diff must count them from the diff text itself and still fire.
        churn = "diff --git a/f.txt b/f.txt\n"
        churn += "".join(f"+line {i}\n-line {i}\n" for i in range(3100))
        r = threats.scan_diff(churn)
        assert "eol-churn-camouflage" in r["signatures"]

    def test_small_diff_without_diffstat_does_not_fire_churn(self):
        small = "diff --git a/f.txt b/f.txt\n+one added line\n-one removed line\n"
        r = threats.scan_diff(small)
        assert "eol-churn-camouflage" not in r["signatures"]

    def test_passed_diffstat_still_overrides_derived_counts(self):
        # A tiny diff but explicit large balanced diffstat → still fires (override wins).
        small = "diff --git a/f.txt b/f.txt\n+x\n-y\n"
        r = threats.scan_diff(small, additions=20000, deletions=20010)
        assert "eol-churn-camouflage" in r["signatures"]

    def test_added_line_shaped_like_a_file_header_is_scanned(self):
        # the added content "++ i; global['!']=…" makes the diff line "+++ i; …"
        diff = ("diff --git a/src/a.js b/src/a.js\n--- a/src/a.js\n+++ b/src/a.js\n"
                "@@ -1 +1,2 @@\n x\n+++ i; global['!']='9-0008-2';\n")
        assert threats.scan_diff(diff)["verdict"] == "malicious"

    @pytest.mark.parametrize("sep", LINE_BREAKS)
    def test_payload_after_a_line_break_character_is_scanned(self, sep):
        # a form feed is whitespace in JavaScript and U+2028 a line terminator,
        # so "void main();<sep>global['!']=…" runs the payload
        diff = ("diff --git a/src/x.js b/src/x.js\n--- a/src/x.js\n+++ b/src/x.js\n@@ -1 +1 @@\n"
                f"-void main();\n+void main();{sep}global['!']='9-0008-2';var _$_1e42=1;\n")
        assert threats.scan_diff(diff)["verdict"] == "malicious"

    @pytest.mark.parametrize("sep", LINE_BREAKS)
    def test_line_break_character_does_not_count_a_second_line(self, sep):
        churn = "diff --git a/f.txt b/f.txt\n"
        churn += "".join(f"+line {i}{sep}+tail\n-line {i}\n" for i in range(3100))
        assert "eol-churn-camouflage" in threats.scan_diff(churn)["signatures"]

    @pytest.mark.parametrize("sep", LINE_BREAKS)
    def test_code_after_a_line_break_character_is_suspicious(self, sep):
        diff = ("diff --git a/src/a.js b/src/a.js\n--- a/src/a.js\n+++ b/src/a.js\n"
                f"@@ -1 +1,2 @@\n x\n+// note{sep}console.log(1);\n")
        r = threats.scan_diff(diff)
        assert r["verdict"] == "suspicious"
        assert r["signatures"] == ["embedded-line-break"]

    def test_crlf_line_ending_is_not_an_embedded_line_break(self):
        diff = ("diff --git a/src/a.js b/src/a.js\n--- a/src/a.js\n+++ b/src/a.js\n"
                "@@ -1 +1,2 @@\n x\r\n+console.log(1);\r\n")
        assert threats.scan_diff(diff)["verdict"] == "clear"

    def test_form_feed_page_break_line_is_not_an_embedded_line_break(self):
        diff = ("diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
                "@@ -1 +1,3 @@\n x\n+\x0c\n+def f(): pass\n")
        assert threats.scan_diff(diff)["verdict"] == "clear"

    @pytest.mark.parametrize("sep", LINE_BREAKS)
    def test_evidence_shows_a_line_break_character_escaped(self, sep):
        diff = ("diff --git a/src/x.js b/src/x.js\n--- a/src/x.js\n+++ b/src/x.js\n@@ -1 +1 @@\n"
                f"-void main();\n+void main();{sep}global['!']='9-0008-2';\n")
        detail = threats.scan_diff(diff)["detail"]
        assert set(detail) == {"obfuscated-self-decoder", "embedded-line-break"}
        for evidence in detail.values():
            assert sep not in evidence
            assert f"\\u{ord(sep):04x}" in evidence

    def test_file_is_tracked_from_the_git_header_alone(self):
        # a diff synthesized from GitHub's per-file listing has no ---/+++ lines
        diff = ("diff --git a/cli/esbuild.config.mjs b/cli/esbuild.config.mjs\n"
                "# modified: +1 -0\n@@ -1 +1,2 @@\n x\n"
                "+const require = createRequire(import.meta.url);")
        assert "build-config-require-injection" in threats.scan_diff(diff)["signatures"]

    def test_unread_files_read_suspicious(self):
        r = threats.scan_diff(CLEAN_DIFF, unread=["dist/huge.js"])
        assert r["verdict"] == "suspicious"
        assert r["signatures"] == ["unscannable-diff"]
        assert "dist/huge.js" in r["detail"]["unscannable-diff"]

    def test_unread_files_do_not_mask_a_malicious_hit(self):
        r = threats.scan_diff(PAYLOAD_DIFF, unread=["dist/huge.js"])
        assert r["verdict"] == "malicious"
        assert "unscannable-diff" in r["signatures"]


# Each shape threats.py reads with a linear scanner, stated as the one regex it
# matches exactly. These regexes backtrack quadratically, so they are only ever
# fed short lines.
SMUGGLE_REGEXES = [re.compile(r"global\s*\[.+\]\s*=\s*require\b"),
                   re.compile(r"global\s*\[.+\]\s*=\s*module\b")]
INLINE_CREATE_REQUIRE_REGEX = re.compile(r"createRequire\s*\([^)]*\)\s*\(")
SECRET_ASSIGN_REGEX = re.compile(
    r"""(?ix)
    \b[A-Za-z0-9_]*(?:SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|
                      AUTH[_-]?TOKEN|ACCESS[_-]?TOKEN|PRIVATE[_-]?KEY|
                      [A-Z0-9]+_KEY|[A-Z0-9]+_TOKEN)\b
    \s*[:=]\s*
    ['"]?(?P<val>[A-Za-z0-9+/=_\-]{24,})['"]?\s*[,;]?\s*$
    """,
)


def _added_file(path: str, lines: list[str]) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
            f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}")


def _lines(slots: list[list[str]], count: int, seed: int) -> list[str]:
    """Random lines built slot by slot, each slot giving up to two of its tokens."""
    rng = random.Random(seed)
    return ["".join(t for slot in slots for t in rng.choices(slot, k=rng.choice((0, 1, 1, 1, 2))))
            for _ in range(count)]


def _scan_within_a_second(diff: str) -> None:
    """scan_diff over `diff`, failed at the one-second mark rather than left to run."""
    def overrun(signum: int, frame: object) -> None:
        raise AssertionError("scan_diff ran past a second")

    previous = signal.signal(signal.SIGALRM, overrun)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        threats.scan_diff(diff)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class TestLinearScan:
    """A diff costs time linear in its size to scan, so no added line or file
    path can stall the threat scan."""

    @pytest.mark.parametrize("line", [
        "global[" * 150_000,
        "createRequire(" * 75_000,
        "a" * 1_000_000,
        "a_key=" * 170_000 + "!",
        "-----BEGIN PRIVATE KEY-----Comment:" * 30_000,
        "-----BEGIN " + "A " * 500_000,
    ], ids=["global-index", "createRequire-call", "secret-name", "secret-value", "key-armor",
            "key-label"])
    def test_a_megabyte_line_scans_within_a_second(self, line: str) -> None:
        # a build config that is not secret-exempt, so every line matcher reads it
        _scan_within_a_second(_added_file("cli/esbuild.config.mjs", [line]))

    def test_a_deep_path_adding_many_lines_scans_within_a_second(self) -> None:
        _scan_within_a_second(_added_file("a/" * 2000 + "x.js", ["x"] * 20_000))

    def test_capability_smuggle_matches_its_regex(self) -> None:
        slots = [["x", " ", "global", "("], ["global", "global ", "globalThis"],
                 ["[", " [", "\t["], ["k", "'!'", "_$_1e42[0]", "]", "["], ["]", "] ", "]\t"],
                 ["=", " = ", "==", "=>"], ["require", "module", "requires", " module", "require_"],
                 [";", "x", "(", "]", "global["]]
        for line in _lines(slots, 4000, seed=1):
            expected = any(p.search(line) for p in SMUGGLE_REGEXES)
            assert threats._capability_smuggle(line) == expected, repr(line)

    def test_inline_create_require_matches_its_regex(self) -> None:
        slots = [["x", " ", "createRequire", ")"], ["createRequire", "createRequire ", "createrequire"],
                 ["(", "\t(", ")"], ["import.meta.url", "x", "(", "createRequire("],
                 [")", ") ", "),"], ["(", " (", "\t(", "x"], ["'node:fs'", ")", "x(", ";"]]
        for line in _lines(slots, 4000, seed=2):
            expected = bool(INLINE_CREATE_REQUIRE_REGEX.search(line))
            assert threats._inline_create_require(line) == expected, repr(line)

    def test_secret_value_matches_its_regex(self) -> None:
        slots = [["x = ", "const ", " ", "a_key=", "é", "!", "'", "-"],
                 ["API_KEY", "a_token", "Secret", "api-key", "PRIVATE-KEY", "passwd", "x_Key",
                  "_KEY", "TOKEN", "queryKey", "KEY", "B_KEY=", "AuthToken"],
                 ["=", ":", " = ", ": '", '="', "==", " :", "-", "\t"],
                 ["0f9e8d7c6b5a4321", "Zq9/+", "B_KEY=", "=", "-", "x", "a_token:"],
                 ["0f9e8d7c6b5a4321", "AbC123xyz/+_-=", "zz"],
                 ["", "'", '",', ";", " ", "!", ", x", " ;", "é"]]
        for line in _lines(slots, 4000, seed=3):
            m = SECRET_ASSIGN_REGEX.search(line)
            assert threats._secret_value(line) == (m and m.group("val")), repr(line)


class TestRegistry:
    def test_block_and_query_actor(self):
        reg = threats.empty_registry()
        threats.block_actor(reg, "mallory", "bad", added="2026-06-12", incidents=[1])
        assert threats.is_blocked_actor(reg, "mallory")
        assert not threats.is_blocked_actor(reg, "alice")
        assert not threats.is_blocked_actor(reg, None)

    def test_block_actor_merges_incidents(self):
        reg = threats.empty_registry()
        threats.block_actor(reg, "mallory", "bad", added="2026-06-12", incidents=[1])
        threats.block_actor(reg, "mallory", "worse", added="2026-06-13", incidents=[2, 1])
        assert reg["actors"]["mallory"]["incidents"] == [1, 2]
        assert reg["actors"]["mallory"]["added"] == "2026-06-12"  # first-seen preserved
        assert reg["actors"]["mallory"]["reason"] == "worse"      # reason refreshed

    def test_record_incident_keeps_the_first_noticed_date(self):
        reg = threats.empty_registry()
        threats.record_incident(reg, 5174, "mallory", "sha", ["x"], noticed="2026-06-12")
        threats.record_incident(reg, 5174, "mallory", "sha", ["x"], noticed="2026-06-13")
        assert reg["incidents"][0]["noticed"] == "2026-06-12"

    def test_record_incident_idempotent(self):
        reg = threats.empty_registry()
        threats.record_incident(reg, 5174, "mallory", "sha", ["x"], noticed="2026-06-12")
        threats.record_incident(reg, 5174, "mallory", "sha2", ["y"], noticed="2026-06-13")
        assert len(reg["incidents"]) == 1
        assert reg["incidents"][0]["head_sha"] == "sha2"


class TestGateConsumesThreat:
    def _pr(self, **over) -> Pr:
        rec = {
            "pr": 1,
            "meta": {"title": "t", "author": "a", "state": "open", "draft": False,
                     "head_sha": "abc", "checked_at": "2026-06-10T00:00:00+00:00"},
            "signals": {"ci": "passing", "mergeable": True,
                        "checked_at": "2026-06-10T00:00:00+00:00", "against_head_sha": "abc"},
            "reviews": reviews_section("abc", "2026-06-10T00:00:00+00:00"),
            "drift": {"state": "applicable", "checked_at": "2026-06-10T00:00:00+00:00",
                      "against_head_sha": "abc"},
        }
        rec.update(over)
        return Pr(None, rec)

    def test_malicious_flag_blocks_otherwise_clean_pr(self):
        rec = self._pr(threat={"verdict": "malicious", "signatures": ["obfuscated-self-decoder"],
                               "checked_at": "2026-06-10T00:00:00+00:00", "against_head_sha": "abc"})
        ok, reasons = gates.pr_clean(rec, today="2026-06-10")
        assert not ok and any("malicious" in r for r in reasons)

    def test_stale_malicious_flag_still_blocks(self):
        # head moved — flag is stale, but a malicious verdict must fail closed
        rec = self._pr(threat={"verdict": "malicious", "signatures": ["capability-smuggle"],
                               "checked_at": "2026-06-10T00:00:00+00:00", "against_head_sha": "OLD"})
        ok, reasons = gates.pr_clean(rec, today="2026-06-10")
        assert not ok and any("malicious" in r for r in reasons)

    def test_clear_flag_does_not_block(self):
        rec = self._pr(threat={"verdict": "clear", "signatures": [],
                               "checked_at": "2026-06-10T00:00:00+00:00", "against_head_sha": "abc"})
        ok, reasons = gates.pr_clean(rec, today="2026-06-10")
        assert ok and reasons == []

    def test_suspicious_flag_does_not_block(self):
        # MEDIUM-only signals surface for a human but are not a hard gate fail
        rec = self._pr(threat={"verdict": "suspicious", "signatures": ["eol-churn-camouflage"],
                               "checked_at": "2026-06-10T00:00:00+00:00", "against_head_sha": "abc"})
        ok, reasons = gates.pr_clean(rec, today="2026-06-10")
        assert ok and reasons == []


class TestScanDriver:
    def _seed(self, store, n, author, head, diff_text, diffs_dir):
        (diffs_dir / f"{head}.diff").write_text(diff_text)
        store.save_pr({
            "pr": n,
            "meta": {"title": "t", "author": author, "state": "open", "draft": False,
                     "head_sha": head, "checked_at": "2026-06-10T00:00:00+00:00"},
            "signals": {"ci": "passing", "mergeable": True,
                        "diffstat": {"additions": 10, "deletions": 5},
                        "checked_at": "2026-06-10T00:00:00+00:00", "against_head_sha": head},
        })

    def test_scan_record_flags_payload(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 5174, "mallory", "sha1", PAYLOAD_DIFF, diffs)
        rec = store.load_pr(5174)
        result = threat_scan.scan_record(rec, store.load_threats(), diffs_dir=diffs)
        assert result["verdict"] == "malicious"

    def test_scan_record_clean(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "alice", "sha2", CLEAN_DIFF, diffs)
        rec = store.load_pr(1)
        assert threat_scan.scan_record(rec, store.load_threats(), diffs_dir=diffs)["verdict"] == "clear"

    def test_blocked_actor_flagged_even_with_clean_diff(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 2, "mallory", "sha3", CLEAN_DIFF, diffs)
        reg = threats.empty_registry()
        threats.block_actor(reg, "mallory", "prior attack", added="2026-06-12")
        rec = store.load_pr(2)
        result = threat_scan.scan_record(rec, reg, diffs_dir=diffs)
        assert result["verdict"] == "malicious"
        assert "blocked-actor" in result["signatures"]

    def test_a_malicious_find_posts_its_slack_alert(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 5174, "mallory", "sha1", PAYLOAD_DIFF, diffs)
        monkeypatch.setenv("TRIAGE_SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
        posted: list[str] = []
        monkeypatch.setattr(notify, "post_webhook", lambda url, text: posted.append(text) or True)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])
        assert len(posted) == 1 and "#5174" in posted[0]

    def test_a_broken_notifier_never_fails_the_scan(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 5174, "mallory", "sha1", PAYLOAD_DIFF, diffs)

        def broken(*a, **k):
            raise RuntimeError("slack down")
        monkeypatch.setattr(notify, "send_due", broken)
        assert threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs),
                                 "--no-fetch"]) == 0
        assert store.load_pr(5174).section("threat")["verdict"] == "malicious"

    def test_stamp_preserves_a_concurrent_write(self, tmp_path, monkeypatch):
        """A section another phase writes while the scan is mid-run survives the
        threat stamp: the stamp lands on a fresh read of the record, not on the
        copy the scan loaded at startup."""
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 7, "alice", "sha7", CLEAN_DIFF, diffs)

        real_scan = threat_scan.scan_record

        def scan_after_concurrent_write(rec, registry, diffs_dir, diff=None):
            # another phase places the PR standalone after the scan's snapshot load
            Store(tmp_path).edit_pr(7).mark_standalone()
            return real_scan(rec, registry, diffs_dir=diffs_dir, diff=diff)

        monkeypatch.setattr(threat_scan, "scan_record", scan_after_concurrent_write)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])

        after = store.load_pr(7)
        assert after.section("threat") is not None                 # the stamp landed
        assert (after.section("cluster") or {}).get("ids") == []   # the concurrent write survived


    def test_no_fetch_scan_never_captures(self, tmp_path, monkeypatch):
        import pytest

        from pipeline import threat_evidence
        monkeypatch.setattr(threat_evidence, "capture", lambda *a, **k: pytest.fail("captured"))
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 5174, "mallory", "sha1", PAYLOAD_DIFF, diffs)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])
        rec = store.load_pr(5174)
        assert rec is not None and rec.threat_verdict == "malicious"

    def test_scan_captures_after_stamping_and_survives_a_capture_crash(self, tmp_path, monkeypatch):
        from pipeline import threat_evidence
        seen: list[tuple[int, str, str | None]] = []

        def fake_capture(store, flag, *, github=None, diffs_dir=None):
            rec = store.load_pr(flag.pr)
            seen.append((flag.pr, flag.head_sha, rec.threat_verdict))
            raise RuntimeError("network down")

        monkeypatch.setattr(threat_evidence, "capture", fake_capture)
        monkeypatch.setattr(threat_scan, "fetch_missing_diffs", lambda *a, **k: ({}, set()))
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 5174, "mallory", "sha1", PAYLOAD_DIFF, diffs)
        self._seed(store, 6, "alice", "sha6", CLEAN_DIFF, diffs)
        assert threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)]) == 0
        assert seen == [(5174, "sha1", "malicious")]            # stamped before capture
        assert "mallory" in store.load_threats()["actors"]       # registry saved
        run = store.latest_run("threat-scan")
        assert run is not None
        assert run.raw["stats"]["evidence_failed"] == 1
        assert run.raw["stats"]["evidence_captured"] == 0


class TestTrustedAuthorExemption:
    """A repository maintainer (profile trusted_authors) is never threat-flagged:
    attack signatures and the actor blocklist do not apply to their PRs. A leaked
    credential still surfaces as a rotate-secret action item — the key is leaked
    regardless of who pushed it — but never as a suspicious verdict."""

    def _trust(self, monkeypatch, *authors: str) -> None:
        from pipeline import profile
        monkeypatch.setattr(profile, "active",
                            lambda: profile.RepoProfile(trusted_authors=authors))

    def _seed(self, store, n, author, head, diff_text, diffs_dir):
        (diffs_dir / f"{head}.diff").write_text(diff_text)
        store.save_pr({
            "pr": n,
            "meta": {"title": "t", "author": author, "state": "open", "draft": False,
                     "head_sha": head, "checked_at": "2026-07-29T00:00:00+00:00"},
        })

    def test_attack_signatures_never_flag_a_maintainer(self, tmp_path, monkeypatch):
        self._trust(monkeypatch, "mira")
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "mira", "sha1", PAYLOAD_DIFF, diffs)

        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])

        t = store.load_pr(1).section("threat")
        assert t is not None and t["verdict"] == "clear"
        assert t["signatures"] == []
        reg = store.load_threats()
        assert "mira" not in reg.get("actors", {})               # never auto-blocked
        assert reg.get("incidents", []) == []                    # no incident logged

    def test_untrusted_author_still_flagged(self, tmp_path, monkeypatch):
        self._trust(monkeypatch, "mira")
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 2, "mallory", "sha2", PAYLOAD_DIFF, diffs)

        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])

        assert store.load_pr(2).section("threat")["verdict"] == "malicious"
        assert "mallory" in store.load_threats()["actors"]

    def test_maintainer_secret_leak_surfaces_without_flagging(self, tmp_path, monkeypatch):
        self._trust(monkeypatch, "mira")
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 3, "mira", "sha3", LEAKED_KEY_DIFF, diffs)

        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])

        t = store.load_pr(3).section("threat")
        assert t["verdict"] == "clear"                           # not suspicious
        assert t["signatures"] == ["secret-leak"]
        items = {i["id"] for i in store.load_action_items()["items"]}
        assert "rotate-secret:3" in items                        # the key still rotates

    def test_blocklist_entry_does_not_flag_a_maintainer(self, tmp_path, monkeypatch):
        # defensive: a stale registry block on a maintainer never yields a verdict
        self._trust(monkeypatch, "mira")
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 4, "mira", "sha4", CLEAN_DIFF, diffs)
        reg = store.load_threats()
        threats.block_actor(reg, "mira", "stale entry", added="2026-07-28")
        store.save_threats(reg)

        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])

        assert store.load_pr(4).section("threat")["verdict"] == "clear"


# ---------------------------------------------------------------------------
# secret-leak signature: a hardcoded live-looking credential in an added line.
# MEDIUM/suspicious (the author fat-fingered a secret; the PR isn't malicious),
# but operationally urgent — drives a rotate-secret action item.
# ---------------------------------------------------------------------------
LEAKED_KEY_DIFF = """\
diff --git a/docker-compose.override.yml b/docker-compose.override.yml
--- a/docker-compose.override.yml
+++ b/docker-compose.override.yml
@@ -1,2 +1,4 @@
 services:
+  app:
+    environment:
+      OBSIDIAN_API_KEY: 1c688774012cfdd1440c21bab81c636c9b2358506031c5999d09a8b93af5f00f
"""

PLACEHOLDER_DIFF = """\
diff --git a/.env.example b/.env.example
--- a/.env.example
+++ b/.env.example
@@ -1,1 +1,3 @@
+OBSIDIAN_API_KEY=your-key-here
+OPENAI_API_KEY=
+SECRET_KEY=${SECRET_KEY}
"""

AWS_KEY_DIFF = """\
diff --git a/config.ts b/config.ts
--- a/config.ts
+++ b/config.ts
@@ -1,1 +1,2 @@
+const awsKey = "AKIAIOSFODNN7EXAMPLE";
"""


def test_secret_leak_fires_on_live_looking_key():
    r = threats.scan_diff(LEAKED_KEY_DIFF)
    assert "secret-leak" in r["signatures"]
    assert r["verdict"] == "suspicious"


def test_secret_leak_ignores_placeholders_and_empty_values():
    r = threats.scan_diff(PLACEHOLDER_DIFF)
    assert "secret-leak" not in r["signatures"]
    assert r["verdict"] == "clear"


def test_secret_leak_detects_aws_key_token():
    r = threats.scan_diff(AWS_KEY_DIFF)
    assert "secret-leak" in r["signatures"]


def test_secret_leak_does_not_block_the_gate():
    r = threats.scan_diff(LEAKED_KEY_DIFF)
    assert r["verdict"] != "malicious"


def test_threat_scan_emits_rotate_secret_action_item(tmp_path):
    """Running the scan over a PR with a leaked key creates a rotate-secret
    action item in the store's action-items registry (re-running = backfill)."""
    from pipeline import actions
    store = Store(tmp_path)
    diffs = tmp_path / "diffs"; diffs.mkdir()
    head = "leak1"
    (diffs / f"{head}.diff").write_text(LEAKED_KEY_DIFF)
    store.save_pr({
        "pr": 3994,
        "meta": {"title": "add override", "author": "myFinTechPL", "state": "open",
                 "draft": False, "head_sha": head, "checked_at": "2026-06-13T00:00:00+00:00"},
        "signals": {"ci": "passing", "mergeable": False,
                    "checked_at": "2026-06-13T00:00:00+00:00", "against_head_sha": head},
        "reviews": reviews_section(head, "2026-06-13T00:00:00+00:00", greptile=greptile_entry(2, head)),
    })
    threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)])
    reg = store.load_action_items()
    ids = {i["id"]: i for i in reg["items"]}
    assert "rotate-secret:3994" in ids
    it = ids["rotate-secret:3994"]
    assert it["status"] == "open" and "OBSIDIAN_API_KEY" in it["evidence"]
    # idempotent + status-preserving: mark done, re-scan, stays done
    actions.set_status(reg, "rotate-secret:3994", "done")
    store.save_action_items(reg)
    threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)])
    assert {i["id"]: i for i in store.load_action_items()["items"]}["rotate-secret:3994"]["status"] == "done"


# False-positive regression set: identifiers/function-calls/placeholders that
# the over-broad first cut flagged. None of these is a leaked credential.
FP_DIFFS = {
    "named-key-fncall": "diff --git a/x.ts b/x.ts\n--- a/x.ts\n+++ b/x.ts\n@@ -1,1 +1,2 @@\n+  executionAgentNameKey: normalizeAgentNameKey(deferredAgent.name),\n",
    "react-query-key": "diff --git a/x.tsx b/x.tsx\n--- a/x.tsx\n+++ b/x.tsx\n@@ -1,1 +1,2 @@\n+  queryKey: selectedCompanyId ? queryKeys.agents.list(selectedCompanyId) : [],\n",
    "short-jobkey": 'diff --git a/m.ts b/m.ts\n--- a/m.ts\n+++ b/m.ts\n@@ -1,1 +1,2 @@\n+  jobKey: "half-open-recovery",\n',
    "env-placeholder": "diff --git a/.env.example b/.env.example\n--- a/.env.example\n+++ b/.env.example\n@@ -1,1 +1,2 @@\n+BETTER_AUTH_SECRET=replace-with-a-strong-random-secret\n",
    "test-fixture-key": 'diff --git a/server/src/__tests__/x.test.ts b/server/src/__tests__/x.test.ts\n--- a/server/src/__tests__/x.test.ts\n+++ b/server/src/__tests__/x.test.ts\n@@ -1,1 +1,2 @@\n+  const VALID_KEY = "sk-test-valid-key-for-49xxxxxxxxxxxxxxxxxxxx";\n',
    "lockfile-hash": "diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml\n--- a/pnpm-lock.yaml\n+++ b/pnpm-lock.yaml\n@@ -1,1 +1,2 @@\n+      integrity: sha512-3a8f2bc9d0e1f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0\n",
    "git-sha-const": 'diff --git a/v.ts b/v.ts\n--- a/v.ts\n+++ b/v.ts\n@@ -1,1 +1,2 @@\n+  const commitKey = baseSha; // 7d53cb85c2db85aa11c9afa8106cc64cbabd60f3\n',
}

REAL_LEAK_DIFFS = {
    "config-hex-secret": 'diff --git a/ecosystem.config.cjs b/ecosystem.config.cjs\n--- a/ecosystem.config.cjs\n+++ b/ecosystem.config.cjs\n@@ -1,1 +1,2 @@\n+      BETTER_AUTH_SECRET: "01f5f8bf4cfb187bdfb583a7be1bf534ca4000abbc7bd5942156aa0f3e9d7c1b2",\n',
    "compose-hex-key": LEAKED_KEY_DIFF,
}


# ---------------------------------------------------------------------------
# The scan's fetch-missing step: uncached open-PR diffs are fetched (read-only
# gh) before scanning, so coverage never depends on a prior CLUSTER wave having
# run on this machine. Genuine dependency bumps stay unfetched and unscanned.
# ---------------------------------------------------------------------------
def _must_not_fetch(*a: object, **k: object) -> bool:
    raise AssertionError("fetch_diff must not be called here")


class TestFetchMissingDiffs:
    def _seed(self, store: Store, n: int, author: str = "alice", head: str = "h1",
              state: str = "open") -> None:
        store.save_pr({
            "pr": n,
            "meta": {"title": "t", "author": author, "state": state, "draft": False,
                     "head_sha": head, "checked_at": "2026-07-27T00:00:00+00:00"},
        })

    def _stats(self, store: Store) -> dict:
        runs = [r for r in store.runs()
                if isinstance(r, storekit.PhaseRun) and r.phase == "threat-scan"]
        return runs[-1].raw["stats"]

    def test_uncached_open_pr_is_fetched_and_scanned(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 7, author="mallory", head="h7")

        def fake_fetch(pr, head, diffs_dir, store=None):
            (diffs_dir / f"{head}.diff").write_text(PAYLOAD_DIFF)
            return True
        monkeypatch.setattr(diff_cache, "fetch_diff", fake_fetch)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)])
        assert store.load_pr(7).section("threat")["verdict"] == "malicious"
        stats = self._stats(store)
        assert stats["fetched"] == 1
        assert stats["uncached"] == 0

    def test_no_fetch_disables_the_fetch_step(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 7)
        monkeypatch.setattr(diff_cache, "fetch_diff", _must_not_fetch)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])
        assert store.load_pr(7).section("threat") is None
        stats = self._stats(store)
        assert stats["uncached"] == 1
        assert "fetched" not in stats

    def test_dependabot_bump_diff_is_never_fetched_and_its_head_stamped_exempt(
            self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 8, author="dependabot[bot]", head="h8")
        monkeypatch.setattr(diff_cache, "changed_paths",
                            lambda pr, head, diffs_dir=None: ["pnpm-lock.yaml"])
        monkeypatch.setattr(diff_cache, "fetch_diff", _must_not_fetch)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)])
        stamp = store.load_pr(8).section("threat")
        assert stamp["verdict"] == "clear" and stamp["against_head_sha"] == "h8"
        assert stamp["detail"] == {"exempt": "dependency-bump"}
        stats = self._stats(store)
        assert stats["bump_exempt"] == 1
        assert stats["fetched"] == 0
        assert stats["uncached"] == 0    # deliberately unfetched ≠ missing coverage

    def test_automation_author_off_shape_is_fetched_and_scanned(self, tmp_path, monkeypatch):
        # An automation-author PR that touches more than manifests does NOT
        # inherit the exemption — it gets the full fetch + scan (fail closed).
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 9, author="dependabot[bot]", head="h9")
        monkeypatch.setattr(diff_cache, "changed_paths",
                            lambda pr, head, diffs_dir=None: ["pnpm-lock.yaml", "src/app.ts"])

        def fake_fetch(pr, head, diffs_dir, store=None):
            (diffs_dir / f"{head}.diff").write_text(CLEAN_DIFF)
            return True
        monkeypatch.setattr(diff_cache, "fetch_diff", fake_fetch)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)])
        assert store.load_pr(9).section("threat")["verdict"] == "clear"
        stats = self._stats(store)
        assert stats["fetched"] == 1
        assert stats["bump_exempt"] == 0

    def test_fetch_failure_leaves_pr_uncached(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 10)
        monkeypatch.setattr(diff_cache, "fetch_diff", lambda pr, head, diffs_dir, store=None: False)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)])
        assert store.load_pr(10).section("threat") is None
        stats = self._stats(store)
        assert stats["fetch_failed"] == 1
        assert stats["fetched"] == 0
        assert stats["uncached"] == 1

    def test_closed_and_already_cached_prs_are_not_fetched(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 11, state="closed", head="hb")
        self._seed(store, 12, head="hc")
        (diffs / "hc.diff").write_text(CLEAN_DIFF)
        monkeypatch.setattr(diff_cache, "fetch_diff", _must_not_fetch)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)])
        assert store.load_pr(12).section("threat")["verdict"] == "clear"
        stats = self._stats(store)
        assert stats["fetched"] == 0
        assert stats["scanned"] == 1
        assert stats["uncached"] == 0    # the closed PR is outside the sweep entirely

    def test_closed_pr_is_not_visited_even_with_a_cached_diff(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 13, author="mallory", state="closed", head="hd")
        self._seed(store, 14, head="he")
        (diffs / "hd.diff").write_text(PAYLOAD_DIFF)
        (diffs / "he.diff").write_text(CLEAN_DIFF)
        monkeypatch.setattr(diff_cache, "fetch_diff", _must_not_fetch)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])
        assert store.load_pr(13).section("threat") is None
        assert store.load_pr(14).section("threat")["verdict"] == "clear"
        assert "mallory" not in store.load_threats().get("actors", {})
        stats = self._stats(store)
        assert stats["scanned"] == 1
        assert stats["malicious"] == 0
        assert stats["uncached"] == 0

    def test_only_names_a_closed_pr_verbatim(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 15, author="mallory", state="closed", head="hf")
        (diffs / "hf.diff").write_text(PAYLOAD_DIFF)
        monkeypatch.setattr(diff_cache, "fetch_diff", _must_not_fetch)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch", "--only", "15"])
        assert store.load_pr(15).section("threat")["verdict"] == "malicious"
        assert "mallory" in store.load_threats()["actors"]
        stats = self._stats(store)
        assert stats["scanned"] == 1
        assert stats["malicious"] == 1


def test_secret_leak_no_false_positives():
    fired = {name: ("secret-leak" in threats.scan_diff(d)["signatures"]) for name, d in FP_DIFFS.items()}
    assert not any(fired.values()), f"false positives: {[k for k,v in fired.items() if v]}"


def test_secret_leak_catches_real_leaks():
    missed = [name for name, d in REAL_LEAK_DIFFS.items() if "secret-leak" not in threats.scan_diff(d)["signatures"]]
    assert not missed, f"missed real leaks: {missed}"


# A private key leaks with its base64 body; a header named in a message, a
# placeholder, or an elided example carries no key. The bodies are synthetic.
RSA_BODY = ["MIIEpAIBAAKCAQEAu7Q1fX9kLm2Zp4Vt8Rw3Hs6Jd0Nc5Ye1Gb7Ua2Ki9Ox4Pl3Q",
            "n6Ev1Tr5Dy0Sf3Cg7Bh2Aj9Zk4Xl8Mo1Np6Iq0Ur5Vs3Wt7Xu2Yv9Zw4Ax6By1Cz"]
OPENSSH_BODY = ["b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gt",
                "ZWQyNTUxOQAAACBu7Q1fX9kLm2Zp4Vt8Rw3Hs6Jd0Nc5Ye1Gb7Ua2Ki9Ox4Pl3QmA"]
ENCRYPTION_HEADERS = ("Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-128-CBC,3F17F5316E2BAC89BCB7BF6E1B4E4B7C", "")


def _pem(label: str, body: list[str] = RSA_BODY, headers: tuple[str, ...] = ()) -> list[str]:
    return [f"-----BEGIN {label}PRIVATE KEY-----", *headers, *body, f"-----END {label}PRIVATE KEY-----"]


PRIVATE_KEY_LEAKS = {
    "pem-file": _added_file("deploy/id_rsa", _pem("RSA ")),
    "openssh-pem-file": _added_file("deploy/id_ed25519", _pem("OPENSSH ", OPENSSH_BODY)),
    "crlf-pem-file": _added_file("deploy/id_rsa", [f"{line}\r" for line in _pem("RSA ")]),
    "encrypted-pem-file": _added_file("deploy/server.key", _pem("RSA ", headers=ENCRYPTION_HEADERS)),
    "pkcs8-encrypted-pem-file": _added_file("deploy/server.key", _pem("ENCRYPTED ")),
    "pgp-block": _added_file("deploy/signing.asc", [
        "-----BEGIN PGP PRIVATE KEY BLOCK-----", "Version: GnuPG v2", "", *RSA_BODY,
        "-----END PGP PRIVATE KEY BLOCK-----"]),
    "yaml-block-scalar": _added_file("deploy/values.yaml", [
        "sshKey: |", *(f"  {line}" for line in _pem("OPENSSH ", OPENSSH_BODY))]),
    "ts-string-literal": _added_file("src/keys.ts", [
        'export const key = "' + "\\n".join(_pem("RSA ")) + '\\n";']),
    "js-template-literal": _added_file("src/keys.js", [
        "const key = `" + _pem("RSA ")[0], *_pem("RSA ")[1:-1], _pem("RSA ")[-1] + "`;"]),
    "js-concatenated-lines": _added_file("src/keys.js", [
        "const key =", *(f'  "{line}\\n" +' for line in _pem("RSA ")), '  "";']),
    "ts-array-of-lines": _added_file("src/keys.ts", [
        "const KEY = [", *(f'  "{line}",' for line in _pem("OPENSSH ", OPENSSH_BODY)), '].join("\\n");']),
    "json-service-account": _added_file("deploy/service-account.json", [
        "{", '  "type": "service_account",',
        '  "private_key": "' + "\\n".join(_pem("")) + '\\n",', "}"]),
    "json-encrypted-crlf": _added_file("deploy/creds.json", [
        '{"key": "' + "\\r\\n".join(_pem("RSA ", headers=ENCRYPTION_HEADERS)) + '"}']),
    "one-line-env": _added_file("deploy/prod.env", ['SSH_KEY="' + " ".join(_pem("RSA ")) + '"']),
}

PRIVATE_KEY_MENTIONS = {
    # A maintainer's key validator, whose error message quotes the header.
    "validation-message":_added_file("packages/plugins/sandbox-providers/exe-dev/src/ssh-key.ts", [
        "  const headerMatch = trimmed.match(/^-----BEGIN ([A-Z0-9 ]*)PRIVATE KEY-----/m);",
        "  if (!headerMatch) {",
        "    return \"sshPrivateKey must be a PEM-encoded private key starting with a line like "
        "'-----BEGIN OPENSSH PRIVATE KEY-----'.\";",
        "  }",
    ]),
    "ui-placeholder": _added_file("ui/src/SshKeyField.tsx", [
        '<textarea placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" />']),
    "elided-example": _added_file("src/keys.ts", [
        'const EXAMPLE = "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----";']),
    "header-constants": _added_file("src/pem.ts", [
        'const HEADER = "-----BEGIN RSA PRIVATE KEY-----";',
        'const FOOTER = "-----END RSA PRIVATE KEY-----";']),
    "template-placeholder": _added_file("deploy/ssh-key.tpl", [
        "-----BEGIN OPENSSH PRIVATE KEY-----", "<paste the private key here>",
        "-----END OPENSSH PRIVATE KEY-----"]),
    "comment-then-code": _added_file("src/pem.ts", [
        "// a key opens with -----BEGIN OPENSSH PRIVATE KEY-----",
        "export function readPrivateKeyWithItsHeaderAndFooterIntact(pem: string): string {"]),
    "header-ends-a-file": _added_file("deploy/header.txt", ["-----BEGIN RSA PRIVATE KEY-----"])
                          + _added_file("deploy/digests.txt", RSA_BODY),
}


@pytest.mark.parametrize("diff", PRIVATE_KEY_LEAKS.values(), ids=PRIVATE_KEY_LEAKS.keys())
def test_private_key_with_its_body_is_a_secret_leak(diff: str) -> None:
    assert "secret-leak" in threats.scan_diff(diff)["signatures"]


@pytest.mark.parametrize("diff", PRIVATE_KEY_MENTIONS.values(), ids=PRIVATE_KEY_MENTIONS.keys())
def test_private_key_header_without_its_body_is_not_a_secret_leak(diff: str) -> None:
    assert "secret-leak" not in threats.scan_diff(diff)["signatures"]


def test_multi_line_private_key_is_reported_at_its_header() -> None:
    diff = PRIVATE_KEY_LEAKS["pem-file"]
    assert threats.locate(diff) == [threats.Match("secret-leak", "deploy/id_rsa", 6)]
    assert threats.scan_diff(diff)["detail"]["secret-leak"] == "deploy/id_rsa: -----BEGIN RSA PRIVATE KEY-----"


class TestScanWriteEconomy:
    """The scan runs over every PR in the store; a re-run must not pay a
    read+write round-trip per PR whose stored verdict it merely re-derives."""

    def _seed(self, store, n, author, head, diff_text, diffs_dir):
        TestScanDriver._seed(self, store, n, author, head, diff_text, diffs_dir)

    def _saved_at(self, store, n):
        return store._prs.stamped(n)[1]

    def test_rerun_with_unchanged_verdict_writes_nothing(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "alice", "shaA", CLEAN_DIFF, diffs)
        self._seed(store, 2, "mallory", "shaB", PAYLOAD_DIFF, diffs)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])
        first = {n: self._saved_at(store, n) for n in (1, 2)}
        assert store.load_pr(2).threat_verdict == "malicious"

        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])
        assert {n: self._saved_at(store, n) for n in (1, 2)} == first

    def test_moved_head_is_rescanned_and_restamped(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "alice", "shaA", CLEAN_DIFF, diffs)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])
        before = self._saved_at(store, 1)

        # the author force-pushes a payload; INGEST moves the head
        (diffs / "shaA2.diff").write_text(PAYLOAD_DIFF)
        pr = store.edit_pr(1)
        pr.set_meta({**pr.section("meta"), "head_sha": "shaA2"})
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])

        after = store.load_pr(1)
        assert after.threat_verdict == "malicious"
        assert after.section("threat")["against_head_sha"] == "shaA2"
        assert self._saved_at(store, 1) != before

    def test_changed_verdict_at_same_head_is_restamped(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "mallory", "shaA", CLEAN_DIFF, diffs)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])
        assert store.load_pr(1).threat_verdict == "clear"

        # the actor lands on the blocklist between runs; the same diff now reads malicious
        reg = store.load_threats()
        threats.block_actor(reg, "mallory", "prior attack", added="2026-06-12")
        store.save_threats(reg)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])
        assert store.load_pr(1).threat_verdict == "malicious"

    def test_a_verdict_an_older_revision_stamped_is_restamped_at_this_one(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "alice", "shaA", CLEAN_DIFF, diffs)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])
        pr = store.edit_pr(1)
        pr.set_threat({**pr.section("threat"), "revision": threats.REVISION - 1})

        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])

        assert store.load_pr(1).section("threat")["revision"] == threats.REVISION

    def test_scan_loop_runs_on_one_bound_connection(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "alice", "shaA", CLEAN_DIFF, diffs)
        seen = []
        real_scan = threat_scan.scan_record

        def scan_noting_binding(rec, registry, diffs_dir, diff=None):
            seen.append(storekit._bound_conn(Store(tmp_path).engine) is not None)
            return real_scan(rec, registry, diffs_dir=diffs_dir, diff=diff)

        monkeypatch.setattr(threat_scan, "scan_record", scan_noting_binding)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])
        assert seen == [True]


# ---------------------------------------------------------------------------
# The scan judges a PR's whole diff. The cache holds a copy capped at
# diff_cache.MAX_DIFF_BYTES, with every file past the cap (and the profile's
# artifact files, ordered last) reduced to a one-line stub; a payload placed
# there must still fire.
# ---------------------------------------------------------------------------
# The zach-hermes shape: a createRequire smuggle plus an obfuscated line hidden
# after thousands of spaces.
HIDDEN_PAYLOAD = [
    "import { createRequire } from 'module';",
    "const require = createRequire(import.meta.url);",
    "export const ready = true;" + " " * 20_000
    + "global['!']='9-0008-2';var _$_1e42=(function(l,e){})('x',1);global[_$_1e42[0]]= require;",
]


def _churn(nbytes: int) -> str:
    """Benign added source files, about 3KB each, totalling at least `nbytes`."""
    out: list[str] = []
    size = 0
    i = 0
    while size < nbytes:
        block = _added_file(f"server/src/generated_{i}.ts",
                            [f"export const value_{i}_{j} = {j};" for j in range(100)])
        out.append(block)
        size += len(block)
        i += 1
    return "".join(out)


def _github(monkeypatch, *, diff: str | None = None, files: list[dict] | None = None) -> list[list[str]]:
    """Answer `gh pr diff` with `diff` and the per-file listing with `files`;
    either left None fails the way GitHub does (a 406, an unreachable API).
    Returns the commands run."""
    import json
    import subprocess

    calls: list[list[str]] = []

    def run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "pr", "diff"]:
            ok, out = diff is not None, diff or ""
        else:
            ok, out = files is not None, json.dumps([files or []])
        return subprocess.CompletedProcess(cmd, 0 if ok else 1, out, "" if ok else "HTTP 406")

    monkeypatch.setattr(diff_cache.subprocess, "run", run)
    return calls


def _no_github(monkeypatch) -> None:
    def run(cmd, **kw):
        raise AssertionError(f"GitHub must not be read here: {cmd}")
    monkeypatch.setattr(diff_cache.subprocess, "run", run)


class TestWholeDiffScan:
    @pytest.fixture(autouse=True)
    def _captures(self, monkeypatch) -> list[tuple[int, str]]:
        """Evidence capture makes GitHub reads of its own (test_threat_evidence
        covers them); here it records which flagged heads the scan hands it, so
        these tests count the scan's own diff reads alone."""
        from pipeline import threat_evidence
        handed: list[tuple[int, str]] = []
        monkeypatch.setattr(threat_evidence, "capture", lambda store, flag, **k:
                            handed.append((flag.pr, flag.head_sha)) or "captured")
        return handed

    def _seed(self, store: Store, n: int, head: str, author: str = "zach-hermes") -> None:
        store.save_pr({
            "pr": n,
            "meta": {"title": "t", "author": author, "state": "open", "draft": False,
                     "head_sha": head, "checked_at": "2026-10-02T00:00:00+00:00"},
        })

    def _stats(self, store: Store) -> dict:
        runs = [r for r in store.runs()
                if isinstance(r, storekit.PhaseRun) and r.phase == "threat-scan"]
        return runs[-1].raw["stats"]

    def _cache(self, monkeypatch, pr: int, head: str, diffs, full: str) -> str:
        """Fetch `full` into the cache the way every caller does; returns the
        capped copy the cache holds."""
        _github(monkeypatch, diff=full)
        assert diff_cache.fetch_diff(pr, head, diffs_dir=diffs)
        return (diffs / f"{head}.diff").read_text()

    def test_payload_past_the_cap_fires(self, tmp_path, monkeypatch, _captures):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 11987, "h1")
        full = (_churn(diff_cache.MAX_DIFF_BYTES)
                + _added_file("server/src/index.ts", HIDDEN_PAYLOAD))
        cached = self._cache(monkeypatch, 11987, "h1", diffs, full)
        assert "global['!']" not in cached          # the cap stubbed the payload file

        calls = _github(monkeypatch, diff=full)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)])

        t = store.load_pr(11987).section("threat")
        assert t["verdict"] == "malicious"
        assert {"obfuscated-self-decoder", "capability-smuggle"} <= set(t["signatures"])
        assert [c[:3] for c in calls] == [["gh", "pr", "diff"]]
        assert (diffs / "h1.diff").read_text() == cached   # the whole diff is never cached
        assert self._stats(store)["complete_fetched"] == 1
        assert _captures == [(11987, "h1")]
        assert self._stats(store)["evidence_captured"] == 1

    def test_payload_in_an_artifact_file_fires(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 12035, "h2")
        # a built dist/ file sorts last in the cache, so it is the one stubbed
        full = (_added_file("dist/server.js", HIDDEN_PAYLOAD)
                + _churn(diff_cache.MAX_DIFF_BYTES - 15_000))
        cached = self._cache(monkeypatch, 12035, "h2", diffs, full)
        assert "global['!']" not in cached
        assert "# omitted: vendored" in cached

        _github(monkeypatch, diff=full)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)])

        assert store.load_pr(12035).section("threat")["verdict"] == "malicious"

    def test_a_file_github_withholds_reads_suspicious(self, tmp_path, monkeypatch):
        # GitHub refuses the .diff of a PR over 20k lines, and its per-file
        # listing carries no patch for a file past its own limit
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 12041, "h3")
        files = [
            {"filename": "src/agent.ts", "status": "modified", "additions": 1,
             "deletions": 1, "patch": "@@ -1 +1 @@\n-old\n+new"},
            {"filename": "dist/huge.js", "status": "added", "additions": 25_000,
             "deletions": 0},
        ]
        _github(monkeypatch, files=files)
        assert diff_cache.fetch_diff(12041, "h3", diffs_dir=diffs)

        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)])

        t = store.load_pr(12041).section("threat")
        assert t["verdict"] == "suspicious"
        assert t["signatures"] == ["unscannable-diff"]
        assert "dist/huge.js" in t["detail"]["unscannable-diff"]

    def test_capped_copy_without_the_whole_diff_is_no_verdict(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 12063, "h4")
        full = (_churn(diff_cache.MAX_DIFF_BYTES)
                + _added_file("server/src/index.ts", HIDDEN_PAYLOAD))
        self._cache(monkeypatch, 12063, "h4", diffs, full)

        _github(monkeypatch)                         # GitHub answers nothing this run
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)])

        assert store.load_pr(12063).section("threat") is None
        stats = self._stats(store)
        assert stats["incomplete"] == 1
        assert stats["uncached"] == 0

    def test_no_fetch_leaves_a_capped_copy_unjudged(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 5, "h5")
        (diffs / "h5.diff").write_text(diff_cache.bound(_churn(diff_cache.MAX_DIFF_BYTES + 10_000)))
        _no_github(monkeypatch)

        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])

        assert store.load_pr(5).section("threat") is None
        assert self._stats(store)["incomplete"] == 1

    def test_a_hit_in_the_capped_copy_is_malicious_without_the_rest(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 6, "h6")
        full = (_added_file("server/src/index.ts", HIDDEN_PAYLOAD)
                + _churn(diff_cache.MAX_DIFF_BYTES))
        (diffs / "h6.diff").write_text(diff_cache.bound(full))
        _no_github(monkeypatch)

        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])

        assert store.load_pr(6).section("threat")["verdict"] == "malicious"

    def test_a_line_break_character_cannot_forge_a_file_in_the_cached_copy(self, tmp_path, monkeypatch):
        # an added line holding a form feed and a forged artifact-file header,
        # then a payload line shaped like a file header
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 8, "h8")
        full = ("diff --git a/src/a.js b/src/a.js\n--- a/src/a.js\n+++ b/src/a.js\n"
                "@@ -1 +1,3 @@\n x\n+// a\x0cdiff --git a/dist/z.js b/dist/z.js\n"
                "+++ global['!']='9-0008-2';\n"
                + _added_file("src/b.js", ["ok();"]))
        assert self._cache(monkeypatch, 8, "h8", diffs, full) == full
        _no_github(monkeypatch)

        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)])

        assert store.load_pr(8).section("threat")["verdict"] == "malicious"

    def test_a_whole_cached_diff_is_judged_without_reading_github(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 7, "h7")
        (diffs / "h7.diff").write_text(PAYLOAD_DIFF)
        _no_github(monkeypatch)

        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)])

        assert store.load_pr(7).section("threat")["verdict"] == "malicious"
        assert self._stats(store)["complete_fetched"] == 0


class TestUnscanned:
    """`unscanned` names the open PRs whose current head has no verdict the scan
    would keep: the selection the worker's new-head pass runs `scan` over."""

    def _seed(self, store, n, *, author="alice", head="h1", state="open",
              stamped=None, verdict="clear", revision=threats.REVISION):
        rec = {"pr": n,
               "meta": {"title": "t", "author": author, "state": state, "draft": False,
                        "head_sha": head, "checked_at": "2026-09-28T00:00:00+00:00"}}
        if stamped is not None:
            rec["threat"] = {"verdict": verdict, "signatures": [], "detail": {},
                             "checked_at": "2026-09-28T00:00:00+00:00",
                             "against_head_sha": stamped}
            if revision is not None:
                rec["threat"]["revision"] = revision
        store.save_pr(rec)

    def test_a_moved_head_and_a_new_arrival_are_named(self, tmp_path):
        store = Store(tmp_path)
        self._seed(store, 1, head="h1", stamped="h1")      # scanned at its head
        self._seed(store, 2, head="h2b", stamped="h2a")    # force-pushed since its scan
        self._seed(store, 3, head="h3")                     # never scanned
        self._seed(store, 4, head="h4b", stamped="h4a", state="closed")
        assert threat_scan.unscanned(store.all_prs(), threats.empty_registry()) == [2, 3]

    def test_a_verdict_an_older_signature_revision_stamped_is_named(self, tmp_path):
        store = Store(tmp_path)
        self._seed(store, 1, head="h1", stamped="h1")
        self._seed(store, 2, head="h2", stamped="h2", revision=threats.REVISION - 1)
        self._seed(store, 3, head="h3", stamped="h3", revision=None)
        assert threat_scan.unscanned(store.all_prs(), threats.empty_registry()) == [2, 3]

    def test_a_blocked_authors_pr_still_reading_clear_is_named(self, tmp_path, monkeypatch):
        from pipeline import profile
        monkeypatch.setattr(profile, "active",
                            lambda: profile.RepoProfile(trusted_authors=("mira",)))
        store = Store(tmp_path)
        self._seed(store, 1, author="mallory", head="h1", stamped="h1")
        self._seed(store, 2, author="mallory", head="h2", stamped="h2", verdict="malicious")
        self._seed(store, 3, author="mira", head="h3", stamped="h3")
        reg = threats.empty_registry()
        threats.block_actor(reg, "mallory", "prior attack", added="2026-09-28")
        threats.block_actor(reg, "mira", "stale entry", added="2026-09-28")
        assert threat_scan.unscanned(store.all_prs(), reg) == [1]


class TestScanRun:
    """`scan` is the run the CLI and the worker pass share."""

    def _seed(self, store, n, author, head, diff_text, diffs_dir):
        TestScanDriver._seed(self, store, n, author, head, diff_text, diffs_dir)

    def test_a_head_that_moves_mid_run_is_left_for_the_next_run(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "mallory", "h1", PAYLOAD_DIFF, diffs)
        snapshot = store.all_prs()
        pr = store.edit_pr(1)                                # INGEST records a newer head
        pr.set_meta({**pr.section("meta"), "head_sha": "h1b"})

        result = threat_scan.scan(store, snapshot, diffs, fetch=False)

        assert store.load_pr(1).section("threat") is None    # no verdict pinned to h1b
        assert result.unstamped == [1] and result.stats["moved"] == 1
        assert result.malicious == [1]                       # what h1 held is still an incident
        reg = store.load_threats()
        assert "mallory" in reg["actors"]
        assert [i["head_sha"] for i in reg["incidents"]] == ["h1"]

    def test_registry_edits_made_while_the_run_scans_are_kept(self, tmp_path, monkeypatch):
        from pipeline import actions
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "mallory", "h1", PAYLOAD_DIFF, diffs)
        self._seed(store, 2, "bob", "h2", LEAKED_KEY_DIFF, diffs)
        items = store.load_action_items()
        actions.upsert(items, actions.make_item(
            "rotate-secret", pr=2, created="2026-09-27", summary="s", evidence="e",
            detail="d"))
        store.save_action_items(items)
        real_scan = threat_scan.scan_record

        def scan_beside_an_operator(rec, registry, diffs_dir, diff=None):
            # mid-run, an operator dismisses the item and another run blocks an actor
            other = Store(tmp_path)
            reg = other.load_action_items()
            actions.set_status(reg, "rotate-secret:2", "dismissed")
            other.save_action_items(reg)
            threats_reg = other.load_threats()
            threats.block_actor(threats_reg, "eve", "elsewhere", added="2026-09-28")
            other.save_threats(threats_reg)
            return real_scan(rec, registry, diffs_dir=diffs_dir, diff=diff)

        monkeypatch.setattr(threat_scan, "scan_record", scan_beside_an_operator)
        threat_scan.scan(store, store.all_prs(), diffs, fetch=False)

        status = {i["id"]: i["status"] for i in store.load_action_items()["items"]}
        assert status["rotate-secret:2"] == "dismissed"
        assert set(store.load_threats()["actors"]) == {"eve", "mallory"}

    def test_a_run_that_finds_nothing_writes_no_registry(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "alice", "h1", CLEAN_DIFF, diffs)
        monkeypatch.setattr(store, "save_threats", lambda reg: pytest.fail("threats saved"))
        monkeypatch.setattr(store, "save_action_items",
                            lambda reg: pytest.fail("action items saved"))
        result = threat_scan.scan(store, store.all_prs(), diffs, fetch=False)
        assert store.load_pr(1).threat_verdict == "clear"
        assert result.unstamped == [] and result.malicious == []


class TestLocate:
    def test_locate_reports_file_and_1_based_diff_line(self):
        hits = threats.locate(PAYLOAD_DIFF)
        lines = PAYLOAD_DIFF.split("\n")
        assert {h.signature for h in hits} == {
            "obfuscated-self-decoder", "capability-smuggle", "build-config-require-injection"}
        for h in hits:
            assert h.file == "cli/esbuild.config.mjs"
            assert lines[h.diff_line - 1].startswith("+")

    def test_locate_agrees_with_scan_diff_on_line_signatures(self):
        for diff in [PAYLOAD_DIFF, CLEAN_DIFF, LEAKED_KEY_DIFF, *FP_DIFFS.values(),
                     *REAL_LEAK_DIFFS.values(), *PRIVATE_KEY_LEAKS.values(),
                     *PRIVATE_KEY_MENTIONS.values()]:
            line_sigs = set(threats.scan_diff(diff)["signatures"]) - {"eol-churn-camouflage"}
            assert {h.signature for h in threats.locate(diff)} == line_sigs

    @pytest.mark.parametrize("sep", LINE_BREAKS)
    def test_locate_reports_code_after_a_line_break_character(self, sep):
        diff = ("diff --git a/src/a.js b/src/a.js\n--- a/src/a.js\n+++ b/src/a.js\n"
                f"@@ -1 +1,2 @@\n x\n+// note{sep}console.log(1);\n")
        assert threats.locate(diff) == [threats.Match("embedded-line-break", "src/a.js", 6)]

    def test_locate_honours_limit(self):
        many = PAYLOAD_DIFF + PAYLOAD_DIFF.replace("cli/", "web/")
        assert len(threats.locate(many, limit=2)) == 2


class TestSecretLeakLifecycle:
    """A secret-leak finding ends when a rescan of the head it judged no longer
    makes it, or when an operator clears it at that head and evidence."""

    VALIDATOR = PRIVATE_KEY_MENTIONS["validation-message"]
    OLD_DETAIL = ("packages/plugins/sandbox-providers/exe-dev/src/ssh-key.ts: return "
                  "\"sshPrivateKey must be a PEM-encoded private key starting with a line "
                  "like '-----BEGIN OPENSSH PRIVATE KEY-----'.…")

    def _seed(self, store, n, head, diff_text, diffs_dir):
        TestScanDriver._seed(self, store, n, "mira", head, diff_text, diffs_dir)

    def _flag(self, store, n, head):
        """Stamp `head` as an earlier signature revision did, with its item open."""
        store.edit_pr(n).set_threat({"verdict": "suspicious", "signatures": ["secret-leak"],
                                     "detail": {"secret-leak": self.OLD_DETAIL}})
        rec = store.edit_pr(n).raw
        rec["threat"]["against_head_sha"] = head
        store.save_pr(rec)
        reg = store.load_action_items()
        actions.upsert(reg, actions.make_item("rotate-secret", pr=n, summary="s",
                                              created="2026-10-02", evidence=self.OLD_DETAIL))
        store.save_action_items(reg)

    def _items(self, store) -> dict[str, dict]:
        return {i["id"]: i for i in store.load_action_items()["items"]}

    def _scan(self, tmp_path, diffs) -> None:
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])

    def test_a_leak_a_rescan_of_its_head_withdraws_retires_its_open_item(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 13575, "h1", self.VALIDATOR, diffs)
        self._flag(store, 13575, "h1")

        self._scan(tmp_path, diffs)

        assert store.load_pr(13575).threat_signatures == []
        assert "rotate-secret:13575" not in self._items(store)

    def test_a_leak_a_later_head_removes_keeps_its_item(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "h1", CLEAN_DIFF, diffs)
        self._flag(store, 1, "h0")

        self._scan(tmp_path, diffs)

        assert self._items(store)["rotate-secret:1"]["status"] == "open"

    def test_a_handled_item_outlives_its_withdrawn_leak(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "h1", self.VALIDATOR, diffs)
        self._flag(store, 1, "h1")
        reg = store.load_action_items()
        actions.set_status(reg, "rotate-secret:1", "done")
        store.save_action_items(reg)

        self._scan(tmp_path, diffs)

        assert self._items(store)["rotate-secret:1"]["status"] == "done"

    def test_clearing_lifts_the_block_and_dismisses_the_item(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "h1", LEAKED_KEY_DIFF, diffs)
        self._scan(tmp_path, diffs)
        assert gates.secret_leak_blocks(store.load_pr(1))

        assert threat_scan.clear_secret_leak(store, 1, "h1", by="Alex") is None

        pr = store.load_pr(1)
        assert not gates.secret_leak_blocks(pr)
        assert not any(r.startswith("secret-leak") for r in gates.threat_blocks(pr))
        assert pr.section("threat")["cleared"]["secret-leak"]["by"] == "Alex"
        assert self._items(store)["rotate-secret:1"]["status"] == "dismissed"
        [run] = [r for r in store.runs() if r.phase == "threat-scan:clear"]
        assert run.raw["pr"] == 1 and run.raw["head_sha"] == "h1" and run.raw["by"] == "Alex"

    def test_clearing_refuses_a_head_other_than_the_scanned_one(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "h1", LEAKED_KEY_DIFF, diffs)
        self._scan(tmp_path, diffs)

        assert threat_scan.clear_secret_leak(store, 1, "h0", by="Alex")
        assert gates.secret_leak_blocks(store.load_pr(1))

    def test_clearing_refuses_a_head_with_no_leak(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "h1", CLEAN_DIFF, diffs)
        self._scan(tmp_path, diffs)

        assert threat_scan.clear_secret_leak(store, 1, "h1", by="Alex")
        assert "cleared" not in store.load_pr(1).section("threat")

    def test_a_restamp_of_the_cleared_head_keeps_the_clearing(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "h1", LEAKED_KEY_DIFF, diffs)
        self._scan(tmp_path, diffs)
        threat_scan.clear_secret_leak(store, 1, "h1", by="Alex")
        rec = store.edit_pr(1).raw
        rec["threat"]["revision"] = threats.REVISION - 1
        store.save_pr(rec)

        self._scan(tmp_path, diffs)

        pr = store.load_pr(1)
        assert pr.section("threat")["revision"] == threats.REVISION
        assert not gates.secret_leak_blocks(pr)

    def test_the_leak_at_a_new_head_blocks_again(self, tmp_path):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 1, "h1", LEAKED_KEY_DIFF, diffs)
        self._scan(tmp_path, diffs)
        threat_scan.clear_secret_leak(store, 1, "h1", by="Alex")
        (diffs / "h2.diff").write_text(LEAKED_KEY_DIFF)
        pr = store.edit_pr(1)
        pr.set_meta({**pr.section("meta"), "head_sha": "h2"})

        self._scan(tmp_path, diffs)

        assert gates.secret_leak_blocks(store.load_pr(1))

    def test_a_clearing_of_other_evidence_does_not_lift_the_block(self):
        pr = Pr(None, {"pr": 1, "meta": {"head_sha": "h1"}, "threat": {
            "verdict": "suspicious", "signatures": ["secret-leak"],
            "detail": {"secret-leak": "a.ts: KEY=one"}, "against_head_sha": "h1",
            "cleared": {"secret-leak": {"by": "Alex", "evidence": "a.ts: KEY=two"}}}})
        assert gates.secret_leak_blocks(pr)
