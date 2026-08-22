"""Append-only activity log — an audit trail of what the app has done.

Records the consequential events (live posts/closes, reopens, handoff emits) so
the team can review their combined work and undo it. Dry-runs are recorded too,
flagged, so previews are visible.

Events are stored in the ``activity`` table of the shared store database (same
SQLite/Postgres file as the PR/issue store). Each event is stamped with the
human ``operator`` (from git identity) so the dashboard can attribute and break
down by teammate.

Every event carries a semantic top-level ``kind`` (#40) so aggregation is a
``Counter`` over one field, not a string-parse of ``action``:

    merge | close | comment | resubmit | reopen | undo | handoff | reconcile
      | issue-close | issue-reopen

PR kinds and issue kinds are disjoint, so ``summarize`` never mixes the two —
an issue close is ``issue-close``, never a PR ``close`` or ``comment``, and an
issue reopen is ``issue-reopen``, never the PR ``reopen`` that drives PR velocity.
``close`` events also carry a ``reason`` (duplicate / already-fixed / stale /
manual). Legacy rows were written with mechanical kinds (``execute``,
``review``, ``line_comment``, ``emit``) + an ``action`` like ``CLOSE_DUP``;
``canonical_kind`` maps both shapes onto the vocabulary above, so old rows are
normalized on read with no destructive migration.
"""
from __future__ import annotations

import functools
import getpass
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING

from pipeline import schema
from pipeline import store
from pipeline import storekit

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue
    from pipeline.model import Pr


_TABLES_READY = False


def _engine() -> storekit.Engine:
    """The shared store engine (same DB as the PR/issue store; one cached engine
    per URL). Ensures the activity/chat tables exist once per process, so any
    entry point can record — not only the app, which builds Store() at
    startup."""
    global _TABLES_READY
    eng = storekit.get_engine(storekit.resolve_url(None, store.DEFAULT_ROOT))
    if not _TABLES_READY:
        schema.METADATA.create_all(eng)
        _TABLES_READY = True
    return eng


# The semantic vocabulary (#40). Aggregation groups on these.
KINDS = ("merge", "close", "comment", "resubmit", "reopen", "undo", "handoff", "reconcile",
         "issue-close", "issue-reopen", "alert-dismiss")


@dataclass(frozen=True)
class ActivityScope:
    """One dashboard selection, applied uniformly to PRs and activity events.

    An author scope contains that author's PR numbers and therefore excludes
    issue events. An operator scope keeps events attributed to that operator
    while retaining the repository's open backlog as the shared remainder in
    progress metrics. Supplying both intersects the two dimensions.
    """

    pr_numbers: frozenset[int] | None = None
    operator: str | None = None

    @classmethod
    def from_selection(
        cls,
        prs: dict[int, Pr],
        *,
        pr_author: str | None = None,
        operator: str | None = None,
    ) -> ActivityScope:
        numbers = (frozenset(n for n, rec in prs.items() if rec.author == pr_author)
                   if pr_author else None)
        return cls(pr_numbers=numbers, operator=operator)

    def prs(self, prs: dict[int, Pr]) -> dict[int, Pr]:
        if self.pr_numbers is None:
            return prs
        return {n: rec for n, rec in prs.items() if n in self.pr_numbers}

    def events(self, events: list[dict]) -> list[dict]:
        out = events
        if self.operator:
            out = [ev for ev in out if (ev.get("operator") or "—") == self.operator]
        if self.pr_numbers is not None:
            out = [ev for ev in out
                   if ev.get("pr") is not None and int(ev["pr"]) in self.pr_numbers]
        return out

# CLOSE_* action → the human reason recorded on a close event.
CLOSE_REASONS = {
    "CLOSE_DUP": "duplicate",
    "CLOSE_FIXED": "already-fixed",
    "CLOSE_STALE": "stale",
    "CLOSE_OVERSIZED": "oversized",
    "CLOSE": "manual",
}


