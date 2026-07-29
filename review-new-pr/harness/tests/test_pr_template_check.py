"""Tests for the pr_template_check gate."""
from __future__ import annotations
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gates._common import PRContext, template_sections  # noqa: E402
from gates import pr_template_check  # noqa: E402


SECTIONS = ["Thinking Path", "What Changed", "Verification", "Risks", "Model Used"]


def install_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    required: list[str],
    recommended: list[str] | None = None,
    trusted: list[str] | None = None,
) -> None:
    """Write a profile JSON carrying the given template policy (and optionally
    a trusted_authors list) and point TRIAGE_PROFILE at it."""
    profile = tmp_path / "profile.json"
    doc: dict = {
        "harness": {"pr_template": {
            "required_sections": required,
            "recommended_sections": recommended if recommended is not None else ["Checklist"],
        }},
    }
    if trusted is not None:
        doc["trusted_authors"] = trusted
    profile.write_text(json.dumps(doc))
    monkeypatch.setenv("TRIAGE_PROFILE", str(profile))


def make_ctx(body: str) -> PRContext:
    return PRContext(
        number=9999, title="t", body=body, author="a", head_sha="x",
        base_ref="master", additions=0, deletions=0, changed_files=0,
    )


GOOD_PR_BODY = """
## Thinking Path

> - Example Project orchestrates AI agents for zero-human companies
> - The heartbeat subsystem manages agent run lifecycle
> - When a session file is missing, the resume path crashes with NPE
> - This PR adds a null check before session.resume()

## What Changed

- Added null check at heartbeat-runner.ts:142
- Added regression test in `heartbeat-workspace-session.test.ts`

## Verification

Run `pnpm test:run` — the new regression test passes.

## Risks

Low. The null check returns early; existing tests cover the happy path.

## Model Used

Claude Sonnet 4.5 (claude-sonnet-4-5, 200K context)

## Checklist

- [x] Tests pass
- [x] Thinking Path included
"""


def test_complete_pr_body_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    install_policy(monkeypatch, tmp_path, SECTIONS)
    r = pr_template_check.run(make_ctx(GOOD_PR_BODY))
    assert r["verdict"] == "pass"


def test_trusted_author_passes_without_template(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # the template exists FOR maintainers; their own PRs are not its subjects
    install_policy(monkeypatch, tmp_path, SECTIONS, trusted=["maintainer-x"])
    ctx = make_ctx("")
    ctx.author = "maintainer-x"
    r = pr_template_check.run(ctx)
    assert r["verdict"] == "pass"
    assert "trusted" in r["evidence"].lower() or "maintainer" in r["evidence"].lower()


def test_untrusted_author_still_checked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    install_policy(monkeypatch, tmp_path, SECTIONS, trusted=["maintainer-x"])
    r = pr_template_check.run(make_ctx(""))          # author "a", empty body
    assert r["verdict"] == "fail"


def test_empty_body_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    install_policy(monkeypatch, tmp_path, SECTIONS)
    r = pr_template_check.run(make_ctx(""))
    assert r["verdict"] == "fail"
    assert r["severity"] == "high"


def test_unedited_template_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    install_policy(monkeypatch, tmp_path, SECTIONS)
    body = """
## Thinking Path

(describe your thinking path here)

## What Changed

TBD

## Verification

N/A

## Risks

Low

## Model Used

(model)

## Checklist

- [ ] Tests pass
"""
    r = pr_template_check.run(make_ctx(body))
    assert r["verdict"] == "fail"


def test_missing_three_sections_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    install_policy(monkeypatch, tmp_path, SECTIONS)
    body = """
## Thinking Path

> - Example Project orchestrates AI agents
> - The heartbeat subsystem manages runs
> - This PR fixes a bug in the recovery path

## What Changed

- Added null check
"""
    r = pr_template_check.run(make_ctx(body))
    assert r["verdict"] == "fail"
    assert "Risks" in r["details"]["missing"]
    assert "Model Used" in r["details"]["missing"]
    assert "Verification" in r["details"]["missing"]


def test_missing_one_section_uncertain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    install_policy(monkeypatch, tmp_path, SECTIONS)
    body = """
## Thinking Path

> - Example Project orchestrates AI agents
> - The heartbeat subsystem
> - This PR fixes the bug

## What Changed

- Added null check at heartbeat-runner.ts

## Verification

Run pnpm test:run.

## Model Used

Claude Sonnet 4.5
"""
    r = pr_template_check.run(make_ctx(body))
    # Missing only Risks -> uncertain (low severity)
    assert r["verdict"] == "uncertain"
    assert r["details"]["missing"] == ["Risks"]


def test_boilerplate_section_flagged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    install_policy(monkeypatch, tmp_path, SECTIONS)
    body = GOOD_PR_BODY.replace(
        "Low. The null check returns early; existing tests cover the happy path.",
        "TBD",
    )
    r = pr_template_check.run(make_ctx(body))
    # Risks is now boilerplate-only
    assert r["verdict"] == "uncertain"
    assert "Risks" in r["details"]["boilerplate"]


def test_no_profile_passes_trivially(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TRIAGE_PROFILE", raising=False)
    r = pr_template_check.run(make_ctx(""))
    assert r["verdict"] == "pass"


def test_custom_sections_enforced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    install_policy(monkeypatch, tmp_path, ["Summary", "Testing"], [])
    body = """
## Summary

Fixes the null-pointer crash in the resume path.
"""
    r = pr_template_check.run(make_ctx(body))
    assert r["verdict"] == "uncertain"
    assert r["details"]["missing"] == ["Testing"]


def test_missing_profile_file_exits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("TRIAGE_PROFILE", str(tmp_path / "nope.json"))
    with pytest.raises(SystemExit):
        pr_template_check.run(make_ctx(GOOD_PR_BODY))


def write_raw_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, doc: object
) -> None:
    """Write an arbitrary JSON document as the profile and point TRIAGE_PROFILE
    at it."""
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(doc))
    monkeypatch.setenv("TRIAGE_PROFILE", str(profile))


def test_top_level_non_object_exits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    write_raw_profile(monkeypatch, tmp_path, ["not", "an", "object"])
    with pytest.raises(SystemExit) as e:
        template_sections()
    assert "must be a JSON object" in str(e.value)


def test_harness_non_object_exits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    write_raw_profile(monkeypatch, tmp_path, {"harness": "nope"})
    with pytest.raises(SystemExit) as e:
        template_sections()
    assert "must be an object" in str(e.value)


def test_required_sections_string_exits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    write_raw_profile(
        monkeypatch, tmp_path,
        {"harness": {"pr_template": {"required_sections": "Summary"}}},
    )
    with pytest.raises(SystemExit) as e:
        template_sections()
    assert "list of strings" in str(e.value)


def test_required_sections_object_exits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    write_raw_profile(
        monkeypatch, tmp_path,
        {"harness": {"pr_template": {"required_sections": {}}}},
    )
    with pytest.raises(SystemExit):
        template_sections()


def test_relative_profile_path_resolves_under_repo_root(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRIAGE_PROFILE", "no-such-profile.json")
    repo_root = Path(__file__).resolve().parents[3]
    with pytest.raises(SystemExit) as e:
        template_sections()
    assert str(repo_root) in str(e.value)
