"""Reconstruct activity-log entries for bot PR closes that landed upstream but
have no landed close in the activity log.

Scans GitHub for PRs the configured bot closed (without merging) inside a time
window, filters to those whose activity log carries no landed close, and — with
--live — appends one reconstructed ``close`` event per PR, stamped with the
close event's real upstream instant and marked ``backfilled``, then sets the
store row's ``meta.state`` to closed when it still reads open. GitHub is only
read (as the operator's gh login); nothing is written upstream. The default is
a dry-run listing of what would be written.
"""
from __future__ import annotations

import argparse
import json

from prospector_app.backend import activity
from prospector_app.backend import data
from prospector_app.backend.executor import CLOSE_ACTIONS
from prospector_app.backend.safety_guard import run
from pipeline import settings


# Bound on closed-PR listing pages (100 PRs each) walked per query. The walk
# normally stops much earlier — as soon as a page's updates predate the window.
_MAX_PAGES = 50


def bot_closes(since: str, until: str) -> list[dict]:
    """PRs closed-without-merge in the inclusive [since, until] window (ISO-8601
    UTC "Z" stamps) whose latest close was performed by the configured bot, as
    ``[{pr, closed_at}]`` sorted by PR number. Candidates come from the repo's
    closed-PR listing (the authority — GitHub search lags behind mass closes),
    newest-updated first, paging until a page's updates predate ``since``: a
    PR's ``updated_at`` is never older than its ``closed_at``, so later pages
    hold no in-window closes. Each PR's issue-events feed is the authority on
    who closed it and exactly when, so ``closed_at`` is the close event's own
    upstream stamp."""
    candidates: set[int] = set()
    for page in range(1, _MAX_PAGES + 1):
        items = _closed_page(page)
        if not items:
            break
        for it in items:
            closed = it.get("closed_at") or ""
            if it.get("merged_at") is None and since <= closed <= until:
                candidates.add(int(it["number"]))
        if min((it.get("updated_at") or "") for it in items) < since:
            break
    out: list[dict] = []
    for n in sorted(candidates):
        ev = _last_close_event(n)
        if ev is not None and _is_bot(ev.get("actor")):
            out.append({"pr": n, "closed_at": ev.get("at")})
    return out


def _closed_page(page: int) -> list[dict]:
    """One page of the repo's closed PRs, newest-updated first, as
    ``[{number, closed_at, merged_at, updated_at}]``."""
    r = run(["gh", "api",
             f"repos/{settings.repo()}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}",
             "--jq", "[.[] | {number, closed_at, merged_at, updated_at}]"], timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"closed-PR listing failed: {r.stderr.strip()[:200]}")
    try:
        items = json.loads(r.stdout or "[]")
    except ValueError:
        return []
    return items if isinstance(items, list) else []


def _is_bot(login: str | None) -> bool:
    """Whether an API-reported login is the configured bot. GitHub reports a
    GitHub App actor under its "[bot]"-suffixed login (``triagebot[bot]``),
    so both that form and the bare configured login count."""
    return login is not None and login.removesuffix("[bot]") == settings.bot_login().removesuffix("[bot]")


def _last_close_event(n: int) -> dict | None:
    """PR ``n``'s latest upstream ``closed`` event as ``{actor, at}``, or None
    when the PR has none (or the events read fails). The jq filter emits one
    JSON object per close event across pages; the last line wins."""
    r = run(["gh", "api", "--paginate", f"repos/{settings.repo()}/issues/{n}/events",
             "--jq", '.[] | select(.event=="closed") | {actor: .actor.login, at: .created_at}'],
            timeout=60)
    if r.returncode != 0:
        return None
    last: dict | None = None
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            last = ev
    return last


def missing(closes: list[dict]) -> list[dict]:
    """The subset of ``closes`` whose PR has no landed close as its latest live
    action in the activity log — the entries the log is missing."""
    state = activity.run_state([int(c["pr"]) for c in closes])
    return [c for c in closes if (state.get(int(c["pr"])) or {}).get("kind") != "close"]


def _entry(close: dict, action: str) -> dict:
    """The activity-event fields reconstructing one upstream close: stamped with
    the close's upstream instant and marked ``backfilled``."""
    return {"pr": int(close["pr"]), "action": action, "status": "executed",
            "detail": "backfilled from the upstream close event",
            "at": close["closed_at"], "backfilled": True}


def backfill(closes: list[dict], *, action: str, live: bool) -> list[dict]:
    """Append one reconstructed activity event per close, attributed to the
    configured bot, and set each store row's ``meta.state`` to closed when it
    still reads open. With ``live=False``, return the entries without writing
    anything. Returns the (would-be) entries."""
    entries = [_entry(c, action) for c in closes]
    if not live:
        return entries
    store = data.store()
    for fields in entries:
        activity.record("execute", identity=settings.bot_login(), dry_run=False, **fields)
        n = int(fields["pr"])
        rec = store.load_pr(n)
        if rec is not None and rec.state == "open":
            store.edit_pr(n).record_live_state(state="closed")
    return entries


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="activity-backfill",
        description="Reconstruct missing activity-log entries for the configured "
                    "bot's upstream PR closes in a time window (dry-run default).")
    p.add_argument("--since", required=True, help="window start, ISO-8601 UTC (e.g. 2026-07-27T18:55:00Z)")
    p.add_argument("--until", required=True, help="window end, ISO-8601 UTC (e.g. 2026-07-27T19:05:00Z)")
    p.add_argument("--action", required=True, choices=sorted(CLOSE_ACTIONS),
                   help="the close action the entries record (sets the close reason)")
    p.add_argument("--live", action="store_true",
                   help="write the entries; the default prints what would be written")
    args = p.parse_args(argv)
    closes = bot_closes(args.since, args.until)
    todo = missing(closes)
    entries = backfill(todo, action=args.action, live=args.live)
    print(json.dumps({
        "bot_closes": len(closes),
        "already_recorded": len(closes) - len(todo),
        "entries": entries,
        "dry_run": not args.live,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
