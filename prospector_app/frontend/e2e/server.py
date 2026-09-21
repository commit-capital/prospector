"""Real app + SQL store, deterministic external boundaries. Never a production entrypoint.

Launched by the Node fixture with a minimal environment and fresh scratch dir.
No test routes or policy overrides: the browser uses the production API, gates,
executor, snapshot, and activity log. Unknown external calls fail the suite even
when application code catches the exception.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
SCRATCH = Path(os.environ["PROSPECTOR_E2E_SCRATCH"])


def forbidden(description):
    with (SCRATCH / "unexpected.jsonl").open("a") as f:
        f.write(json.dumps(description) + "\n")
    raise RuntimeError(f"Unexpected e2e external operation: {description}")


def audit(event, args):
    if event == "subprocess.Popen":
        forbidden({"subprocess": str(args[0])})
    if event == "socket.connect" and args[0].family in (socket.AF_INET, socket.AF_INET6):
        if args[1][0] not in ("127.0.0.1", "::1"):
            forbidden({"connect": str(args[1])})


sys.addaudithook(audit)

# Configuration is established before importing any module that loads settings.
os.environ.update({
    "TRIAGE_SKIP_DOTENV": "1",
    "TRIAGE_REPO": "e2e-owner/e2e-repo",
    "TRIAGE_BOT_LOGIN": "e2e-bot",
    "TRIAGE_DEFAULT_BRANCH": "trunk",
    "TRIAGE_STORE_URL": f"sqlite:///{SCRATCH / 'store.db'}",
    "TRIAGE_REVIEW_PROVIDER": "none",
    "TRIAGE_AGENT_PROVIDER": "none",
    "TRIAGE_VERIFY_SCRATCH": str(SCRATCH / "verify"),
    "PROSPECTOR_OPERATOR": "E2E Operator",
    "PROSPECTOR_FEEDBACK_REPO": "",
})
profile = SCRATCH / "profile.json"
profile.write_text(json.dumps({"version": 1}))
os.environ["TRIAGE_PROFILE"] = str(profile)

from pipeline import gh  # noqa: E402
from pipeline.store import Store  # noqa: E402
from prospector_app.backend import safety_guard  # noqa: E402

HEAD = "a" * 40
NOW = datetime.now(timezone.utc).isoformat()
store = Store()
for number, title in ((101, "Fix retry counter"), (102, "Blocked security fixture")):
    record = {
        "pr": number,
        "meta": {"title": title, "body": f"Description for {title}.",
                 "author": "fixture-author", "state": "open", "draft": False,
                 "head_sha": HEAD, "base": "trunk",
                 "url": f"https://github.com/e2e-owner/e2e-repo/pull/{number}",
                 "created_at": NOW, "updated_at": NOW, "checked_at": NOW},
        "signals": {"against_head_sha": HEAD, "checked_at": NOW,
                    "ci": "passing", "mergeable": True,
                    "diffstat": {"additions": 1, "deletions": 1, "changed_files": 1}},
        "summary": {"against_head_sha": HEAD, "checked_at": NOW,
                    "one_liner": f"Evidence for PR {number}", "paths": ["src/retry.py"]},
    }
    if number == 102:
        record["threat"] = {"verdict": "malicious", "against_head_sha": HEAD,
                            "checked_at": NOW}
    store.save_pr(record)


def github_api(path, **kwargs):
    if path.endswith("/check-runs?per_page=100&filter=latest"):
        return {"check_runs": []}
    return forbidden({"gh_api": path})


def github_graphql(query, **kwargs):
    matches = re.findall(r"(p\d+): pullRequest\(number: (101|102)\)", query)
    if not matches or "statusCheckRollup" not in query:
        return forbidden({"graphql": query})
    nodes = {alias: {
        "number": int(number), "state": "OPEN", "merged": False,
        "headRefOid": HEAD, "mergeable": "MERGEABLE", "updatedAt": NOW,
        "additions": 1, "deletions": 1, "changedFiles": 1,
        "files": {"nodes": [{"path": "src/retry.py"}]},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {
            "nodes": [{"__typename": "StatusContext", "context": "test", "state": "SUCCESS"}],
        }}}}]},
    } for alias, number in matches}
    return {"data": {"repository": nodes}}


def github_read(argv, **kwargs):
    try:
        safety_guard.assert_read_only(argv)
    except safety_guard.WriteAttemptBlocked:
        return forbidden({"non_read_command": argv})
    if argv == ["gh", "api", "user", "--jq", ".login"]:
        output = "e2e-operator"
    elif (argv[:4] == ["gh", "api", "graphql", "-f"] and len(argv) == 5
          and "timelineItems" in argv[4]
          and re.search(r"pullRequest\(number: (101|102)\)", argv[4])):
        output = json.dumps({"data": {"repository": {"pullRequest": {
            field: {"nodes": []} for field in ("comments", "reviews", "commits", "timelineItems")
        }}}})
    elif (argv[:2] == ["gh", "api"] and "--jq" in argv
          and argv[argv.index("--jq") + 1] == ".[].filename"):
        output = "src/retry.py\n"
    elif (argv[:2] == ["gh", "api"] and len(argv) == 5
          and argv[2] in ("repos/e2e-owner/e2e-repo/pulls/101", "repos/e2e-owner/e2e-repo/pulls/102")
          and argv[4].startswith("{state:")):
        output = json.dumps({"state": "open", "head": HEAD, "merged": False,
                             "mergeable_state": "clean"})
    else:
        return forbidden({"gh_read": argv})
    return subprocess.CompletedProcess(argv, 0, output, "")


gh.gh_api = github_api
gh.gh_graphql = github_graphql
safety_guard.run = github_read
# A few UI metadata helpers invoke gh directly rather than through safety_guard.
# They get the same strict fixture transport; Popen remains blocked by the audit.
subprocess.run = github_read

from prospector_app.backend import app as appmod  # noqa: E402
from prospector_app.backend import executor, instance, service  # noqa: E402

# Missing token helper exercises the real no-token capability and dry-run paths.
executor.GET_TOKEN = SCRATCH / "no-token-helper"
instance._cache = {"branch": "main", "worktree": "e2e"}
service.DIFF_CACHE = SCRATCH / "diffs"
service.PIPELINE_DIFF_CACHE = SCRATCH / "pipeline-diffs"
service.DIFF_CACHE.mkdir()
(service.DIFF_CACHE / f"{HEAD}.diff").write_text(
    "diff --git a/src/retry.py b/src/retry.py\n"
    "--- a/src/retry.py\n+++ b/src/retry.py\n@@ -1 +1 @@\n-retries = 0\n+retries = 1\n")


@appmod.app.middleware("http")
async def record_requests(request, call_next):
    if request.method == "POST":
        body = (await request.body()).decode()
        with (SCRATCH / "requests.jsonl").open("a") as f:
            f.write(json.dumps({"path": request.url.path, "query": request.url.query,
                                "body": json.loads(body) if body else None}) + "\n")
    return await call_next(request)


if __name__ == "__main__":
    import uvicorn

    # Bind an OS-assigned port; no check-then-bind race with another test run.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    (SCRATCH / "port").write_text(str(sock.getsockname()[1]))
    # Workers/sweeps are external services; these journeys exercise HTTP requests.
    config = uvicorn.Config(appmod.app, lifespan="off", log_level="info")
    uvicorn.Server(config).run(sockets=[sock])
