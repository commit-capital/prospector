"""Upstream execution as the configured triage bot.

Acts on the configured repository as the configured bot: close + comment on
duplicates/already-fixed PRs, submit reviews (approve / request-changes / inline
comments), reopen, and squash-merge gate-clean PRs (merge_pr). Every write goes
out under a real installation token minted by pipeline/get-bot-token.sh from
the private-key path in TRIAGE_BOT_KEY_FILE. When no token can be minted, mint_bot_token()
returns None and every execution — including merge — is forced to DRY-RUN, so
the app cannot post.

Merges are upstream squash-merges via merge_pr (gated by
gates.merge_eligibility), not fork cherry-picks. The disposition path here
(execute_pr) only closes/comments; merge has its own gated path.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.model import Pr

from prospector_app.backend import activity
from prospector_app.backend import data
from prospector_app.backend import decisions
from prospector_app.backend import models
from prospector_app.backend.safety_guard import bot_merge_run, bot_run, run
from pipeline import review_policy
from pipeline.settings import BOT_LOGIN, REPO

REPO_ROOT = Path(__file__).resolve().parents[2]
GET_TOKEN = REPO_ROOT / "pipeline" / "get-bot-token.sh"

CLOSE_ACTIONS = {"CLOSE_DUP", "CLOSE_FIXED", "CLOSE", "CLOSE_STALE", "CLOSE_OVERSIZED"}


def _cluster_id(n: int) -> str | None:
    """The primary (lowest) cluster id for a PR, zero-padded to 3 digits, e.g.
    "042", or None for a PR not in any cluster. When a PR straddles multiple
    clusters the minimum id is used — a single grouping tag per activity event,
    since activity grouping is display, not policy."""
    cids = data.pr_to_clusters().get(int(n)) or []
    return f"{min(cids):03d}" if cids else None


_last_mint_error: str | None = None


def mint_error() -> str | None:
    """Why the last mint_bot_token() call returned None — key missing, the
    minting script missing, or whatever get-bot-token.sh printed to stderr
    (bad TRIAGE_BOT_APP_ID, the app not installed on the org, a network
    failure, …). None after a successful mint, or before any call has run.
    "No token" alone doesn't tell an operator which of those to check."""
    return _last_mint_error


def mint_bot_token() -> str | None:
    """Mint a 1-hour bot installation token, or None if unavailable.

    The token helper loads TRIAGE_BOT_KEY_FILE from the process environment or
    repo-root .env and reads the PEM only inside its short-lived subprocess.
    Any configuration or mint failure returns None and forces dry-run. Never
    raises into the request path.
    """
    global _last_mint_error
    if not GET_TOKEN.exists():
        _last_mint_error = f"{GET_TOKEN} not found"
        return None
    try:
        r = subprocess.run(["bash", str(GET_TOKEN)], capture_output=True, text=True, timeout=30)
    except Exception as e:
        _last_mint_error = f"{type(e).__name__}: {e}"
        return None
    tok = (r.stdout or "").strip()
    if tok:
        _last_mint_error = None
        return tok
    _last_mint_error = (r.stderr or "").strip() or f"get-bot-token.sh exited {r.returncode} with no output"
    return None


_live_possible: bool | None = None


def live_possible() -> bool:
    """Whether this machine can actually post upstream as the configured bot.

    The SINGLE source of truth for "live possible". It PROBES — mints a token
    once and caches the result — rather than inferring from the mere presence of
    a configured key path. A malformed key or revoked install therefore reports
    unavailable, instead of advertising a capability that every write would then
    refuse. Cached for the process's lifetime once probed — a fix made after
    that first probe (the key file appears, the app gets installed, a network
    blip clears) does not take effect until refresh_live() re-probes; the
    app's "retry live mode" action (POST /api/identities/refresh) is what
    calls it, since nothing else in the request path does."""
    global _live_possible
    if _live_possible is None:
        _live_possible = mint_bot_token() is not None
    return _live_possible


def refresh_live() -> None:
    global _live_possible
    _live_possible = None


def identities() -> dict:
    live = live_possible()
    error = None if live else mint_error()
    note = None if live else (f"no {BOT_LOGIN} token on this machine — dry-run only"
                              + (f" ({error})" if error else ""))
    return {
        "identities": [
            {"id": BOT_LOGIN, "label": f"{BOT_LOGIN} (bot)", "available": live, "note": note},
        ],
        "live_possible": live,
        "live_error": error,
    }


def _effective_action(a: models.CloseAction) -> str:
    return a.override_action or a.action or "UNKNOWN"


def _pr_live(n: int) -> dict | None:
    """Live {state, head, merged, mergeable_state} for a PR, or None if GitHub is
    unreachable. `mergeable_state` is "dirty" when the branch now conflicts."""
    r = run(["gh", "api", f"repos/{REPO}/pulls/{n}",
             "--jq", "{state: .state, head: .head.sha, merged: .merged, mergeable_state: .mergeable_state}"], timeout=30)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except (ValueError, TypeError):
        return None


def _html_url(stdout: str | None) -> str | None:
    """The `html_url` from a `gh api` JSON response (e.g. a posted comment), or
    None — used to deep-link an action to the exact GitHub event it created."""
    try:
        return (json.loads(stdout or "").get("html_url") or "").strip() or None
    except (ValueError, AttributeError):
        return None


