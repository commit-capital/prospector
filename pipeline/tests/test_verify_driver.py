"""VERIFY driver: the env allowlist, the scrub-then-assert gate, and the
base-image tag. The driver is the trusted half — it owns every store write and
never lets an agent near one."""
import copy
import io
import json
import os
import socket
import subprocess
import tracemalloc
from pathlib import Path
from typing import Self

import pytest

from pipeline import analyze_driver as ad
from pipeline import gates
from pipeline import profile
from pipeline import settings
from pipeline import verify_driver as vd
from pipeline.store import Store
from pipeline import wire
from pipeline.wire import BlindItem, JudgeItem

# A stand-in suite-config path for verify_pr calls whose run_phase is
# monkeypatched — the path is threaded, never opened.
SUITE_CFG = Path("suite-config-stub.json")

# A profile whose verify section carries a full-suite contract, for tests of
# the suite lane (the generic default has none).
SUITE_PROFILE = profile.RepoProfile(verify=profile.VerifyPolicy(
    suite=profile.SuiteConfig(wrapper="scripts/w.mjs", server_project="@x/server")))


class TestLauncherEnv:
    """CRITICAL: DEPLOYMENT_PRIVATE_KEY is a GitHub App private key with org admin on
    the upstream repo, and it is exported in the operator's shell profile — so it
    is in this driver's own environment. It must never reach the sandbox."""

    def test_poisoned_secrets_never_reach_the_launcher(self, monkeypatch):
        monkeypatch.setenv("DEPLOYMENT_PRIVATE_KEY", "POISON-PRIVATE-KEY")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-POISON")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_POISON")
        monkeypatch.setenv("TRIAGE_STORE_URL", "postgresql://POISON@host/db")
        env = vd.launcher_env()
        assert "POISON" not in repr(env)
        assert "DEPLOYMENT_PRIVATE_KEY" not in env

    def test_carries_only_what_docker_needs(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", "/home/op")
        monkeypatch.setenv("SOMETHING_ELSE", "x")
        env = vd.launcher_env()
        assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/op"
        assert "SOMETHING_ELSE" not in env

    def test_is_never_os_environ(self, monkeypatch):
        monkeypatch.setenv("DEPLOYMENT_PRIVATE_KEY", "POISON")
        assert vd.launcher_env() is not os.environ
        assert set(vd.launcher_env()) <= set(vd._LAUNCHER_ENV_ALLOW)


class TestScrub:
    def _checkout(self, tmp_path):
        src = tmp_path / "src"
        (src / "packages" / "core").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(src)], check=True)
        (src / "package.json").write_text("{}")
        return src

    def test_removes_credential_files_at_any_depth(self, tmp_path):
        src = self._checkout(tmp_path)
        (src / ".npmrc").write_text("//registry.npmjs.org/:_authToken=npm_SECRET")
        (src / "packages" / "core" / ".npmrc").write_text("_authToken=npm_OTHER")
        (src / ".env").write_text("TOKEN=ghp_abc")
        (src / ".netrc").write_text("machine github.com password x")
        vd.scrub_checkout(src)
        assert not (src / ".npmrc").exists()
        assert not (src / "packages" / "core" / ".npmrc").exists()
        assert not (src / ".env").exists()
        assert not (src / ".netrc").exists()

    def test_removes_the_dotenv_family(self, tmp_path):
        src = self._checkout(tmp_path)
        (src / ".env.local").write_text("TOKEN=ghp_abc")
        (src / ".env.production").write_text("TOKEN=ghp_def")
        vd.scrub_checkout(src)
        assert not (src / ".env.local").exists()
        assert not (src / ".env.production").exists()

    def test_keeps_env_example(self, tmp_path):
        src = self._checkout(tmp_path)
        (src / ".env.example").write_text("TOKEN=replace-me")
        vd.scrub_checkout(src)
        assert (src / ".env.example").exists()

    def test_assert_passes_on_a_clean_tree(self, tmp_path):
        vd.assert_scrubbed(self._checkout(tmp_path))

    def test_assert_fails_closed_on_a_missed_secret(self, tmp_path):
        # The assert is the gate, not the delete list: a secret in a file the
        # scrub does not know about must still abort the build.
        src = self._checkout(tmp_path)
        (src / "packages" / "core" / "weird.conf").write_text(
            "//registry.npmjs.org/:_authToken=npm_LEAK")
        with pytest.raises(RuntimeError, match="_authToken"):
            vd.assert_scrubbed(src)

    def test_workflow_token_wiring_is_not_a_credential(self, tmp_path):
        # Upstream's release workflow names _authToken twice — a publish step
        # wiring the token from the runner's secret store, and a log-redactor's
        # own sed regex — public CI config, not an operator credential. The
        # daily pin refresh must survive it.
        src = self._checkout(tmp_path)
        wf = src / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "release.yml").write_text(
            "run: |\n"
            "  echo '//registry.npmjs.org/:_authToken=${NPM_TOKEN}' > .npmrc\n"
            "  sed -e 's#((_authToken)\"?[:=])[^\",]+#\\1***REDACTED***#Ig'\n")
        vd.assert_scrubbed(src)

    def test_a_literal_token_in_a_workflow_still_aborts(self, tmp_path):
        # The workflow exemption covers the token-wiring NAME, never literal
        # secret material.
        src = self._checkout(tmp_path)
        wf = src / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "release.yml").write_text(
            "env:\n  GH_TOKEN: ghp_0123456789abcdefghijklmnopqrstuvwxyz\n")
        with pytest.raises(RuntimeError, match="ghp_"):
            vd.assert_scrubbed(src)

    def test_an_authtoken_outside_workflows_still_aborts(self, tmp_path):
        src = self._checkout(tmp_path)
        (src / ".github").mkdir()
        (src / ".github" / "publish.md").write_text("_authToken=npm_LEAK")
        with pytest.raises(RuntimeError, match="_authToken"):
            vd.assert_scrubbed(src)

    def test_assert_catches_a_private_key(self, tmp_path):
        src = self._checkout(tmp_path)
        (src / "key.pem").write_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n")
        with pytest.raises(RuntimeError, match="PRIVATE KEY"):
            vd.assert_scrubbed(src)

    def test_assert_catches_a_github_token(self, tmp_path):
        src = self._checkout(tmp_path)
        (src / "notes.md").write_text("token: ghp_0123456789abcdefghijklmnopqrstuvwxyz")
        with pytest.raises(RuntimeError, match="ghp_"):
            vd.assert_scrubbed(src)

    def test_assert_catches_a_secret_in_a_dir_whose_name_merely_ends_in_dot_git(self, tmp_path):
        # vendor.git is an ordinary directory, not VCS metadata: the skip is a
        # path-segment match against a directory literally named ".git", not a
        # substring match on the rendered relative path.
        src = self._checkout(tmp_path)
        (src / "vendor.git").mkdir()
        (src / "vendor.git" / "leak.txt").write_text(
            "token: ghp_0123456789abcdefghijklmnopqrstuvwxyz")
        with pytest.raises(RuntimeError, match="ghp_"):
            vd.assert_scrubbed(src)

    def test_assert_catches_a_secret_behind_a_symlinked_directory(self, tmp_path):
        src = self._checkout(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.txt").write_text(
            "token: ghp_0123456789abcdefghijklmnopqrstuvwxyz")
        (src / "linked").symlink_to(outside, target_is_directory=True)
        with pytest.raises(RuntimeError, match="ghp_"):
            vd.assert_scrubbed(src)


class TestSandboxImageTag:
    def test_tag_is_keyed_by_the_profile_pnpm_pin(self, monkeypatch):
        p = profile.RepoProfile(verify=profile.VerifyPolicy(pnpm_version="10.4.1"))
        monkeypatch.setattr(vd.profile, "active", lambda: p)
        assert vd.sandbox_image() == "pr-verify:pnpm-10.4.1"

    def test_two_pins_are_two_images(self, monkeypatch):
        tags = set()
        for v in ("9.15.4", "10.4.1"):
            p = profile.RepoProfile(verify=profile.VerifyPolicy(pnpm_version=v))
            monkeypatch.setattr(vd.profile, "active", lambda p=p: p)
            tags.add(vd.sandbox_image())
        assert len(tags) == 2


class TestBaseImageTag:
    def test_tag_is_pinned_to_sha_and_tier(self):
        tag = vd.base_image_tag("deadbeefcafebabe1234", 1)
        assert tag == "pr-verify-base:deadbeefcafe-t1"

    def test_tier_0_and_1_are_distinct_images(self):
        assert vd.base_image_tag("deadbeefcafebabe1234", 0) != \
               vd.base_image_tag("deadbeefcafebabe1234", 1)


class TestPrepareBase:
    """prepare_base's security property is an ordering: scrub_checkout and
    assert_scrubbed must run before the checkout is ever handed to `docker
    build`, because an image layer is durable. These drive prepare_base itself,
    with subprocess.run replaced so no network or Docker call is ever made."""

    def test_rejects_a_non_hex_base_sha_before_any_filesystem_work(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")

        def guard_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError(f"subprocess.run must not run for an invalid base_sha: {cmd}")

        def guard_rmtree(path: object, *args: object, **kwargs: object) -> None:
            raise AssertionError(f"shutil.rmtree must not run for an invalid base_sha: {path}")

        monkeypatch.setattr(vd.subprocess, "run", guard_run)
        monkeypatch.setattr(vd.shutil, "rmtree", guard_rmtree)
        store = Store(tmp_path / "store")

        with pytest.raises(ValueError, match="base_sha"):
            vd.prepare_base(store, base_sha="../../../..")

    def test_assert_scrubbed_gates_the_build(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if cmd[:2] == ["git", "clone"]:
                # Stand in for what a real clone would populate, planting a
                # secret the way an untouched checkout could contain one.
                dest = Path(cmd[-1])
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "leak.txt").write_text(
                    "token: ghp_0123456789abcdefghijklmnopqrstuvwxyz")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(vd.subprocess, "run", fake_run)
        store = Store(tmp_path / "store")

        with pytest.raises(RuntimeError, match="ghp_"):
            vd.prepare_base(store, base_sha="deadbeefcafebabe1234")

        # The gate is about the CHECKOUT reaching Docker, and exactly two verbs
        # receive it: `build` takes it as the build context, `run` mounts it for
        # the Tier 1 prefetch. Either one before the scrub assertion bakes the
        # secret into a durable layer. The sweep's own queries (`image ls`,
        # `rmi`, `prune`) name no path and are listed here so that adding a
        # third tree-carrying verb has to come through this assertion.
        tree_carrying = [c for c in calls if c[0] == "docker" and c[1] in ("build", "run")]
        assert tree_carrying == []

    def test_a_public_certificate_is_not_a_credential(self, tmp_path):
        """A .pem is a credential file only when it carries a private key."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "ca.pem").write_text("-----BEGIN CERTIFICATE-----\nMIIDdzCCAl+gAwIB\n")

        vd.assert_scrubbed(src)

    def test_key_shaped_text_in_source_and_tests_is_the_baseline(self, tmp_path):
        """Upstream's own tree: a redactor's source carries the patterns it
        redacts, and its tests carry real keys that were thrown away. Neither is
        a credential, and no content test tells them from a live one. A key a PR
        ADDS is threats.py's `secret-leak`, upstream of this phase."""
        src = tmp_path / "src"
        (src / "src").mkdir(parents=True)
        (src / "src" / "redaction.ts").write_text(
            'output.replace(/-----BEGIN PRIVATE KEY-----[\\s\\S]*?'
            '-----END PRIVATE KEY-----/g, "[REDACTED]")')
        (src / "src" / "http.test.ts").write_text(
            'private_key: "-----BEGIN PRIVATE KEY-----\\nfake\\n-----END PRIVATE KEY-----",')
        (src / "src" / "gh.test.ts").write_text(
            'const token = "ghp_supersecretcommenttoken1234567890"')

        vd.assert_scrubbed(src)

    def test_a_scrub_glob_that_survives_aborts(self, tmp_path):
        """The delete list's postcondition: scrub_checkout ran, and this proves it."""
        src = tmp_path / "src"
        src.mkdir()
        (src / ".npmrc").write_text("//registry.npmjs.org/:_authToken=npm_live")

        with pytest.raises(RuntimeError, match="survived the scrub"):
            vd.assert_scrubbed(src)

    def test_env_example_is_documentation_not_a_credential(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / ".env.example").write_text("API_KEY=your-key-here")

        vd.assert_scrubbed(src)

    def test_resolves_the_head_of_whatever_the_repo_calls_its_default_branch(self, monkeypatch):
        """The pinned base is the default branch's HEAD, and the default branch is
        read from the repo. `test-owner/test-repo` calls it `master`, so a
        hardcoded `main` resolves nothing."""
        seen: list[str] = []

        def fake_gh_json(path: str) -> dict[str, object] | None:
            seen.append(path)
            if path == f"repos/{settings.repo()}":
                return {"default_branch": "master"}
            if path == f"repos/{settings.repo()}/commits/master":
                return {"sha": "deadbeefcafebabe1234"}
            return None

        monkeypatch.setattr(vd.gh, "gh_json", fake_gh_json)

        assert vd.resolve_base_sha() == "deadbeefcafebabe1234"
        assert f"repos/{settings.repo()}/commits/master" in seen

    def test_refuses_a_repo_whose_default_branch_it_cannot_read(self, monkeypatch):
        monkeypatch.setattr(vd.gh, "gh_json", lambda path: None)

        with pytest.raises(RuntimeError, match="default branch"):
            vd.resolve_base_sha()


NOW = "2026-06-10T00:00:00+00:00"


def _pr(store, n, head="h1", disposition="merge", greptile=5, verify=None):
    rec = {"pr": n,
           "meta": {"title": f"t{n}", "author": "a", "state": "open", "draft": False,
                    "head_sha": head, "checked_at": NOW},
           "signals": {"greptile": greptile, "ci": "passing", "mergeable": True,
                       "checked_at": NOW, "against_head_sha": head},
           "drift": {"state": "applicable", "checked_at": NOW, "against_head_sha": head},
           "analysis": {"disposition": disposition, "rationale": "r",
                        "checked_at": NOW, "against_head_sha": head}}
    if disposition == "close-dup":
        rec["analysis"]["canonical"] = 999
    if verify:
        rec["verify"] = dict(verify, checked_at=verify.get("checked_at", NOW),
                             against_head_sha=verify.get("against_head_sha", head))
    store.save_pr(rec)


def _diff(diffs_dir, head, *paths):
    diffs_dir.mkdir(parents=True, exist_ok=True)
    body = "".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1,2 @@\n+x\n"
                   for p in paths)
    (diffs_dir / f"{head}.diff").write_text(body)


