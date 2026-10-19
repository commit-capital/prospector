"""Headless runners share the drivers' prompts and trust boundaries."""
from __future__ import annotations

from pipeline import analyze_driver as ad
from pipeline import cluster_driver as cd
from pipeline import recluster
from pipeline import security_driver as sd
from pipeline import security_review
from pipeline import settings
from pipeline import triage_cluster


class TestHeadlessPathsImportCanonical:
    """The per-PR headless paths consume the driver's prompts."""

    def test_analyze_and_summarize(self, monkeypatch):
        prompts: list[str] = []
        monkeypatch.setattr(ad.headless_agent, "run_agent",
                            lambda prompt, **kw: prompts.append(prompt) or "{}")
        ad.run_analyze_agent({"cluster": {"id": 1}, "members": []})
        cd.run_summarize_agent([])
        assert prompts[0].startswith(ad.ANALYZE_PROMPT[:100])
        assert prompts[1].startswith(cd.summarize_prompt()[:200])
        for module in (triage_cluster, recluster):
            assert not hasattr(module, "ANALYZE_PROMPT")
            assert not hasattr(module, "summarize_prompt")

    def test_security(self):
        assert security_review.REVIEW_PROMPT is sd.REVIEW_PROMPT
        assert security_review.VERIFY_PROMPT is sd.VERIFY_PROMPT
        assert security_review.LENSES is sd.LENSES

    def test_verify(self):
        from pipeline import verify_driver
        from pipeline import verify_pr
        assert verify_pr.BLIND_PROMPT is verify_driver.BLIND_PROMPT
        assert verify_pr.AUTHOR_PROMPT is verify_driver.AUTHOR_PROMPT
        assert verify_pr.JUDGE_PROMPT is verify_driver.JUDGE_PROMPT

    def test_verify_prompt_layers(self) -> None:
        from pipeline import verify_driver
        from pipeline import verify_pr
        prompts = (
            verify_driver.BLIND_PROMPT,
            verify_driver.AUTHOR_PROMPT,
            verify_driver.JUDGE_PROMPT,
        )
        for prompt in prompts:
            assert prompt.startswith("# Background\n")
            assert prompt.count("# Background") == 1
            assert prompt.count("# Behavior") == 1
            assert "# Output" not in prompt
        tails = (
            verify_pr.BLIND_FENCED_TAIL,
            verify_pr.AUTHOR_FENCED_TAIL,
            verify_pr.JUDGE_FENCED_TAIL,
        )
        assert all(tail.lstrip().startswith("# Output\n") for tail in tails)


class TestPlaceholders:
    def test_each_canonical_body_has_its_placeholders_once(self):
        assert ad.ANALYZE_PROMPT.count("__BUNDLE_PATH__") == 1
        assert cd.SUMMARIZE_PROMPT.count("__BATCH_PATH__") == 1
        assert cd.SUMMARIZE_PROMPT.count("__SUBSYSTEMS__") == 1
        assert cd.SUMMARIZE_PROMPT.count("__REPO__") == 1
        for tok in ("__PR__", "__TITLE__", "__DIFF_PATH__", "__LENS__", "__LENS_PROMPT__"):
            assert sd.REVIEW_PROMPT.count(tok) == 1, tok
        for tok in ("__N__", "__PR__", "__DIFF_PATH__", "__CHUNK__"):
            assert sd.VERIFY_PROMPT.count(tok) == 1, tok

    def test_no_leftover_format_style_placeholders(self):
        # The bodies are filled via str.replace / split-join, never str.format.
        assert "{bundle_path}" not in ad.ANALYZE_PROMPT
        assert "{batch_path}" not in cd.SUMMARIZE_PROMPT
        assert "{pr}" not in sd.REVIEW_PROMPT and "{chunk}" not in sd.VERIFY_PROMPT

    def test_summarize_prompt_fills_the_config_placeholders(self):
        from pipeline import taxonomy
        filled = cd.summarize_prompt()
        assert ", ".join(taxonomy.subsystem_names()) in filled
        assert settings.repo() in filled
        assert "__SUBSYSTEMS__" not in filled
        assert "__REPO__" not in filled

    def test_analyze_requires_whole_pr_coverage_before_close(self):
        assert "account for every substantive primary and secondary change" in ad.ANALYZE_PROMPT

    def test_analyze_requires_downstream_claim_provenance(self):
        assert "trace every guard and required input" in ad.ANALYZE_PROMPT
        assert "attribute the claim explicitly to the author" in ad.ANALYZE_PROMPT
        assert 'Use "diff verified" only' in ad.ANALYZE_PROMPT

    def test_analyze_names_the_history_reads_its_agent_holds(self):
        gh_read = ad.headless_agent.GH_READ
        assert f"`{gh_read} commits <path>`" in ad.ANALYZE_PROMPT
        assert f"`{gh_read} commit <sha>`" in ad.ANALYZE_PROMPT
        assert "__GH_READ__" not in ad.ANALYZE_PROMPT
        assert "git log" not in ad.ANALYZE_PROMPT and "gh api" not in ad.ANALYZE_PROMPT

    def test_analyze_separates_background_behavior_and_output(self) -> None:
        assert ad.ANALYZE_PROMPT.startswith("# Background\n")
        assert ad.ANALYZE_PROMPT.count("# Background") == 1
        assert ad.ANALYZE_PROMPT.count("# Behavior") == 1
        assert "# Output" not in ad.ANALYZE_PROMPT
        assert ad.ANALYZE_FENCED_TAIL.lstrip().startswith("# Output\n")


class TestSecurityReviewScope:
    """A merge conflict is gated by pr_clean's mergeable bar; the security review
    judges the change itself, so it never asks for or upholds such a finding (#473)."""

    def test_no_lens_asks_for_mergeability(self):
        for lens in sd.LENSES:
            assert "mergeab" not in lens["prompt"].lower(), lens["key"]

    def test_review_and_verify_exclude_base_branch_state(self):
        for prompt in (sd.REVIEW_PROMPT, sd.VERIFY_PROMPT):
            assert "merge conflict" in prompt
            assert "never a finding" in prompt or "is not-an-issue" in prompt