def _reflect_state(n: int, *, state: str | None, merged: bool = False) -> None:
    """Record our action's effect on a PR's upstream state — open/closed/merged —
    durably in the shared store (meta.state via record_live_state, so every operator
    sees it without waiting for the next live sweep or INGEST) and refresh this
    app's snapshot so it reflects the change instantly. A merge outranks a raw
    state, matching GitHub's own merged→closed. A no-op write when the PR has no
    store row — a brand-new PR we closed without ever ingesting has nothing to
    update."""
    effective = "merged" if merged else state
    store = data.store()
    if effective and store.load_pr(n) is not None:
        store.edit_pr(n).record_live_state(state=effective)
    data.refresh()


def _preflight(n: int, rec: Pr | None, *, check_head: bool,
               check_mergeable: bool = False,
               fail_closed: bool = False) -> tuple[bool, str]:
    """Re-check the PR's live state right before a write (#11). The app acts
    on snapshotted data; by now the PR may have been merged, closed, its head
    moved, or (for a merge) developed conflicts. Returns (ok, message).

    When GitHub is unreachable (`live is None`), the default is ok=True
    (fail-open) — a transient read failure shouldn't block a comment/close, and
    the write has its own errors. A caller that gates an IRREVERSIBLE action on
    the live head (merge) passes `fail_closed=True`: an unconfirmable head must
    block, because merging blind would defeat the head-match check entirely."""
    live = _pr_live(n)
    if live is None:
        if fail_closed:
            return False, ("couldn't confirm the PR's live state upstream — "
                           "refusing to merge without re-checking the head")
        return True, ""
    if live.get("merged"):
        _reflect_state(n, state=live.get("state"), merged=True)
        return False, "already merged upstream — your action is no longer needed"
    state = live.get("state")
    if state and state != "open":
        _reflect_state(n, state=state)
        return False, f"PR is {state} upstream — no longer open"
    if check_head and rec:
        stored = rec.head_sha if rec is not None else None
        head = live.get("head")
        if stored and head and head != stored:
            return False, (f"head moved since analysis (was {stored[:7]}, now {head[:7]}) — "
                           "re-ingest/re-analyze before merging")
    # A merge into a conflicting branch can't succeed — refuse it here even when
    # the stored gate was clean at analysis (#189).
    if check_mergeable and live.get("mergeable_state") == "dirty":
        return False, "PR now has merge conflicts — needs a rebase before it can merge"
    return True, ""


def _changed_paths(n: int) -> list[str]:
    """Live list of changed file paths for a PR (read-only, paginated)."""
    r = run(["gh", "api", "--paginate", f"repos/{REPO}/pulls/{n}/files",
             "--jq", ".[].filename"], timeout=60)
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _is_bot_login(login: str | None) -> bool:
    """True when `login` identifies the configured bot. The REST API reports a
    GitHub App's actions under the `[bot]`-suffixed login (`triagebot[bot]`),
    while TRIAGE_BOT_LOGIN is the bare App slug — accept either form, on either
    side."""
    return bool(login) and login.removesuffix("[bot]") == BOT_LOGIN.removesuffix("[bot]")


def _jq_rows(stdout: str | None) -> list[dict]:
    """Parse `gh api --jq '.[] | {…}'` output — one compact JSON object per line,
    across every page under `--paginate`. Malformed lines are skipped."""
    rows: list[dict] = []
    for line in (stdout or "").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _has_bot_comment(n: int, contains: str | None = None) -> bool:
    """True when the configured bot has already commented on issue/PR `n`. With
    `contains`, only comments whose body includes that substring count, scoping the
    idempotency check to comments about this specific action (a close-fixed comment
    references its fixing PR; a close-dup comment references its canonical). Both
    the login and the substring are matched in Python: the REST login carries the
    `[bot]` suffix, and jq's `contains("…")` takes a quoted literal, so a body
    holding a quote or backslash cannot be searched for through the filter."""
    r = run(["gh", "api", f"repos/{REPO}/issues/{n}/comments",
             "--jq", '.[] | {login: .user.login, body: .body}'], timeout=30)
    bodies = [row.get("body") for row in _jq_rows(r.stdout)
              if _is_bot_login(row.get("login"))]
    if contains is None:
        return bool(bodies)
    return any(contains in (b or "") for b in bodies)


def _comment_marker(comment: str) -> str | None:
    """An idempotency key scoping `_has_bot_comment` to this comment's own text: the
    first 40 chars of its first line, verbatim, so it is a genuine substring of the
    body once posted. None when the comment is blank."""
    first = comment.strip().split("\n", 1)[0] if comment else ""
    marker = first[:40].strip()
    return marker or None


def _note_bookkeeping_failure(res: dict, what: str, e: Exception) -> None:
    """Append a local bookkeeping failure to the result's `bookkeeping_error`
    (and its human-readable `detail`) without touching `status` — the status
    reports the upstream write, and local bookkeeping never changes it."""
    note = f"{what}: {type(e).__name__}: {str(e)[:160]}"
    prior = res.get("bookkeeping_error")
    res["bookkeeping_error"] = f"{prior}; {note}" if prior else note
    detail = res.get("detail") or ""
    res["detail"] = f"{detail}; {note}" if detail else note


