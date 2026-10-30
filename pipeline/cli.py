"""Umbrella CLI for the triage pipeline + app.

`prospector <command>` dispatches to the existing tools: each handler
lazily imports its tool and forwards the remaining argv to the tool's main(),
so the tools stay independently runnable (`uv run python pipeline/ingest.py`).
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable

from pipeline import settings

USAGE = """\
usage: prospector <command> [args]

commands:
  serve            run the app (API + built frontend); --dev runs the hot-reload dev servers
  ingest           refresh open PRs + issue links into the store
  threat-scan      deterministic threat scan over each PR's whole diff
  status           regenerate STATUS.md from the store
  triage-cluster   refresh one cluster's member facts + re-classify
  recluster        re-summarize + re-cluster one cluster's members
  security-review  3-lens adversarial security review of one PR
  activity-backfill  reconstruct missing activity-log entries for bot closes (dry-run default)
  triage-backfill  judge the fixability of every huntable issue not yet triaged, while AI capacity allows
  feature-screen-backfill  screen every open feature request not yet screened or decided, while AI capacity allows
  backlog-sweep    bring stale author-finished PRs current, or ask their authors to rebase (dry-run default)
  abandoned-close  list the PRs the abandoned-PR lane would close, and why (read-only)
  drain            let this machine's worker lanes finish their runs before a restart

`prospector <command> --help` shows the command's own options.
"""


def _serve(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="prospector serve", description="Run the app server.")
    parser.add_argument("--dev", action="store_true", help="run the hot-reload dev servers (uvicorn --reload + Vite)")
    parser.add_argument("--port", type=int, default=None, help="API port (default: API_PORT from .env, else 8787)")
    args = parser.parse_args(argv)
    if args.dev:
        if args.port is not None:
            print(
                "--port applies to serve without --dev; the dev servers read API_PORT/VITE_PORT from .env",
                file=sys.stderr,
            )
            return 2
        from pipeline import devserve

        return devserve.run()
    settings.load_env_file()
    port: int = args.port if args.port is not None else int(os.environ.get("API_PORT", "8787"))
    dist = settings.REPO_ROOT / "prospector_app" / "frontend" / "dist"
    if not dist.is_dir():
        print(
            "note: no built frontend (prospector_app/frontend/dist) — serving the API only.\n"
            "      build it with: pnpm --dir prospector_app/frontend build",
            file=sys.stderr,
        )
    import uvicorn

    uvicorn.run("prospector_app.backend.app:app", port=port)
    return 0


def _ingest(argv: list[str]) -> int:
    from pipeline import ingest

    return ingest.main(argv)


def _threat_scan(argv: list[str]) -> int:
    from pipeline import threat_scan

    return threat_scan.main(argv)


def _status(argv: list[str]) -> int:
    if argv:
        print("prospector status takes no arguments", file=sys.stderr)
        return 2
    from pipeline import views

    return views.main()


def _triage_cluster(argv: list[str]) -> int:
    from pipeline import triage_cluster

    return triage_cluster.main(argv)


def _recluster(argv: list[str]) -> int:
    from pipeline import recluster

    return recluster.main(argv)


def _security_review(argv: list[str]) -> int:
    from pipeline import security_review

    return security_review.main(argv)


def _activity_backfill(argv: list[str]) -> int:
    from prospector_app.backend import activity_backfill

    return activity_backfill.main(argv)


def _triage_backfill(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="prospector triage-backfill",
        description="Judge the fixability of the huntable issues with no current triage, newest "
                    "first, on this machine's pinned base. Stops when AI capacity closes; rerun to resume.")
    parser.add_argument("--limit", type=int, default=None, help="judge at most this many")
    args = parser.parse_args(argv)
    from issue_triage.issue_store import IssueStore
    from prospector_app.backend import issue_fix_worker

    judged = issue_fix_worker.triage(IssueStore(), args.limit, trigger="backfill")
    print(f"triaged {len(judged)} issues")
    return 0


def _feature_screen_backfill(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="prospector feature-screen-backfill",
        description="Screen the open feature requests with no current screen or decision, newest "
                    "first, on this machine's pinned base. Stops when AI capacity closes; rerun to resume.")
    parser.add_argument("--limit", type=int, default=None, help="screen at most this many")
    args = parser.parse_args(argv)
    from issue_triage.issue_store import IssueStore
    from prospector_app.backend import issue_fix_worker

    store = IssueStore()
    screened = issue_fix_worker.screen_features(store, args.limit, trigger="backfill")
    consider = 0
    for n in screened:
        issue = store.load_issue(n)
        consider += issue is not None and (issue.feature_screen or {}).get("verdict") == "consider"
    rate = round(100 * consider / len(screened)) if screened else 0
    print(f"screened {len(screened)}, consider {consider} ({rate}%)")
    return 0


def _backlog_sweep(argv: list[str]) -> int:
    from prospector_app.backend import backlog_sweep

    return backlog_sweep.main(argv)


def _abandoned_close(argv: list[str]) -> int:
    from prospector_app.backend import abandoned_close

    return abandoned_close.main(argv)


def _drain(argv: list[str]) -> int:
    from prospector_app.backend import host_drain

    return host_drain.main(argv)


COMMANDS: dict[str, Callable[[list[str]], int]] = {
    "serve": _serve,
    "ingest": _ingest,
    "threat-scan": _threat_scan,
    "status": _status,
    "triage-cluster": _triage_cluster,
    "recluster": _recluster,
    "security-review": _security_review,
    "activity-backfill": _activity_backfill,
    "triage-backfill": _triage_backfill,
    "feature-screen-backfill": _feature_screen_backfill,
    "backlog-sweep": _backlog_sweep,
    "abandoned-close": _abandoned_close,
    "drain": _drain,
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if not args or args[0] not in COMMANDS:
        print(USAGE, file=sys.stderr)
        return 2
    return COMMANDS[args[0]](args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
