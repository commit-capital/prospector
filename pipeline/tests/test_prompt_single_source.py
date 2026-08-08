"""Each agent prompt the pipeline runs has exactly ONE canonical copy: the driver
owns it, ships it to its Workflow via the file the workflow already reads (the bundle
/ batch index, or the security manifest), and the per-PR headless path imports the same
constant. These tests enforce that invariant — they fail if a copy is re-inlined in a
.js workflow or the workflow and headless paths ever serve different prompt text."""
import json
from pathlib import Path

from pipeline import analyze_driver as ad
from pipeline import cluster_driver as cd
from pipeline import recluster
from pipeline import security_driver as sd
from pipeline import security_review
from pipeline import settings
from pipeline import triage_cluster
from pipeline.store import Store

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


class TestHeadlessPathsImportCanonical:
    """The per-PR headless paths consume the SAME object the driver ships — not a copy."""

    def test_analyze_and_summarize(self):
        assert triage_cluster.ANALYZE_PROMPT is ad.ANALYZE_PROMPT
        assert triage_cluster.summarize_prompt is cd.summarize_prompt
        assert recluster.summarize_prompt is cd.summarize_prompt

    def test_security(self):
        assert security_review.REVIEW_PROMPT is sd.REVIEW_PROMPT
        assert security_review.VERIFY_PROMPT is sd.VERIFY_PROMPT
        assert security_review.LENSES is sd.LENSES

    def test_verify(self):
        from pipeline import verify_driver
        from pipeline import verify_pr
        assert verify_pr.BLIND_PROMPT is verify_driver.BLIND_PROMPT
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

    def test_no_leftover_format_style_placeholders(self):
        # The bodies are filled via str.replace / split-join, never str.format.
        assert "{bundle_path}" not in ad.ANALYZE_PROMPT
        assert "{batch_path}" not in cd.SUMMARIZE_PROMPT
        assert "{pr}" not in sd.REVIEW_PROMPT and "{chunk}" not in sd.VERIFY_PROMPT

    def test_summarize_prompt_fills_the_config_placeholders(self):
        from pipeline import taxonomy
        filled = cd.summarize_prompt()
        assert ", ".join(taxonomy.subsystem_names()) in filled
        assert settings.REPO in filled
        assert "__SUBSYSTEMS__" not in filled
        assert "__REPO__" not in filled

    def test_analyze_requires_whole_pr_coverage_before_close(self):
        assert "account for every substantive primary and secondary change" in ad.ANALYZE_PROMPT


class TestDriversShipTheCanonicalText:
    def test_analyze_index_ships_the_prompt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ad, "BUNDLE_DIR", tmp_path / "bundles")
        monkeypatch.setattr(ad, "ANALYZE_OUT_DIR", tmp_path / "out")
        ad.write_bundles(Store(tmp_path / "store"))
        index = json.loads((tmp_path / "bundles" / "index.json").read_text())
        # The shipped prompt is the canonical constant with the default branch
        # filled in (conftest pins TRIAGE_DEFAULT_BRANCH=trunk).
        assert index["prompt"] == ad.ANALYZE_PROMPT.replace(
            "__BRANCH__", settings.default_branch())
        assert "__BRANCH__" not in index["prompt"]
        assert "trunk" in index["prompt"]

    def test_summarize_index_ships_the_prompt_and_vocabulary(self, tmp_path, monkeypatch):
        from pipeline import taxonomy
        monkeypatch.setattr(cd, "BATCH_DIR", tmp_path / "batches")
        monkeypatch.setattr(cd, "SUMMARY_OUT_DIR", tmp_path / "out")
        cd.write_batches([])
        index = json.loads((tmp_path / "batches" / "index.json").read_text())
        assert index["prompt"] == cd.summarize_prompt()
        assert "__SUBSYSTEMS__" not in index["prompt"]
        assert index["subsystems"] == taxonomy.subsystem_names()

    def test_security_manifest_ships_the_prompts_and_lenses(self, tmp_path):
        m = sd.wave_manifest(Store(tmp_path))
        assert m["review_prompt"] == sd.REVIEW_PROMPT
        assert m["verify_prompt"] == sd.VERIFY_PROMPT
        assert m["lenses"] == sd.LENSES


class TestWorkflowsConsumeNotRestate:
    """The .js workflows read the prompt from the file the driver writes; they must
    carry no copy of the decision prose, so the two paths cannot drift."""

    def test_analyze_js(self):
        src = (WORKFLOWS / "analyze.js").read_text()
        assert "index.prompt.replace('__BUNDLE_PATH__'" in src
        assert "You are the triage analyst" in ad.ANALYZE_PROMPT  # lives in the constant
        assert "You are the triage analyst" not in src            # not in the workflow

    def test_summarize_js(self):
        src = (WORKFLOWS / "summarize.js").read_text()
        assert "index.prompt.replace('__BATCH_PATH__'" in src
        assert "Weight by diffstat" in cd.SUMMARIZE_PROMPT
        assert "Weight by diffstat" not in src
        assert "index.subsystems" in src

    def test_security_js(self):
        src = (WORKFLOWS / "security.js").read_text()
        assert "fill(manifest.review_prompt" in src
        assert "fill(manifest.verify_prompt" in src
        assert "manifest.lenses" in src
        # the lens definitions + review/verify prose live ONLY in the Python constants
        assert "Pre-merge security review" in sd.REVIEW_PROMPT
        assert "Pre-merge security review" not in src
        assert "Adversarially verify" in sd.VERIFY_PROMPT
        assert "Adversarially verify" not in src
        assert "privilege escalation" in sd.LENSES[0]["prompt"]
        assert "privilege escalation" not in src
