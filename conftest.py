"""Repo-root pytest config: make every test session hermetic against a developer's
real .env. Set before any project module imports settings (which loads .env).

The identity values here are deliberately fake and intentionally NOT the real
deployment's repo/bot — a test that silently depends on the real identity fails
here, which is the point. setdefault so a real shell export still wins."""
import os
import tempfile

os.environ["TRIAGE_SKIP_DOTENV"] = "1"
# Keep tests off the shared DB, and keep parallel workers off each other. Under
# `pytest -n auto` (xdist) every worker process imports prospector_app.backend.data,
# whose module-level `_store = Store()` runs create_all on the default SQLite store;
# a single shared file makes concurrent workers race ("table prs already exists").
# Hand each worker a fresh private store DB. Serial runs just drop TRIAGE_STORE_URL
# so a bare Store() lands on the local default path. Either way a real
# TRIAGE_STORE_URL export is discarded — the shared store is never touched.
_worker = os.environ.get("PYTEST_XDIST_WORKER")
if _worker:
    _store_dir = tempfile.mkdtemp(prefix=f"triage-{_worker}-")
    os.environ["TRIAGE_STORE_URL"] = f"sqlite:///{_store_dir}/store.db"
else:
    os.environ.pop("TRIAGE_STORE_URL", None)
# The verify scratch directory defaults to one per repository under the home
# directory, shared by every process that uses it: two test workers writing the
# same `autofix/pr-1.related-tests.patch` there read each other's. Each test
# process gets a private one, and a real TRIAGE_VERIFY_SCRATCH export is
# discarded.
os.environ["TRIAGE_VERIFY_SCRATCH"] = tempfile.mkdtemp(
    prefix=f"triage-scratch-{_worker or 'serial'}-")
# The developer's Docker daemon is the one a live verify worker on this machine
# boots its pinned base image from, and verify_gc's sweep ends in `docker rmi`.
# Every docker call a test leaves unstubbed reaches this socket and fails.
os.environ["DOCKER_HOST"] = "unix:///nonexistent/prospector-tests.sock"
os.environ.pop("DOCKER_CONTEXT", None)
# A developer's own worker name must never leak into a test's host stamps.
os.environ.pop("TRIAGE_WORKER_ID", None)
os.environ.setdefault("TRIAGE_REPO", "test-owner/test-repo")
os.environ.setdefault("TRIAGE_BOT_LOGIN", "test-bot")
# A deliberately non-"master", non-"main" branch: tests that build prompts or
# comments fail if they assume a fixed branch name, and settings.default_branch()
# never shells out to gh during a test run.
os.environ.setdefault("TRIAGE_DEFAULT_BRANCH", "trunk")
os.environ.setdefault("PROSPECTOR_FEEDBACK_REPO", "test-owner/test-meta-repo")
# Fixture policy profile with deliberately fake vocabulary — a test that
# silently depends on the real deployment's taxonomy fails here, which is the
# point (same idiom as the fake repo/bot identity above). All three suites
# (pipeline, issue_triage, app) run under it, so editing the fixture can
# move assertions in any of them.
os.environ.setdefault("TRIAGE_PROFILE", os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "pipeline", "tests", "fixtures", "profile.json"))
# Unattended agent work is marked by this variable; a developer's shell must
# never make a test's agent calls gated.
os.environ.pop("PROSPECTOR_UNATTENDED", None)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_ai_account(monkeypatch):
    """`capacity.account()` asks the machine's real Claude CLI who is signed
    in; no test reaches it. A test that needs an account sets one."""
    from pipeline import capacity
    monkeypatch.setattr(capacity, "account", lambda refresh=False: None)


@pytest.fixture(autouse=True)
def _no_bot_account_lookup(monkeypatch):
    """`resubmit_identity.commit_identity` asks GitHub for the App's bot
    account; no test reaches it, so machine commits carry the push user. A test
    that needs the App's account sets one."""
    from prospector_app.backend import resubmit_identity
    monkeypatch.setattr(resubmit_identity, "_account_id", lambda login: None)


@pytest.fixture(autouse=True)
def _no_related_pr_search(monkeypatch):
    """`related_prs.search` asks GitHub's search which pull requests name an
    issue; no test reaches it. A test that needs pull requests found sets them."""
    from issue_triage import related_prs
    monkeypatch.setattr(related_prs, "search", lambda issue, exclude=None: [])


@pytest.fixture(autouse=True)
def _no_trust_boundary_reviewer(monkeypatch):
    """`trust_boundary.review` runs an agent over every fix that clears the
    lane's other checks; no test reaches it, so a fix crosses no boundary the
    reviewer names. A test that needs crossings sets them."""
    from issue_triage import trust_boundary
    monkeypatch.setattr(trust_boundary, "review",
                        lambda *a, **k: {"crossings": [], "reason": "no reviewer in tests"})


@pytest.fixture(autouse=True)
def _no_intake_reviewer(monkeypatch):
    """`intake_audit.review` runs an agent over an outsider's report before the
    factory builds it; no test reaches it, so a report reads clear. A test that
    needs another verdict sets one."""
    from issue_triage import intake_audit
    monkeypatch.setattr(intake_audit, "review", lambda *a, **k: {
        "verdict": "clear", "findings": [], "reason": "no reviewer in tests"})


class _OfflineGitHub:
    """threat_evidence.GitHubReads that answers nothing."""

    def pull(self, n): return None
    def compare(self, base, head): return None
    def compare_diff(self, base, head): return None
    def listing_diff(self, n): return None
    def force_pushes(self, n): return None
    def user(self, login): return None
    def login(self): return None


@pytest.fixture(autouse=True)
def _no_evidence_github_reads(monkeypatch):
    """A scan that flags a PR captures its evidence through
    `threat_evidence.LiveGitHub`; no test reaches GitHub through it. A test
    that needs answers passes its own reads or sets LiveGitHub."""
    from pipeline import threat_evidence
    monkeypatch.setattr(threat_evidence, "LiveGitHub", _OfflineGitHub)