def _comment_then_close(n: int, *, base: dict, idempotency_key: str | None,
                        comment_argv: list[str], close_argv: list[str], token: str,
                        log_verb: str, capture_event_url: bool = False,
                        on_success: Callable[[], None] | None = None) -> dict:
    """Post a comment (skipped when the configured bot already left one matching
    `idempotency_key`), then close. `idempotency_key` is required and scopes the
    check to this action's own comment: an unscoped check would let any earlier bot
    comment — a re-triggered Greptile review, a prior close comment — suppress this
    one, closing the PR or issue with no explanation. Every exit attempts an
    activity-log record, so a failed upstream write is never silent. Once the
    close has landed upstream the returned status stays "executed": `on_success`
    (store bookkeeping) and the activity append are each best-effort, and a
    failure in either is carried on the result as `bookkeeping_error`."""
    def _fail(detail: str) -> dict:
        res = {**base, "status": "error", "detail": detail}
        try:
            activity.record(log_verb, identity=BOT_LOGIN, dry_run=False, **res)
        except Exception as e:  # the log write never masks the upstream failure
            _note_bookkeeping_failure(res, "activity log write failed", e)
        return res

    steps: list[str] = []
    event_url = None  # deep-link to the comment, when we post one
    try:
        if not _has_bot_comment(n, idempotency_key):
            cr = bot_run(comment_argv, token)
            if cr.returncode != 0:
                return _fail(f"comment failed: {cr.stderr.strip()[:160]}")
            if capture_event_url:
                event_url = (cr.stdout or "").strip() or None  # gh prints the new comment's URL
            steps.append("commented")
        else:
            steps.append("comment-exists")
        clr = bot_run(close_argv, token)
        if clr.returncode != 0:
            return _fail(f"close failed: {clr.stderr.strip()[:160]}")
        steps.append("closed")
    except Exception as e:  # WriteAttemptBlocked or subprocess error
        return _fail(str(e)[:160])

    res = {**base, "status": "executed", "detail": " + ".join(steps)}
    if capture_event_url and event_url:
        res["event_url"] = event_url
    if on_success is not None:
        try:
            on_success()
        except Exception as e:
            _note_bookkeeping_failure(res, "post-close bookkeeping failed", e)
    try:
        activity.record(log_verb, identity=BOT_LOGIN, dry_run=False, **res)
    except Exception as e:
        _note_bookkeeping_failure(res, "activity log write failed", e)
    return res


def _backfill_dup_refs(n: int, action: models.CloseAction) -> models.CloseAction:
    """Return `action` with its CLOSE_DUP canonical / CLOSE_FIXED upstream refs
    backfilled from the PR's stored analysis when the caller didn't supply them.

    A close fired without explicit refs — e.g. a bulk close that carried no
    canonical — would otherwise fall back to the neutral "duplicate during triage"
    wording, dropping the link to the PR it's a dup of even though the store knows
    one (#195). An explicit ref always wins; nothing is invented when the store
    has none."""
    eff = _effective_action(action)
    if eff not in ("CLOSE_DUP", "CLOSE_FIXED"):
        return action
    rec = data.prs().get(int(n))
    if rec is None:
        return action
    updates: dict[str, int | str] = {}
    if eff == "CLOSE_DUP" and not action.canonical and rec.canonical:
        updates["canonical"] = rec.canonical
    if eff == "CLOSE_FIXED":
        if not action.upstream_pr and rec.upstream_pr:
            updates["upstream_pr"] = rec.upstream_pr
        if not action.upstream_commit and rec.upstream_commit:
            updates["upstream_commit"] = rec.upstream_commit
        if not action.upstream_date and rec.upstream_date:
            updates["upstream_date"] = rec.upstream_date
    return action.model_copy(update=updates) if updates else action


def execute_pr(n: int, action: models.CloseAction, *, token: str | None, dry_run: bool) -> dict:
    """Execute one PR's disposition. Returns a result record."""
    eff = _effective_action(action)
    base = {"pr": int(n), "cluster_id": _cluster_id(n), "action": eff}

    if eff not in CLOSE_ACTIONS:
        reason = "merge runs through the gated merge_pr path, not the disposition executor" if eff == "MERGE" else "no upstream action"
        return {**base, "status": "skipped", "detail": reason}

    comment = action.comment or decisions.default_comment(_backfill_dup_refs(n, action))
    plan = [f'comment: "{comment[:80]}…"', f"close #{n}"]

    # live pre-flight: don't act on a PR that's already merged/closed (#11)
    ok_pf, msg = _preflight(n, data.prs().get(int(n)), check_head=False)
    if not ok_pf:
        return {**base, "status": "skipped", "detail": f"pre-flight: {msg}"}

    # forced dry-run when no token
    if dry_run or not token:
        res = {**base, "status": "dry-run", "detail": "; ".join(plan), "forced": not token and not dry_run}
        activity.record("execute", identity=BOT_LOGIN, dry_run=True, **res)
        return res

    return _comment_then_close(
        n, base=base, idempotency_key=_comment_marker(comment),
        comment_argv=["gh", "pr", "comment", str(n), "--repo", REPO, "--body", comment],
        close_argv=["gh", "pr", "close", str(n), "--repo", REPO],
        token=token, log_verb="execute", capture_event_url=True,
        on_success=lambda: _reflect_state(n, state="closed"))


def _bot_comment_ids(n: int) -> list[int]:
    r = run(["gh", "api", f"repos/{REPO}/issues/{n}/comments",
             "--jq", '.[] | {login: .user.login, id: .id}'], timeout=30)
    if r.returncode != 0:
        return []
    return [int(row["id"]) for row in _jq_rows(r.stdout)
            if _is_bot_login(row.get("login")) and isinstance(row.get("id"), int)]


