"""pipeline/profile.py — strict parse + selection of the repository policy profile."""
import dataclasses
import json
from pathlib import Path

import pytest

from pipeline import profile, settings


def write(tmp_path, payload) -> str:
    p = tmp_path / "profile.json"
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return str(p)


VALID = {
    "version": 1,
    "subsystems": [
        {"name": "engine", "match_terms": ["fuel pump", "crankshaft"]},
        {"name": "brakes", "match_terms": ["caliper"]},
    ],
    "risk_tiers": {
        "tier0_globs": ["core/**", "package.json"],
        "tier1_globs": ["db/schema/**"],
        "instruction_globs": ["skills/**"],
        "tier3_globs": ["docs/**"],
        "default_tier": 2,
    },
    "codeowners": {
        "gated_globs": [".github/**", "package.json"],
        "owners": ["@owner-a", "@owner-b"],
    },
    "trusted_authors": ["good-dev"],
    "automation_bots": ["dependabot[bot]", "robo-bump[bot]"],
    "dependency_manifests": ["package.json", "deps/*.lock"],
    "test_paths": {"dir_pattern": "(^|/)qa/", "file_pattern": "_check\\.py$"},
    "artifact_rules": [{"category": "vendored", "pattern": "(^|/)third_party/"}],
    "harness": {"pr_template": {
        "required_sections": ["Summary"],
        "recommended_sections": ["Testing"],
    }},
    "verify": {"suite": {
        "wrapper": "scripts/run-tests.mjs",
        "server_project": "@example/server",
        "home_env": "EXAMPLE_HOME",
        "instance_env": "EXAMPLE_INSTANCE_ID",
    }},
}