@pytest.fixture
def diffs(tmp_path, monkeypatch):
    d = tmp_path / "diffs"
    monkeypatch.setattr(vd, "DIFFS", d)
    return d


def _pinned(tmp_path, base="base1", tier=0, baseline=()) -> Store:
    s = Store(tmp_path)
    s.save_verify_base({"host": socket.gethostname(), "base_sha": base,
                        "tier": tier, "pinned_at": NOW,
                        "baseline_failing": list(baseline),
                        "baseline_captured_at": NOW})
    return s


def _suite_tail(failed, mode="baseline"):
    body = json.dumps({"mode": mode, "planned": 5, "reported": 5, "excluded": 0,
                       "invocations": 3, "failed": failed, "anomalies": []})
    return ("vitest noise\n===VERIFY-SUITE:BEGIN===\n"
            f"{body}\n===VERIFY-SUITE:END===\n")


class TestParseSuiteTrailer:
    def test_parses_the_last_trailer_from_a_noisy_tail(self):
        tail = "junk\n" + _suite_tail(["a.test.ts"]) + _suite_tail(["b.test.ts"])
        got = vd.parse_suite_trailer(tail)
        assert got is not None and got["failed"] == ["b.test.ts"]

    def test_none_when_absent(self):
        assert vd.parse_suite_trailer("no trailer here") is None

    def test_none_when_the_begin_marker_is_cut_off_by_the_tail_cap(self):
        assert vd.parse_suite_trailer(_suite_tail([])[20:]) is None

    def test_none_on_invalid_json(self):
        tail = "===VERIFY-SUITE:BEGIN===\n{oops\n===VERIFY-SUITE:END===\n"
        assert vd.parse_suite_trailer(tail) is None


FIXTURES = Path(__file__).parent / "fixtures"

# The contamination pair captured from PR #3718's real run (vitest 4, ANSI
# colors on; deployment identifiers scrubbed): red fails on the target test
# AND the psql-dependent test, green fails on the psql-dependent test alone.
DIRTY_RED_TAIL = (FIXTURES / "vitest_dirty_red_tail.txt").read_text()
DIRTY_GREEN_TAIL = (FIXTURES / "vitest_dirty_green_tail.txt").read_text()

_TARGET = ("@exampleorg/db src/backup-lib.test.ts > runDatabaseBackup > "
           "keeps the newest backup for each retained calendar month")
_CONTAM = ("@exampleorg/db src/backup-lib.test.ts > runDatabaseBackup > "
           "restores fallback COPY data when child tables are dumped before "
           "parent tables")


def _vitest_tail(names: list[str]) -> str:
    """A minimal plain (no-ANSI) vitest failure tail naming `names` in its
    Failed Tests section."""
    fails = "\n\n".join(f" FAIL  {n}\nAssertionError: expected 2 to be 3" for n in names)
    return (f"noise\n"
            f"⎯⎯⎯⎯⎯⎯⎯ Failed Tests {len(names)} ⎯⎯⎯⎯⎯⎯⎯\n\n{fails}\n\n"
            f" Test Files  1 failed (1)\n"
            f"      Tests  {len(names)} failed | 5 passed (7)\n")


class TestParseFailedTests:
    """The deterministic per-test parse of a red/green tail: the runner's own
    Failed Tests report, ANSI stripped, integrity-checked against its own
    count. Anything that does not parse cleanly is None — fail-closed, the
    containment exemption simply never applies."""

    def test_parses_the_real_red_tail(self):
        assert vd.parse_failed_tests(DIRTY_RED_TAIL) == [_TARGET, _CONTAM]

    def test_parses_the_real_green_tail(self):
        assert vd.parse_failed_tests(DIRTY_GREEN_TAIL) == [_CONTAM]

    def test_parses_a_plain_tail(self):
        assert vd.parse_failed_tests(_vitest_tail(["a.test.ts > s > one"])) == [
            "a.test.ts > s > one"]

    def test_none_without_a_failed_tests_header(self):
        assert vd.parse_failed_tests("FAIL  a.test.ts > s > one\n") is None

    def test_none_when_the_count_disagrees_with_the_fail_lines(self):
        # The tail cap cut an earlier FAIL block, or stray output matched the
        # FAIL shape — either way the report is not trustworthy.
        tail = _vitest_tail(["a.test.ts > s > one"]).replace(
            "Failed Tests 1", "Failed Tests 2")
        assert vd.parse_failed_tests(tail) is None

    def test_the_last_header_wins(self):
        # Test output printed before the runner's real summary (including an
        # attacker-shaped fake section) is superseded by the final report.
        tail = _vitest_tail(["a.test.ts > s > fake"]) + _vitest_tail(
            ["a.test.ts > s > real"])
        assert vd.parse_failed_tests(tail) == ["a.test.ts > s > real"]

    def test_none_on_unhandled_errors(self):
        # An Unhandled Errors section fails the run beyond its per-test
        # results, so the failing set alone cannot account for the exit code.
        tail = _vitest_tail(["a.test.ts > s > one"]) + (
            "⎯⎯⎯⎯⎯⎯⎯ Unhandled Errors 1 ⎯⎯⎯⎯⎯⎯⎯\nError: boom\n")
        assert vd.parse_failed_tests(tail) is None

    def test_none_beyond_the_name_cap(self):
        names = [f"a.test.ts > s > t{i}" for i in range(51)]
        assert vd.parse_failed_tests(_vitest_tail(names)) is None

    def test_none_on_an_empty_tail(self):
        assert vd.parse_failed_tests("") is None


class TestFailingInTestDiff:
    """The diff-membership fact: which parsed failing tests carry a leaf title
    the PR's own test-file hunks mention. Conservative by construction — any
    mention (added, removed, or context line) counts, and an unreadable diff
    yields None so the containment check fails closed."""

    DIFF = ("diff --git a/src/a.test.ts b/src/a.test.ts\n"
            "--- a/src/a.test.ts\n+++ b/src/a.test.ts\n@@ -1 +1,2 @@\n"
            "+  it(\"keeps the newest backup\", async () => {\n"
            "diff --git a/src/impl.ts b/src/impl.ts\n"
            "--- a/src/impl.ts\n+++ b/src/impl.ts\n@@ -1 +1,2 @@\n"
            "+  // restores fallback COPY data\n")

    def test_a_title_in_the_test_diff_is_named(self):
        got = vd.failing_in_test_diff(
            ["a.test.ts > s > keeps the newest backup"], self.DIFF)
        assert got == ["a.test.ts > s > keeps the newest backup"]

    def test_a_title_absent_from_the_test_diff_is_not(self):
        assert vd.failing_in_test_diff(
            ["a.test.ts > s > restores fallback COPY data"], self.DIFF) == []

    def test_only_test_file_hunks_are_searched(self):
        # "restores fallback COPY data" appears in the diff, but only in a
        # non-test file's hunk — a source comment is not a test the PR touches.
        assert vd.failing_in_test_diff(
            ["a.test.ts > s > restores fallback COPY data"], self.DIFF) == []

    def test_an_unreadable_diff_yields_none(self):
        assert vd.failing_in_test_diff(["a.test.ts > s > x"], "") is None

    def test_an_unparsed_set_yields_none(self):
        assert vd.failing_in_test_diff(None, self.DIFF) is None


class _SubprocessOK:
    stdout = ""
    returncode = 0


class TestPrepareBaseBaseline:
    """prepare-base captures the pinned base's failing set and refuses to pin
    without it — a pin with no baseline would run waves with the regression
    gate silently off."""

    def _quiet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vd.subprocess, "run", lambda *a, **k: _SubprocessOK())
        monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
        monkeypatch.setattr(vd.profile, "active", lambda: SUITE_PROFILE)

    def test_pin_carries_the_captured_baseline(self, tmp_path, monkeypatch):
        self._quiet(tmp_path, monkeypatch)
        phases = []
        monkeypatch.setattr(vd, "run_phase", lambda phase, image, **kw: (
            phases.append(phase) or (0, _suite_tail(["server/src/__tests__/x.test.ts"]))))
        s = Store(tmp_path)
        vd.prepare_base(s, base_sha="a" * 12, tier=0)
        assert phases == ["baseline"]
        reg = vd.local_pin(s)
        assert reg["baseline_failing"] == ["server/src/__tests__/x.test.ts"]
        assert reg["baseline_captured_at"]
        assert reg["suite"] is True
        assert reg["host"]
        assert reg["arch"]

    def test_a_profile_without_a_suite_pins_without_a_baseline_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vd.subprocess, "run", lambda *a, **k: _SubprocessOK())
        monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
        monkeypatch.setattr(vd.profile, "active", lambda: profile.GENERIC)
        phases = []
        monkeypatch.setattr(vd, "run_phase", lambda phase, image, **kw: (
            phases.append(phase) or (0, _suite_tail([]))))
        s = Store(tmp_path)
        vd.prepare_base(s, base_sha="a" * 12, tier=0)
        assert phases == []
        reg = vd.local_pin(s)
        assert reg["suite"] is False
        assert reg["baseline_failing"] == []
        assert vd.pinned_suite(s) is False

    def test_a_pre_provenance_pin_reads_as_having_a_suite(self, tmp_path):
        s = Store(tmp_path)
        s.save_verify_base({"host": socket.gethostname(), "base_sha": "b" * 12,
                            "tier": 0, "pinned_at": NOW,
                            "baseline_failing": [], "baseline_captured_at": NOW})
        assert vd.pinned_suite(s) is True

    def test_refuses_to_pin_on_a_nonzero_baseline_phase(self, tmp_path, monkeypatch):
        self._quiet(tmp_path, monkeypatch)
        monkeypatch.setattr(vd, "run_phase", lambda *a, **kw: (1, _suite_tail([])))
        s = Store(tmp_path)
        with pytest.raises(RuntimeError, match="refusing to pin"):
            vd.prepare_base(s, base_sha="a" * 12, tier=0)
        assert vd.local_pin(s)["base_sha"] is None

    def test_refuses_to_pin_on_an_unparsable_trailer(self, tmp_path, monkeypatch):
        self._quiet(tmp_path, monkeypatch)
        monkeypatch.setattr(vd, "run_phase", lambda *a, **kw: (0, "86 bytes of junk"))
        with pytest.raises(RuntimeError, match="refusing to pin"):
            vd.prepare_base(Store(tmp_path), base_sha="a" * 12, tier=0)

    def test_refuses_a_trailer_of_the_wrong_mode(self, tmp_path, monkeypatch):
        self._quiet(tmp_path, monkeypatch)
        monkeypatch.setattr(vd, "run_phase",
                            lambda *a, **kw: (0, _suite_tail([], mode="regress")))
        with pytest.raises(RuntimeError, match="refusing to pin"):
            vd.prepare_base(Store(tmp_path), base_sha="a" * 12, tier=0)


class TestPrepareBaseGarbageCollection:
    """prepare_base owns the artifacts it creates, so it owns reclaiming them.
    The sweep runs on both sides of the build: a full disk breaks the build, so
    a sweep that only ran after a successful pin could never run again."""

    def _quiet(self, tmp_path, monkeypatch, events: list):
        monkeypatch.setattr(vd.subprocess, "run",
                            lambda *a, **k: events.append("build") or _SubprocessOK())
        monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
        monkeypatch.setattr(vd.profile, "active", lambda: profile.GENERIC)

    def test_sweeps_before_the_build_and_after_the_pin(self, tmp_path, monkeypatch):
        events: list = []
        self._quiet(tmp_path, monkeypatch, events)
        monkeypatch.setattr(vd.verify_gc, "collect",
                            lambda pinned, **kw: events.append(("gc", pinned)) or {"ok": True})
        vd.prepare_base(Store(tmp_path), base_sha="a" * 12, tier=0)
        gc_events = [e for e in events if e != "build"]
        assert len(gc_events) == 2
        assert events.index(gc_events[0]) < events.index("build")

    def test_the_sweep_before_the_build_protects_the_outgoing_pin(self, tmp_path, monkeypatch):
        """It runs while the old pin is still the pin, so the artifacts a verify
        run in flight is reading are the ones retention keeps."""
        events: list = []
        self._quiet(tmp_path, monkeypatch, events)
        s = Store(tmp_path)
        s.save_verify_base({"host": socket.gethostname(), "base_sha": "o" * 12,
                            "tier": 0, "pinned_at": NOW,
                            "baseline_failing": [], "baseline_captured_at": NOW})
        monkeypatch.setattr(vd.verify_gc, "collect",
                            lambda pinned, **kw: events.append(("gc", pinned)) or {"ok": True})
        vd.prepare_base(s, base_sha="a" * 12, tier=0)
        assert [e[1] for e in events if e != "build"] == ["o" * 12, "a" * 12]

    def test_a_sweep_that_raises_never_fails_the_pin(self, tmp_path, monkeypatch):
        def boom(pinned, **kw):
            raise RuntimeError("docker exploded")

        self._quiet(tmp_path, monkeypatch, [])
        monkeypatch.setattr(vd.verify_gc, "collect", boom)
        s = Store(tmp_path)
        assert vd.prepare_base(s, base_sha="a" * 12, tier=0)
        assert vd.local_pin(s)["base_sha"] == "a" * 12