def _bot_change_request_ids(n: int) -> list[int]:
    """IDs of the configured bot's PR reviews still standing as CHANGES_REQUESTED.

    A request-changes is a PR *review* (pulls/{n}/reviews), not an issue
    comment, so deleting issue comments on reopen leaves its "here's what to
    fix" body visible — the cause of #70. These get dismissed on reopen."""
    r = run(["gh", "api", "--paginate", f"repos/{REPO}/pulls/{n}/reviews",
             "--jq", '.[] | {login: .user.login, id: .id, state: .state}'], timeout=30)
    if r.returncode != 0:
        return []
    return [int(row["id"]) for row in _jq_rows(r.stdout)
            if _is_bot_login(row.get("login")) and row.get("state") == "CHANGES_REQUESTED"
            and isinstance(row.get("id"), int)]


def reopen_pr(n: int, *, token: str | None, dry_run: bool) -> dict:
    """Undo: reopen a PR, delete the configured bot's closing comment(s), and
    dismiss any standing bot request-changes review so the PR doesn't
    reopen still showing the bot's "things to fix" block (#70)."""
    base = {"pr": int(n), "cluster_id": _cluster_id(n), "action": "REOPEN"}
    if dry_run or not token:
        return {**base, "status": "dry-run",
                "detail": "would reopen + remove bot comment(s) + dismiss any request-changes review",
                "forced": not token and not dry_run}
    try:
        rr = bot_run(["gh", "pr", "reopen", str(n), "--repo", REPO], token)
        if rr.returncode != 0:
            res = {**base, "status": "error", "detail": f"reopen failed: {rr.stderr.strip()[:160]}"}
            activity.record("reopen", identity=BOT_LOGIN, dry_run=False, **res)
            return res
        removed = 0
        for cid in _bot_comment_ids(n):
            dr = bot_run(["gh", "api", "--method", "DELETE", f"repos/{REPO}/issues/comments/{cid}"], token)
            if dr.returncode == 0:
                removed += 1
        # withdraw any standing request-changes so reopening leaves a clean slate
        dismissed = 0
        for rid in _bot_change_request_ids(n):
            dn = bot_run(["gh", "api", "--method", "PUT",
                          f"repos/{REPO}/pulls/{n}/reviews/{rid}/dismissals",
                          "-f", "message=Reopened during triage — earlier change request withdrawn.",
                          "-f", "event=DISMISS"], token)
            if dn.returncode == 0:
                dismissed += 1
    except Exception as e:
        res = {**base, "status": "error", "detail": str(e)[:160]}
        activity.record("reopen", identity=BOT_LOGIN, dry_run=False, **res)
        return res
    detail = f"reopened + removed {removed} bot comment(s)"
    if dismissed:
        detail += f" + dismissed {dismissed} request-changes review(s)"
    res = {**base, "status": "reopened", "detail": detail}
    _reflect_state(n, state="open")
    activity.record("reopen", identity=BOT_LOGIN, dry_run=False, **res)
    return res


def reopen_issue(n: int, *, token: str | None, dry_run: bool) -> dict:
    """Undo an issue close: reopen the issue and delete the configured bot's closing
    comment(s), as the bot. Reflects the reopened state back into the shared
    store so the Issues view restores the issue at once — the inverse of the
    write-back the close paths do (#192). Records as the disjoint ``issue-reopen``
    kind, so it never counts toward PR-reopen velocity."""
    from prospector_app.backend import issues as issues_mod
    base = {"issue": int(n), "action": "REOPEN_ISSUE"}
    if dry_run or not token:
        return {**base, "status": "dry-run", "detail": "would reopen + remove bot comment(s)",
                "forced": not token and not dry_run}
    try:
        rr = bot_run(["gh", "issue", "reopen", str(n), "--repo", REPO], token)
        if rr.returncode != 0:
            res = {**base, "status": "error", "detail": f"reopen failed: {rr.stderr.strip()[:160]}"}
            activity.record("issue-reopen", identity=BOT_LOGIN, dry_run=False, **res)
            return res
        removed = 0
        for cid in _bot_comment_ids(n):
            dr = bot_run(["gh", "api", "--method", "DELETE", f"repos/{REPO}/issues/comments/{cid}"], token)
            if dr.returncode == 0:
                removed += 1
    except Exception as e:
        res = {**base, "status": "error", "detail": str(e)[:160]}
        activity.record("issue-reopen", identity=BOT_LOGIN, dry_run=False, **res)
        return res
    res = {**base, "status": "reopened", "detail": f"reopened + removed {removed} bot comment(s)"}
    issues_mod.reflect_issue_state(n, "open")
    activity.record("issue-reopen", identity=BOT_LOGIN, dry_run=False, **res)
    return res


def comment_issue(n: int, action: models.IssueCommentBody, *, token: str | None, dry_run: bool) -> dict:
    """Post a comment on a GitHub issue as the configured bot, without closing it.
    Reversible (the bot can delete its own comment). Shares the issues/comments
    endpoint with the close paths, so the same bot-write gate applies."""
    base = {"issue": int(n), "action": "COMMENT_ISSUE"}
    comment = (action.comment or "").strip()
    if not comment:
        res = {**base, "status": "error", "detail": "comment is empty"}
        activity.record("issue-comment", identity=BOT_LOGIN, dry_run=dry_run, **res)
        return res
    if dry_run or not token:
        res = {**base, "status": "dry-run", "detail": f'comment: "{comment[:80]}…"',
               "forced": not token and not dry_run}
        activity.record("issue-comment", identity=BOT_LOGIN, dry_run=True, **res)
        return res
    try:
        r = bot_run(["gh", "issue", "comment", str(n), "--repo", REPO, "--body", comment], token)
    except Exception as e:
        res = {**base, "status": "error", "detail": str(e)[:160]}
        activity.record("issue-comment", identity=BOT_LOGIN, dry_run=False, **res)
        return res
    if r.returncode != 0:
        res = {**base, "status": "error", "detail": f"comment failed: {r.stderr.strip()[:160]}"}
    else:
        res = {**base, "status": "executed", "detail": "comment posted"}
    activity.record("issue-comment", identity=BOT_LOGIN, dry_run=False, **res)
    return res