class TestActive:
    def test_unset_selects_generic_default(self, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", "")
        assert profile.active() is profile.GENERIC
        assert profile.active().subsystem_names() == ["other"]

    def test_path_selects_parsed_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", write(tmp_path, VALID))
        assert profile.active().subsystem_names() == ["engine", "brakes", "other"]
        assert profile.active().subsystems[0].match_terms == ("fuel pump", "crankshaft")

    def test_same_path_returns_cached_object(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", write(tmp_path, VALID))
        assert profile.active() is profile.active()

    def test_relative_path_resolves_against_repo_root(self, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", "no-such-profile.json")
        with pytest.raises(SystemExit) as e:
            profile.active()
        assert str(settings.REPO_ROOT) in str(e.value)

    def test_missing_file_is_a_hard_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", str(tmp_path / "gone.json"))
        with pytest.raises(SystemExit, match="file not found"):
            profile.active()

    def test_invalid_json_is_a_hard_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", write(tmp_path, "{nope"))
        with pytest.raises(SystemExit, match="invalid JSON"):
            profile.active()

    def test_unreadable_file_is_a_hard_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", str(tmp_path))  # a directory
        with pytest.raises(SystemExit, match="unreadable"):
            profile.active()


class TestParse:
    def test_omitted_subsystems_means_the_generic_default(self):
        assert profile.parse_profile({"version": 1}, "t").subsystem_names() == ["other"]

    @pytest.mark.parametrize("payload,match", [
        ([1, 2], "must be an object"),
        ({"version": 1, "nope": []}, "unknown key"),
        ({"subsystems": []}, "version"),
        ({"version": 2, "subsystems": []}, "version"),
        ({"version": "1", "subsystems": []}, "version"),
        ({"version": 1, "subsystems": {}}, "must be a list"),
        ({"version": 1, "subsystems": ["x"]}, "must be an object"),
        ({"version": 1, "subsystems": [{"name": "a", "match_terms": [], "extra": 1}]}, "unknown key"),
        ({"version": 1, "subsystems": [{"match_terms": []}]}, "non-empty string"),
        ({"version": 1, "subsystems": [{"name": "", "match_terms": []}]}, "non-empty string"),
        ({"version": 1, "subsystems": [{"name": "has space", "match_terms": []}]}, "lowercase slug"),
        ({"version": 1, "subsystems": [{"name": "Upper-Case", "match_terms": []}]}, "lowercase slug"),
        ({"version": 1, "subsystems": [{"name": "comma,name", "match_terms": []}]}, "lowercase slug"),
        ({"version": 1, "subsystems": [{"name": "trailing-", "match_terms": []}]}, "lowercase slug"),
        ({"version": 1, "subsystems": [{"name": "other", "match_terms": []}]}, "reserved"),
        ({"version": 1, "subsystems": [
            {"name": "a", "match_terms": []}, {"name": "a", "match_terms": []}]}, "duplicate"),
        ({"version": 1, "subsystems": [{"name": "a"}]}, "list of strings"),
        ({"version": 1, "subsystems": [{"name": "a", "match_terms": [5]}]}, "list of strings"),
        ({"version": 1, "subsystems": [{"name": "a", "match_terms": [""]}]}, "non-empty"),
        ({"version": 1, "subsystems": [{"name": "a", "match_terms": [" x"]}]}, "whitespace"),
        ({"version": 1, "subsystems": [{"name": "a", "match_terms": ["("]}]}, "valid regex"),
        ({"version": 1, "risk_tiers": []}, "must be an object"),
        ({"version": 1, "risk_tiers": {"nope": []}}, "unknown key"),
        ({"version": 1, "risk_tiers": {"tier0_globs": "x"}}, "list of"),
        ({"version": 1, "risk_tiers": {"tier0_globs": [""]}}, "non-empty"),
        ({"version": 1, "risk_tiers": {"default_tier": 5}}, "default_tier"),
        ({"version": 1, "risk_tiers": {"default_tier": True}}, "default_tier"),
        ({"version": 1, "risk_tiers": {"default_tier": "2"}}, "default_tier"),
        ({"version": 1, "codeowners": []}, "must be an object"),
        ({"version": 1, "codeowners": {"nope": []}}, "unknown key"),
        ({"version": 1, "codeowners": {"owners": [1]}}, "list of"),
        ({"version": 1, "codeowners": {"gated_globs": [""]}}, "non-empty"),
        ({"version": 1, "risk_tiers": {"tier0_globs": [" "]}}, "non-empty"),
        ({"version": 1, "codeowners": {"owners": ["  "]}}, "non-empty"),
        ({"version": 1, "risk_tiers": {"tier0_globs": [" ui/**"]}}, "whitespace"),
        ({"version": 1, "codeowners": {"owners": ["@owner-a "]}}, "whitespace"),
        ({"version": 1, "trusted_authors": "x"}, "list of strings"),
        ({"version": 1, "automation_bots": [""]}, "non-empty"),
        ({"version": 1, "dependency_manifests": {"a": 1}}, "list of strings"),
        ({"version": 1, "test_paths": []}, "must be an object"),
        ({"version": 1, "test_paths": {"nope": "x"}}, "unknown key"),
        ({"version": 1, "test_paths": {"dir_pattern": 5}}, "must be a string"),
        ({"version": 1, "test_paths": {"dir_pattern": "("}}, "valid regex"),
        ({"version": 1, "test_paths": {"dir_pattern": ""}}, "non-empty regex"),
        ({"version": 1, "artifact_rules": [{"category": "lockfile", "pattern": " "}]}, "non-empty regex"),
        ({"version": 1, "artifact_rules": {}}, "must be a list"),
        ({"version": 1, "artifact_rules": ["x"]}, "must be an object"),
        ({"version": 1, "artifact_rules": [{"category": "nope", "pattern": "x"}]}, "category"),
        ({"version": 1, "artifact_rules": [{"category": "lockfile", "pattern": "("}]}, "valid regex"),
        ({"version": 1, "artifact_rules": [{"category": "lockfile"}]}, "must be a string"),
        ({"version": 1, "artifact_rules": [{"category": "lockfile", "pattern": "x", "extra": 1}]}, "unknown key"),
        ({"version": 1, "harness": []}, "must be an object"),
        ({"version": 1, "harness": {"nope": {}}}, "unknown key"),
        ({"version": 1, "harness": {"pr_template": []}}, "must be an object"),
        ({"version": 1, "harness": {"pr_template": {"nope": []}}}, "unknown key"),
        ({"version": 1, "harness": {"pr_template": {"required_sections": [1]}}}, "list of strings"),
        ({"version": 1, "harness": {"pr_template": {"required_sections": [""]}}}, "non-empty"),
        ({"version": 1, "verify": []}, "must be an object"),
        ({"version": 1, "verify": {"suite": {
            "wrapper": "x", "server_project": "y", "home_env": "BAD-NAME",
            "instance_env": "OK"}}}, "environment variable name"),
    ])
    def test_malformed_profile_is_a_hard_error(self, payload, match):
        with pytest.raises(SystemExit, match=match):
            profile.parse_profile(payload, "t")

    def test_hyphenated_slugs_are_accepted(self):
        p = profile.parse_profile(
            {"version": 1, "subsystems": [{"name": "execution-locks", "match_terms": []}]}, "t")
        assert p.subsystem_names() == ["execution-locks", "other"]

    def test_error_names_the_offending_location(self):
        with pytest.raises(SystemExit, match=r"subsystems\[1\]"):
            profile.parse_profile(
                {"version": 1, "subsystems": [
                    {"name": "ok", "match_terms": []}, {"name": "", "match_terms": []}]},
                "t")


class TestSections:
    def test_risk_tiers_parse(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", write(tmp_path, VALID))
        rt = profile.active().risk_tiers
        assert rt.tier0_globs == ("core/**", "package.json")
        assert rt.tier1_globs == ("db/schema/**",)
        assert rt.instruction_globs == ("skills/**",)
        assert rt.tier3_globs == ("docs/**",)
        assert rt.default_tier == 2

    def test_codeowners_parse(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", write(tmp_path, VALID))
        co = profile.active().codeowners
        assert co.gated_globs == (".github/**", "package.json")
        assert co.owners == ("@owner-a", "@owner-b")

    def test_generic_default_sections(self):
        assert profile.GENERIC.codeowners.gated_globs == ()
        assert profile.GENERIC.codeowners.owners == ()
        assert profile.GENERIC.risk_tiers.default_tier == 2
        assert ".github/**" in profile.GENERIC.risk_tiers.tier0_globs
        assert "package.json" in profile.GENERIC.risk_tiers.tier0_globs
        assert profile.GENERIC.risk_tiers.tier1_globs == ()

    def test_present_section_replaces_never_merges(self):
        p = profile.parse_profile({"version": 1, "risk_tiers": {"tier1_globs": ["x/**"]}}, "t")
        assert p.risk_tiers.tier1_globs == ("x/**",)
        assert p.risk_tiers.tier0_globs == ()          # neutral, NOT the generic supply chain
        assert p.risk_tiers.default_tier == 2

    def test_omitted_sections_mean_generic(self):
        p = profile.parse_profile({"version": 1}, "t")
        assert p.risk_tiers == profile.GENERIC.risk_tiers
        assert p.codeowners == profile.GENERIC.codeowners

    def test_pr3_sections_parse(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", write(tmp_path, VALID))
        p = profile.active()
        assert p.trusted_authors == ("good-dev",)
        assert p.automation_bots == ("dependabot[bot]", "robo-bump[bot]")
        assert p.dependency_manifests == ("package.json", "deps/*.lock")
        assert p.test_paths.dir_pattern == "(^|/)qa/"
        assert p.artifact_rules == (profile.ArtifactRule("vendored", "(^|/)third_party/"),)

    def test_pr3_generic_defaults(self):
        g = profile.GENERIC
        assert g.trusted_authors == ()
        assert g.automation_bots == ("dependabot[bot]",)
        assert "pnpm-lock.yaml" in g.dependency_manifests
        assert "pyproject.toml" in g.dependency_manifests
        assert "bun.lock" in g.dependency_manifests
        assert "pom.xml" in g.dependency_manifests
        assert "tests?" in g.test_paths.dir_pattern
        assert any(r.category == "lockfile" for r in g.artifact_rules)
        assert not any(r.category == "migrations" for r in g.artifact_rules)

    def test_sections_match_repo_profile_fields(self):
        # every allowed section key is a RepoProfile field, so a key cannot
        # pass validation without parse_profile building it
        assert set(profile._SECTIONS) == {
            f.name for f in dataclasses.fields(profile.RepoProfile)}

    def test_committed_example_parses(self):
        path = Path(__file__).resolve().parents[2] / "profile.example.json"
        p = profile.parse_profile(json.loads(path.read_text()), "profile.example.json")
        assert p.subsystem_names()[-1] == "other"
        assert p.codeowners.owners != ()

    def test_harness_parses(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", write(tmp_path, VALID))
        h = profile.active().harness
        assert h.pr_template_required == ("Summary",)
        assert h.pr_template_recommended == ("Testing",)

    def test_harness_omitted_pr_template_means_empty(self):
        p = profile.parse_profile({"version": 1, "harness": {}}, "t")
        assert p.harness.pr_template_required == ()
        assert p.harness.pr_template_recommended == ()

    def test_harness_omitted_lists_mean_empty(self):
        p = profile.parse_profile(
            {"version": 1, "harness": {"pr_template": {}}}, "t")
        assert p.harness.pr_template_required == ()
        assert p.harness.pr_template_recommended == ()

    def test_harness_generic_default(self):
        assert profile.GENERIC.harness.pr_template_required == ()
        assert profile.GENERIC.harness.pr_template_recommended == ()

    def test_verify_generic_default(self):
        v = profile.GENERIC.verify
        assert v.test_runner == ("npx", "vitest", "run")
        assert v.pnpm_version == "9.15.4"
        assert v.compile_cmd is None
        assert v.suite is None

    def test_verify_parses(self):
        p = profile.parse_profile({"version": 1, "verify": {
            "test_runner": ["npx", "jest"], "test_flags": ["--ci"],
            "pnpm_version": "10.2.1",
            "compile_cmd": "pnpm -r typecheck",
            "suite": {"wrapper": "scripts/stable.mjs",
                      "server_project": "@acme/server",
                      "preflight": "preflight:links",
                      "home_env": "ACME_HOME", "instance_env": None}}}, "t")
        v = p.verify
        assert v.test_runner == ("npx", "jest")
        assert v.test_flags == ("--ci",)
        assert v.pnpm_version == "10.2.1"
        assert v.compile_cmd == "pnpm -r typecheck"
        assert v.suite is not None
        assert v.suite.wrapper == "scripts/stable.mjs"
        assert v.suite.server_project == "@acme/server"
        assert v.suite.preflight == "preflight:links"
        assert v.suite.home_env == "ACME_HOME"
        assert v.suite.instance_env is None

    def test_verify_omitted_fields_keep_defaults(self):
        p = profile.parse_profile({"version": 1, "verify": {}}, "t")
        assert p.verify == profile.VerifyPolicy()

    @pytest.mark.parametrize("payload, match", [
        ({"verify": {"nope": 1}}, "unknown key"),
        ({"verify": {"test_runner": []}}, "runner executable"),
        ({"verify": {"test_runner": "npx"}}, "list of strings"),
        ({"verify": {"pnpm_version": "9"}}, "semver"),
        ({"verify": {"pnpm_version": 9}}, "semver"),
        ({"verify": {"compile_cmd": ""}}, "non-empty string or null"),
        ({"verify": {"compile_cmd": 5}}, "non-empty string or null"),
        ({"verify": {"suite": {"wrapper": "w.mjs"}}}, "server_project"),
        ({"verify": {"suite": {"wrapper": "", "server_project": "@a/s"}}}, "wrapper"),
        ({"verify": {"suite": {"wrapper": "w.mjs", "server_project": "@a/s",
                               "preflight": 3}}}, "non-empty string or null"),
        ({"verify": {"suite": {"wrapper": "w.mjs", "server_project": "@a/s",
                               "home_env": "not a name"}}},
         "environment variable name"),
    ])
    def test_malformed_verify_is_a_hard_error(self, payload, match):
        with pytest.raises(SystemExit, match=match):
            profile.parse_profile({"version": 1, **payload}, "t")

    def test_verify_null_suite_means_no_suite(self):
        p = profile.parse_profile({"version": 1, "verify": {"suite": None}}, "t")
        assert p.verify.suite is None


class TestVerifyLaneKeys:
    def test_build_cmd_parses(self):
        p = profile.parse_profile(
            {"version": 1, "verify": {"build_cmd": "pnpm build"}}, "t")
        assert p.verify.build_cmd == "pnpm build"

    def test_build_cmd_defaults_to_none(self):
        p = profile.parse_profile({"version": 1}, "t")
        assert p.verify.build_cmd is None

    def test_e2e_cmd_is_rejected(self):
        # Reserved lane: no key may promise coverage that does not exist yet.
        with pytest.raises(SystemExit, match="unknown key"):
            profile.parse_profile(
                {"version": 1, "verify": {"e2e_cmd": "pnpm test:e2e"}}, "t")