class TestPinnedBaseline:
    def test_reads_the_pinned_exclusion_set(self, tmp_path):
        s = _pinned(tmp_path, baseline=["a.test.ts"])
        assert vd.pinned_baseline(s) == ["a.test.ts"]

    def test_raises_when_the_pin_has_no_baseline(self, tmp_path):
        # A registry written before the baseline fields existed reads back with
        # baseline_failing None; running a wave on it must refuse, not skip the gate.
        s = Store(tmp_path)
        s._save_registry("verify_base", {"base_sha": "base1", "tier": 0,
                                         "pinned_at": NOW})
        with pytest.raises(RuntimeError, match="baseline"):
            vd.pinned_baseline(s)


class TestWriteExcludeFile:
    def test_writes_a_sorted_json_array_keyed_by_sha12(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vd, "SCRATCH", tmp_path)
        p = vd.write_exclude_file("a" * 40, ["b.test.ts", "a.test.ts"])
        assert p.name == f"{'a' * 12}.json"
        assert json.loads(p.read_text()) == ["a.test.ts", "b.test.ts"]


class TestCommitBlind:
    """Signal 1 is committed BEFORE any sandbox runs, so it cannot be
    rationalized backward from a green result."""

    def test_writes_the_signal_with_a_null_outcome(self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        ok, errs = vd.commit_blind(s, [BlindItem.from_dict({
            "pr": 1, "head_sha": "h1", "has_test": True, "faithful": True,
            "test_cmd": "pnpm -s test a.test.ts", "confidence": "high",
            "claimed_symptom": "throws on empty input",
            "expected_red_signature": "TypeError: cannot read length",
            "reasoning": "the test asserts exactly the claimed symptom"})])
        assert (ok, errs) == (1, [])
        got = s.load_pr(1)
        assert got.verify_outcome is None          # not yet judged
        blind = got.verify_signals["blind_adequacy"]
        assert blind["faithful"] is True
        assert blind["expected_red_signature"] == "TypeError: cannot read length"
        assert got.verify_base_sha == "base1"
        assert got.disposition == "merge"          # a null outcome forces no route

    def test_an_unfaithful_blind_verdict_forces_no_route_yet(self, tmp_path, diffs):
        # The escalation needs Signal 2, which does not exist yet.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        ok, errs = vd.commit_blind(s, [BlindItem.from_dict({
            "pr": 1, "head_sha": "h1", "has_test": True, "faithful": False,
            "test_cmd": "pnpm -s test a.test.ts", "reasoning": "tests an unrelated path"})])
        assert (ok, errs) == (1, [])
        got = s.load_pr(1)
        assert got.verify_signals["blind_adequacy"]["faithful"] is False
        assert got.verify_outcome is None
        assert got.disposition == "merge"          # a null outcome forces no route

    def test_unknown_pr_is_an_error_not_a_crash(self, tmp_path, diffs):
        s = _pinned(tmp_path)
        ok, errs = vd.commit_blind(s, [BlindItem.from_dict({
            "pr": 99, "head_sha": "h9", "has_test": True, "faithful": True,
            "reasoning": "r"})])
        assert ok == 0 and "99" in errs[0]

    def test_derives_the_test_command_ignoring_the_agent(self, tmp_path, diffs):
        # The stored red/green command is the driver's derivation from the diff's
        # test files — the agent's (here, #7524-shaped filtered) command is
        # discarded, so no name filter can ever reach the sandbox.
        s = _pinned(tmp_path)
        _pr(s, 1)
        _diff(diffs, "h1", "server/src/__tests__/lock.test.ts", "src/impl.ts")
        ok, errs = vd.commit_blind(s, [BlindItem.from_dict({
            "pr": 1, "head_sha": "h1", "has_test": True, "faithful": True,
            "test_cmd": 'npx vitest run x.test.ts -t "release preserves status"',
            "reasoning": "r"})])
        assert (ok, errs) == (1, [])
        blind = s.load_pr(1).verify_signals["blind_adequacy"]
        assert "-t " not in blind["test_cmd"]
        assert "server/src/__tests__/lock.test.ts" in blind["test_cmd"]
        assert "src/impl.ts" not in blind["test_cmd"]   # only the test file runs
        assert blind["has_test"] is True

    def test_a_testless_diff_is_marked_unverifiable(self, tmp_path, diffs):
        # No test file in the diff → no command, has_test False, whatever the
        # agent claimed (gates reads this as unverifiable-no-test).
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/impl.ts")
        vd.commit_blind(s, [BlindItem.from_dict({
            "pr": 1, "head_sha": "h1", "has_test": True, "faithful": True,
            "test_cmd": "pnpm -s test whatever", "reasoning": "r"})])
        blind = s.load_pr(1).verify_signals["blind_adequacy"]
        assert blind["test_cmd"] is None and blind["has_test"] is False

    def test_preserves_the_agent_authored_repro(self, tmp_path, diffs):
        # Signal 4 (the independent repro) stays agent-authored — only the
        # gating red/green command is derived.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "x.test.ts")
        vd.commit_blind(s, [BlindItem.from_dict({
            "pr": 1, "head_sha": "h1", "has_test": True, "faithful": True,
            "repro_command": "node repro.mjs --testTimeout=30000", "reasoning": "r"})])
        blind = s.load_pr(1).verify_signals["blind_adequacy"]
        assert blind["repro_command"] == "node repro.mjs --testTimeout=30000"


class TestDerivedTestCommand:
    """The red/green command is DERIVED from the PR's changed test files by the
    driver, never authored by an agent — so it carries no name filter (the #7524
    class) and no host path (the #587 class). It always runs the whole test file."""

    def test_lists_only_the_test_files_the_diff_touches(self):
        files = vd.derived_test_files(
            ["src/a.ts", "server/src/__tests__/lock.test.ts", "README.md"])
        assert files == ["server/src/__tests__/lock.test.ts"]

    def test_command_runs_the_whole_file_with_no_name_filter(self):
        cmd = vd.derive_test_command(["server/src/__tests__/lock.test.ts"])
        assert cmd is not None
        assert "server/src/__tests__/lock.test.ts" in cmd
        # the #7524 class: no name filter of any dialect may appear
        for flag in ("-t ", "--testNamePattern", "--grep"):
            assert flag not in cmd

    def test_command_names_no_host_path(self):
        # the #587 class: paths come from the diff (repo-relative), never the
        # host scratch/base-clone tree
        cmd = vd.derive_test_command(["x/y.test.ts"])
        assert cmd is not None
        assert str(vd.SCRATCH) not in cmd
        assert "/Users/" not in cmd

    def test_multiple_test_files_all_run(self):
        cmd = vd.derive_test_command(["a.test.ts", "b/c.spec.ts", "src/impl.ts"])
        assert cmd is not None
        assert "a.test.ts" in cmd and "b/c.spec.ts" in cmd

    def test_no_test_file_yields_no_command(self):
        assert vd.derive_test_command(["src/a.ts", "README.md"]) is None
        assert vd.derive_test_command([]) is None

    def test_a_path_with_shell_metacharacters_is_quoted(self):
        cmd = vd.derive_test_command(["weird dir/a.test.ts"])
        assert cmd is not None
        # the space-bearing path must be shell-safe (quoted), never bare
        assert "'weird dir/a.test.ts'" in cmd or '"weird dir/a.test.ts"' in cmd


class TestCanaries:
    """The harness self-test: before trusting a real PR's red→green, prove the
    harness reproduces a known bug, confirms its fix, and — the load-bearing
    mutation — fails when a non-fix patch is applied instead."""

    def test_a_healthy_harness_has_no_problems(self):
        assert vd.canary_checks(bug_reproduces=20, fix_resolves=0,
                                nonfix_rejected=20) == []

    def test_a_harness_that_cannot_produce_a_red_is_flagged(self):
        problems = vd.canary_checks(bug_reproduces=0, fix_resolves=0,
                                    nonfix_rejected=20)
        assert len(problems) == 1 and "did not reproduce" in problems[0]

    def test_a_harness_that_cannot_produce_a_green_is_flagged(self):
        problems = vd.canary_checks(bug_reproduces=20, fix_resolves=20,
                                    nonfix_rejected=20)
        assert len(problems) == 1 and "did not resolve" in problems[0]

    def test_a_harness_rigged_to_pass_is_flagged(self):
        # THE key check: a non-fix patch that still passes means the harness
        # cannot tell a real fix from a no-op — a green proves nothing.
        problems = vd.canary_checks(bug_reproduces=20, fix_resolves=0,
                                    nonfix_rejected=0)
        assert len(problems) == 1 and "NON-fix" in problems[0]

    def test_a_non_sentinel_exit_is_flagged(self):
        # an infra death (137/124) is not a healthy canary either
        assert vd.canary_checks(bug_reproduces=137, fix_resolves=0,
                                nonfix_rejected=20) != []

    def test_run_canaries_runs_red_green_mutant_and_returns_problems(self, monkeypatch):
        phases: list[str] = []

        def fake(phase, image, **kw):
            phases.append(phase)
            return {0: 20, 1: 0, 2: 20}[len(phases) - 1], ""   # red 20, green 0, mutant 20

        monkeypatch.setattr(vd, "run_phase", fake)
        monkeypatch.setattr(vd, "_canary_patch", lambda marker: Path(f"/tmp/{marker}.patch"))
        problems = vd.run_canaries("img:t0", "base1", 0)
        assert problems == []
        assert phases == ["red", "green", "green"]   # bug / fix / non-fix mutant

    def test_run_canaries_surfaces_a_rigged_harness(self, monkeypatch):
        # red 20, green 0, but mutant green ALSO 0 → rigged
        seq = iter([20, 0, 0])
        monkeypatch.setattr(vd, "run_phase", lambda phase, image, **kw: (next(seq), ""))
        monkeypatch.setattr(vd, "_canary_patch", lambda marker: Path(f"/tmp/{marker}.patch"))
        problems = vd.run_canaries("img:t0", "base1", 0)
        assert problems and "NON-fix" in problems[0]


class TestBlindPromptSingleSource:
    def test_prompt_ships_its_placeholders(self):
        for token in ("__PR__", "__TITLE__", "__DIFF_PATH__", "__LINKED_ISSUES__",
                      "__BASE_CLONE__"):
            assert token in vd.BLIND_PROMPT

    def test_prompt_forbids_reading_run_results(self):
        assert "no run has happened" in vd.BLIND_PROMPT

    def test_prompt_asks_for_a_repro_reason_prediction(self):
        assert "expected_repro_signature" in vd.BLIND_PROMPT

    def test_prompt_tells_the_agent_to_bound_its_own_repro(self):
        assert "timeout" in vd.BLIND_PROMPT.lower()

    def test_prompt_says_the_driver_owns_the_command_not_the_agent(self):
        # The agent no longer authors the red/green command (that was the #7524 /
        # #587 root cause) — the driver derives it and runs the whole test file.
        # The prompt must tell the agent it does not choose the command.
        assert "do not choose" in vd.BLIND_PROMPT.lower()
        assert "whole test file" in vd.BLIND_PROMPT.lower()

    def test_prompt_pins_the_repro_command_to_the_sandbox_tree(self):
        # repro_command is the one command the agent still authors; it executes
        # inside the container, whose tree is /work/src — the prompt must say so
        # and forbid host paths (the __BASE_CLONE__ tree is host-side, read-only).
        assert "/work/src" in vd.BLIND_PROMPT
        assert "relative" in vd.BLIND_PROMPT.lower()
        assert "host path" in vd.BLIND_PROMPT.lower()

    def test_prompt_forbids_a_subdirectory_config_flag(self):
        # A `--config <subdir>/...` leaves the runner's root at the working
        # directory, so that config's own relative setupFiles resolve against
        # the repo root and the suite dies at load — an exit the host cannot
        # tell from a reproduction (#3192, #9041).
        assert "--config <subdir>/..." in vd.BLIND_PROMPT
        assert "setupFiles" in vd.BLIND_PROMPT

    def test_prompt_offers_the_repos_own_harness_instead_of_a_rebuilt_one(self):
        # The declines that cost corroboration all read "I would have to
        # reconstruct the harness" (#8873, #9078, #9535, #9549, #9579). The
        # harness is already next to the tests the agent reads, so the prompt
        # must say the repro need not be self-contained and may import it.
        low = vd.BLIND_PROMPT.lower()
        assert "does not have to be self-contained" in low
        assert "existing test directory" in low
        assert "import its existing helpers" in low

    def test_prompt_keeps_declining_honest_but_narrow(self):
        # Null stays the right answer for a defect no shell command reaches,
        # and stays the WRONG answer for one that is merely laborious — the
        # bar this phase exists to hold is "never invent a repro".
        low = vd.BLIND_PROMPT.lower()
        assert "laborious" in low
        assert "never invent a repro" in low