_REVIEW_FLAG = {"approve": "--approve", "request-changes": "--request-changes", "comment": "--comment"}
# Human-readable effect of each review event — what state the PR lands in. All
# three leave the PR open and write nothing to the store (disposition is owned by
# ANALYZE); they differ only in the review state GitHub records.
_REVIEW_EFFECT = {"approve": "an APPROVE review", "request-changes": "a CHANGES_REQUESTED review",
                  "comment": "a review comment"}


def _latest_bot_review_url(n: int) -> str | None:
    """html_url of the configured bot's most recent review on a PR — the deep-link
    anchor for a review that carries a body (request-changes / comment). `gh pr
    review` prints no URL, so we read it back from the reviews API."""
    r = run(["gh", "api", f"repos/{REPO}/pulls/{n}/reviews?per_page=100",
             "--jq", '.[] | {login: .user.login, url: .html_url}'], timeout=30)
    urls = [row["url"] for row in _jq_rows(r.stdout)
            if _is_bot_login(row.get("login")) and isinstance(row.get("url"), str)]
    return urls[-1] if urls else None


def submit_review(n: int, event: str, body: str, *, token: str | None, dry_run: bool) -> dict:
    """Submit a PR review (approve / request-changes / comment) as the configured bot."""
    flag = _REVIEW_FLAG.get(event)
    base = {"pr": int(n), "cluster_id": _cluster_id(n), "action": f"REVIEW:{event}"}
    if not flag:
        return {**base, "status": "error", "detail": f"unknown review event: {event}"}
    if event in ("comment", "request-changes") and not body.strip():
        return {**base, "status": "error", "detail": "a comment body is required for this review type"}
    ok_pf, msg = _preflight(n, None, check_head=False)
    if not ok_pf:
        return {**base, "status": "skipped", "detail": f"pre-flight: {msg}"}
    if dry_run or not token:
        effect = _REVIEW_EFFECT.get(event, f"a {event} review")
        detail = (f"would submit {effect} on #{n} as {BOT_LOGIN} — PR stays open, "
                  f"nothing written to the store" + (f"; comment: “{body[:50]}…”" if body.strip() else ""))
        res = {**base, "status": "dry-run", "detail": detail, "forced": not token and not dry_run}
        activity.record("review", identity=BOT_LOGIN, dry_run=True, event=event, **res)
        return res
    argv = ["gh", "pr", "review", str(n), "--repo", REPO, flag]
    if body.strip():
        argv += ["--body", body]
    try:
        r = bot_run(argv, token)
        if r.returncode != 0:
            res = {**base, "status": "error", "detail": r.stderr.strip()[:160]}
        else:
            res = {**base, "status": "executed", "detail": f"submitted {_REVIEW_EFFECT.get(event, event)} — PR stays open"}
            if body.strip():  # a review with a body is a content anchor worth deep-linking
                url = _latest_bot_review_url(n)
                if url:
                    res["event_url"] = url
    except Exception as e:
        res = {**base, "status": "error", "detail": str(e)[:160]}
    activity.record("review", identity=BOT_LOGIN, dry_run=False, event=event, **res)
    return res


def comment_line(n: int, file: str, line: int, body: str, *, token: str | None, dry_run: bool) -> dict:
    """Post an inline review comment on a specific diff line as the configured bot."""
    base = {"pr": int(n), "cluster_id": _cluster_id(n), "action": "LINE_COMMENT", "detail": f"{file}:{line}"}
    if not body.strip():
        return {**base, "status": "error", "detail": "comment body required"}
    ok_pf, msg = _preflight(n, None, check_head=False)
    if not ok_pf:
        return {**base, "status": "skipped", "detail": f"pre-flight: {msg}"}
    if dry_run or not token:
        res = {**base, "status": "dry-run", "detail": f"would comment on {file}:{line}: “{body[:50]}…”",
               "forced": not token and not dry_run}
        activity.record("line_comment", identity=BOT_LOGIN, dry_run=True, **res)
        return res
    head = (run(["gh", "api", f"repos/{REPO}/pulls/{n}", "--jq", ".head.sha"], timeout=30).stdout or "").strip()
    if not head:
        return {**base, "status": "error", "detail": "could not resolve head sha"}
    argv = ["gh", "api", "--method", "POST", f"repos/{REPO}/pulls/{n}/comments",
            "-f", f"body={body}", "-f", f"commit_id={head}", "-f", f"path={file}",
            "-F", f"line={int(line)}", "-f", "side=RIGHT"]
    try:
        r = bot_run(argv, token)
        if r.returncode != 0:
            res = {**base, "status": "error", "detail": r.stderr.strip()[:160]}
        else:
            res = {**base, "status": "executed", "detail": f"commented on {file}:{line}"}
            url = _html_url(r.stdout)  # deep-link to the inline comment
            if url:
                res["event_url"] = url
    except Exception as e:
        res = {**base, "status": "error", "detail": str(e)[:160]}
    activity.record("line_comment", identity=BOT_LOGIN, dry_run=False, **res)
    return res