def canonical_kind(event: dict) -> str:
    """Map any event — current semantic kind or legacy mechanical kind — onto
    the #40 vocabulary. Pure; safe to call on already-normalized events.

    The ``CLOSE_ISSUE_*`` action check comes before the kind check so a legacy
    issue row stored under a PR kind still reads as ``issue-close``."""
    action = (event.get("action") or "").upper()
    if action.startswith("CLOSE_ISSUE"):
        return "issue-close"
    k = (event.get("kind") or "").lower()
    if k in KINDS:
        return k
    if k == "merge" or action == "MERGE":
        return "merge"
    if k == "reopen" or action == "REOPEN":
        return "reopen"
    if k in ("emit", "handoff"):
        return "handoff"
    if k == "undo":
        return "undo"
    if action in CLOSE_REASONS:
        return "close"
    # legacy "execute" (non-close), "review", "line_comment" all posted a
    # comment without closing → comment.
    return "comment"


def close_reason(event: dict) -> str | None:
    return CLOSE_REASONS.get((event.get("action") or "").upper())


def normalize(event: dict) -> dict:
    """Return the canonical read shape, including recorded branch transitions."""
    out = dict(event)
    out["kind"] = canonical_kind(event)
    branch_transition = isinstance(out.get("from_sha"), str) and isinstance(out.get("to_sha"), str)
    if branch_transition:
        out["kind"] = "resubmit"
    if out["kind"] == "close" and not out.get("reason"):
        r = close_reason(event)
        if r:
            out["reason"] = r
    if branch_transition and not out.get("status"):
        out["status"] = "dry-run" if out.get("dry_run") else "executed"
    return out


