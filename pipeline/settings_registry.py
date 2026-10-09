"""Every environment setting the code reads, in one list: what it means, its
default, and whether the Setup page may change it.

`SETTINGS` is what the Setup page's settings panel shows and what
`worker_control` may write (`editable()`); `INTERNAL` names the variables the
code reads that are not an operator's to set — what Prospector hands its own
child processes, CI's, and the shell's. A test holds every variable the code
reads to one of the two, so a setting cannot be added without appearing here.
Nothing editable names a credential, a path, or the store: those are set by
the setup wizard's own steps.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# What each group means on the Setup page.
GROUPS = {
    "behavior": "What the automation does",
    "machine": "This machine's sandbox",
    "workers": "Worker lanes",
    "deployment": "Deployment",
    "advanced": "Advanced",
}


@dataclass(frozen=True)
class Setting:
    name: str
    group: str
    kind: str  # bool | int | choice | list | text | path | secret
    label: str
    default: str
    help: str
    editable: bool = False
    choices: tuple[str, ...] = ()
    minimum: int = 1
    maximum: int | None = None
    on_by_default: bool = False


def _b(name: str, group: str, label: str, help: str, *, editable: bool = True,
       on_by_default: bool = False) -> Setting:
    return Setting(name, group, "bool", label, "on" if on_by_default else "off", help,
                   editable=editable, on_by_default=on_by_default)


def _i(name: str, group: str, label: str, default: str, help: str, *, editable: bool = True,
       minimum: int = 1, maximum: int | None = None) -> Setting:
    return Setting(name, group, "int", label, default, help, editable=editable,
                   minimum=minimum, maximum=maximum)


SETTINGS: tuple[Setting, ...] = (
    # --- what the automation does ------------------------------------------------
    Setting("TRIAGE_AGENT_MODEL", "behavior", "choice", "Agent model", "opus",
            "The model every headless agent runs on.", editable=True,
            choices=("opus", "sonnet", "haiku")),
    Setting("TRIAGE_ISSUE_FIX_MODELS", "behavior", "list", "Issue-fix candidate models",
            "opus,sonnet,opus",
            "One cross-tested issue-fix candidate per model, comma-separated.", editable=True),
    Setting("TRIAGE_ISSUE_FIX_FOLLOWUP", "behavior", "choice", "Follow up proposed PRs", "live",
            "What the issue-fix worker does with a pull request it opened that is not yet "
            "green: live, dry-run (notes only), or off.", editable=True,
            choices=("live", "dry-run", "off")),
    Setting("TRIAGE_ISSUE_FIX_PUBLIC", "behavior", "choice", "Issue fixes on GitHub", "live",
            "For issues in scope, the issue-fix worker sets a status label, posts its "
            "conclusions, opens fixed pull requests, asks its questions, and acts on "
            "replies on GitHub: live, dry-run (notes only), or off.", editable=True,
            choices=("live", "dry-run", "off")),
    Setting("TRIAGE_ISSUE_FIX_PUBLIC_SCOPE", "behavior", "choice",
            "Issues fixed on GitHub", "maintainers",
            "Which issues the GitHub loop serves: the ones maintainers filed, all, or "
            "all-dry-run (every issue, with the community's held to dry-run).",
            editable=True, choices=("maintainers", "all", "all-dry-run")),
    _b("TRIAGE_ISSUE_FIX_SUITE", "behavior", "Run the full suite on issue fixes",
       "A fix that clears its other checks also runs the repository's full test suite.",
       on_by_default=True),
    _i("TRIAGE_ISSUE_FIX_HUNT_BUDGET", "behavior", "Issue-fix hunts per day", "5",
       "How many attempts the issue-fix hunter starts on its own per UTC day."),
    _i("TRIAGE_ISSUE_FIX_MAX_LINES", "behavior", "Issue-fix size cap (lines)", "300",
       "The most changed lines an issue fix may carry."),
    _b("TRIAGE_FIX_HUNT_SECURITY", "behavior", "Fix YELLOW security findings",
       "The autofix hunter may queue a fix for a PR's current YELLOW security finding."),
    _b("TRIAGE_PR_WATCH", "behavior", "Watch GitHub for new PRs",
       "A worker records new and pushed-to PRs every 15 minutes, so the threat scan reads them.",
       on_by_default=True),
    _b("TRIAGE_FIX_HUNT_REREVIEW", "behavior", "Ask reviewers to re-review",
       "Post a reviewer's mention as the bot when its verdict is stale on a green PR.",
       on_by_default=True),
    _i("TRIAGE_FIX_HUNT_LIMIT", "behavior", "Hunted fixes in flight", "3",
       "The most hunter-queued agent fixes running at once."),
    _i("TRIAGE_CLUSTER_DAILY_PRS", "behavior", "PRs clustered per day", "150",
       "PRs the clustering worker summarizes and places into clusters per UTC day."),
    _i("TRIAGE_CLUSTER_DAILY_CLUSTERS", "behavior", "Clusters analyzed per day", "30",
       "Clusters the clustering worker analyzes per UTC day."),
    _i("TRIAGE_FIX_OBJECTION_BUDGET", "behavior", "Objection fixes per day", "20",
       "Continuations from a machine objection, per worker per UTC day."),
    _i("TRIAGE_REREVIEW_BUDGET", "behavior", "Re-review requests per day", "40",
       "Reviewer re-review mentions, per worker per UTC day."),
    _i("TRIAGE_FIX_AUTOPUSH_MIN_TIER", "behavior", "Lowest risk tier pushed unattended", "2",
       "An unattended fix push needs every touched path at or above this risk tier "
       "(0 is the highest risk).", maximum=3),
    _i("TRIAGE_FIX_AUTOPUSH_MAX_LINES", "behavior", "Unattended push size cap (lines)", "300",
       "The most changed lines a fix may push unattended."),
    _i("TRIAGE_REVIEWER_ACTIVE_DAYS", "behavior", "Reviewer activity window (days)", "14",
       "In auto review mode, a reviewer gates PRs when it posted within this many days."),
    _i("TRIAGE_VERIFY_MAX_AGE_DAYS", "behavior", "Verification window (days)", "30",
       "How long a passing or inconclusive sandbox verification counts for merge before "
       "it must be re-run. A failing one blocks until a re-run, whatever its age."),
    _i("TRIAGE_VERIFY_PIN_MAX_AGE_DAYS", "behavior", "Verify base age limit (days)", "2",
       "A verify worker whose pinned default-branch base has not been brought current "
       "for this many days verifies nothing until its pin is rebuilt."),
    # --- this machine's sandbox -----------------------------------------------------
    _i("TRIAGE_SANDBOX_LARGE_SLOTS", "machine", "Large sandbox phases at once", "auto",
       "Compile, build and full-suite phases (10 GB each) run at once; auto sizes it to "
       "the Docker VM's memory.", maximum=8),
    _i("TRIAGE_SANDBOX_LARGE_CPUS", "machine", "CPUs per large phase", "auto",
       "CPUs each large phase gets; auto shares the Docker VM's CPUs.", maximum=32),
    # --- worker lanes (the Setup page's worker switches) -----------------------------
    _b("TRIAGE_VERIFY_WORKER", "workers", "Verify worker", "This machine verifies PRs."),
    _b("TRIAGE_VERIFY_AUTOHUNT", "workers", "Verify hunter",
       "An idle verify worker picks its own work."),
    _b("TRIAGE_FIX_WORKER", "workers", "Autofix worker", "This machine runs PR autofixes."),
    _b("TRIAGE_FIX_AUTOHUNT", "workers", "Autofix hunter",
       "An idle autofix worker queues update and rebase actions itself."),
    _b("TRIAGE_FIX_HUNT_FIX", "workers", "Hunt agent fixes",
       "The autofix hunter may queue agent-written fixes."),
    _b("TRIAGE_FIX_HUNT_RESOLVE", "workers", "Hunt conflict resolves",
       "A conflicted hunted rebase may escalate to an agent resolve."),
    Setting("TRIAGE_FIX_AUTOPUSH", "workers", "list", "Autofix actions that push unattended",
            "(none)", "Comma-separated autofix actions that push without approval.",
            editable=True),
    _b("TRIAGE_ISSUE_FIX_WORKER", "workers", "Issue-fix worker",
       "This machine runs the issue-fix factory."),
    _b("TRIAGE_CLUSTER_WORKER", "workers", "Clustering worker",
       "This machine summarizes, clusters, and analyzes new PRs every hour, within the "
       "daily limits. One machine runs it at a time."),
    _b("TRIAGE_ISSUE_FIX_HUNT", "workers", "Issue-fix hunter",
       "An idle issue-fix worker starts attempts on fresh issues itself."),
    Setting("TRIAGE_WORKER_ID", "workers", "text", "Worker name", "(host name)",
            "The name this machine stamps on its work.", editable=True),
    # --- deployment (set by the setup wizard's steps) --------------------------------
    Setting("TRIAGE_REPO", "deployment", "text", "Repository", "(required)",
            "The repository being triaged."),
    Setting("TRIAGE_STORE_URL", "deployment", "secret", "Shared database", "(local SQLite)",
            "The shared store's URL."),
    Setting("TRIAGE_PROFILE", "deployment", "path", "Repository profile", "(generic)",
            "The repository policy profile."),
    Setting("TRIAGE_DISPLAY_NAME", "deployment", "text", "Display name", "(repository name)",
            "The product name the app shows."),
    Setting("TRIAGE_DEFAULT_BRANCH", "deployment", "text", "Default branch", "(read from GitHub)",
            "The repository's default branch."),
    Setting("TRIAGE_BOT_LOGIN", "deployment", "text", "Bot login", "(none: writes are dry runs)",
            "The GitHub App writes post as."),
    Setting("TRIAGE_BOT_APP_ID", "deployment", "text", "Bot app id", "(none)",
            "The GitHub App's id."),
    Setting("TRIAGE_BOT_KEY_FILE", "deployment", "path", "Bot private key", "(none)",
            "The GitHub App's private key file."),
    Setting("TRIAGE_PUSH_LOGIN", "deployment", "text", "Push user", "(none)",
            "The GitHub user that pushes fixes."),
    Setting("TRIAGE_PUSH_EMAIL", "deployment", "text", "Push user email", "(none)",
            "The push user's no-reply email, its commit email when no App is configured."),
    Setting("TRIAGE_PUSH_SSH_KEY_FILE", "deployment", "path", "Push user SSH key", "(none)",
            "The push user's SSH key file."),
    Setting("TRIAGE_REVIEW_PROVIDER", "deployment", "text", "Review provider", "auto",
            "Which reviewers gate a merge: auto, none, or reviewer ids."),
    Setting("TRIAGE_REVIEW_THRESHOLD", "deployment", "int", "Greptile pass score", "5",
            "The Greptile score a PR needs."),
    Setting("TRIAGE_AGENT_PROVIDER", "deployment", "choice", "In-app agent", "claude",
            "The CLI behind the in-app agent pane.", choices=("claude", "codex", "none")),
    Setting("PROSPECTOR_FEEDBACK_REPO", "deployment", "text", "Feedback repository", "(none)",
            "Where the feedback button files issues."),
    Setting("TRIAGE_SLACK_WEBHOOK_URL", "deployment", "secret", "Slack alerts webhook",
            "(off)", "The Slack incoming webhook malicious PRs and maintainer credential "
            "leaks are posted to."),
    Setting("PROSPECTOR_FEEDBACK_TOKEN", "deployment", "secret", "Feedback token",
            "(gh's stored login)",
            "The token the in-app agent files feedback-repository issues with."),
    # --- advanced: development, emergencies, plumbing --------------------------------
    Setting("TRIAGE_VERIFY_SCRATCH", "advanced", "path", "Verify scratch directory",
            "~/.pr-triage-verify/<owner>-<name>", "Clones, patches and logs for the workers."),
    Setting("TRIAGE_VERIFY_PROBE_DENY", "advanced", "text", "Sandbox probe deny list",
            "(built in)", "Host services the sandbox boot probe must fail to reach."),
    Setting("TRIAGE_STORE_ALLOW_STALE", "advanced", "bool", "Allow an older checkout to write",
            "off", "Emergency only: lets a checkout behind the store's schema write."),
    Setting("TRIAGE_STORE_ALLOW_FOREIGN_REPO", "advanced", "bool",
            "Allow writes for another repository", "off",
            "Emergency only: lets the activity log write when the store names another repo."),
    Setting("TRIAGE_SKIP_DOTENV", "advanced", "bool", "Skip .env", "off",
            "Tests only: do not load the repo-root .env."),
    Setting("PROSPECTOR_CACHE_DIR", "advanced", "path", "Snapshot cache directory",
            "~/.cache/prospector", "Where the app keeps its local copy of the store."),
    Setting("PROSPECTOR_LIVE_TTL_MIN", "advanced", "int", "Live sweep freshness (minutes)", "60",
            "How old the last live GitHub sweep may be before a launch runs another."),
    Setting("PROSPECTOR_NO_LAUNCH_SWEEP", "advanced", "bool", "Skip the launch sweep", "off",
            "Do not refresh live GitHub state at launch."),
    Setting("PROSPECTOR_REVIEW_REFRESH_POLL_SECONDS", "advanced", "int",
            "Review refresh poll (seconds)", "10",
            "How often a re-review wait polls for the verdict."),
    Setting("PROSPECTOR_REVIEW_REFRESH_ATTEMPTS", "advanced", "int", "Review refresh polls", "30",
            "How many polls a re-review wait makes."),
    Setting("PROSPECTOR_OPERATOR", "advanced", "text", "Operator name", "(git user)",
            "The name stamped on the activity log."),
    Setting("ANTHROPIC_API_KEY", "advanced", "secret", "Anthropic API key", "(none)",
            "Feedback issue drafting and rationale rewording; both work without it."),
    Setting("API_PORT", "advanced", "int", "Backend port", "8787",
            "Set per checkout by setup.sh."),
    Setting("VITE_PORT", "advanced", "int", "Frontend dev port", "5173",
            "Set per checkout by setup.sh."),
    Setting("PR_VERIFY_NET", "advanced", "text", "Sandbox Docker network", "pr-verify-net",
            "The Docker network sandbox containers join."),
    Setting("PR_VERIFY_DEBUG", "advanced", "bool", "Sandbox debug output", "off",
            "Development only: verbose sandbox launcher output."),
    Setting("HARNESS_DEP_SEED_REPO", "advanced", "path", "Review harness dependency seed",
            "(none)", "Development only: a local repo the review harness seeds dependencies "
            "from."),
    Setting("CODEX_HOME", "advanced", "path", "Codex home", "~/.codex",
            "Where the Codex CLI keeps its login."),
    Setting("SSL_CERT_FILE", "advanced", "path", "CA bundle", "(system)",
            "A custom CA bundle for direct API calls."),
    Setting("REQUESTS_CA_BUNDLE", "advanced", "path", "CA bundle (requests)", "(system)",
            "A custom CA bundle for direct API calls."),
    Setting("DOCKER_HOST", "advanced", "text", "Docker host", "(default)",
            "Passed through to the sandbox launcher."),
    Setting("DOCKER_CONTEXT", "advanced", "text", "Docker context", "(default)",
            "Passed through to the sandbox launcher."),
    Setting("DOCKER_CONFIG", "advanced", "path", "Docker config", "(default)",
            "Passed through to the sandbox launcher."),
)

# Variables the code reads that are not an operator's to set, by prefix or name.
INTERNAL: tuple[str, ...] = (
    "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_ACTIONS", "GH_CONFIG_DIR", "GH_REPO",
    "CLAUDE_PROJECT_DIR", "PROSPECTOR_PYTHON", "PROSPECTOR_GIT_WORKTREE",
    "PROSPECTOR_CHECK_", "PROSPECTOR_ISSUE_CHECK_", "PROSPECTOR_RESUBMIT_",
    "PROSPECTOR_E2E_SCRATCH", "PROSPECTOR_PROGRESS", "PROSPECTOR_UNATTENDED", "PROSPECTOR_AGENT_LANE",
    "SUITE_CONFIG",
    "PROBE_DENY", "HOME", "TZ",
    "PYTEST_XDIST_WORKER",
)

BY_NAME = {s.name: s for s in SETTINGS}
_LIST_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")


def editable() -> tuple[str, ...]:
    return tuple(s.name for s in SETTINGS if s.editable)


def is_internal(name: str) -> bool:
    return any(name == p or (p.endswith("_") and name.startswith(p)) for p in INTERNAL)


def validate(name: str, value: str) -> str:
    """`value` as `name` is written to .env, or ValueError when the code would
    not read it as meant. Empty means the default, written so the code reads
    it as the default."""
    s = BY_NAME[name]
    v = str(value).strip()
    if s.kind == "bool":
        allowed = ("1", "0") if s.on_by_default else ("1", "")
        if v not in allowed and v != "":
            raise ValueError(f"{name} is {' or '.join(repr(a) for a in allowed)}, not {v!r}")
        return "1" if s.on_by_default and v == "" else v
    if v == "":
        return s.default if s.kind == "choice" else v
    if s.kind == "int":
        try:
            n = int(v)
        except ValueError:
            raise ValueError(f"{name} is a whole number, not {v!r}") from None
        if n < s.minimum or (s.maximum is not None and n > s.maximum):
            top = f" to {s.maximum}" if s.maximum is not None else " or more"
            raise ValueError(f"{name} is {s.minimum}{top}, not {n}")
        return str(n)
    if s.kind == "choice":
        if v not in s.choices:
            raise ValueError(f"{name} is one of {', '.join(s.choices)}, not {v!r}")
        return v
    if s.kind == "list":
        parts = [p.strip() for p in v.split(",") if p.strip()]
        if not parts or not all(_LIST_TOKEN.match(p) for p in parts):
            raise ValueError(f"{name} is a comma-separated list of names, not {v!r}")
        return ",".join(parts)
    return v