def retrigger_greptile(n: int, *, token: str | None, dry_run: bool) -> dict:
    """Post the configured review provider's mention as a plain PR comment to
    re-trigger its review.

    The bare mention is the provider's manual-review trigger: it re-runs the review
    against the PR's current head with no new commit. Each call posts it again."""
    base = {"pr": int(n), "cluster_id": _cluster_id(n), "action": "GREPTILE_RETRIGGER"}
    mention = review_policy.active().retrigger_mention
    if mention is None:
        return {**base, "status": "skipped",
                "detail": "the configured review provider has no re-trigger action"}
    ok_pf, msg = _preflight(n, None, check_head=False)
    if not ok_pf:
        return {**base, "status": "skipped", "detail": f"pre-flight: {msg}"}
    if dry_run or not token:
        res = {**base, "status": "dry-run",
               "detail": f"would comment “{mention}” on #{n} as {BOT_LOGIN} to re-trigger the review",
               "forced": not token and not dry_run}
        activity.record("greptile_retrigger", identity=BOT_LOGIN, dry_run=True, **res)
        return res
    try:
        r = bot_run(["gh", "pr", "comment", str(n), "--repo", REPO, "--body", mention], token)
        if r.returncode != 0:
            res = {**base, "status": "error", "detail": r.stderr.strip()[:160]}
        else:
            res = {**base, "status": "executed", "detail": f"posted “{mention}” — the provider will re-review"}
            url = (r.stdout or "").strip() or None  # gh prints the new comment's URL
            if url:
                res["event_url"] = url
    except Exception as e:
        res = {**base, "status": "error", "detail": str(e)[:160]}
    activity.record("greptile_retrigger", identity=BOT_LOGIN, dry_run=False, **res)
    return res


def merge_pr(n: int, method: str = "squash", *, dry_run: bool, reason: str | None = None) -> dict:
    """Merge a PR upstream as the configured bot — ONLY when the human-merge
    gate passes (gates.merge_eligibility: gate-clean, security GREEN-or-never-run,
    not CODEOWNERS-gated). A current YELLOW verdict blocks unless the operator
    supplies `reason`; the reason is logged durably to the store as the verdict's
    override (Pr.log_security_override) before the merge executes, so the pass is
    auditable. RED always blocks. With no configured bot key this is forced to
    dry-run (no token to mint), so the app cannot merge.

    A live merge additionally passes the deterministic compile preflight:
    pipeline/compile_preflight.py runs the profile's verify.compile_cmd over
    (current default-branch HEAD + this PR's diff) in the sandbox, and
    gates.compile_preflight_gate blocks on anything but a clean pass — fail
    closed on a machine that cannot run the sandbox. The run and its outcome
    land in the activity log either way: a block records its own event, a pass
    rides the merge event's compile_preflight payload."""
    base = {"pr": int(n), "cluster_id": _cluster_id(n), "action": "MERGE"}
    from pipeline import gates  # pipeline policy (path set up by data import)
    reason = (reason or "").strip() or None
    rec = data.prs().get(int(n))
    # Live changed-file list so the gate can refuse CODEOWNERS-gated PRs (#15/#26).
    paths = _changed_paths(n)
    ok, why = (gates.merge_eligibility(rec, changed_paths=paths, override_reason=reason)
               if rec else (False, "PR not in store"))
    if not ok:
        return {**base, "status": "blocked", "detail": f"merge gate: {why}"}
    # Whether the gate passed on the strength of the operator's reason — that
    # override must land in the store before any live merge. Security (YELLOW)
    # and verify (escalate) are disambiguated by the overridable checks, so the
    # reason is logged to whichever section it actually cleared (never both from
    # one reason unless both blocks were present).
    override_pending = (reason is not None and rec is not None
                        and gates.security_overridable(rec, changed_paths=paths))
    verify_override_pending = (reason is not None and rec is not None
                               and gates.verify_overridable(rec, changed_paths=paths))
    if override_pending:
        base["security_override"] = reason
    if verify_override_pending:
        base["verify_override"] = reason
    # live pre-flight: refuse to merge a PR that's no longer open, whose head
    # moved since we analyzed it (#11), or that now has merge conflicts (#189).
    # fail_closed: an unconfirmable live head blocks — the merge below pins to
    # the verified head, and we won't merge blind if we can't re-check it.
    ok_pf, msg = _preflight(n, rec, check_head=True, check_mergeable=True,
                            fail_closed=True)
    if not ok_pf:
        return {**base, "status": "blocked", "detail": f"pre-flight: {msg}"}
    if method not in ("merge", "squash", "rebase"):
        method = "squash"
    token = None if dry_run else mint_bot_token()
    if dry_run or not token:
        detail = f"would merge #{n} (--{method}) as {BOT_LOGIN}"
        if override_pending:
            detail += " after logging the security-YELLOW override"
        if verify_override_pending:
            detail += " after logging the verify-escalate override"
        res = {**base, "status": "dry-run", "detail": detail,
               "forced": not token and not dry_run}
        activity.record("merge", identity=BOT_LOGIN, dry_run=True, **res)
        _credit_merge_closed_issues(rec, n, dry_run=True)
        return res
    # Compile preflight, live path only: (current default-branch HEAD + this
    # PR's diff) must compile clean in the sandbox before the merge fires. The
    # dry-run branch above returns first — a preview never boots a container —
    # and this runs before the override logging below, so a blocked merge
    # writes no durable override. run_for_merge is None only when the profile
    # configures no compile_cmd; every failure shape blocks via the gate.
    from pipeline import compile_preflight
    pf = compile_preflight.run_for_merge(int(n), rec.head_sha if rec and rec.head_sha else "")
    if pf is not None:
        ok_cp, why_cp = gates.compile_preflight_gate(pf)
        base["compile_preflight"] = {**pf, "ok": ok_cp}
        if not ok_cp:
            res = {**base, "status": "blocked", "detail": f"compile preflight: {why_cp}"}
            activity.record("merge", identity=BOT_LOGIN, dry_run=False, **res)
            return res
    if override_pending or verify_override_pending:
        from prospector_app.backend import caps  # deferred: caps imports executor
        assert reason is not None
        by = caps.capabilities().get("login")
        edit = data.store().edit_pr(int(n))
        if override_pending:
            edit.log_security_override(reason, by=by)
        if verify_override_pending:
            edit.log_verify_override(reason, by=by)
        data.refresh()
    # Pin the merge to the exact head the gate verified. GitHub's merge endpoint
    # refuses server-side when the live head no longer matches, closing the
    # window between the pre-flight head read above and this call — a force-push
    # in that window can't land unverified code.
    argv = ["gh", "pr", "merge", str(n), "--repo", REPO, f"--{method}"]
    if rec is not None and rec.head_sha:
        argv += ["--match-head-commit", rec.head_sha]
    try:
        r = bot_merge_run(argv, token)
        res = ({**base, "status": "merged", "detail": f"merged (--{method})"} if r.returncode == 0
               else {**base, "status": "error", "detail": r.stderr.strip()[:160]})
    except Exception as e:
        res = {**base, "status": "error", "detail": str(e)[:160]}
    if res["status"] == "merged":
        _reflect_state(n, state="closed", merged=True)
    activity.record("merge", identity=BOT_LOGIN, dry_run=False, **res)
    if res["status"] == "merged":
        _credit_merge_closed_issues(rec, n, dry_run=False)
    return res


