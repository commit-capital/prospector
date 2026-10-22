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

    def test_summarize_prompt_fills_the_config_placeholders(self):
        from pipeline import taxonomy
        filled = cd.summarize_prompt()
        assert ", ".join(taxonomy.subsystem_names()) in filled
        assert settings.repo() in filled
        assert "__SUBSYSTEMS__" not in filled
        assert "__REPO__" not in filled

    def test_analyze_names_only_the_history_reader_its_agent_holds(self):
        # The agent holds no git or gh rule (#275), so the prompt offers neither.
        assert ad.headless_agent.GH_READ in ad.ANALYZE_PROMPT
        assert "__GH_READ__" not in ad.ANALYZE_PROMPT
        assert "git log" not in ad.ANALYZE_PROMPT and "gh api" not in ad.ANALYZE_PROMPT


class TestSecurityReviewScope:
    """A merge conflict is gated by pr_clean's mergeable bar; the security review
    judges the change itself, so no lens asks for one (#473)."""

    def test_no_lens_asks_for_mergeability(self):
        for lens in sd.LENSES:
            assert "mergeab" not in lens["prompt"].lower(), lens["key"]
