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
  threat-scan      deterministic threat scan over cached diffs
  status           regenerate STATUS.md from the store
  triage-cluster   refresh one cluster's member facts + re-classify
  recluster        re-summarize + re-cluster one cluster's members
  security-review  3-lens adversarial security review of one PR

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
    dist = settings.REPO_ROOT / "app" / "frontend" / "dist"
    if not dist.is_dir():
        print(
            "note: no built frontend (app/frontend/dist) — serving the API only.\n"
            "      build it with: pnpm --dir app/frontend build",
            file=sys.stderr,
        )
    import uvicorn

    uvicorn.run("app.backend.app:app", port=port)
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


COMMANDS: dict[str, Callable[[list[str]], int]] = {
    "serve": _serve,
    "ingest": _ingest,
    "threat-scan": _threat_scan,
    "status": _status,
    "triage-cluster": _triage_cluster,
    "recluster": _recluster,
    "security-review": _security_review,
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