def _credit_merge_closed_issues(rec: Pr | None, pr: int, *, dry_run: bool) -> None:
    """Credit each issue GitHub auto-closes when PR `pr` merges. A PR body's
    Fixes/Closes #N (its "explicit" issue link) is exactly the set GitHub closes on
    merge, so a merge that lands a fix is the strongest issue action there is — it
    belongs in the issue bucket of the activity log even though GitHub, not our
    executor, runs the close. Records one issue-close per explicit-linked issue we
    don't already see closed (action CLOSE_ISSUE_FIXED, via="merge") and mirrors the
    close into the issue store. Makes no gh call of its own — a live merge closes the
    issue upstream as a side effect; a dry-run only records the preview entry."""
    if rec is None:
        return
    from prospector_app.backend import issues as issues_mod
    seen: set[int] = set()
    for link in rec.linked_issues:
        if link.get("how") != "explicit":
            continue
        m = int(link["issue"])
        if m in seen:
            continue
        seen.add(m)
        if issues_mod.issue_state(m) == "closed":
            continue  # already closed before the merge — our merge didn't close it
        base = {"issue": m, "action": "CLOSE_ISSUE_FIXED", "fixed_by": int(pr), "via": "merge"}
        if dry_run:
            res = {**base, "status": "dry-run", "detail": f"would close #{m} as fixed by merged #{pr}"}
            activity.record("issue-close", identity=BOT_LOGIN, dry_run=True, **res)
            continue
        res = {**base, "status": "closed", "detail": f"closed by merge of #{pr}"}
        activity.record("issue-close", identity=BOT_LOGIN, dry_run=False, **res)
        issues_mod.reflect_issue_state(m, "closed", "completed")


def _issue_close_argv(n: int, reason: str, canonical: int | None) -> list[str]:
    """The argv that closes issue `n` upstream with GitHub state_reason `reason`. A
    'duplicate' close carries --duplicate-of when the canonical is known, so GitHub
    records the marked_as_duplicate link on the canonical issue."""
    argv = ["gh", "issue", "close", str(n), "--repo", REPO, "--reason", reason]
    if reason == "duplicate" and canonical is not None:
        argv += ["--duplicate-of", str(canonical)]
    return argv


def close_issue(n: int, action: models.IssueCloseDupBody, *, token: str | None, dry_run: bool) -> dict:
    """Close a GitHub issue as a duplicate, as the configured bot: post a comment
    pointing at the canonical issue, then close it `duplicate` — via --duplicate-of
    when a canonical is known, so GitHub records the marked_as_duplicate link on it
    (#192). Reversible (reopen + delete the comment). Shares the comment endpoint
    with PRs, so the same bot-write gate applies."""
    from prospector_app.backend import issues as issues_mod
    canonical = action.canonical
    base = {"issue": int(n), "action": "CLOSE_ISSUE_DUP", "canonical": canonical}
    # Gate: only a confirmed, eligible duplicate may be closed (the issue-side
    # analog of the merge path's gates.merge_eligibility). Live-checks upstream
    # that the dup is open and its canonical is open or fixed (#411), and blocks
    # dry-runs too — so a dry-run preview matches what a live run does.
    ok, reason = issues_mod.close_dup_gate(int(n))
    if not ok:
        res = {**base, "status": "blocked", "detail": f"close-dup gate: {reason}"}
        activity.record("issue-close", identity=BOT_LOGIN, dry_run=dry_run, **res)
        return res
    comment = action.comment or issues_mod.dup_issue_comment(canonical)
    plan = [f'comment: "{comment[:80]}…"', f"close issue #{n}"]

    if dry_run or not token:
        res = {**base, "status": "dry-run", "detail": "; ".join(plan), "forced": not token and not dry_run}
        activity.record("issue-close", identity=BOT_LOGIN, dry_run=True, **res)
        return res

    # issues and PRs share the issues/comments endpoint
    return _comment_then_close(
        n, base=base, idempotency_key=f"#{canonical}" if canonical else _comment_marker(comment),
        comment_argv=["gh", "issue", "comment", str(n), "--repo", REPO, "--body", comment],
        close_argv=_issue_close_argv(n, "duplicate", canonical),
        token=token, log_verb="issue-close",
        on_success=lambda: issues_mod.reflect_issue_state(n, "closed", "duplicate"))


