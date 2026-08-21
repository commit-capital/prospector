"""The ONE repository-policy profile — repository-specific vocabulary as data.

A profile carries the policy knowledge that differs per triaged repository;
this version owns the subsystem taxonomy, the path→risk-tier glob map, the
CODEOWNERS gating policy, trusted/automation authors, dependency manifests,
the test/artifact path rules, and the VERIFY sandbox policy (test runner,
pnpm pin, full-suite contract). TRIAGE_PROFILE selects a JSON file;
unset selects the built-in generic default, whose empty taxonomy classifies
every PR as "other". Validation is strict: a missing file, unknown key, wrong
type, or invalid pattern is a hard error, never a silent default.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from pipeline import settings

PROFILE_VERSION = 1
_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True)
class Subsystem:
    name: str
    match_terms: tuple[str, ...]


@dataclass(frozen=True)
class RiskTiers:
    tier0_globs: tuple[str, ...] = ()
    tier1_globs: tuple[str, ...] = ()
    instruction_globs: tuple[str, ...] = ()
    tier3_globs: tuple[str, ...] = ()
    default_tier: int = 2


@dataclass(frozen=True)
class CodeownersPolicy:
    gated_globs: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()


# Patterns are matched case-insensitively by consumers (re.IGNORECASE).
@dataclass(frozen=True)
class TestPaths:
    dir_pattern: str = r"(^|/)(tests?|__tests__|spec|e2e)(/)"
    file_pattern: str = r"(\.(test|spec)\.[^/]+|_(test|spec)\.[^/]+|(^|/)test_[^/]+)$"


@dataclass(frozen=True)
class HarnessPolicy:
    pr_template_required: tuple[str, ...] = ()
    pr_template_recommended: tuple[str, ...] = ()


# Which failing gates an autofix `fix` action may attempt to clear.
AUTOFIX_GATES: tuple[str, ...] = ("ci", "review")


# Autofix policy: the surfaces the push bot keeps its hands off, and the failing
# gates an agent may author against. deny_globs ranks effort and blast radius,
# not trust — the security boundary is the threat verdict, the security review,
# and CODEOWNERS routing, all of which gate autofix independently of any path
# shape a contributor controls. Empty deny_globs with no fixable gates is the
# generic default: mechanical actions stay available, agent-authored fixes do
# not, so a repository opts into them by naming its own policy.
@dataclass(frozen=True)
class AutofixPolicy:
    deny_globs: tuple[str, ...] = ()
    fixable_gates: tuple[str, ...] = ()


# The full-suite regression lane's repository contract: the stabilized-wrapper
# script the plan derives from, the vitest project its serialized/general-server
# suites run under, an optional preflight npm script, and the names of the
# per-invocation fixture env vars (a fresh home dir and an instance id) the
# repository's tests expect. A profile without this section has no full-suite
# lane — prepare-base captures no baseline and the regress phase is skipped.
@dataclass(frozen=True)
class SuiteConfig:
    wrapper: str
    server_project: str
    preflight: str | None = None
    home_env: str | None = None
    instance_env: str | None = None


# The VERIFY sandbox's repository policy: the fixed red/green test runner and
# flags derive_test_command builds commands from, the pnpm version the sandbox
# image caches (the target repository's packageManager pin), the optional
# whole-repo compile command the merge-time compile preflight runs (None means
# the deployment requires no compile preflight), the optional whole-repo build
# command (the second merge-gate lane; None means the deployment requires no
# build lane), and the optional full-suite contract. A repository with a
# compile_cmd must install offline at tier 1 — the command runs against
# baked-in node_modules with no network.
@dataclass(frozen=True)
class VerifyPolicy:
    test_runner: tuple[str, ...] = ("npx", "vitest", "run")
    test_flags: tuple[str, ...] = ("--testTimeout=120000", "--hookTimeout=120000")
    pnpm_version: str = "9.15.4"
    compile_cmd: str | None = None
    build_cmd: str | None = None
    suite: SuiteConfig | None = None


ARTIFACT_CATEGORIES: tuple[str, ...] = ("lockfile", "migrations", "locale", "vendored", "generated")


# Patterns are matched case-insensitively by consumers (re.IGNORECASE).
@dataclass(frozen=True)
class ArtifactRule:
    category: str
    pattern: str


# Dependency-manifest names every ecosystem shares. Entries without a "/" match
# a path's basename (fnmatch); entries with one match the whole path in the
# diffpaths glob dialect.
GENERIC_DEPENDENCY_MANIFESTS: tuple[str, ...] = (
    "package.json", "package-lock.json", "npm-shrinkwrap.json",
    "pnpm-lock.yaml", "pnpm-workspace.yaml",
    "yarn.lock", "bun.lock", "bun.lockb", "deno.lock",
    "pyproject.toml", "poetry.lock", "uv.lock", "requirements*.txt",
    "Pipfile", "Pipfile.lock",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
    "pom.xml", "build.gradle", "build.gradle.kts", "gradle.lockfile",
    "Gemfile", "Gemfile.lock", "composer.json", "composer.lock",
    "packages.config", "packages.lock.json",
    "mix.exs", "mix.lock", "pubspec.yaml", "pubspec.lock",
)

# Artifact classification every ecosystem shares: lockfiles, i18n bundles,
# vendored/build output, generated code. Migration-snapshot conventions are
# repository policy and come only from a profile.
GENERIC_ARTIFACT_RULES: tuple[ArtifactRule, ...] = (
    ArtifactRule("lockfile", (
        r"(^|/)(pnpm-lock\.yaml|package-lock\.json|npm-shrinkwrap\.json|yarn\.lock"
        r"|bun\.lockb?|Cargo\.lock|poetry\.lock|composer\.lock|Gemfile\.lock|go\.sum"
        r"|uv\.lock|Pipfile\.lock)$")),
    ArtifactRule("locale", (
        r"(^|/)(i18n|locales?|lang|translations?)/.*\.(json|po|pot|xliff|xlf|ftl|arb|strings)$")),
    ArtifactRule("vendored", (
        r"(^|/)(node_modules|dist|build|out|vendor|\.next|\.nuxt|\.vite|coverage|storybook-static)/")),
    ArtifactRule("generated", (
        r"\.min\.(js|css)$|\.map$|\.tsbuildinfo$|\.d\.ts$|\.generated\.[^/]+$"
        r"|(^|/)__generated__/|_pb2\.pyi?$|\.pb\.go$")),
)


# The generic default's only risk knowledge: the supply-chain surface every
# ecosystem shares (CI config, manifests, lockfiles) ranks tier 0; everything
# else sits at the default tier.
_GENERIC_SUPPLY_CHAIN: tuple[str, ...] = (
    ".github/**",
    "package.json", "**/package.json",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "pyproject.toml", "poetry.lock", "uv.lock", "requirements.txt",
    "Gemfile", "Gemfile.lock",
    "Cargo.toml", "Cargo.lock",
    "go.mod", "go.sum",
    "composer.json", "composer.lock",
)
GENERIC_RISK_TIERS = RiskTiers(tier0_globs=_GENERIC_SUPPLY_CHAIN)


@dataclass(frozen=True)
class RepoProfile:
    subsystems: tuple[Subsystem, ...] = ()
    risk_tiers: RiskTiers = GENERIC_RISK_TIERS
    codeowners: CodeownersPolicy = CodeownersPolicy()
    trusted_authors: tuple[str, ...] = ()
    automation_bots: tuple[str, ...] = ("dependabot[bot]",)
    dependency_manifests: tuple[str, ...] = GENERIC_DEPENDENCY_MANIFESTS
    test_paths: TestPaths = TestPaths()
    artifact_rules: tuple[ArtifactRule, ...] = GENERIC_ARTIFACT_RULES
    harness: HarnessPolicy = HarnessPolicy()
    verify: VerifyPolicy = VerifyPolicy()
    autofix: AutofixPolicy = AutofixPolicy()

    def subsystem_names(self) -> list[str]:
        """Accepted subsystem values, ending with the catch-all "other"."""
        return [s.name for s in self.subsystems] + ["other"]


GENERIC = RepoProfile()


def _fail(source: str, where: str, problem: str) -> SystemExit:
    return SystemExit(f"TRIAGE_PROFILE {source}: {where}: {problem}")


def _require_object(raw: object, source: str, where: str, allowed: set[str]) -> dict[str, object]:
    """`raw` as a dict whose every key is in `allowed`; anything else is a
    hard error naming the location."""
    if not isinstance(raw, dict):
        raise _fail(source, where, "must be an object")
    unknown = set(raw) - allowed
    if unknown:
        raise _fail(source, where, f"unknown key(s) {sorted(unknown)}")
    return raw


def _parse_str_list(raw: object, source: str, where: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise _fail(source, where, "must be a list of strings")
    if any(not x.strip() for x in raw):
        raise _fail(source, where, "entries must be non-empty strings")
    if any(x != x.strip() for x in raw):
        raise _fail(source, where, "entries must not have leading/trailing whitespace")
    return tuple(raw)


def _valid_regex(pattern: str, source: str, where: str) -> None:
    """`pattern` is non-blank and compiles as a regex; anything else is a
    hard error."""
    if not pattern.strip():
        raise _fail(source, where, "pattern must be a non-empty regex")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise _fail(source, where, f"{pattern!r} is not a valid regex: {exc}")


def _parse_subsystems(raw: object, source: str) -> tuple[Subsystem, ...]:
    if not isinstance(raw, list):
        raise _fail(source, "subsystems", "must be a list")
    subs: list[Subsystem] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        where = f"subsystems[{i}]"
        entry = _require_object(item, source, where, {"name", "match_terms"})
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise _fail(source, where, "name must be a non-empty string")
        if not _NAME_RE.match(name):
            raise _fail(source, where, f"name {name!r} must be a lowercase slug ([a-z0-9-])")
        if name == "other":
            raise _fail(source, where, "name 'other' is reserved for the catch-all")
        if name in seen:
            raise _fail(source, where, f"duplicate name {name!r}")
        seen.add(name)
        terms = _parse_str_list(entry.get("match_terms"), source, f"{where}.match_terms")
        for t in terms:
            _valid_regex(t, source, f"{where}.match_terms")
        subs.append(Subsystem(name=name, match_terms=terms))
    return tuple(subs)


def _parse_risk_tiers(raw: object, source: str) -> RiskTiers:
    section = _require_object(raw, source, "risk_tiers", {
        "tier0_globs", "tier1_globs", "instruction_globs", "tier3_globs", "default_tier"})
    tier = section.get("default_tier", RiskTiers.default_tier)
    if isinstance(tier, bool) or not isinstance(tier, int) or not 0 <= tier <= 3:
        raise _fail(source, "risk_tiers", "default_tier must be an integer 0-3")
    return RiskTiers(
        tier0_globs=_parse_str_list(section.get("tier0_globs", []), source, "risk_tiers.tier0_globs"),
        tier1_globs=_parse_str_list(section.get("tier1_globs", []), source, "risk_tiers.tier1_globs"),
        instruction_globs=_parse_str_list(
            section.get("instruction_globs", []), source, "risk_tiers.instruction_globs"),
        tier3_globs=_parse_str_list(section.get("tier3_globs", []), source, "risk_tiers.tier3_globs"),
        default_tier=tier,
    )


def _parse_codeowners(raw: object, source: str) -> CodeownersPolicy:
    section = _require_object(raw, source, "codeowners", {"gated_globs", "owners"})
    return CodeownersPolicy(
        gated_globs=_parse_str_list(section.get("gated_globs", []), source, "codeowners.gated_globs"),
        owners=_parse_str_list(section.get("owners", []), source, "codeowners.owners"),
    )


def _parse_autofix(raw: object, source: str) -> AutofixPolicy:
    section = _require_object(raw, source, "autofix", {"deny_globs", "fixable_gates"})
    gates = _parse_str_list(section.get("fixable_gates", []), source, "autofix.fixable_gates")
    unknown = sorted(set(gates) - set(AUTOFIX_GATES))
    if unknown:
        raise _fail(source, "autofix.fixable_gates",
                    f"unknown gate(s) {unknown}; valid gates are {list(AUTOFIX_GATES)}")
    return AutofixPolicy(
        deny_globs=_parse_str_list(section.get("deny_globs", []), source, "autofix.deny_globs"),
        fixable_gates=gates,
    )


def _parse_test_paths(raw: object, source: str) -> TestPaths:
    """Parse the test_paths section. An omitted field keeps the generic pattern
    (the TestPaths dataclass default) — an empty test-path regex is never
    useful, so this section defaults field-by-field, not to a neutral empty."""
    section = _require_object(raw, source, "test_paths", {"dir_pattern", "file_pattern"})
    out: dict[str, str] = {}
    for key in ("dir_pattern", "file_pattern"):
        if key not in section:
            continue
        value = section[key]
        if not isinstance(value, str):
            raise _fail(source, f"test_paths.{key}", "must be a string")
        _valid_regex(value, source, f"test_paths.{key}")
        out[key] = value
    return TestPaths(**out)


def _parse_harness(raw: object, source: str) -> HarnessPolicy:
    section = _require_object(raw, source, "harness", {"pr_template"})
    tmpl = _require_object(
        section.get("pr_template", {}), source, "harness.pr_template",
        {"required_sections", "recommended_sections"})
    return HarnessPolicy(
        pr_template_required=_parse_str_list(
            tmpl.get("required_sections", []), source, "harness.pr_template.required_sections"),
        pr_template_recommended=_parse_str_list(
            tmpl.get("recommended_sections", []), source, "harness.pr_template.recommended_sections"),
    )


_PNPM_VERSION_RE = re.compile(r"\d+\.\d+\.\d+\Z")


def _parse_opt_str(section: dict[str, object], key: str, source: str,
                   where: str) -> str | None:
    """`section[key]` as a non-empty string, or None when the key is absent or
    explicitly null."""
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _fail(source, f"{where}.{key}", "must be a non-empty string or null")
    return value


def _parse_suite(raw: object, source: str) -> SuiteConfig:
    section = _require_object(raw, source, "verify.suite", {
        "wrapper", "server_project", "preflight", "home_env", "instance_env"})
    wrapper = section.get("wrapper")
    server_project = section.get("server_project")
    if not isinstance(wrapper, str) or not wrapper.strip():
        raise _fail(source, "verify.suite.wrapper", "must be a non-empty string")
    if not isinstance(server_project, str) or not server_project.strip():
        raise _fail(source, "verify.suite.server_project", "must be a non-empty string")
    home_env = _parse_opt_str(section, "home_env", source, "verify.suite")
    instance_env = _parse_opt_str(section, "instance_env", source, "verify.suite")
    for key, value in (("home_env", home_env), ("instance_env", instance_env)):
        if value is not None and not _ENV_NAME_RE.fullmatch(value):
            raise _fail(source, f"verify.suite.{key}",
                        "must be an environment variable name")
    return SuiteConfig(
        wrapper=wrapper,
        server_project=server_project,
        preflight=_parse_opt_str(section, "preflight", source, "verify.suite"),
        home_env=home_env,
        instance_env=instance_env,
    )


def _parse_verify(raw: object, source: str) -> VerifyPolicy:
    section = _require_object(raw, source, "verify", {
        "test_runner", "test_flags", "pnpm_version", "compile_cmd", "build_cmd",
        "suite"})
    runner = (_parse_str_list(section["test_runner"], source, "verify.test_runner")
              if "test_runner" in section else VerifyPolicy.test_runner)
    if not runner:
        raise _fail(source, "verify.test_runner", "must name at least the runner executable")
    flags = (_parse_str_list(section["test_flags"], source, "verify.test_flags")
             if "test_flags" in section else VerifyPolicy.test_flags)
    pnpm = section.get("pnpm_version", VerifyPolicy.pnpm_version)
    if not isinstance(pnpm, str) or not _PNPM_VERSION_RE.fullmatch(pnpm):
        raise _fail(source, "verify.pnpm_version",
                    "must be an exact semver string (e.g. 9.15.4)")
    suite = (_parse_suite(section["suite"], source)
             if section.get("suite") is not None else None)
    return VerifyPolicy(test_runner=runner, test_flags=flags,
                        pnpm_version=pnpm,
                        compile_cmd=_parse_opt_str(section, "compile_cmd", source, "verify"),
                        build_cmd=_parse_opt_str(section, "build_cmd", source, "verify"),
                        suite=suite)


def _parse_artifact_rules(raw: object, source: str) -> tuple[ArtifactRule, ...]:
    if not isinstance(raw, list):
        raise _fail(source, "artifact_rules", "must be a list")
    rules: list[ArtifactRule] = []
    for i, item in enumerate(raw):
        where = f"artifact_rules[{i}]"
        entry = _require_object(item, source, where, {"category", "pattern"})
        category = entry.get("category")
        if not isinstance(category, str) or category not in ARTIFACT_CATEGORIES:
            raise _fail(source, where, f"category must be one of {ARTIFACT_CATEGORIES}")
        pattern = entry.get("pattern")
        if not isinstance(pattern, str):
            raise _fail(source, where, "pattern must be a string")
        _valid_regex(pattern, source, where)
        rules.append(ArtifactRule(category=category, pattern=pattern))
    return tuple(rules)


# The profile's sections, one per RepoProfile field — a test pins the
# correspondence, so a key cannot pass validation without being built.
_SECTIONS: tuple[str, ...] = (
    "subsystems", "risk_tiers", "codeowners", "trusted_authors",
    "automation_bots", "dependency_manifests", "test_paths", "artifact_rules",
    "harness", "verify", "autofix")


def parse_profile(payload: object, source: str) -> RepoProfile:
    """Validate a decoded profile document into a RepoProfile. Every problem is
    a SystemExit naming the offending location, so a malformed profile can
    never degrade to defaults silently. An omitted section means the generic
    default for that section."""
    doc = _require_object(payload, source, "top level", {"version", *_SECTIONS})
    if doc.get("version") != PROFILE_VERSION:
        raise _fail(source, "version", f"must be the integer {PROFILE_VERSION}")
    return RepoProfile(
        subsystems=_parse_subsystems(doc["subsystems"], source)
        if "subsystems" in doc else (),
        risk_tiers=_parse_risk_tiers(doc["risk_tiers"], source)
        if "risk_tiers" in doc else GENERIC_RISK_TIERS,
        codeowners=_parse_codeowners(doc["codeowners"], source)
        if "codeowners" in doc else CodeownersPolicy(),
        trusted_authors=_parse_str_list(doc["trusted_authors"], source, "trusted_authors")
        if "trusted_authors" in doc else (),
        automation_bots=_parse_str_list(doc["automation_bots"], source, "automation_bots")
        if "automation_bots" in doc else ("dependabot[bot]",),
        dependency_manifests=_parse_str_list(
            doc["dependency_manifests"], source, "dependency_manifests")
        if "dependency_manifests" in doc else GENERIC_DEPENDENCY_MANIFESTS,
        test_paths=_parse_test_paths(doc["test_paths"], source)
        if "test_paths" in doc else TestPaths(),
        artifact_rules=_parse_artifact_rules(doc["artifact_rules"], source)
        if "artifact_rules" in doc else GENERIC_ARTIFACT_RULES,
        harness=_parse_harness(doc["harness"], source)
        if "harness" in doc else HarnessPolicy(),
        verify=_parse_verify(doc["verify"], source)
        if "verify" in doc else VerifyPolicy(),
        autofix=_parse_autofix(doc["autofix"], source)
        if "autofix" in doc else AutofixPolicy(),
    )


@cache
def _load(path_str: str) -> RepoProfile:
    path = Path(path_str)
    if not path.is_absolute():
        path = settings.REPO_ROOT / path
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise SystemExit(f"TRIAGE_PROFILE {path}: file not found")
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit(f"TRIAGE_PROFILE {path}: unreadable: {exc}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"TRIAGE_PROFILE {path}: invalid JSON: {exc}")
    return parse_profile(payload, str(path))


def reset_cache() -> None:
    """Drop the parsed-profile cache so a rewritten file at the same path is read
    again. `_load` is keyed by path, so a same-path rewrite is otherwise
    invisible."""
    _load.cache_clear()


def active() -> RepoProfile:
    """The configured repository profile. Reads settings on each call (cheap;
    the parsed file is cached per path) so tests can monkeypatch
    settings.profile_path() without cache invalidation. Profiles do not change
    mid-run."""
    if not settings.profile_path():
        return GENERIC
    return _load(settings.profile_path())