def _git_config(key: str) -> str | None:
    """`git config <key>` resolved from the process working directory, or None
    if unset/git unavailable. Used to identify the operator without prompting."""
    try:
        r = subprocess.run(["git", "config", "--get", key],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    val = (r.stdout or "").strip()
    return val or None


def _slug(s: str) -> str:
    """A filesystem-safe, attributable shard name: lowercased, non-alnum → '-'."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-") or "unknown"


@functools.lru_cache(maxsize=1)
def operator() -> dict:
    """Who is running the app, resolved unobtrusively (never prompts): the
    ``PROSPECTOR_OPERATOR`` env override → git ``user.name`` / ``user.email`` → OS
    login. ``name`` is shown in the UI; ``slug`` is the per-operator shard name
    (derived from the name so the file is easily attributable)."""
    env = os.environ.get("PROSPECTOR_OPERATOR")
    name = env or _git_config("user.name")
    email = None if env else _git_config("user.email")
    try:
        login = getpass.getuser()
    except Exception:
        login = None
    display = name or email or login or "unknown"
    slug = _slug(name or (email.split("@")[0] if email else None) or login or "unknown")
    return {"name": display, "email": email, "slug": slug}


def record(kind: str, **fields) -> dict:
    """Append one event to the activity log. Raises storekit.ForeignRepoError when
    this process targets a different repo than the store was stamped with — an
    event carries a bare PR/issue number, so such a row is indistinguishable from
    a real one once written."""
    op = operator()
    fields.setdefault("operator", op["name"])
    if op["email"]:
        fields.setdefault("operator_email", op["email"])
    # Timezone-aware UTC, never naive local: the merged feed orders by this instant.
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **fields}
    entry = normalize(entry)  # store the semantic kind, not the mechanical one
    eng = _engine()
    storekit.assert_repo(eng)
    with eng.begin() as conn:
        conn.execute(schema.activity.insert().values(**schema.activity_row(entry)))
    return entry


def _instant(e: dict) -> datetime:
    """The event's true instant, for ordering across operators. Parse ``at`` to an
    aware UTC datetime so two teammates in different timezones sort by when things
    actually happened, not by each machine's wall-clock string. A naive legacy
    stamp (written before we recorded offsets) is read as UTC — deterministic and
    reader-independent; a missing or unparseable stamp sorts oldest."""
    ts = e.get("at")
    if not isinstance(ts, str):
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(ts)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _read(stmt) -> list[dict]:
    """Run a read `stmt` selecting the `data` column and return the normalized
    rows. AUTOCOMMIT so a read holds no transaction — a stalled or killed reader
    can't strand a server session idle-in-transaction (which would pin a pooler
    backend and block DDL), matching storekit.read_retrying."""
    eng = _engine()
    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        rows = conn.execute(stmt).all()
    return [normalize(r[0]) for r in rows]


def _all_sorted() -> list[dict]:
    """Every event, normalized and ordered oldest-first by true instant. Reads the
    shared `activity` table; legacy rows are normalized on read (`normalize`), and
    `_instant` gives offset-aware cross-timezone ordering."""
    from sqlalchemy import select
    out = _read(select(schema.activity.c.data).order_by(schema.activity.c.at))
    out.sort(key=_instant)
    return out


def _events_for_prs(prs: list[int]) -> list[dict]:
    """Events touching one of `prs`, normalized and newest-first by true instant.
    A targeted read on the indexed `pr` column, so the per-PR views never scan the
    whole activity log."""
    from sqlalchemy import select
    ids = [int(n) for n in prs]
    if not ids:
        return []
    out = _read(select(schema.activity.c.data).where(schema.activity.c.pr.in_(ids)))
    out.sort(key=_instant, reverse=True)  # newest-first
    return out


def recent(limit: int = 200) -> list[dict]:
    return list(reversed(_all_sorted()[-limit:]))  # newest first


# A live event with one of these statuses actually happened upstream (#10).
DONE_STATUSES = {"executed", "merged", "closed", "reopened"}


def is_landed(ev: dict) -> bool:
    """True when ``ev`` is a live action that actually happened upstream (#10):
    not a dry-run, and with a terminal status in ``DONE_STATUSES``. Errors,
    skips, and pending attempts don't count. The single source of truth for
    "did this action land" — run_state, progress, and summarize all defer to it
    so an errored/retried attempt is never tallied as a completed action."""
    return not ev.get("dry_run") and ev.get("status") in DONE_STATUSES


def run_state(prs) -> dict[int, dict]:
    """The latest *live* (non-dry-run) consequential action per PR, so the UI
    can show what's already been done and guard a re-fire (#10).

    A reopen is the operator's undo: if it's the newest action, the PR is back
    to actionable (``done": False``). Errors/skips are ignored — only actions
    that landed count. Returns ``{pr: {kind, status, at, action, reason, done,
    undoable}}`` for the subset of ``prs`` that have any landed action."""
    want = {int(n) for n in prs}
    if not want:
        return {}
    latest: dict[int, dict] = {}
    for ev in _events_for_prs(list(want)):  # newest-first; first landed hit per PR wins
        pr = ev.get("pr")
        if pr is None or not is_landed(ev):
            continue
        pr = int(pr)
        if pr not in latest:
            latest[pr] = ev
    out: dict[int, dict] = {}
    for pr, ev in latest.items():
        kind = ev.get("kind")
        out[pr] = {
            "kind": kind,
            "status": ev.get("status"),
            "at": ev.get("at"),
            "action": ev.get("action"),
            "reason": ev.get("reason"),
            "done": kind in ("close", "merge"),   # reopen → undone → actionable again
            "undoable": kind == "close",          # closes reopen; merges are permanent
        }
    return out


# A non-dry-run event with one of these statuses didn't take effect upstream,
# so it's not part of "what was done to this PR".
FAILED_STATUSES = {"error", "skipped", "blocked"}


def for_pr(n: int) -> list[dict]:
    """Every real action taken on PR ``n``, newest-first — the per-PR record of
    who did what (close/merge/reopen/comment/review). Dry-run previews and
    failed/skipped attempts are excluded; each row carries the bot ``identity``
    that posted it and the human ``operator`` who initiated it."""
    n = int(n)
    out: list[dict] = []
    for ev in _events_for_prs([n]):  # newest-first, only this PR
        if ev.get("dry_run") or ev.get("status") in FAILED_STATUSES:
            continue
        out.append({
            "kind": ev.get("kind"),
            "action": ev.get("action"),
            "status": ev.get("status"),
            "identity": ev.get("identity"),
            "operator": ev.get("operator"),
            "at": ev.get("at"),
            "reason": ev.get("reason"),
            "detail": ev.get("detail"),
            "event_url": ev.get("event_url"),  # deep-link to the exact GitHub event, when captured
        })
    return out


def for_issue(n: int) -> list[dict]:
    """Every real action taken on issue ``n``, newest-first — the per-issue
    record of who did what (close/reopen/comment). Issue events carry their
    number inside the event body rather than the indexed ``pr`` column, so this
    scans the whole (small) log. Dry-run previews and failed/skipped attempts
    are excluded; each row carries the bot ``identity`` that posted it and the
    human ``operator`` who initiated it."""
    n = int(n)
    out: list[dict] = []
    for ev in reversed(_all_sorted()):  # newest-first
        num = ev.get("issue")
        if not isinstance(num, (int, str)) or str(num) != str(n):
            continue
        if ev.get("dry_run") or ev.get("status") in FAILED_STATUSES:
            continue
        out.append({
            "kind": ev.get("kind"),
            "action": ev.get("action"),
            "status": ev.get("status"),
            "identity": ev.get("identity"),
            "operator": ev.get("operator"),
            "at": ev.get("at"),
            "reason": ev.get("reason"),
            "detail": ev.get("detail"),
            "canonical": ev.get("canonical"),  # dup closes: the canonical issue
            "fixed_by": ev.get("fixed_by"),    # fixed closes: the fixing PR
            "via": ev.get("via"),              # e.g. "merge" for a merge-side-effect close
        })
    return out


def all_events() -> list[dict]:
    """The whole normalized log, newest-first. Small enough to fold in memory."""
    return recent(limit=10**8)


def _bucket_key(ev: dict, group_by: str, day: str) -> str | None:
    if group_by == "day":
        return day or None
    if group_by == "week":
        try:
            y, w, _ = date.fromisoformat(day).isocalendar()
            return f"{y}-W{w:02d}"
        except ValueError:
            return None
    if group_by == "cluster":
        return ev.get("cluster_id") or "—"
    if group_by == "identity":
        return ev.get("identity") or "—"
    if group_by == "operator":
        return ev.get("operator") or "—"
    return None


def summarize(events, *, group_by: str = "day", include_dry_run: bool = False,
              since: str | None = None, until: str | None = None,
              identity: str | None = None, operator: str | None = None,
              tz: tzinfo | None = None) -> dict:
    """Fold activity events into totals + grouped buckets for the dashboard
    (#42). Counts only landed actions (``is_landed``) — errored/skipped attempts
    never tally; dry-runs count only when ``include_dry_run``. ``since``/``until``
    are inclusive ``YYYY-MM-DD`` bounds on the event day; ``identity`` (posting
    bot) and ``operator`` (human teammate) filter to one actor. Buckets are sorted
    chronologically for day/week, and by descending volume otherwise.

    Each UTC timestamp is bucketed (and bounded by ``since``/``until``) on its day
    in ``tz`` — the operator's local timezone by default — so a late-evening action
    lands on the local bar the operator sees it on, matching ``firehose_stats``.

    Returns ``{range, group_by, totals: {kind: n}, buckets: [{key, total,
    <kind>: n}]}`` — totals and every bucket carry a count for each of KINDS."""
    if group_by not in ("day", "week", "cluster", "identity", "operator"):
        group_by = "day"
    local_tz = tz or datetime.now(timezone.utc).astimezone().tzinfo or timezone.utc
    totals = {k: 0 for k in KINDS}
    buckets: dict[str, dict] = {}
    for ev in events:
        # Only count landed actions (#10); a dry-run is shown solely when the
        # operator opts in. Errored/skipped attempts count in neither view, so a
        # retried action — e.g. a merge that errored then succeeded — tallies once.
        if not is_landed(ev) and not (include_dry_run and ev.get("dry_run")):
            continue
        if identity and (ev.get("identity") or "—") != identity:
            continue
        if operator and (ev.get("operator") or "—") != operator:
            continue
        day = _local_day(ev.get("at"), local_tz)
        if since and day and day < since:
            continue
        if until and day and day > until:
            continue
        kind = ev.get("kind")
        if kind not in totals:
            continue
        key = _bucket_key(ev, group_by, day)
        if key is None:
            continue
        totals[kind] += 1
        b = buckets.get(key)
        if b is None:
            b = buckets[key] = {"key": key, "total": 0, **{k: 0 for k in KINDS}}
        b[kind] += 1
        b["total"] += 1
    ordered = (sorted(buckets.values(), key=lambda b: b["key"]) if group_by in ("day", "week")
               else sorted(buckets.values(), key=lambda b: (-b["total"], b["key"])))
    return {
        "range": {"since": since, "until": until},
        "group_by": group_by,
        "totals": totals,
        "buckets": ordered,
    }


def _local_day(stamp: str | None, tz: tzinfo) -> str:
    """The calendar day of a UTC-stamped ISO timestamp in timezone ``tz``.

    The store stamps every ``at`` / ``created_at`` in UTC, but the app shows
    and buckets them in the operator's local time, so a late-evening action
    (already "tomorrow" in UTC) sits on today's local bar. A naive timestamp is
    read as UTC; an unparseable one falls back to its bare date prefix."""
    if not stamp:
        return ""
    try:
        dt = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).date().isoformat()


def firehose_stats(
    prs: dict[int, Pr],
    issues: list[dict],
    n_days: int = 30,
    events: list[dict] | None = None,
    today: date | None = None,
    tz: tzinfo | None = None,
    start_date: date | None = None,
) -> dict:
    """Aggregate incoming-vs-triaged stats for the firehose panel.

    Counts new PRs and issues opened on the upstream repo (by ``created_at``)
    against our landed triage actions broken down by kind (close / merge /
    reopen / issue-close) per day over the last ``n_days``, plus 7-day and
    full-period totals. ``pr_triaged`` (close + merge combined) is kept
    alongside the granular counts for backwards compatibility.

    When ``start_date`` is provided it overrides ``n_days`` and spans from
    that date to today — use this for "all time" queries.

    Every UTC timestamp is bucketed by its day in ``tz`` (the operator's local
    timezone by default), and the window ends on today in that same zone — so an
    action stamped just after UTC midnight lands on the local bar the operator
    sees it on, not a UTC day past the window's edge.

    ``today`` and ``tz`` override the wall-clock date and zone — pass them in
    tests for determinism."""
    local_tz = tz or datetime.now(timezone.utc).astimezone().tzinfo or timezone.utc
    today_d = today or datetime.now(local_tz).date()
    if start_date is not None:
        n_days = (today_d - start_date).days + 1
    days = [(today_d - timedelta(days=i)).isoformat() for i in range(n_days - 1, -1, -1)]
    day_set = set(days)

    pr_incoming: dict[str, int] = {d: 0 for d in days}
    for rec in prs.values():
        day = _local_day(rec.created_at, local_tz)
        if day in day_set:
            pr_incoming[day] += 1

    iss_incoming: dict[str, int] = {d: 0 for d in days}
    for iss in issues:
        day = _local_day(iss.get("created_at"), local_tz)
        if day in day_set:
            iss_incoming[day] += 1

    if events is None:
        events = all_events()
    pr_triaged: dict[str, int] = {d: 0 for d in days}
    pr_closed: dict[str, int] = {d: 0 for d in days}
    pr_merged: dict[str, int] = {d: 0 for d in days}
    pr_reopened: dict[str, int] = {d: 0 for d in days}
    iss_closed: dict[str, int] = {d: 0 for d in days}
    for ev in events:
        if not is_landed(ev):
            continue
        kind = ev.get("kind")
        day = _local_day(ev.get("at"), local_tz)
        if kind == "issue-close":
            if day in iss_closed:
                iss_closed[day] += 1
            continue
        if kind == "close" and day in pr_closed:
            pr_closed[day] += 1
            pr_triaged[day] += 1
        elif kind == "merge" and day in pr_merged:
            pr_merged[day] += 1
            pr_triaged[day] += 1
        elif kind == "reopen" and day in pr_reopened:
            pr_reopened[day] += 1

    last7 = set(days[-7:])
    return {
        "days": days,
        "pr_incoming": [pr_incoming[d] for d in days],
        "pr_closed": [pr_closed[d] for d in days],
        "pr_merged": [pr_merged[d] for d in days],
        "pr_reopened": [pr_reopened[d] for d in days],
        "pr_triaged": [pr_triaged[d] for d in days],
        "iss_incoming": [iss_incoming[d] for d in days],
        "iss_closed": [iss_closed[d] for d in days],
        "totals": {
            "pr_incoming_7d": sum(pr_incoming[d] for d in last7),
            "pr_closed_7d": sum(pr_closed[d] for d in last7),
            "pr_merged_7d": sum(pr_merged[d] for d in last7),
            "pr_reopened_7d": sum(pr_reopened[d] for d in last7),
            "pr_triaged_7d": sum(pr_triaged[d] for d in last7),
            "iss_incoming_7d": sum(iss_incoming[d] for d in last7),
            "iss_closed_7d": sum(iss_closed[d] for d in last7),
            "pr_incoming_nd": sum(pr_incoming.values()),
            "pr_closed_nd": sum(pr_closed.values()),
            "pr_merged_nd": sum(pr_merged.values()),
            "pr_reopened_nd": sum(pr_reopened.values()),
            "pr_triaged_nd": sum(pr_triaged.values()),
            "iss_incoming_nd": sum(iss_incoming.values()),
            "iss_closed_nd": sum(iss_closed.values()),
            # kept for backwards compat
            "pr_incoming_30d": sum(pr_incoming.values()),
            "pr_triaged_30d": sum(pr_triaged.values()),
            "iss_incoming_30d": sum(iss_incoming.values()),
        },
    }


def _latest_landed_state_action(events: list[dict]) -> dict[int, dict]:
    """The latest landed state-changing action (close / merge / reopen) per PR,
    from a newest-first event list — the first such event seen for each PR wins."""
    latest: dict[int, dict] = {}
    for ev in events:
        if not is_landed(ev) or ev.get("kind") not in ("close", "merge", "reopen"):
            continue
        pr_n = ev.get("pr")
        if pr_n is not None:
            latest.setdefault(int(pr_n), ev)
    return latest


def closed_by_us_prs(events: list[dict] | None = None) -> list[int]:
    """PRs whose latest landed action was a close — the candidates for an upstream
    reopen. The live sweep re-checks these on top of the store-open set, so a reopen
    lands back in the store even though a closed PR is otherwise off the sweep's
    radar."""
    events = all_events() if events is None else events
    return sorted(n for n, ev in _latest_landed_state_action(events).items()
                  if ev.get("kind") == "close")


def reopened_after_close(prs: dict[int, Pr], events: list[dict] | None = None) -> list[dict]:
    """PRs where our latest landed action was a close, yet the store now sees them
    open again — reopened by the author (or a maintainer) after we closed them.

    The store's PR state is live-maintained: our own close persists state='closed'
    immediately (executor), and the live sweep persists an upstream reopen back to
    state='open' (it re-checks the closed-by-us set for exactly this). So a
    latest-landed-close PR reading 'open' was genuinely reopened — there is no stale
    committed 'open' to guard against."""
    events = all_events() if events is None else events
    result = []
    for pr_n, ev in _latest_landed_state_action(events).items():
        if ev.get("kind") != "close":
            continue
        rec = prs.get(pr_n)
        if rec and rec.state == "open":
            result.append({
                "pr": pr_n,
                "title": rec.title,
                "url": rec.url,
                "author": rec.author,
                "closed_at": ev.get("at"),
                "reason": ev.get("reason"),
            })
    return sorted(result, key=lambda x: x["closed_at"] or "", reverse=True)


def progress(prs: dict[int, Pr], events: list[dict] | None = None) -> dict:
    """Backlog progress (#27): how far through the PR backlog we've gotten.

    Acting on a PR flips its store state out of "open", so the *done* set (PRs
    whose latest landed action is a merge/close) and the *still-open* set are
    disjoint. The universe — the denominator — is their union, so progress climbs
    toward 100% instead of the denominator shrinking as we work. Pairs landed
    live actions (from the log; a reopen returns a PR to the backlog) against the
    store's open PRs. Dry-runs never count toward "done". Returns merged/closed
    splits, a close-reason breakdown, the still-open remainder, the universe, and
    a percentage."""
    events = all_events() if events is None else events
    open_ids = {n for n, r in prs.items() if r.state == "open"}
    seen: dict[int, dict] = {}
    for ev in events:  # newest-first; first landed action per PR wins
        pr = ev.get("pr")
        if pr is None or not is_landed(ev):
            continue
        pr = int(pr)
        if pr not in seen:
            seen[pr] = ev
    merged = closed = 0
    by_reason: dict[str, int] = {}
    actioned_ids: set[int] = set()
    for pr, ev in seen.items():
        if ev.get("kind") == "merge":
            merged += 1
            actioned_ids.add(pr)
        elif ev.get("kind") == "close":
            closed += 1
            actioned_ids.add(pr)
            r = ev.get("reason") or "manual"
            by_reason[r] = by_reason.get(r, 0) + 1
        # latest action is a reopen → PR is back in the backlog, not "done"
    actioned = merged + closed
    # Union, so a not-yet-reconciled PR sitting in both sets is counted once.
    universe = len(open_ids | actioned_ids)
    return {
        "open_total": len(open_ids),
        "universe": universe,
        "actioned": actioned,
        "remaining": len(open_ids - actioned_ids),
        "merged": merged,
        "closed": closed,
        "by_reason": by_reason,
        "pct": round(100 * actioned / universe, 1) if universe else 0.0,
    }


def issue_progress(issues: dict[int, Issue], events: list[dict] | None = None) -> dict:
    """Backlog progress for issues closed through the app.

    The denominator is the union of issues still open in the store and issues
    whose latest landed state action is an issue close. A later issue reopen
    returns the issue to the backlog. Dry-runs and failed actions do not count.
    Returns the same done/remaining shape as ``progress``, with issue close
    reasons in place of the PR merge/close split.
    """
    events = all_events() if events is None else events
    open_ids = {n for n, issue in issues.items() if issue.state == "open"}
    seen: dict[int, dict] = {}
    for ev in events:  # newest-first; first landed state action per issue wins
        issue = ev.get("issue")
        if issue is None or not is_landed(ev) or ev.get("kind") not in ("issue-close", "issue-reopen"):
            continue
        issue = int(issue)
        if issue not in seen:
            seen[issue] = ev

    by_reason: dict[str, int] = {}
    actioned_ids: set[int] = set()
    for issue, ev in seen.items():
        if ev.get("kind") != "issue-close":
            continue
        actioned_ids.add(issue)
        action = ev.get("action")
        if action == "CLOSE_ISSUE_DUP":
            reason = "duplicate"
        elif action == "CLOSE_ISSUE_FIXED":
            reason = "already-fixed"
        else:
            reason = ev.get("reason") or "manual"
        by_reason[reason] = by_reason.get(reason, 0) + 1

    actioned = len(actioned_ids)
    universe = len(open_ids | actioned_ids)
    return {
        "open_total": len(open_ids),
        "universe": universe,
        "actioned": actioned,
        "remaining": len(open_ids - actioned_ids),
        "closed": actioned,
        "by_reason": by_reason,
        "pct": round(100 * actioned / universe, 1) if universe else 0.0,
    }


def issue_action_counts(events: list[dict] | None = None) -> dict[str, int]:
    """Count issue-close events from the activity log by action type.

    Returns a dict mapping action strings (e.g. ``CLOSE_ISSUE_DUP``) to
    their total landed counts — or an empty dict when no issue actions have
    been recorded yet."""
    if events is None:
        events = all_events()
    counts: dict[str, int] = {}
    for ev in events:
        if ev.get("kind") == "issue-close" and is_landed(ev):
            action = ev.get("action") or "other"
            counts[action] = counts.get(action, 0) + 1
    return counts


def operators_with_login(events: list[dict] | None = None) -> list[dict]:
    """Operators from the activity log with their display name and inferred GitHub login.

    The login is derived from the operator email prefix when present (e.g.
    ``octocat@example.com`` → ``octocat``). The resulting list is
    deduplicated by login so each person appears once."""
    if events is None:
        events = all_events()
    seen: dict[str, dict] = {}
    for ev in events:
        op = ev.get("operator")
        email = ev.get("operator_email") or ""
        if not op:
            continue
        login: str = email.split("@")[0] if "@" in email else op.lower().replace(" ", "")
        if login not in seen:
            seen[login] = {"display": op, "login": login}
    return list(seen.values())