def close_issue_fixed(n: int, action: models.IssueCloseFixedBody, *, token: str | None, dry_run: bool) -> dict:
    """Close a GitHub issue as fixed by a merged PR, as the configured bot: post a comment
    pointing at the fixing PR, then close it `completed`. Reversible (reopen + delete
    the comment). Gated on issues.close_fixed_gate, which re-verifies live at write
    time that the fixing PR is merged and the issue still open."""
    from prospector_app.backend import issues as issues_mod
    fixed_by = int(action.fixed_by)
    base = {"issue": int(n), "action": "CLOSE_ISSUE_FIXED", "fixed_by": fixed_by}
    ok, reason = issues_mod.close_fixed_gate(int(n), fixed_by)
    if not ok:
        res = {**base, "status": "blocked", "detail": f"close-fixed gate: {reason}"}
        activity.record("issue-close", identity=BOT_LOGIN, dry_run=dry_run, **res)
        return res
    comment = action.comment or issues_mod.fixed_issue_comment(fixed_by)
    plan = [f'comment: "{comment[:80]}…"', f"close issue #{n} as fixed by #{fixed_by}"]

    if dry_run or not token:
        res = {**base, "status": "dry-run", "detail": "; ".join(plan), "forced": not token and not dry_run}
        activity.record("issue-close", identity=BOT_LOGIN, dry_run=True, **res)
        return res

    # issues and PRs share the issues/comments endpoint
    return _comment_then_close(
        n, base=base, idempotency_key=f"#{fixed_by}",
        comment_argv=["gh", "issue", "comment", str(n), "--repo", REPO, "--body", comment],
        close_argv=_issue_close_argv(n, "completed", None),
        token=token, log_verb="issue-close",
        on_success=lambda: issues_mod.reflect_issue_state(n, "closed", "completed"))


# An operator-directed close disposition maps to the GitHub state_reason the issue
# closes with, the activity-log action it lands as (so fixed/dup attribute to their
# stat cards), and — for fixed/dup — the templated comment posted when the operator
# leaves the box empty.
_CLOSE_REASON = {"not-planned": "not planned", "completed": "completed",
                 "fixed": "completed", "dup": "duplicate"}
_CLOSE_ACTION = {"not-planned": "CLOSE_ISSUE", "completed": "CLOSE_ISSUE",
                 "fixed": "CLOSE_ISSUE_FIXED", "dup": "CLOSE_ISSUE_DUP"}


def close_issue_with_comment(n: int, action: models.IssueCloseBody, *, token: str | None,
                             dry_run: bool) -> dict:
    """Close a GitHub issue as directed by the operator, as the configured bot: post a
    comment, then close it with the disposition's state_reason. Dispositions:
    not-planned / completed are plain closes (comment required); fixed closes
    'completed' as fixed by `action.fixed_by`; dup closes 'duplicate' of
    `action.canonical`. fixed/dup fall back to a templated comment when
    the operator leaves it empty, and land as CLOSE_ISSUE_FIXED / CLOSE_ISSUE_DUP so
    they attribute to the activity stat cards. Operator-asserted (light live checks,
    no recorded-link requirement), gated on issues.close_gate, reversible."""
    from prospector_app.backend import issues as issues_mod
    disp = action.disposition
    reason = _CLOSE_REASON.get(disp, "not planned")
    base = {"issue": int(n), "action": _CLOSE_ACTION.get(disp, "CLOSE_ISSUE"), "reason": reason}
    if disp == "fixed":
        base["fixed_by"] = action.fixed_by
    elif disp == "dup":
        base["canonical"] = action.canonical
    ok, why = issues_mod.close_gate(int(n), disp, action.comment, action.fixed_by, action.canonical)
    if not ok:
        res = {**base, "status": "blocked", "detail": f"close gate: {why}"}
        activity.record("issue-close", identity=BOT_LOGIN, dry_run=dry_run, **res)
        return res
    comment = (action.comment or "").strip()
    if not comment and disp == "fixed" and action.fixed_by is not None:
        comment = issues_mod.fixed_issue_comment(int(action.fixed_by))
    elif not comment and disp == "dup":
        comment = issues_mod.dup_issue_comment(action.canonical)
    plan = [f'comment: "{comment[:80]}…"', f"close issue #{n} ({reason})"]

    if dry_run or not token:
        res = {**base, "status": "dry-run", "detail": "; ".join(plan), "forced": not token and not dry_run}
        activity.record("issue-close", identity=BOT_LOGIN, dry_run=True, **res)
        return res

    return _comment_then_close(
        n, base=base, idempotency_key=_comment_marker(comment),
        comment_argv=["gh", "issue", "comment", str(n), "--repo", REPO, "--body", comment],
        close_argv=_issue_close_argv(n, reason, action.canonical),
        token=token, log_verb="issue-close",
        on_success=lambda: issues_mod.reflect_issue_state(n, "closed", reason))
