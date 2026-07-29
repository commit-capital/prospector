"""Repo-root pytest config: make every test session hermetic against a developer's
real .env. Set before any project module imports settings (which loads .env).

`settings` requires TRIAGE_REPO and TRIAGE_BOT_LOGIN (no defaults), so supply
deliberately fake identity values here. They are intentionally NOT the real
deployment's repo/bot — a test that silently depends on the real identity fails
here, which is the point. setdefault so a real shell export still wins."""
import os

os.environ["TRIAGE_SKIP_DOTENV"] = "1"
# Keep tests off the shared DB, and keep parallel workers off each other. Under
# `pytest -n auto` (xdist) every worker process imports review_cockpit.backend.data,
# whose module-level `_store = Store()` runs create_all on the default SQLite store;
# a single shared file makes concurrent workers race ("table prs already exists").
# Hand each worker a fresh private store DB. Serial runs just drop TRIAGE_STORE_URL
# so a bare Store() lands on the local default path. Either way a real
# TRIAGE_STORE_URL export is discarded — the shared store is never touched.
_worker = os.environ.get("PYTEST_XDIST_WORKER")
if _worker:
    import tempfile
    _store_dir = tempfile.mkdtemp(prefix=f"triage-{_worker}-")
    os.environ["TRIAGE_STORE_URL"] = f"sqlite:///{_store_dir}/store.db"
else:
    os.environ.pop("TRIAGE_STORE_URL", None)
os.environ.setdefault("TRIAGE_REPO", "test-owner/test-repo")
os.environ.setdefault("TRIAGE_BOT_LOGIN", "test-bot")
# A deliberately non-"master", non-"main" branch: tests that build prompts or
# comments fail if they assume a fixed branch name, and settings.default_branch()
# never shells out to gh during a test run.
os.environ.setdefault("TRIAGE_DEFAULT_BRANCH", "trunk")
os.environ.setdefault("COCKPIT_FEEDBACK_REPO", "test-owner/test-meta-repo")
# Fixture policy profile with deliberately fake vocabulary — a test that
# silently depends on the real deployment's taxonomy fails here, which is the
# point (same idiom as the fake repo/bot identity above). All three suites
# (pipeline, issue_triage, cockpit) run under it, so editing the fixture can
# move assertions in any of them.
os.environ.setdefault("TRIAGE_PROFILE", os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "pipeline", "tests", "fixtures", "profile.json"))