class _FakePopen:
    """Stands in for subprocess.Popen: serves `output` bytes through
    .stdout.read() like a pipe, and records the constructor's argv/kwargs into
    `.seen` so a test can assert on them. `raise_timeout` makes the first
    .wait() call raise subprocess.TimeoutExpired; every call after (real code
    always waits again after killing) returns `returncode`."""

    def __init__(self, output: bytes = b"", returncode: int = 0, *,
                raise_timeout: bool = False) -> None:
        self.output = output
        self.returncode = returncode
        self.raise_timeout = raise_timeout
        self.killed = False
        self.seen: dict = {}
        self.stdout: io.BytesIO | None = None

    def __call__(self, argv: list[str], **kw: object) -> Self:
        self.seen["argv"] = argv
        self.seen["env"] = kw.get("env")
        self.seen["stdout"] = kw.get("stdout")
        self.seen["stderr"] = kw.get("stderr")
        self.stdout = io.BytesIO(self.output)
        return self

    def wait(self, timeout: float | None = None) -> int:
        if self.raise_timeout:
            self.raise_timeout = False
            raise subprocess.TimeoutExpired(self.seen.get("argv", []), timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class TestRunPhase:
    """The host reads each phase's result from the container's EXIT CODE. The
    launcher is never handed os.environ."""

    def test_passes_only_the_allowlisted_env(self, monkeypatch):
        fake = _FakePopen(output=b"ok", returncode=0)
        monkeypatch.setenv("DEPLOYMENT_PRIVATE_KEY", "POISON-PRIVATE-KEY")
        monkeypatch.setattr(vd.subprocess, "Popen", fake)
        vd.run_phase("red", "img:t0", test_cmd="pnpm -s test", base_sha="b", head_sha="h")
        assert "POISON" not in repr(fake.seen["env"])
        assert "DEPLOYMENT_PRIVATE_KEY" not in fake.seen["env"]

    def test_builds_the_launcher_argv(self, monkeypatch):
        fake = _FakePopen(output=b"boom", returncode=20)
        monkeypatch.setattr(vd.subprocess, "Popen", fake)
        rc, tail = vd.run_phase("red", "img:t0", tier=1, test_cmd="pnpm -s test x",
                                base_sha="bbb", head_sha="hhh")
        argv = fake.seen["argv"]
        assert argv[0].endswith("sandbox-run.sh")
        assert "--phase" in argv and argv[argv.index("--phase") + 1] == "red"
        assert argv[argv.index("--image") + 1] == "img:t0"
        assert argv[argv.index("--tier") + 1] == "1"
        assert "--patch" not in argv
        assert (rc, tail) == (20, "boom")
        # stdout and stderr share one pipe, so there is one buffer to bound.
        assert fake.seen["stdout"] == subprocess.PIPE
        assert fake.seen["stderr"] == subprocess.STDOUT

    def test_passes_exclude_file_when_given(self, monkeypatch):
        fake = _FakePopen(output=b"ok", returncode=0)
        monkeypatch.setattr(vd.subprocess, "Popen", fake)
        vd.run_phase("regress", "img:t0", exclude_file=Path("/tmp/x.json"))
        argv = fake.seen["argv"]
        assert "--exclude-file" in argv
        assert argv[argv.index("--exclude-file") + 1] == "/tmp/x.json"

    def test_omits_exclude_file_when_not_given(self, monkeypatch):
        fake = _FakePopen(output=b"ok", returncode=0)
        monkeypatch.setattr(vd.subprocess, "Popen", fake)
        vd.run_phase("red", "img:t0")
        assert "--exclude-file" not in fake.seen["argv"]

    def test_captures_both_streams_and_truncates(self, monkeypatch):
        # stderr=STDOUT merges both streams into the same pipe, so the tail
        # can carry bytes contributed by either marker below.
        merged = ("O" * 5000 + "E" * 5000).encode()
        fake = _FakePopen(output=merged, returncode=20)
        monkeypatch.setattr(vd.subprocess, "Popen", fake)
        _, tail = vd.run_phase("red", "img:t0")
        assert len(tail) == vd.OUTPUT_TAIL_BYTES
        assert "O" in tail and "E" in tail
        assert tail.endswith("E")

    def test_a_timeout_is_an_error_not_a_red(self, monkeypatch):
        fake = _FakePopen(output=b"", returncode=-9, raise_timeout=True)
        monkeypatch.setattr(vd.subprocess, "Popen", fake)
        rc, tail = vd.run_phase("red", "img:t0")
        # Checked against every sentinel, not just PASS and TEST_FAIL.
        sentinels = (gates.SENTINEL_PASS, gates.SENTINEL_PROBE_FAIL,
                    gates.SENTINEL_TEST_FAIL, gates.SENTINEL_PATCH_CONFLICT)
        assert rc not in sentinels
        assert "timed out" in tail
        assert fake.killed

    def test_survives_non_utf8_output_and_still_returns_the_exit_code(
            self, tmp_path, monkeypatch):
        # A real subprocess, not a monkeypatched one, so the decode runs on
        # the real read path.
        monkeypatch.setattr(vd, "SANDBOX", tmp_path)
        script = tmp_path / "sandbox-run.sh"
        script.write_text("#!/bin/sh\nprintf '\\377'\nexit 7\n")
        script.chmod(0o755)

        rc, tail = vd.run_phase("red", "img:t0")

        assert rc == 7
        assert "�" in tail

    def test_bounded_host_memory_regardless_of_output_size(self, tmp_path, monkeypatch):
        # A real subprocess prints far more than OUTPUT_TAIL_BYTES; tracemalloc
        # measures actual Python-level allocation during the call — the
        # host-memory property in question.
        monkeypatch.setattr(vd, "SANDBOX", tmp_path)
        script = tmp_path / "sandbox-run.sh"
        script.write_text(
            "#!/bin/sh\nyes AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA | head -c 8000000\nexit 20\n")
        script.chmod(0o755)

        tracemalloc.start()
        tracemalloc.reset_peak()
        try:
            rc, tail = vd.run_phase("red", "img:t0")
        finally:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

        assert rc == 20
        assert len(tail) == vd.OUTPUT_TAIL_BYTES
        assert peak < 2_000_000   # the container printed 8,000,000 bytes


class TestVerifyPr:
    """The four signals, kept separate. Only the host-observed exit codes decide
    red->green; anything the PR wrote is advisory."""

    def _blind(self, store, n=1, **over):
        vd.commit_blind(store, [BlindItem.from_dict(dict(
            {"pr": n, "head_sha": "h1", "has_test": True, "faithful": True,
             "test_cmd": "pnpm -s test", "expected_red_signature": "AssertionError",
             "reasoning": "r"}, **over))])

    def _phases(self, monkeypatch, regress_exits=(0,), **codes):
        calls = []
        self.patches: dict[str, Path | None] = {}
        self.exclude_files: dict[str, Path | None] = {}
        regress_seq = list(regress_exits)

        def fake(phase, image, **kw):
            calls.append(phase)
            self.patches[phase] = kw.get("patch")
            self.exclude_files[phase] = kw.get("exclude_file")
            if phase == "regress":
                return regress_seq.pop(0), _suite_tail(["src/new.test.ts"], mode="regress")
            return codes.get(phase, 0), f"{phase}-output"
        monkeypatch.setattr(vd, "run_phase", fake)
        monkeypatch.setattr(vd, "fetch_patch", lambda pr, head: Path("/tmp/fake.patch"))
        monkeypatch.setattr(vd, "write_exclude_file",
                            lambda base, baseline: Path("/tmp/fake-exclude.json"))
        # A diff with test hunks by default — most tests exercise the ordinary
        # red->green flow, not the no-test-hunks edge case (covered separately).
        monkeypatch.setattr(vd, "test_only_patch",
                            lambda head, patch: Path("/tmp/fake-test.patch"))
        return calls

    def test_refuses_a_pr_without_a_committed_blind_verdict(self, tmp_path, diffs, monkeypatch):
        # The ordering invariant, enforced in code: no blind verdict, no run.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        calls = self._phases(monkeypatch)
        assert vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG) is None
        assert calls == []

    def test_clean_red_green_records_host_observed_exits(self, tmp_path, diffs, monkeypatch):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        calls = self._phases(monkeypatch, **{"apply-check": 0, "red": 20, "green": 0})
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        # a clean red->green confirms (a second red+green) before regress
        assert calls == ["apply-check", "red", "green", "red", "green", "regress"]
        assert ev["red_green"] == {"apply_exit": 0, "red_exit": 20, "green_exit": 0,
                                   "red_output_tail": "red-output",
                                   "green_output_tail": "green-output",
                                   "red_exit_confirm": 20, "green_exit_confirm": 0}

    def _dirty_phases(self, monkeypatch, green_tails: list[str],
                      red_tail: str | None = None) -> list[str]:
        # red always exits 20 with target+contaminant failing; green exits 20
        # with the given tail per call (first run, then confirm).
        calls: list[str] = []
        greens = iter(green_tails)
        red = red_tail if red_tail is not None else _vitest_tail(
            ["a.test.ts > s > target", "a.test.ts > s > contam"])

        def fake(phase, image, **kw):
            calls.append(phase)
            if phase == "red":
                return 20, red
            if phase == "green":
                return 20, next(greens)
            if phase == "regress":
                return 0, _suite_tail([], mode="regress")
            return 0, f"{phase}-output"
        monkeypatch.setattr(vd, "run_phase", fake)
        monkeypatch.setattr(vd, "fetch_patch", lambda pr, head: Path("/tmp/fake.patch"))
        monkeypatch.setattr(vd, "write_exclude_file",
                            lambda base, baseline: Path("/tmp/fake-exclude.json"))
        monkeypatch.setattr(vd, "test_only_patch",
                            lambda head, patch: Path("/tmp/fake-test.patch"))
        return calls

    def test_a_contained_dirty_green_confirms_and_regresses(self, tmp_path, diffs, monkeypatch):
        # #3718/#3368: green fails only on a test that also failed red — the
        # run proceeds to the confirm and regress legs, with the parsed facts
        # on the record.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        contained = _vitest_tail(["a.test.ts > s > contam"])
        calls = self._dirty_phases(monkeypatch, [contained, contained])
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert calls == ["apply-check", "red", "green", "red", "green", "regress"]
        rg = ev["red_green"]
        assert rg["red_failing"] == ["a.test.ts > s > target", "a.test.ts > s > contam"]
        assert rg["green_failing"] == ["a.test.ts > s > contam"]
        assert rg["green_failing_confirm"] == ["a.test.ts > s > contam"]
        assert rg["failing_in_diff"] == []
        assert ev["regress"]["ran"] is True

    def test_an_uncontained_dirty_green_stops_after_the_first_run(
            self, tmp_path, diffs, monkeypatch):
        # The green failure is not among red's — no confirm, no regress, and
        # the parsed facts stay on the record as evidence.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        calls = self._dirty_phases(
            monkeypatch, [_vitest_tail(["a.test.ts > s > brand-new-failure"])])
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert calls == ["apply-check", "red", "green"]
        assert ev["red_green"]["green_failing"] == ["a.test.ts > s > brand-new-failure"]
        assert ev["regress"] == {"ran": False, "skipped_reason": "red-green-not-clean"}

    def test_a_diff_named_contaminant_stops_after_the_first_run(
            self, tmp_path, diffs, monkeypatch):
        # The green-failing test's title appears in the PR's own test hunks —
        # that is the PR's test still failing, never contamination.
        s = _pinned(tmp_path)
        _pr(s, 1)
        diffs.mkdir(parents=True, exist_ok=True)
        (diffs / "h1.diff").write_text(
            "diff --git a/src/a.test.ts b/src/a.test.ts\n"
            "--- a/src/a.test.ts\n+++ b/src/a.test.ts\n@@ -1 +1,2 @@\n"
            "+  it(\"contam\", async () => {\n")
        self._blind(s)
        calls = self._dirty_phases(monkeypatch, [_vitest_tail(["a.test.ts > s > contam"])])
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert calls == ["apply-check", "red", "green"]
        assert ev["red_green"]["failing_in_diff"] == ["a.test.ts > s > contam"]

    def test_a_confirm_green_outside_the_red_set_skips_regress(
            self, tmp_path, diffs, monkeypatch):
        # First green contained, confirm green fails on a brand-new test — the
        # runs disagree, so the regress leg never runs (gates escalate).
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        calls = self._dirty_phases(monkeypatch, [
            _vitest_tail(["a.test.ts > s > contam"]),
            _vitest_tail(["a.test.ts > s > brand-new-failure"])])
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert calls == ["apply-check", "red", "green", "red", "green"]
        assert ev["red_green"]["green_failing_confirm"] == [
            "a.test.ts > s > brand-new-failure"]
        assert ev["regress"]["ran"] is False

    def test_a_flaky_red_green_confirms_and_skips_regress(self, tmp_path, diffs, monkeypatch):
        # First red->green clean, but the confirm red passes (flaky) — the
        # confirm exits are recorded, regress does not run (gates escalate).
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        # red returns 20 on the first call, 0 on the confirm — a flaky reproduction
        reds = iter([20, 0])
        calls: list[str] = []

        def fake(phase, image, **kw):
            calls.append(phase)
            if phase == "red":
                return next(reds), "red-output"
            return 0, f"{phase}-output"
        monkeypatch.setattr(vd, "run_phase", fake)
        monkeypatch.setattr(vd, "fetch_patch", lambda pr, head: Path("/tmp/fake.patch"))
        monkeypatch.setattr(vd, "test_only_patch",
                            lambda head, patch: Path("/tmp/fake-test.patch"))
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert calls == ["apply-check", "red", "green", "red", "green"]  # no regress
        assert ev["red_green"]["red_exit"] == 20
        assert ev["red_green"]["red_exit_confirm"] == 0
        assert ev["regress"]["ran"] is False

    def test_regress_alone_receives_the_written_exclude_file(
            self, tmp_path, diffs, monkeypatch):
        # write_exclude_file's return value is wired only to the regress
        # phase; the earlier phases run against no exclusion set.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        self._phases(monkeypatch, **{"apply-check": 0, "red": 20, "green": 0})
        vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert self.exclude_files["regress"] == Path("/tmp/fake-exclude.json")
        assert self.exclude_files["apply-check"] is None
        assert self.exclude_files["red"] is None
        assert self.exclude_files["green"] is None

    def test_patch_conflict_short_circuits_before_any_test_runs(self, tmp_path, diffs, monkeypatch):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        calls = self._phases(monkeypatch, **{"apply-check": 30})
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert calls == ["apply-check"]
        assert ev["red_green"]["apply_exit"] == 30
        assert ev["red_green"]["red_exit"] is None

    def test_probe_failure_aborts_the_batch(self, tmp_path, diffs, monkeypatch):
        # Isolation unproven -> no PR code runs anywhere. Never a per-PR outcome.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        self._phases(monkeypatch, **{"apply-check": 10})
        with pytest.raises(vd.ProbeFailure, match="isolation"):
            vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)

    def test_probe_failure_on_a_later_phase_also_aborts(self, tmp_path, diffs, monkeypatch):
        # The raise lives in one shared phase() helper, but each call site
        # (apply-check, repro, red, green) reaches it independently — pin a
        # phase after the first.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        self._phases(monkeypatch, **{"apply-check": 0, "red": 20,
                                     "green": gates.SENTINEL_PROBE_FAIL})
        with pytest.raises(vd.ProbeFailure, match="isolation"):
            vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)

    def test_runs_the_independent_repro_when_blind_supplies_one(self, tmp_path, diffs, monkeypatch):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s, repro_command="node -e 'require(\"./x\")'")
        calls = self._phases(monkeypatch, **{"apply-check": 0, "repro": 20,
                                             "red": 20, "green": 0})
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert "repro" in calls
        assert ev["independent_repro"] == {"ran": True, "exit_code": 20,
                                           "from_linked_issue": False,
                                           "output_tail": "repro-output"}

    def test_a_repro_naming_the_host_scratch_tree_is_flagged_not_run(
            self, tmp_path, diffs, monkeypatch):
        # A repro_command that references the host scratch tree (the pinned-base
        # clone the blind agent reads source from) matches nothing inside the
        # container, so its exit code carries no signal — skipped, no sandbox
        # time spent, and the flag lands in the evidence.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s, repro_command=(
            f"pnpm -s vitest run {vd.SCRATCH}/base/abcdef123456/src/server "
            "--testTimeout=30000"))
        calls = self._phases(monkeypatch, **{"apply-check": 0, "red": 20, "green": 0})
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert "repro" not in calls
        assert ev["independent_repro"]["ran"] is False
        assert ev["independent_repro"]["exit_code"] is None
        assert ev["independent_repro"]["skipped_reason"] == "host-path-in-command"

    def test_a_repro_targeting_the_prs_own_test_is_flagged_not_run(
            self, tmp_path, diffs, monkeypatch):
        # #9041/#7936: the repro phase runs the pinned base with no patch
        # applied, so a repro_command naming a test the diff itself introduces
        # targets nothing that exists there — skipped, no sandbox time spent.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s, repro_command='npx vitest run src/a.test.ts -t "anything"')
        calls = self._phases(monkeypatch, **{"apply-check": 0, "red": 20, "green": 0})
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert "repro" not in calls
        assert ev["independent_repro"]["ran"] is False
        assert ev["independent_repro"]["exit_code"] is None
        assert ev["independent_repro"]["skipped_reason"] == "repro-targets-pr-test"

    def test_skips_the_repro_when_blind_supplies_none(self, tmp_path, diffs, monkeypatch):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        calls = self._phases(monkeypatch, **{"apply-check": 0, "red": 20, "green": 0})
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert "repro" not in calls
        assert ev["independent_repro"]["ran"] is False

    def test_no_test_file_in_the_diff_skips_the_sandbox_entirely(self, tmp_path, diffs, monkeypatch):
        # A diff with no test file derives no command — unverifiable-no-test, no
        # sandbox time spent.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/impl.ts")   # no test file
        self._blind(s)
        calls = self._phases(monkeypatch)
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert calls == []
        assert ev["red_green"]["red_exit"] is None

    def test_red_applies_the_test_only_patch_when_the_diff_has_test_hunks(
            self, tmp_path, diffs, monkeypatch):
        # #551: a regression test a PR adds does not exist on pinned main. Red
        # must apply just the test hunks to run it — the fix under test here.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        calls = self._phases(monkeypatch, **{"apply-check": 0, "red": 20, "green": 0})
        vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert calls == ["apply-check", "red", "green", "red", "green", "regress"]
        assert self.patches["apply-check"] == Path("/tmp/fake.patch")
        assert self.patches["green"] == Path("/tmp/fake.patch")
        assert self.patches["red"] == Path("/tmp/fake-test.patch")
        assert self.patches["red"] != self.patches["apply-check"]

    def test_from_linked_issue_no_longer_skips_the_test_patch(
            self, tmp_path, diffs, monkeypatch):
        # from_linked_issue is informational now: the red/green command derives
        # from the diff's test files, so red always applies the test-only patch.
        # There is no separate unpatched linked-issue red path.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s, from_linked_issue=True)
        calls = self._phases(monkeypatch, **{"apply-check": 0, "red": 20, "green": 0})
        vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert calls == ["apply-check", "red", "green", "red", "green", "regress"]
        assert self.patches["red"] == Path("/tmp/fake-test.patch")  # test-only patch applied

    def test_no_test_hunks_in_the_diff_short_circuits_before_red(
            self, tmp_path, diffs, monkeypatch):
        # has_test claimed a PR-authored test, but the diff carries no test hunk
        # to apply — there is no legitimate red to produce. Stay unverifiable
        # rather than silently running red unpatched (the bug this issue fixes).
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        calls = self._phases(monkeypatch, **{"apply-check": 0})
        monkeypatch.setattr(vd, "test_only_patch", lambda head, patch: None)
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert calls == ["apply-check"]
        assert ev["red_green"]["red_exit"] is None
        assert ev["red_green"]["green_exit"] is None
        assert ev["red_green"]["no_test_hunks"] is True

    def test_no_suite_config_skips_regress_after_a_clean_confirm(
            self, tmp_path, diffs, monkeypatch):
        # suite_config None is the pin's declaration that the repository has no
        # full-suite contract: the clean confirmed red->green records the leg
        # as deliberately skipped rather than booting a suite that cannot run.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        calls = self._phases(monkeypatch, **{"apply-check": 0, "red": 20, "green": 0})
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=None)
        assert calls == ["apply-check", "red", "green", "red", "green"]
        assert ev["regress"] == {"ran": False, "skipped_reason": "no-suite-config"}

    def test_clean_red_green_runs_regress_once_when_it_passes(self, tmp_path, diffs, monkeypatch):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        calls = self._phases(monkeypatch, regress_exits=(0,),
                             **{"apply-check": 0, "red": 20, "green": 0})
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, ["base-fail.test.ts"], suite_config=SUITE_CFG)
        assert calls.count("regress") == 1
        assert ev["regress"] == {"ran": True, "exit_first": 0, "exit_confirm": None,
                                 "confirmed": False, "flake": False,
                                 "excluded_count": 1, "new_failures": []}

    def test_a_failing_regress_is_confirmed_by_one_fresh_run(self, tmp_path, diffs, monkeypatch):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        calls = self._phases(monkeypatch, regress_exits=(20, 20),
                             **{"apply-check": 0, "red": 20, "green": 0})
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert calls.count("regress") == 2
        assert ev["regress"]["confirmed"] is True
        assert ev["regress"]["flake"] is False
        assert ev["regress"]["new_failures"] == ["src/new.test.ts"]

    def test_a_20_then_0_is_a_flake_not_a_regression(self, tmp_path, diffs, monkeypatch):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        self._phases(monkeypatch, regress_exits=(20, 0),
                     **{"apply-check": 0, "red": 20, "green": 0})
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert ev["regress"]["confirmed"] is False
        assert ev["regress"]["flake"] is True
        # the first run's names stay recorded as advisory evidence of the flake
        assert ev["regress"]["new_failures"] == ["src/new.test.ts"]

    def test_regress_is_skipped_when_red_green_is_not_clean(self, tmp_path, diffs, monkeypatch):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        calls = self._phases(monkeypatch, **{"apply-check": 0, "red": 0, "green": 0})
        ev = vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert "regress" not in calls
        assert ev["regress"] == {"ran": False, "skipped_reason": "red-green-not-clean"}

    def test_regress_applies_the_full_patch(self, tmp_path, diffs, monkeypatch):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        self._phases(monkeypatch, **{"apply-check": 0, "red": 20, "green": 0})
        vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)
        assert self.patches["regress"] == Path("/tmp/fake.patch")

    def test_a_probe_failure_in_regress_aborts_the_batch(self, tmp_path, diffs, monkeypatch):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._blind(s)
        self._phases(monkeypatch, regress_exits=(gates.SENTINEL_PROBE_FAIL,),
                     **{"apply-check": 0, "red": 20, "green": 0})
        with pytest.raises(vd.ProbeFailure, match="isolation"):
            vd.verify_pr(s.load_pr(1), "img:t0", "base1", 0, [], suite_config=SUITE_CFG)


