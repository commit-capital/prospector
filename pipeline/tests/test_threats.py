"""threats.py — the ONE threat-detection policy: signatures, actor blocklist,
the scan driver, and the gate's fail-closed consumption of a malicious flag."""
from pipeline import diff_cache
from pipeline import gates
from pipeline import storekit
from pipeline import threats
from pipeline import threat_scan
from pipeline.model import Pr
from pipeline.store import Store


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
            "signals": {"greptile": 5, "ci": "passing", "mergeable": True,
                        "checked_at": "2026-06-10T00:00:00+00:00", "against_head_sha": "abc"},
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
            "signals": {"greptile": 5, "ci": "passing", "mergeable": True,
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

    def test_stamp_preserves_a_concurrent_write(self, tmp_path, monkeypatch):
        """A section another phase writes while the scan is mid-run survives the
        threat stamp: the stamp lands on a fresh read of the record, not on the
        copy the scan loaded at startup."""
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 7, "alice", "sha7", CLEAN_DIFF, diffs)

        real_scan = threat_scan.scan_record

        def scan_after_concurrent_write(rec, registry, diffs_dir):
            # another phase places the PR standalone after the scan's snapshot load
            Store(tmp_path).edit_pr(7).mark_standalone()
            return real_scan(rec, registry, diffs_dir=diffs_dir)

        monkeypatch.setattr(threat_scan, "scan_record", scan_after_concurrent_write)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])

        after = store.load_pr(7)
        assert after.section("threat") is not None                 # the stamp landed
        assert (after.section("cluster") or {}).get("ids") == []   # the concurrent write survived


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
        "signals": {"greptile": 2, "ci": "passing", "mergeable": False,
                    "checked_at": "2026-06-13T00:00:00+00:00", "against_head_sha": head},
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

    def test_dependabot_bump_diff_is_never_fetched(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 8, author="dependabot[bot]", head="h8")
        monkeypatch.setattr(diff_cache, "changed_paths",
                            lambda pr, head, diffs_dir=None: ["pnpm-lock.yaml"])
        monkeypatch.setattr(diff_cache, "fetch_diff", _must_not_fetch)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)])
        assert store.load_pr(8).section("threat") is None
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
        assert stats["uncached"] == 1    # the closed PR stays unfetched, uncached


def test_secret_leak_no_false_positives():
    fired = {name: ("secret-leak" in threats.scan_diff(d)["signatures"]) for name, d in FP_DIFFS.items()}
    assert not any(fired.values()), f"false positives: {[k for k,v in fired.items() if v]}"


def test_secret_leak_catches_real_leaks():
    missed = [name for name, d in REAL_LEAK_DIFFS.items() if "secret-leak" not in threats.scan_diff(d)["signatures"]]
    assert not missed, f"missed real leaks: {missed}"