class TestTestOnlyPatch:
    """#551: the patch red applies is the diff narrowed to test hunks — written
    under SCRATCH (Colima's virtiofs shares only $HOME), None when the diff adds
    no test."""

    def _full_patch(self, tmp_path, *paths):
        p = tmp_path / "full.patch"
        p.write_text("".join(
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1,2 @@\n+x\n"
            for path in paths))
        return p

    def test_keeps_only_the_test_hunks(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
        full = self._full_patch(tmp_path, "src/a.ts", "src/a.test.ts")

        out = vd.test_only_patch("h1", full)

        assert out is not None
        text = out.read_text()
        assert "src/a.test.ts" in text
        assert "diff --git a/src/a.ts b/src/a.ts" not in text

    def test_written_under_scratch_keyed_by_head_sha(self, tmp_path, monkeypatch):
        scratch = tmp_path / "scratch"
        monkeypatch.setattr(vd, "SCRATCH", scratch)
        full = self._full_patch(tmp_path, "src/a.test.ts")

        out = vd.test_only_patch("deadbeef", full)

        assert out == scratch / "patches" / "deadbeef.test.patch"
        assert out.exists()

    def test_none_when_the_diff_has_no_test_hunks(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
        full = self._full_patch(tmp_path, "src/a.ts", "README.md")

        assert vd.test_only_patch("h1", full) is None

    def test_none_on_an_empty_diff(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
        full = tmp_path / "full.patch"
        full.write_text("")

        assert vd.test_only_patch("h1", full) is None


def _blind_committed(s, n=1):
    vd.commit_blind(s, [BlindItem.from_dict(
        {"pr": n, "head_sha": "h1", "has_test": True, "faithful": True,
         "test_cmd": "pnpm -s test", "expected_red_signature": "x",
         "reasoning": "r"})])


def _clean_red_green() -> dict:
    return {"apply_exit": 0, "red_exit": 20, "green_exit": 0,
            "red_output_tail": "", "green_output_tail": "",
            "red_exit_confirm": 20, "green_exit_confirm": 0}


class TestRegressFailClosed:
    def test_commit_writes_regressed_with_the_advisory_finding(self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        _blind_committed(s)
        rec = s.load_pr(1)
        signals = dict(rec.verify_signals)
        signals["red_green"] = _clean_red_green()
        signals["regress"] = {"ran": True, "exit_first": 20, "exit_confirm": 20,
                              "confirmed": True, "flake": False,
                              "excluded_count": 2, "new_failures": ["src/x.test.ts"]}
        s.edit_pr(1).record_verify(None, signals, base_sha="base1", head_sha="h1")
        ok, held, errs = vd.commit_outcomes(s, [JudgeItem.from_dict(
            {"pr": 1, "red_reason_match": {"matches": True, "confidence": "high"}})])
        assert (ok, held, errs) == (1, [], [])
        rec = s.load_pr(1)
        assert rec.verify_outcome == "regressed"
        assert any(f.get("signal") == "regress" and f.get("tests") == ["src/x.test.ts"]
                   for f in rec.verify_findings)


class TestPinnedTier:
    """The tier is a property of the base image, so verify_pr reads it from the
    pin prepare-base wrote. The two cannot name different images."""

    def test_an_unpinned_store_refuses_to_name_a_tier(self, tmp_path):
        with pytest.raises(RuntimeError, match="prepare-base"):
            vd.pinned_tier(Store(tmp_path))


class TestFetchPatch:
    """The one site in this module that shells out to `gh` with the operator's
    own read-only credentials reachable in the environment."""

    def test_poisoned_secrets_never_reach_the_launcher(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vd, "SCRATCH", tmp_path)
        monkeypatch.setenv("DEPLOYMENT_PRIVATE_KEY", "POISON-PRIVATE-KEY")
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            seen["env"] = kw.get("env")
            return subprocess.CompletedProcess(argv, 0, stdout="diff content", stderr="")

        monkeypatch.setattr(vd.subprocess, "run", fake_run)
        vd.fetch_patch(1, "h1")
        assert "POISON" not in repr(seen["env"])
        assert "DEPLOYMENT_PRIVATE_KEY" not in seen["env"]

    def test_retries_a_transient_failure_then_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vd, "SCRATCH", tmp_path)
        sleeps: list[float] = []
        monkeypatch.setattr(vd.time, "sleep", sleeps.append)
        calls = {"n": 0}

        def fake_run(argv, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                return subprocess.CompletedProcess(argv, 1, stdout="",
                                                   stderr="HTTP 500")
            return subprocess.CompletedProcess(argv, 0, stdout="diff content",
                                               stderr="")

        monkeypatch.setattr(vd.subprocess, "run", fake_run)
        path = vd.fetch_patch(1, "h500")
        assert path.read_text() == "diff content"
        assert calls["n"] == 3
        assert len(sleeps) == 2

    def test_raises_fetch_failure_when_every_attempt_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vd, "SCRATCH", tmp_path)
        sleeps: list[float] = []
        monkeypatch.setattr(vd.time, "sleep", sleeps.append)

        def fake_run(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 500")

        monkeypatch.setattr(vd.subprocess, "run", fake_run)
        with pytest.raises(vd.FetchFailure, match="HTTP 500"):
            vd.fetch_patch(1, "hdead")
        assert len(sleeps) == 2

    def test_fetch_failure_is_a_runtime_error(self):
        assert issubclass(vd.FetchFailure, RuntimeError)


class TestAuthoredAttemptNote:
    def test_skip_reason(self):
        note = vd.authored_attempt_note(
            {"attempted": True, "skipped_reason": "path-not-a-test-path"}, {})
        assert note is not None and "path-not-a-test-path" in note

    def test_red_never_failed(self):
        note = vd.authored_attempt_note(
            {"attempted": True, "test_cmd": "c", "red_exit": 0}, {})
        assert note is not None and "did not fail" in note

    def test_green_still_failing(self):
        note = vd.authored_attempt_note(
            {"attempted": True, "test_cmd": "c", "red_exit": 20, "green_exit": 20}, {})
        assert note is not None and "still failed" in note

    def test_flaky_confirm(self):
        note = vd.authored_attempt_note(
            {"attempted": True, "test_cmd": "c", "red_exit": 20, "green_exit": 0,
             "red_exit_confirm": 0, "green_exit_confirm": 0}, {})
        assert note is not None and "confirm" in note

    def test_wrong_reason(self):
        note = vd.authored_attempt_note(
            {"attempted": True, "test_cmd": "c", "red_exit": 20, "green_exit": 0,
             "red_exit_confirm": 20, "green_exit_confirm": 0},
            {"matches": False, "confidence": "high"})
        assert note is not None and "reason" in note

    def test_no_attempt_is_none(self):
        assert vd.authored_attempt_note({}, {}) is None


class TestCommitOutcomes:
    """The outcome comes from gates.verify_outcome, never from the agent. An
    error HOLDS — no section is written, so a failed run can never present as a
    clean bill."""

    def _ran(self, store, n=1, *, faithful=True, red=20, green=0, apply_exit=0,
             test_cmd="pnpm -s test", repro_cmd=None, repro_exit=None):
        vd.commit_blind(store, [BlindItem.from_dict({
            "pr": n, "head_sha": "h1", "has_test": True, "faithful": faithful,
            "expected_red_signature": "AssertionError", "reasoning": "r"})])
        rec = store.load_pr(n)
        signals = dict(rec.verify_signals)
        # commit_blind now DERIVES the stored command; set it directly here so a
        # test can exercise the gate belt (e.g. a residual name filter that
        # gates.vacuous_name_filter must still catch even though the driver no
        # longer emits one).
        blind = dict(signals["blind_adequacy"])
        blind["test_cmd"] = test_cmd
        if repro_cmd is not None:
            blind["repro_command"] = repro_cmd
        signals["blind_adequacy"] = blind
        # A clean red->green confirms; mirror the first run's exits on the confirm
        # so these tests exercise a confirmed run (the confirm's own behavior is
        # tested separately). A dirty first run runs no confirm (None).
        clean = red == 20 and green == 0
        signals["red_green"] = {"apply_exit": apply_exit, "red_exit": red,
                                "green_exit": green, "red_output_tail": "AssertionError: x",
                                "green_output_tail": "",
                                "red_exit_confirm": red if clean else None,
                                "green_exit_confirm": green if clean else None}
        if repro_cmd is not None:
            signals["independent_repro"] = {
                "ran": True, "exit_code": repro_exit, "from_linked_issue": False,
                "output_tail": "No test files found, exiting with code 1"}
        else:
            signals["independent_repro"] = {"ran": False, "exit_code": None,
                                            "from_linked_issue": False, "output_tail": ""}
        # A completed, clean regress leg — the fail-closed rule only cares that a
        # clean red->green carries one; these tests are exercising other outcomes.
        signals["regress"] = {"ran": True, "exit_first": 0, "exit_confirm": None,
                              "confirmed": False, "flake": False, "excluded_count": 0,
                              "new_failures": []}
        store.edit_pr(n).record_verify(None, signals, base_sha="base1", head_sha="h1")

    def _judge(self, n=1, matches=True, confidence="high") -> JudgeItem:
        return JudgeItem.from_dict({
            "pr": n, "red_reason_match": {"matches": matches, "confidence": confidence,
                                          "reasoning": "same assertion"},
            "findings": []})

    def _ran_authored(self, store, n=1, *, authored: dict):
        # The no-test blind + the authored lane's committed record — the state
        # verify_pr's authored branch produces for a PR whose diff ships no test.
        signals = {
            "blind_adequacy": {"has_test": False, "faithful": False,
                               "test_cmd": None, "requires_live_agent": False},
            "red_green": {"apply_exit": 0, "red_exit": None, "green_exit": None,
                          "red_output_tail": "", "green_output_tail": "",
                          "red_exit_confirm": None, "green_exit_confirm": None},
            "authored_test": authored,
        }
        store.edit_pr(n).record_verify(None, signals, base_sha="base1", head_sha="h1")

    def test_authored_lane_confirmed_clean_yields_agent_verified(self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/impl.ts")
        self._ran_authored(s, authored={
            "attempted": True, "can_author": True, "test_cmd": "npx vitest run x.test.ts",
            "files": [{"path": "x.test.ts", "contents": "t\n"}],
            "red_exit": 20, "green_exit": 0, "red_exit_confirm": 20, "green_exit_confirm": 0,
            "red_output_tail": "", "green_output_tail": ""})
        ok, held, errs = vd.commit_outcomes(s, [self._judge()])
        assert (ok, held, errs) == (1, [], [])
        got = s.load_pr(1)
        assert got.verify_outcome == "agent-verified"
        assert [f for f in got.verify_findings if f.get("signal") == "authored-test"] == []

    def test_authored_lane_skip_reason_records_a_finding(self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/impl.ts")
        self._ran_authored(s, authored={
            "attempted": True, "can_author": True, "test_cmd": None,
            "skipped_reason": "path-not-a-test-path"})
        ok, held, errs = vd.commit_outcomes(s, [self._judge()])
        assert (ok, held, errs) == (1, [], [])
        got = s.load_pr(1)
        assert got.verify_outcome == "unverifiable-no-test"
        finding = [f for f in got.verify_findings if f.get("signal") == "authored-test"]
        assert len(finding) == 1
        assert "path-not-a-test-path" in finding[0]["note"]

    def test_clean_signals_yield_verified_fix(self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran(s)
        ok, held, errs = vd.commit_outcomes(s, [self._judge()])
        assert (ok, held, errs) == (1, [], [])
        got = s.load_pr(1)
        assert got.verify_outcome == "verified-fix"
        assert got.disposition == "merge"

    def test_a_failed_lane_demotes_the_outcome_and_records_a_finding(self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran(s)
        rec = s.load_pr(1)
        signals = dict(rec.verify_signals)
        signals["lanes"] = {"compile": {"cmd": "c", "exit": 20, "ok": False,
                                        "error_excerpt": "error TS1: x"}}
        s.edit_pr(1).record_verify(None, signals, base_sha="base1", head_sha="h1")
        ok, held, errs = vd.commit_outcomes(s, [self._judge()])
        assert (ok, held, errs) == (1, [], [])
        got = s.load_pr(1)
        assert got.verify_outcome == "regressed"
        assert any(f.get("signal") == "lane" for f in got.verify_findings)

    def test_repro_reason_match_is_stored_but_does_not_gate_the_outcome(self, tmp_path, diffs):
        # Signal 4 stays corroborating evidence: a repro that failed for the
        # wrong reason (e.g. a timeout, not the claimed assertion) is recorded
        # on the PR, but verified-fix still follows from signals 1-3 alone.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran(s)
        judge = JudgeItem.from_dict({
            "pr": 1, "red_reason_match": {"matches": True, "confidence": "high",
                                          "reasoning": "same assertion"},
            "repro_reason_match": {"applicable": True, "matches": False,
                                   "confidence": "high",
                                   "reasoning": "repro timed out, not an assertion failure"},
            "findings": []})
        ok, held, errs = vd.commit_outcomes(s, [judge])
        assert (ok, held, errs) == (1, [], [])
        got = s.load_pr(1)
        assert got.verify_outcome == "verified-fix"
        assert got.verify_signals["repro_reason_match"] == {
            "applicable": True, "matches": False, "confidence": "high",
            "reasoning": "repro timed out, not an assertion failure"}

    def test_repro_rejected_records_a_dedicated_finding(self, tmp_path, diffs):
        # The blind lane rejected the repro pre-run and committed the verdict
        # with the repro fields nulled and repro_rejected recording why; the
        # outcome is untouched and the rejection surfaces as a finding.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran(s)
        rec = s.load_pr(1)
        signals = dict(rec.verify_signals)
        blind = dict(signals["blind_adequacy"])
        blind["repro_command"] = None
        blind["repro_rejected"] = ("repro_command targets a test the PR itself "
                                   "introduces ('src/x.test.ts')")
        signals["blind_adequacy"] = blind
        s.edit_pr(1).record_verify(None, signals, base_sha="base1", head_sha="h1")
        ok, held, errs = vd.commit_outcomes(s, [self._judge()])
        assert (ok, held, errs) == (1, [], [])
        got = s.load_pr(1)
        assert got.verify_outcome == "verified-fix"
        finding = [f for f in got.verify_findings
                   if f.get("signal") == "repro-rejected"]
        assert len(finding) == 1
        assert "rejected pre-run" in finding[0]["note"]
        assert "src/x.test.ts" in finding[0]["note"]

    def test_blind_disagreement_escalates_even_when_the_agent_says_it_matches(
            self, tmp_path, diffs):
        # THE rule. The agent has no outcome field and cannot resolve this.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran(s, faithful=False)
        ok, held, errs = vd.commit_outcomes(s, [self._judge(matches=True,
                                                            confidence="high")])
        assert ok == 1
        got = s.load_pr(1)
        assert got.verify_outcome == "escalate"
        assert got.disposition == "needs-human"

    def test_escalate_reopens_every_cluster_the_pr_belongs_to(self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        rec = s.load_pr(1).raw
        rec["cluster"] = {"ids": [7, 9], "checked_at": NOW, "against_head_sha": "h1"}
        s.save_pr(rec)
        for cid in (7, 9):
            s.create_cluster(cid, "root", outcome="merge-ready")
        self._ran(s, faithful=False)
        vd.commit_outcomes(s, [self._judge()])
        assert s.load_cluster(7).outcome is None
        assert s.load_cluster(9).outcome is None

    def test_red_that_passed_on_main_is_not_verified(self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran(s, red=0)
        vd.commit_outcomes(s, [self._judge()])
        got = s.load_pr(1)
        assert got.verify_outcome == "not-verified"
        assert got.disposition == "request-changes"

    def test_a_vacuous_red_with_a_name_filter_records_a_dedicated_finding(
            self, tmp_path, diffs):
        # The #7524 shape: the blind agent's -t filter matched no test names, so
        # vitest skipped everything and exited 0 on both red and green. The
        # outcome is still not-verified (no verdict from output tails), but the
        # finding names the harness cause so the app story is precise.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran(s, red=0, test_cmd='npx vitest run x.test.ts -t "release preserves status"')
        ok, held, errs = vd.commit_outcomes(s, [self._judge(matches=False)])
        assert (ok, held, errs) == (1, [], [])
        got = s.load_pr(1)
        assert got.verify_outcome == "not-verified"
        vac = [f for f in got.verify_findings if f.get("signal") == "vacuous-filter"]
        assert len(vac) == 1
        assert "-t" in vac[0]["note"]
        assert vac[0]["test_cmd"] == 'npx vitest run x.test.ts -t "release preserves status"'

    def test_a_genuine_red_with_a_name_filter_records_no_vacuous_finding(
            self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran(s, test_cmd='npx vitest run x.test.ts -t "static title"')
        ok, held, errs = vd.commit_outcomes(s, [self._judge()])
        assert (ok, held, errs) == (1, [], [])
        got = s.load_pr(1)
        assert got.verify_outcome == "verified-fix"
        assert [f for f in got.verify_findings if f.get("signal") == "vacuous-filter"] == []

    def test_a_misrooted_repro_config_records_a_dedicated_finding(
            self, tmp_path, diffs):
        # The #3192 shape: --config names a subdirectory config the command
        # never makes the runner's root, so that config's own relative
        # setupFiles resolve against the repo root and the suite dies at load —
        # an exit indistinguishable from a genuine reproduction. The finding
        # names the harness cause; the red->green outcome is untouched.
        cmd = ('npx vitest run --config server/vitest.config.ts '
               'src/__tests__/repro-legacy-refs.test.ts --testTimeout=20000')
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran(s, repro_cmd=cmd, repro_exit=20)
        ok, held, errs = vd.commit_outcomes(s, [self._judge()])
        assert (ok, held, errs) == (1, [], [])
        got = s.load_pr(1)
        assert got.verify_outcome == "verified-fix"
        vac = [f for f in got.verify_findings
               if f.get("signal") == "misrooted-repro-config"]
        assert len(vac) == 1
        assert "server/vitest.config.ts" in vac[0]["note"]
        assert vac[0]["repro_command"] == cmd

    def test_a_repo_root_repro_invocation_records_no_misrooted_finding(
            self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran(s, repro_cmd="npx vitest run server/src/x.test.ts "
                               "--testTimeout=20000", repro_exit=20)
        ok, held, errs = vd.commit_outcomes(s, [self._judge()])
        assert (ok, held, errs) == (1, [], [])
        got = s.load_pr(1)
        assert [f for f in got.verify_findings
                if f.get("signal") == "misrooted-repro-config"] == []

    def test_patch_conflict_is_needs_rebase(self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran(s, apply_exit=30, red=None, green=None)
        vd.commit_outcomes(s, [self._judge()])
        assert s.load_pr(1).verify_outcome == "needs-rebase"

    def _ran_contained(self, store, n=1, *, green_failing=None):
        # The contained-dirty-green shape: both legs exit 20, the parsed facts
        # show green failing only on a test red also failed on, confirmed.
        self._ran(store, n)
        rec = store.load_pr(n)
        signals = dict(rec.verify_signals)
        contam = green_failing if green_failing is not None else ["a.test.ts > s > contam"]
        signals["red_green"] = {
            "apply_exit": 0, "red_exit": 20, "green_exit": 20,
            "red_exit_confirm": 20, "green_exit_confirm": 20,
            "red_output_tail": "AssertionError: x", "green_output_tail": "x",
            "red_failing": ["a.test.ts > s > target", "a.test.ts > s > contam"],
            "green_failing": contam, "green_failing_confirm": contam,
            "failing_in_diff": []}
        store.edit_pr(n).record_verify(None, signals, base_sha="base1", head_sha="h1")

    def test_a_contained_dirty_green_verifies_with_a_dedicated_finding(
            self, tmp_path, diffs):
        # The #3718/#3368 shape after the parse: green failed only on a test
        # that also failed red. Verified, with the contamination named for the
        # operator.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran_contained(s)
        ok, held, errs = vd.commit_outcomes(s, [self._judge()])
        assert (ok, held, errs) == (1, [], [])
        got = s.load_pr(1)
        assert got.verify_outcome == "verified-fix"
        dirty = [f for f in got.verify_findings if f.get("signal") == "dirty-green"]
        assert len(dirty) == 1
        assert dirty[0]["tests"] == ["a.test.ts > s > contam"]
        assert "also failed" in dirty[0]["note"]

    def test_an_uncontained_dirty_green_records_no_dirty_green_finding(
            self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran_contained(s, green_failing=["a.test.ts > s > brand-new-failure"])
        ok, held, errs = vd.commit_outcomes(s, [self._judge()])
        assert (ok, held, errs) == (1, [], [])
        got = s.load_pr(1)
        assert got.verify_outcome == "not-verified"
        assert [f for f in got.verify_findings if f.get("signal") == "dirty-green"] == []

    def test_a_non_sentinel_exit_holds_and_writes_no_outcome(self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran(s, red=137)          # killed container
        before = copy.deepcopy(s.load_pr(1).raw)
        ok, held, errs = vd.commit_outcomes(s, [self._judge()])
        assert (ok, held) == (0, [1])
        assert s.load_pr(1).verify_outcome is None   # still unjudged; re-queues
        # The hold path writes no section at all: the stored record, reloaded
        # fresh, is identical to the snapshot taken before the call.
        assert s.load_pr(1).raw == before

    def test_an_unusable_rating_is_an_error_not_a_re_running_hold(self, tmp_path, diffs):
        # The run itself is sound, so re-booting the sandbox is not the fix — the
        # judge is. It reports as an error rather than a hold, so `held` stays empty.
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran(s)
        ok, held, errs = vd.commit_outcomes(s, [JudgeItem.from_dict({"pr": 1})])
        assert (ok, held) == (0, [])
        assert len(errs) == 1 and "judge" in errs[0]
        assert s.load_pr(1).verify_outcome is None

    def test_findings_are_stored(self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran(s)
        vd.commit_outcomes(s, [JudgeItem.from_dict({
            "pr": 1, "red_reason_match": {"matches": False, "confidence": "high",
                                          "reasoning": "ImportError, not the claimed bug"},
            "findings": [{"title": "red is an ImportError for a helper the PR adds",
                          "detail": "d"}]})])
        got = s.load_pr(1)
        assert got.verify_outcome == "not-verified"
        assert got.verify_findings[0]["title"].startswith("red is an ImportError")

    def test_an_agent_supplied_outcome_is_ignored(self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        self._ran(s, red=0)                        # host says: no repro
        vd.commit_outcomes(s, [JudgeItem.from_dict({
            "pr": 1, "outcome": "verified-fix",    # the agent lies
            "red_reason_match": {"matches": True, "confidence": "high", "reasoning": "r"},
            "findings": []})])
        assert s.load_pr(1).verify_outcome == "not-verified"

    def test_pr_without_a_run_is_an_error(self, tmp_path, diffs):
        s = _pinned(tmp_path)
        _pr(s, 1); _diff(diffs, "h1", "src/a.test.ts")
        ok, held, errs = vd.commit_outcomes(s, [self._judge()])
        assert ok == 0 and errs


class TestEscalateSurvivesReanalysis:
    """The escalate path end to end. An escalate reopens the PR's clusters so
    ANALYZE revisits them; ANALYZE reads no verify signal, so it re-proposes the
    same merge. That proposal must self-demote through the gate."""

    def _escalated(self, store, diffs, n=1, cluster=7):
        _pr(store, n); _diff(diffs, "h1", "src/a.test.ts")
        rec = store.load_pr(n).raw
        rec["cluster"] = {"ids": [cluster], "checked_at": NOW, "against_head_sha": "h1"}
        store.save_pr(rec)
        store.create_cluster(cluster, "root", outcome="merge-ready").set_members([n])
        vd.commit_blind(store, [BlindItem.from_dict({
            "pr": n, "head_sha": "h1", "has_test": True, "faithful": False,
            "test_cmd": "pnpm -s test", "expected_red_signature": "AssertionError",
            "reasoning": "the test asserts on a marker the fix itself creates"})])
        signals = dict(store.load_pr(n).verify_signals)
        signals["red_green"] = {"apply_exit": 0, "red_exit": 20, "green_exit": 0,
                                "red_output_tail": "", "green_output_tail": "",
                                "red_exit_confirm": 20, "green_exit_confirm": 0}
        signals["regress"] = {"ran": True, "exit_first": 0, "exit_confirm": None,
                              "confirmed": False, "flake": False, "excluded_count": 0,
                              "new_failures": []}
        store.edit_pr(n).record_verify(None, signals, base_sha="base1", head_sha="h1")
        vd.commit_outcomes(store, [JudgeItem.from_dict({
            "pr": n, "red_reason_match": {"matches": True, "confidence": "high",
                                          "reasoning": "r"}, "findings": []})])

    def _analysis_file(self, out_dir, cid, n):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"cluster-{cid}.json").write_text(json.dumps(
            {"cluster_id": cid, "outcome": "merge-ready", "rationale": "the canonical fix",
             "prs": [{"pr": n, "disposition": "merge", "rationale": "clean"}]}))

    def test_reanalysis_of_an_escalated_pr_self_demotes(self, tmp_path, diffs):
        s = _pinned(tmp_path)
        self._escalated(s, diffs)
        assert s.load_pr(1).verify_outcome == "escalate"
        assert s.load_pr(1).disposition == "needs-human"
        assert s.load_cluster(7).outcome is None      # reopened for ANALYZE

        errs = ad.commit_analysis(s, {
            "cluster_id": 7, "outcome": "merge-ready", "rationale": "the canonical fix",
            "prs": [{"pr": 1, "disposition": "merge", "rationale": "clean"}]})

        assert errs == []
        got = s.load_pr(1)
        assert got.disposition == "needs-human"       # self-demoted, not raised
        assert got.verify_outcome == "escalate"       # the escalation survives

    def test_a_cluster_behind_an_escalated_one_still_lands(self, tmp_path, diffs, monkeypatch):
        # The blast radius of the raise: commit_analysis writes proposals + the
        # cluster outcome BEFORE reconciling, and store.batch() is not
        # transactional, so an exception here strands every later file.
        s = _pinned(tmp_path)
        self._escalated(s, diffs)
        _pr(s, 2, head="h2"); _diff(diffs, "h2", "src/b.test.ts")
        rec = s.load_pr(2).raw
        rec["cluster"] = {"ids": [8], "checked_at": NOW, "against_head_sha": "h2"}
        s.save_pr(rec)
        s.create_cluster(8, "other", outcome=None).set_members([2])
        out = tmp_path / "analyze-out"
        self._analysis_file(out, 7, 1)               # sorts first
        self._analysis_file(out, 8, 2)
        monkeypatch.setattr(ad, "backfill_salvage_items", lambda store, today: 0)

        r = ad.commit_analyses_dir(s, out)

        assert r["failed"] == 0 and r["errors"] == []
        assert s.load_pr(1).disposition == "needs-human"
        assert s.load_cluster(8).outcome == "merge-ready"   # never stranded
        assert s.load_pr(2).disposition == "merge"


class TestJudgePromptSingleSource:
    def test_prompt_ships_its_placeholders(self):
        for token in ("__PR__", "__EVIDENCE__", "__EXPECTED_RED__", "__EXPECTED_REPRO__"):
            assert token in vd.JUDGE_PROMPT

    def test_prompt_marks_the_output_as_untrusted(self):
        assert "attacker" in vd.JUDGE_PROMPT.lower()

    def test_prompt_never_asks_for_an_outcome(self):
        assert "outcome" not in vd.JUDGE_PROMPT.lower()

    def test_prompt_asks_for_a_repro_reason_match(self):
        assert "repro_reason_match" in vd.JUDGE_PROMPT


class TestAuthorItem:
    """The authoring agent's envelope: booleans only as actual bools, files
    filtered to well-formed {path, contents} string pairs."""

    def test_from_dict_happy_path(self):
        it = wire.AuthorItem.from_dict({
            "pr": 7, "can_author": True,
            "files": [{"path": "ui/src/a.test.ts", "contents": "x"}],
            "expected_red_signature": "AssertionError: toast",
            "confidence": "high", "reasoning": "why"})
        assert it.pr == 7 and it.can_author is True
        assert it.files == [{"path": "ui/src/a.test.ts", "contents": "x"}]
        assert it.expected_red_signature == "AssertionError: toast"

    def test_string_bool_reads_as_false(self):
        it = wire.AuthorItem.from_dict({"pr": 7, "can_author": "true", "files": []})
        assert it.can_author is False

    def test_malformed_file_entries_are_dropped(self):
        it = wire.AuthorItem.from_dict({
            "pr": 7, "can_author": True,
            "files": [{"path": "a.test.ts"}, "junk", {"path": 3, "contents": "x"},
                      {"path": "b.test.ts", "contents": "ok"}]})
        assert it.files == [{"path": "b.test.ts", "contents": "ok"}]

    def test_to_signal_shape(self):
        it = wire.AuthorItem.from_dict({
            "pr": 7, "can_author": True,
            "files": [{"path": "a.test.ts", "contents": "x"}],
            "expected_red_signature": "boom", "confidence": "low",
            "reasoning": "r"})
        assert it.to_signal() == {
            "attempted": True, "can_author": True,
            "files": [{"path": "a.test.ts", "contents": "x"}],
            "expected_red_signature": "boom", "confidence": "low",
            "reasoning": "r"}


def _author_item(**over) -> wire.AuthorItem:
    d = {"pr": 7, "can_author": True,
         "files": [{"path": "ui/src/pages/routine-toast.test.tsx",
                    "contents": "test('t', () => {})\n"}],
         "expected_red_signature": "AssertionError: expected toast",
         "confidence": "high", "reasoning": "r"}
    d.update(over)
    return wire.AuthorItem.from_dict(d)


class TestValidateAuthored:
    """Fail-closed validation: the driver is the sole author of anything
    executable, and authored files must be new files under test paths."""

    def _clone(self, tmp_path, existing=()):
        for p in existing:
            f = tmp_path / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        return tmp_path

    def test_valid_item_derives_the_command(self, tmp_path):
        cmd, skip = vd.validate_authored(
            _author_item(), base_clone=self._clone(tmp_path), pr_paths=["ui/src/a.ts"])
        assert skip is None
        assert cmd is not None and "routine-toast.test.tsx" in cmd
        assert cmd.startswith("npx vitest run ")

    def test_declined_author(self, tmp_path):
        cmd, skip = vd.validate_authored(
            _author_item(can_author=False), base_clone=tmp_path, pr_paths=[])
        assert (cmd, skip) == (None, "agent-declined")

    def test_no_files(self, tmp_path):
        _, skip = vd.validate_authored(
            _author_item(files=[]), base_clone=tmp_path, pr_paths=[])
        assert skip == "file-count-not-1-to-3"

    def test_too_many_files(self, tmp_path):
        files = [{"path": f"a{i}.test.ts", "contents": "x"} for i in range(4)]
        _, skip = vd.validate_authored(
            _author_item(files=files), base_clone=tmp_path, pr_paths=[])
        assert skip == "file-count-not-1-to-3"

    def test_non_test_path_rejected(self, tmp_path):
        files = [{"path": "ui/src/pages/RoutineDetail.tsx", "contents": "x"}]
        _, skip = vd.validate_authored(
            _author_item(files=files), base_clone=tmp_path, pr_paths=[])
        assert skip == "path-not-a-test-path"

    def test_traversal_rejected(self, tmp_path):
        files = [{"path": "../evil.test.ts", "contents": "x"}]
        _, skip = vd.validate_authored(
            _author_item(files=files), base_clone=tmp_path, pr_paths=[])
        assert skip == "path-not-repo-relative"

    def test_absolute_path_rejected(self, tmp_path):
        files = [{"path": "/tmp/evil.test.ts", "contents": "x"}]
        _, skip = vd.validate_authored(
            _author_item(files=files), base_clone=tmp_path, pr_paths=[])
        assert skip == "path-not-repo-relative"

    def test_existing_file_on_base_rejected(self, tmp_path):
        clone = self._clone(tmp_path, existing=["ui/src/pages/routine-toast.test.tsx"])
        _, skip = vd.validate_authored(
            _author_item(), base_clone=clone, pr_paths=[])
        assert skip == "path-exists-on-base"

    def test_path_in_pr_diff_rejected(self, tmp_path):
        _, skip = vd.validate_authored(
            _author_item(), base_clone=tmp_path,
            pr_paths=["ui/src/pages/routine-toast.test.tsx"])
        assert skip == "path-in-pr-diff"

    def test_oversized_contents_rejected(self, tmp_path):
        files = [{"path": "big.test.ts", "contents": "x" * (64 * 1024 + 1)}]
        _, skip = vd.validate_authored(
            _author_item(files=files), base_clone=tmp_path, pr_paths=[])
        assert skip == "contents-too-large"

    def test_empty_signature_rejected(self, tmp_path):
        _, skip = vd.validate_authored(
            _author_item(expected_red_signature="  "), base_clone=tmp_path, pr_paths=[])
        assert skip == "no-expected-red-signature"

    def test_duplicate_paths_rejected(self, tmp_path):
        f = {"path": "a.test.ts", "contents": "x"}
        _, skip = vd.validate_authored(
            _author_item(files=[f, dict(f)]), base_clone=tmp_path, pr_paths=[])
        assert skip == "duplicate-paths"


class TestAuthoredPatches:
    def test_patch_applies_and_creates_the_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
        files = [{"path": "ui/src/a.test.ts", "contents": "line1\nline2"},
                 {"path": "ui/src/b.test.ts", "contents": "only\n"}]
        patch = vd.authored_test_patch("deadbeef", files)
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=repo, check=True)
        assert (repo / "ui/src/a.test.ts").read_text() == "line1\nline2\n"
        assert (repo / "ui/src/b.test.ts").read_text() == "only\n"

    def test_combined_patch_is_pr_then_authored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
        authored = vd.authored_test_patch(
            "deadbeef", [{"path": "a.test.ts", "contents": "x\n"}])
        pr = tmp_path / "pr.patch"
        pr.write_text("PRDIFF")
        combined = vd.combined_patch("deadbeef", pr, authored)
        text = combined.read_text()
        assert text.startswith("PRDIFF\n")
        assert "a.test.ts" in text


class TestVerifyPrAuthoredLane:
    def _rec_with_authored(self, store):
        # A PR whose blind verdict has no test_cmd and whose authored_test
        # section is committed — the state Task 7's orchestrator produces.
        _pr(store, 1)
        signals = {
            "blind_adequacy": {"has_test": False, "faithful": False,
                               "test_cmd": None, "requires_live_agent": False},
            "authored_test": {
                "attempted": True, "can_author": True,
                "test_cmd": ("npx vitest run ui/src/x.test.tsx "
                             "--testTimeout=120000 --hookTimeout=120000"),
                "files": [{"path": "ui/src/x.test.tsx", "contents": "t\n"}],
                "expected_red_signature": "boom", "confidence": "high", "reasoning": "r",
            },
        }
        store.edit_pr(1).record_verify(None, signals, base_sha="basesha", head_sha="h1")
        return store.load_pr(1)

    def test_lane_runs_authored_patches_and_records_exits(self, tmp_path, monkeypatch):
        calls: list[tuple[str, str, str | None]] = []

        def fake_run_phase(name, image, *, tier, base_sha, head_sha, test_cmd,
                           suite_config=None,
                           patch=None, exclude_file=None, timeout=0):
            calls.append((name, test_cmd, str(patch) if patch else None))
            if name == "apply-check":
                return 0, ""
            if name == "red":
                return 20, "authored red tail"
            if name == "green":
                return 0, "authored green tail"
            if name == "regress":
                return 0, ""
            raise AssertionError(name)

        monkeypatch.setattr(vd, "run_phase", fake_run_phase)
        monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
        pr_patch = tmp_path / "pr.patch"
        pr_patch.write_text("PRDIFF\n")
        monkeypatch.setattr(vd, "fetch_patch", lambda n, h: pr_patch)

        s = _pinned(tmp_path)
        rec = self._rec_with_authored(s)
        ev = vd.verify_pr(rec, "img", "basesha", 1, baseline=[], suite_config=SUITE_CFG)

        lane = ev["authored_test"]
        assert (lane["red_exit"], lane["green_exit"]) == (20, 0)
        assert (lane["red_exit_confirm"], lane["green_exit_confirm"]) == (20, 0)
        assert lane["red_output_tail"] == "authored red tail"
        assert ev["red_green"]["apply_exit"] == 0
        assert ev["red_green"]["red_exit"] is None
        # red phases run the authored patch alone; green phases the combined patch
        red_calls = [c for c in calls if c[0] == "red"]
        green_calls = [c for c in calls if c[0] == "green"]
        assert len(red_calls) == 2 and len(green_calls) == 2
        assert all("authored" in c[2] for c in red_calls)
        assert all("combined" in c[2] for c in green_calls)
        # regress ran (clean confirmed lane) with the PR patch, suite command
        assert [c for c in calls if c[0] == "regress"]

    def test_a_dirty_authored_green_gets_no_containment(self, tmp_path, monkeypatch):
        # The authored file is new, so it has no pre-existing tests to be
        # contaminated by: a green failure is the authored test itself failing,
        # and the lane stops with no parsed facts and no confirm.
        calls: list[str] = []

        def fake_run_phase(name, image, *, tier, base_sha, head_sha, test_cmd,
                           suite_config=None,
                           patch=None, exclude_file=None, timeout=0):
            calls.append(name)
            if name == "apply-check":
                return 0, ""
            return 20, _vitest_tail(["x.test.tsx > s > authored"])

        monkeypatch.setattr(vd, "run_phase", fake_run_phase)
        monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
        pr_patch = tmp_path / "pr.patch"
        pr_patch.write_text("PRDIFF\n")
        monkeypatch.setattr(vd, "fetch_patch", lambda n, h: pr_patch)

        s = _pinned(tmp_path)
        rec = self._rec_with_authored(s)
        ev = vd.verify_pr(rec, "img", "basesha", 1, baseline=[], suite_config=SUITE_CFG)
        lane = ev["authored_test"]
        assert (lane["red_exit"], lane["green_exit"]) == (20, 20)
        assert "red_failing" not in lane and "green_failing" not in lane
        assert calls == ["apply-check", "red", "green"]

    def test_no_authored_cmd_spends_no_sandbox_time(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("no phase may run")
        monkeypatch.setattr(vd, "run_phase", boom)
        s = _pinned(tmp_path)
        _pr(s, 1)
        signals = {"blind_adequacy": {"has_test": False, "faithful": False,
                                      "test_cmd": None, "requires_live_agent": False}}
        s.edit_pr(1).record_verify(None, signals, base_sha="basesha", head_sha="h1")
        rec = s.load_pr(1)
        ev = vd.verify_pr(rec, "img", "basesha", 1, baseline=[], suite_config=SUITE_CFG)
        assert ev["red_green"]["apply_exit"] is None
        assert "authored_test" not in ev

    def test_test_lane_wins_when_both_commands_exist(self, tmp_path, monkeypatch):
        # A PR whose blind verdict derived its own test_cmd but also carries a
        # committed authored_test record (e.g. queued twice, or authored before
        # the PR pushed its own test) runs the PR's own test lane, not the
        # authored one — the test lane takes priority.
        calls: list[tuple[str, str, str | None]] = []

        def fake_run_phase(name, image, *, tier, base_sha, head_sha, test_cmd,
                           suite_config=None,
                           patch=None, exclude_file=None, timeout=0):
            calls.append((name, test_cmd, str(patch) if patch else None))
            if name == "apply-check":
                return 0, ""
            if name == "red":
                return 20, "test-lane red tail"
            if name == "green":
                return 0, "test-lane green tail"
            if name == "regress":
                return 0, ""
            raise AssertionError(name)

        monkeypatch.setattr(vd, "run_phase", fake_run_phase)
        monkeypatch.setattr(vd, "SCRATCH", tmp_path / "scratch")
        pr_patch = tmp_path / "pr.patch"
        pr_patch.write_text("PRDIFF\n")
        monkeypatch.setattr(vd, "fetch_patch", lambda n, h: pr_patch)
        monkeypatch.setattr(vd, "test_only_patch", lambda head, patch: pr_patch)

        s = _pinned(tmp_path)
        _pr(s, 1)
        signals = {
            "blind_adequacy": {"has_test": True, "faithful": True,
                               "test_cmd": "npx vitest run ui/src/y.test.tsx",
                               "requires_live_agent": False},
            "authored_test": {
                "attempted": True, "can_author": True,
                "test_cmd": ("npx vitest run ui/src/x.test.tsx "
                             "--testTimeout=120000 --hookTimeout=120000"),
                "files": [{"path": "ui/src/x.test.tsx", "contents": "t\n"}],
                "expected_red_signature": "boom", "confidence": "high", "reasoning": "r",
            },
        }
        s.edit_pr(1).record_verify(None, signals, base_sha="basesha", head_sha="h1")
        rec = s.load_pr(1)
        ev = vd.verify_pr(rec, "img", "basesha", 1, baseline=[], suite_config=SUITE_CFG)

        red_calls = [c for c in calls if c[0] == "red"]
        assert red_calls and all("authored" not in (c[2] or "") for c in red_calls)
        assert "authored_test" not in ev
        assert ev["red_green"]["red_exit"] == 20
        assert ev["red_green"]["green_exit"] == 0


class TestRunLanes:
    def _phase_script(self, results):
        calls = []

        def phase(name, *, test_cmd, patch=None, exclude_file=None,
                  suite_config=None, timeout=0):
            calls.append((name, test_cmd))
            return results[name]
        return phase, calls

    def test_all_lanes_pass_and_record(self, monkeypatch):
        p = profile.parse_profile(
            {"version": 1, "verify": {"compile_cmd": "pnpm -r typecheck",
                                      "build_cmd": "pnpm build"}}, "t")
        monkeypatch.setattr(profile, "active", lambda: p)
        phase, calls = self._phase_script({"compile": (0, ""), "build": (0, "")})
        ev = {}
        failed = vd._run_lanes(ev, phase, Path("/tmp/x.patch"))
        assert failed is None
        assert ev["lanes"]["compile"]["ok"] is True
        assert ev["lanes"]["build"]["exit"] == 0
        assert [c[0] for c in calls] == ["compile", "build"]

    def test_failed_lane_stops_later_lanes(self, monkeypatch):
        p = profile.parse_profile(
            {"version": 1, "verify": {"compile_cmd": "c", "build_cmd": "b"}}, "t")
        monkeypatch.setattr(profile, "active", lambda: p)
        phase, calls = self._phase_script(
            {"compile": (20, "src/x.ts(1,1): error TS2739: nope")})
        ev = {}
        failed = vd._run_lanes(ev, phase, Path("/tmp/x.patch"))
        assert failed == "compile"
        assert "error TS2739" in ev["lanes"]["compile"]["error_excerpt"]
        assert ev["lanes"]["build"] == {"cmd": "b", "skipped": "compile failed"}
        assert [c[0] for c in calls] == ["compile"]

    def test_unconfigured_profile_records_nothing(self, monkeypatch):
        monkeypatch.setattr(profile, "active",
                            lambda: profile.parse_profile({"version": 1}, "t"))
        ev = {}
        assert vd._run_lanes(ev, None, Path("/tmp/x.patch")) is None
        assert "lanes" not in ev
