"""Context-aware 'ask the agent' chat — headless `claude -p`, streamed as SSE.

The cockpit's agent pane is global: it knows what you're looking at. A question
carries a context (a PR, a cluster, an issue, or nothing) plus an optional diff anchor
(file:line). Any question also carries the operator's currently visible/
filtered PR list (e.g. PR Explorer, #355) whenever one is on screen — so
"review these" doesn't need the numbers spelled out, even when a PR's flyout
happens to be open on top of that list (#507) — that list doesn't change the
thread identity, only the prompt. We build the right context block, spawn a
sandboxed headless Claude rooted at the repo, and stream the answer. Threads +
claude sessions are kept per thread key — a subject's context id (pr/cluster/
issue/general) by default, or an explicit `chat_id` for an operator-named session
(#343) — so each has its own running conversation.

It is mostly a reader, but when a bot token can be minted it may also
run a curated set of upstream writes — on PRs (edit/comment/close/reopen/review),
issues (create/close/reopen/comment/edit), and workflow runs (rerun) — AS the bot,
after the operator confirms in chat — see _GH_WRITE_ALLOW / isolation_flags. Two
write paths run AS THE OPERATOR instead, each through a helper script that drops the
bot token: "resubmit" (_RESUBMIT_ALLOW), which both authors a change on a
contributor's fork branch and pushes it over the operator's ssh key — a GitHub App
can't push to a fork (#210) — and merges the base branch into a stale PR's head
(`resubmit <pr> update`), which the bot can't do either once that merge carries a
`.github/workflows/**` change; and "file-issue" (_FILE_ISSUE_ALLOW), opening a
triager bug on the meta-repo, which lies outside the bot's app installation.

ISOLATED. This is a lean Q&A assistant, NOT the developer's full dev agent. We
run it sandboxed from the operator's personal Claude Code harness so it doesn't
inherit plugins (e.g. superpowers + its SessionStart skill preamble), MCP
servers, slash-command skills, hooks, or the repo's dev-facing CLAUDE.md — all of
which made the embedded agent announce "superpowers", talk about plan mode, and
behave like a coding agent. The lockdown (see isolation_flags):
  - --safe-mode → disable ALL discovered customizations in one flag: CLAUDE.md
    auto-discovery, hooks, plugins, skills, MCP servers, custom agents/commands.
    Unlike --bare it keeps normal OAuth/subscription auth. This is what stops
    CLAUDE.md from doing double duty: the agent gets its OWN context from
    agent/context.md, not the dev manual.
  - --setting-sources "" → load NONE of the settings files (user, project,
    local). safe-mode disables customizations but leaves *permissions* working
    normally, so the agent's permission model comes entirely from the CLI
    allow/deny here — not from the repo `.claude/settings.json`, whose deny layer
    is the operator-session guard against hand-run upstream `gh`
    writes and is not this agent's boundary (its own dontAsk + allowlist is).
  - --permission-mode dontAsk → never prompt, silently deny anything outside the
    allowlist. (NOT `plan`: plan mode is a *coding* affordance — propose-a-plan-
    then-await-approval — which produced the "permissions / plan" chatter.)
  - --allowedTools Read,Grep,Glob + read-only `gh` (see _GH_ALLOW) + the helper
    scripts, plus the curated upstream PR + issue writes (_GH_WRITE_ALLOW) when a
    bot token is available; --disallowedTools <everything else> → dontAsk denies
    any command not on the allowlist.

Context comes from two dedicated, agent-facing sources (NOT the dev CLAUDE.md):
agent/context.md (stable operating manual, injected into the system prompt) and
agent_memory (durable learnings, injected into the first message of each thread).

Chat transcripts are stored per operator in the shared SQL `chat_messages` table
(same DB as the store and activity). Only the claude resume session_id — a
local-machine handle — stays in gitignored cache/.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from app.backend import activity
from app.backend import agent_memory
from app.backend import data
from app.backend import executor
from app.backend import issues
from app.backend import safety_guard
from app.backend import subproc
from pipeline import review_policy
from pipeline import schema
from app.backend import service
from pipeline import store
from pipeline import storekit
from pipeline.settings import BOT_LOGIN, DISPLAY_NAME, FEEDBACK_REPO, REPO

REPO_ROOT = Path(__file__).resolve().parents[2]
COCKPIT = Path(__file__).resolve().parents[1]
# Gitignored cache for per-thread machine-local state (partials, claude session_ids).
SESSION_DIR = COCKPIT / "cache" / "chat"
AGENT_CONTEXT = COCKPIT / "agent" / "context.md"
CLAUDE_BIN = shutil.which("claude") or "claude"
DIFF_BUDGET = 16000

# The agent's read `gh` allowlist: read-only PR/issue/search commands. Every write
# lives elsewhere — the curated UPSTREAM writes (_GH_WRITE_ALLOW, as the bot) are
# added on top of this only when a bot token is available, and meta-repo issue
# filing runs as the operator through its own script (_FILE_ISSUE_ALLOW). dontAsk
# denies anything not listed; --setting-sources "" loads no settings file so a repo
# grant can't widen this. The agent is instructed (agent/context.md) which repo to
# target and to confirm before any write.
#
# Why Bash-with-allowlist and not an MCP server: in headless `claude -p` +
# stream-json (what the cockpit streams over SSE), MCP tools register too late to
# be exposed to the model — measured ~20% availability, unusable. A `gh`-command
# allowlist is reliable there AND matches how the rest of this repo reads GitHub
# (safety_guard's `run`). Safety: --permission-mode dontAsk runs ONLY allowlisted
# commands; everything else (gh pr merge, gh api -X POST, redirects, …) is
# silently denied. --setting-sources "" loads none of the settings files, so a
# repo `Bash(gh pr *)` grant can't widen this allowlist. Verified end-to-end.
_GH_ALLOW = [
    "Bash(gh pr view:*)", "Bash(gh pr diff:*)", "Bash(gh pr list:*)",
    "Bash(gh pr checks:*)", "Bash(gh pr status:*)", "Bash(gh issue view:*)",
    "Bash(gh issue list:*)", "Bash(gh search prs:*)", "Bash(gh search issues:*)",
    "Bash(gh search commits:*)", "Bash(gh release view:*)",
    "Bash(gh run view:*)",
]

# The curated upstream-write commands the agent may run on the configured repo
# — but ONLY when a bot token is minted, in which case the whole `gh`
# subprocess is authenticated as the bot (see _bot_token + stream_chat). These are
# added to the allowlist on top of _GH_ALLOW only in that case; with no token they
# are withheld and the agent's only write is the meta-repo issue it files as the
# operator (_FILE_ISSUE_ALLOW). Covers PR writes and issue writes, `gh issue
# create` among them — upstream issue filing is a bot write like the rest, and the
# script owns the meta-repo. Workflow reruns are included so
# the agent can retry a failed CI run after confirmation. `gh pr merge` is
# deliberately ABSENT: merges stay on the executor's gated path. Updating a stale
# PR's branch is absent too — a bot token may not write `.github/workflows/**`, which
# a moved base branch routinely carries into the merge, so that runs as the operator
# through `resubmit <pr> update` (_RESUBMIT_ALLOW). The agent is
# instructed (agent/context.md) to confirm with the operator before each write and to use
# `gh pr edit` for the body/title only. dontAsk denies anything not listed here.
_GH_WRITE_ALLOW = [
    "Bash(gh issue create:*)",
    "Bash(gh pr edit:*)",
    "Bash(gh pr comment:*)",
    "Bash(gh pr close:*)",
    "Bash(gh pr reopen:*)",
    "Bash(gh pr review:*)",
    "Bash(gh issue close:*)",
    "Bash(gh issue reopen:*)",
    "Bash(gh issue comment:*)",
    "Bash(gh issue edit:*)",
    "Bash(gh run rerun:*)",
]

# The agent's one self-write: persist a learning to its committed memory. Same
# allowlisted-Bash mechanism as gh-issue-create (MCP is unreliable in headless
# stream-json), scoped to exactly this command. The agent runs from REPO_ROOT, so
# the path is repo-relative; dontAsk denies any other invocation. The agent is
# instructed (agent/context.md) to call it proactively when it learns something
# durable. NOTE: keep this prefix in sync with the script's actual path.
_REMEMBER_ALLOW = ["Bash(app/agent/remember:*)"]

# The agent's bug-filing write: open an issue on the meta-repo (COCKPIT_FEEDBACK_REPO)
# AS THE OPERATOR. The meta-repo sits outside the bot's GitHub App installation, so
# a bot-authenticated `gh` can't even resolve it; the script drops the bot token and
# files under the operator's own login. Always available — a triager bug is
# reportable on every machine — and scoped by the script to that one repo, so the
# operator-identity write reaches nothing else. NOTE: keep this prefix in sync with
# the script's actual path.
_FILE_ISSUE_ALLOW = ["Bash(app/agent/file-issue:*)"]

# The agent's manual clustering override: detach a mis-grouped PR from a cluster.
# A LOCAL store edit (no upstream write, no bot token), so it rides the same
# always-available allowlisted-Bash path as `remember` rather than _GH_WRITE_ALLOW.
# It mutates through the validated store accessor; the agent is instructed
# (agent/context.md) to confirm with the operator before detaching. NOTE: keep
# this prefix in sync with the script's actual path.
_UNCLUSTER_ALLOW = ["Bash(app/agent/uncluster:*)"]

# The agent's read window into GitHub's raw file contents + code search. `gh api`
# is read-only only by default (`-X PUT …/contents/<path>` writes a file, `-X PUT
# …/pulls/N/merge` merges), and a prefix allowlist can't forbid a trailing `-X
# PUT`, so raw `gh api` can't be allowlisted safely. This script is the safe slice:
# the HTTP method is hardcoded to GET, so it can only read. A read only (no bot
# token, no upstream write), so it rides the same always-available allowlisted-Bash
# path as `store-read`. NOTE: keep this prefix in sync with the script's actual path.
_GH_READ_ALLOW = ["Bash(app/agent/gh-read:*)"]

# The agent's read window into the local git tree: `git diff` reads the working
# tree / index / commits and prints — it never mutates the repo, whatever its args
# — so it rides the same always-available allowlisted-Bash path as the other reads.
# The `git diff` token pair matches only that subcommand; dontAsk denies every other
# git invocation (commit, push, checkout, …), so no write or history edit is reachable.
_GIT_ALLOW = ["Bash(git diff:*)"]

# The agent's read window into the store. The store is SQL (Supabase Postgres via
# TRIAGE_STORE_URL, or local SQLite) — the agent's file tools can't open a DB
# connection, so this allowlisted command reads a PR/cluster record (or the threat
# registry) through the validated store accessor. A read only (no upstream write,
# no bot token), so it rides the same always-available path as `remember`. NOTE:
# keep this prefix in sync with the script's actual path.
_STORE_READ_ALLOW = ["Bash(app/agent/store-read:*)"]

# The agent's "reingest a PR" path: refresh one PR's store record against its
# current head SHA and re-triage it (re-summarize + re-analyze the stale sections),
# the natural follow-on to a `resubmit` push so the resubmitted PR becomes
# mergeable without an out-of-band pipeline run. A LOCAL store edit (no upstream
# write, no bot token) that mutates through the validated accessor, so it rides the
# same always-available allowlisted-Bash path as `store-read` / `uncluster` rather
# than the write-gated ones. NOTE: keep this prefix in sync with the script's path.
_REINGEST_ALLOW = ["Bash(app/agent/reingest:*)"]

# The agent's "resubmit a PR" path: author changes on a contributor's fork branch
# and push them AS THE OPERATOR (not the bot — a GitHub App can't push to a
# fork even with "Allow edits from maintainers"; that grants push to maintainer
# *users*, #210). The script owns all git mechanics and drops the bot token so the
# push goes out under the operator's ssh identity. Its `update` subcommand is the
# same operator identity applied to a base-branch merge (`gh pr update-branch`),
# which the bot can't run once the merge carries a `.github/workflows/**` change:
# an App token needs the `workflows` permission to write those, the operator's
# `workflow` token scope covers it. It rides the same allowlisted-
# Bash path, but — like _GH_WRITE_ALLOW — is unlocked only on a real operator
# machine (can_write), together with the Edit/Write tools the agent needs to author
# the change in the prepared worktree. NOTE: keep this prefix in sync with the
# script's actual path.
_RESUBMIT_ALLOW = ["Bash(app/agent/resubmit:*)"]

# Everything that is NOT a read tool. dontAsk already blocks their *use*; listing
# them here removes them from the model's advertised toolset so it doesn't believe
# it can spawn agents, enter plan mode, or invoke skills. Edit/Write are here as
# the read-only default but are lifted on a real operator machine (see
# isolation_flags) so the agent can author a resubmit. NOTE: Bash is deliberately
# NOT here — it stays available but is constrained to the allowlisted `gh` commands
# in _GH_ALLOW (dontAsk denies any Bash command not allowlisted).
_DISALLOWED_TOOLS = [
    "Task", "Edit", "Write", "NotebookEdit",
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    "Skill", "Workflow", "SendMessage", "TeamCreate", "TeamDelete",
    "CronCreate", "CronDelete", "CronList",
    "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate",
    "Monitor", "RemoteTrigger", "PushNotification", "ScheduleWakeup",
    "DesignSync", "ToolSearch", "WebFetch", "WebSearch", "AskUserQuestion", "LSP",
]

# Flags that sandbox the embedded agent from the operator's global harness.
# --safe-mode disables CLAUDE.md, hooks, plugins, skills, MCP servers, and custom
# agents/commands (keeping OAuth auth, unlike --bare); --setting-sources "" then
# drops every settings file (user/project/local) so the agent's permissions come
# only from the explicit allow/deny below. safe-mode keeps permissions working
# normally (the repo's project deny among them), so dropping the settings files is
# what keeps that deny out of this agent's boundary.
def isolation_flags(can_write: bool) -> list[str]:
    """The agent's sandbox flags. When can_write (a bot token was
    minted, so this is a real operator machine) the curated upstream writes in
    _GH_WRITE_ALLOW are added to the allowlist, along with the resubmit path
    (_RESUBMIT_ALLOW) and the Edit/Write tools it needs to author a change in the
    prepared fork worktree. Otherwise the agent's only write is the meta-repo issue
    it files as the operator (_FILE_ISSUE_ALLOW) — no upstream write command or
    file-edit tool is even advertised, so there is no way to touch the configured
    repo or files on disk."""
    allowed = ["Read", "Grep", "Glob", *_GH_ALLOW, *_GH_READ_ALLOW, *_GIT_ALLOW,
               *_REMEMBER_ALLOW, *_UNCLUSTER_ALLOW, *_STORE_READ_ALLOW, *_REINGEST_ALLOW,
               *_FILE_ISSUE_ALLOW]
    disallowed = list(_DISALLOWED_TOOLS)
    if can_write:
        allowed += [*_GH_WRITE_ALLOW, *_RESUBMIT_ALLOW, "Edit", "Write"]
        # The resubmit path authors real code edits, so Edit/Write come off the
        # deny list. They stay filesystem-wide (claude can't path-scope them), so
        # the agent is instructed (agent/context.md) to edit only inside the
        # worktree `resubmit prepare` sets up; the push only ever commits from
        # that isolated clone, so a stray edit elsewhere never reaches upstream.
        disallowed = [t for t in disallowed if t not in ("Edit", "Write")]
    return [
        "--allowedTools", ",".join(allowed),
        "--disallowedTools", *disallowed,
        "--permission-mode", "dontAsk",
        "--safe-mode",
        "--setting-sources", "",
    ]


# A minted bot token, cached so we don't re-mint (a shell-out + two API
# calls) on every chat turn. The token is valid ~1h; we refresh well before that.
# None when no key is installed / the token can't be minted — in which case the
# agent stays read-only (writes are simply never offered, never sent as the
# operator's login).
_BOT_TOKEN: str | None = None
_BOT_TOKEN_EXP: float = 0.0


def _bot_token() -> str | None:
    global _BOT_TOKEN, _BOT_TOKEN_EXP
    if _BOT_TOKEN and _BOT_TOKEN_EXP > time.monotonic():
        return _BOT_TOKEN
    _BOT_TOKEN = executor.mint_bot_token()
    if _BOT_TOKEN:
        _BOT_TOKEN_EXP = time.monotonic() + 50 * 60  # refresh before the 1h expiry
    return _BOT_TOKEN

# ctx_id -> the live subprocess, so an operator can stop an in-flight answer (#14).
_RUNNING: dict[str, asyncio.subprocess.Process] = {}

def system_prompt() -> str:
    """The agent's operating manual (agent/context.md), injected as its system
    prompt. REQUIRED — a missing or empty file is a hard failure, never a silent
    fall back to a lesser prompt: running the triage agent without its real
    context is worse than refusing to run it."""
    try:
        text = AGENT_CONTEXT.read_text().strip()
    except OSError as e:
        raise RuntimeError(f"cockpit agent context missing: {AGENT_CONTEXT} ({e})") from e
    if not text:
        raise RuntimeError(f"cockpit agent context is empty: {AGENT_CONTEXT}")
    return (text.replace("{display_name}", DISPLAY_NAME)
                .replace("{repo}", REPO).replace("{bot}", BOT_LOGIN)
                .replace("{feedback_repo}", FEEDBACK_REPO or "(none configured)")
                .replace("{retrigger_mention}",
                         review_policy.active().retrigger_mention or "(none configured)"))


def _ctx_id(pr: int | None, cluster: int | None, issue: int | None = None) -> str:
    if pr:
        return f"pr-{pr}"
    if cluster:
        return f"cluster-{cluster}"
    if issue:
        return f"issue-{issue}"
    return "general"


def _thread_key(chat_id: str | None, pr: int | None, cluster: int | None,
                issue: int | None = None) -> str:
    """The thread's storage/claude-resume key. An explicit `chat_id` — an
    operator-created named session (#343) — always wins, so a session keeps its
    own conversation independent of whatever subject the operator is currently
    viewing. A caller with no chat_id falls back to one thread per subject,
    unchanged from before named sessions existed."""
    return chat_id or _ctx_id(pr, cluster, issue)


def _op_slug() -> str:
    """The operator's shard name — same derivation activity uses, so a chat
    transcript is attributable to the same person."""
    return activity.operator()["slug"]


_TABLES_READY = False


def _engine() -> storekit.Engine:
    global _TABLES_READY
    eng = storekit.get_engine(storekit.resolve_url(None, store.DEFAULT_ROOT))
    if not _TABLES_READY:
        schema.METADATA.create_all(eng)
        _TABLES_READY = True
    return eng


def _meta_path(ctx_id: str) -> Path:
    # Local-only claude resume handle, scoped per operator so switching identity
    # doesn't resume someone else's session.
    return SESSION_DIR / _op_slug() / f"{ctx_id}.meta.json"


def _partial_path(ctx_id: str) -> Path:
    # In-progress streamed reply, persisted locally so a backend hot-reload doesn't
    # lose it (#191). Machine-local recovery state — lives in the gitignored cache,
    # never the shared store.
    return SESSION_DIR / _op_slug() / f"{ctx_id}.partial"


def _write_partial(ctx_id: str, text: str) -> None:
    pp = _partial_path(ctx_id)
    pp.parent.mkdir(parents=True, exist_ok=True)
    pp.write_text(text)


def _clear_partial(ctx_id: str) -> None:
    _partial_path(ctx_id).unlink(missing_ok=True)


def _reground_path(ctx_id: str) -> Path:
    # Marker: the live claude session for this ctx is unreliable — the last turn was
    # stopped, or a resume silently started a fresh session — so the next turn
    # re-grounds from the persisted transcript instead of trusting `-r` resume.
    return SESSION_DIR / _op_slug() / f"{ctx_id}.reground"


def _mark_reground(ctx_id: str) -> None:
    p = _reground_path(ctx_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("1")


def _consume_reground(ctx_id: str) -> bool:
    # Read-and-clear: a re-ground applies to exactly the next turn.
    p = _reground_path(ctx_id)
    if p.exists():
        p.unlink(missing_ok=True)
        return True
    return False


# Cap the replayed transcript so a re-grounded prompt stays bounded on a long
# thread; the most-recent turns matter most, so it is kept from the end.
_REPLAY_BUDGET = 8000


def _thread_digest(thread: list[dict]) -> str:
    """Compact replay of a thread's prior turns, injected when the live claude
    session was lost (e.g. after Stop) so a fresh session continues seamlessly.
    Kept from the end (most recent) within a char budget."""
    lines: list[str] = []
    used = 0
    for m in reversed(thread):
        text = (m.get("text") or "").strip()
        if not text:
            continue
        who = "operator" if m.get("role") == "user" else "you"
        entry = f"[{who}] {text}"
        if lines and used + len(entry) > _REPLAY_BUDGET:
            break
        lines.append(entry)
        used += len(entry)
    lines.reverse()
    return ("CONVERSATION SO FAR — the live session was interrupted, so this "
            "thread's prior turns are replayed here; continue from where it "
            "left off:\n" + "\n".join(lines))


def load_thread(ctx_id: str) -> list[dict]:
    # Recover an orphaned partial: a sidecar for a ctx that isn't streaming means
    # its stream died before the reply was saved — fold it into the thread (#191).
    pp = _partial_path(ctx_id)
    if pp.exists() and ctx_id not in _RUNNING:
        partial = pp.read_text()
        if partial.strip():
            _save(ctx_id, "assistant", partial, None)
        pp.unlink(missing_ok=True)
    from sqlalchemy import select
    eng = _engine()
    c = schema.chat_messages.c
    with eng.connect() as conn:
        rows = conn.execute(
            select(c.role, c.text, c.at)
            .where((c.operator == _op_slug()) & (c.ctx_id == ctx_id))
            .order_by(c.rowid)).all()
    thread = [{"role": r[0], "text": r[1], "at": r[2]} for r in rows]
    # While a stream is live, show its current partial as a trailing bubble.
    if pp.exists() and ctx_id in _RUNNING:
        partial = pp.read_text()
        if partial.strip():
            thread.append({"role": "assistant", "text": partial, "at": datetime.now(timezone.utc).isoformat()})
    return thread


def _session_id(ctx_id: str) -> str | None:
    p = _meta_path(ctx_id)
    if p.exists():
        try:
            return json.loads(p.read_text()).get("session_id")
        except json.JSONDecodeError:
            return None
    return None


def _save(ctx_id: str, role: str, text: str, session_id: str | None) -> None:
    eng = _engine()
    with eng.begin() as conn:
        conn.execute(schema.chat_messages.insert().values(
            operator=_op_slug(), ctx_id=ctx_id, role=role, text=text,
            at=datetime.now(timezone.utc).isoformat()))
    if session_id:
        mp = _meta_path(ctx_id)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps({"session_id": session_id}))


def _activity_lines(acts: list[dict]) -> list[str]:
    """Render executed-action rows (activity.for_pr / for_issue) for a context
    block: instant, kind (+reason), posting identity, initiating operator, and
    the action's detail when present."""
    out: list[str] = []
    for a in acts:
        reason = f" ({a['reason']})" if a.get("reason") else ""
        op = f", operator {a['operator']}" if a.get("operator") else ""
        detail = f" — {a['detail']}" if a.get("detail") else ""
        out.append(f"  - {a.get('at')}: {a.get('kind') or '?'}{reason} "
                   f"as {a.get('identity') or '?'}{op}{detail}")
    return out


def _pr_context(pr: int, file: str | None, line: int | None) -> str:
    detail = service.pr_detail(pr) or {}
    sv = data.safety_by_pr().get(int(pr))
    findings = sv.get("findings", []) if sv else []
    lines = [
        f"CONTEXT: PR #{pr}: {detail.get('title')}",
        f"author @{detail.get('author')} · clusters {','.join(map(str, detail.get('clusters') or [])) or '—'} · "
        f"safety {detail.get('safety')} · drift {detail.get('drift_state')}",
    ]
    if findings:
        lines.append("Prior agent safety findings:")
        for f in findings:
            lines.append(f"  - [{f.get('severity')}] {f.get('title')} @ {f.get('location')}")
    acts = activity.for_pr(pr)
    if acts:
        lines.append("Actions already executed on this PR through Prospector "
                     "(the activity log — what actually happened upstream; "
                     "anything the pipeline recommends is only a proposal until "
                     "it appears here):")
        lines.extend(_activity_lines(acts))
    if file and line:
        lines.append(f"\nThe reviewer is asking specifically about {file}:{line}.")
    diff = (service.get_diff(pr) or {}).get("diff", "")
    if diff:
        if len(diff) > DIFF_BUDGET:
            diff = diff[:DIFF_BUDGET] + "\n…(diff truncated)…"
        lines.append(f"\n--- unified diff ---\n{diff}")
    return "\n".join(lines)


def _cluster_context(cid: int) -> str:
    """Neutral facts about a cluster — member PRs + signals — with the pipeline's
    own verdict/outcome/rationale DELIBERATELY withheld so the agent forms an
    unbiased opinion first. It reads the recorded verdict from the store itself,
    afterwards (see agent/context.md)."""
    d = service.cluster_detail(cid) or {}
    lines = [
        f"CONTEXT: Cluster {cid}: {d.get('root_problem')}",
        ("Member PRs with neutral signals only. The pipeline's recorded "
         "verdict/outcome/rationale is withheld here on purpose — read it from "
         "the store yourself AFTER you've formed your own view:"),
    ]
    for r in d.get("prs", []):
        s = r.get("signals") or {}
        g = s.get("greptile")
        lines.append(
            f"  - #{r.get('number')} \"{r.get('title')}\" @{r.get('author')} — "
            f"greptile {g if g is not None else '?'}/5, CI {s.get('ci')}, "
            f"mergeable {not s.get('conflicts')}, tests {s.get('has_tests')}, "
            f"+{s.get('additions')}/-{s.get('deletions')} over "
            f"{s.get('changed_files')} files"
        )
    return "\n".join(lines)


# Cap on the issue body rendered into the context block, so a pasted megabyte
# log doesn't blow the prompt.
_ISSUE_BODY_BUDGET = 6000

_HOW_LABEL = {"explicit": "explicit Fixes/Closes", "fix-found": "detector-found fix",
              "issue-ref": "references the issue", "subsystem": "same subsystem"}


def _issue_context(n: int) -> str:
    """Neutral facts about an issue — report, signals, dedup-cluster membership,
    and candidate PRs — with the pipeline's recorded disposition/rationale
    DELIBERATELY withheld so the agent forms an unbiased opinion first (same
    convention as _cluster_context). The live thread is a `gh issue view` away."""
    d = issues.get_issue(n) or {}
    lines = [
        f"CONTEXT: Issue #{n}: {d.get('title')}",
        f"author @{d.get('author')} · state {d.get('state')} · "
        f"labels {','.join(d.get('labels') or []) or '—'} · "
        f"{d.get('comments')} comments · {d.get('reactions')} reactions",
        f"subsystem {d.get('subsystem') or '—'} · repro grade {d.get('repro_grade') or '?'} · "
        f"pain {d.get('pain') if d.get('pain') is not None else '?'}",
        ("The pipeline's recorded disposition/rationale for this issue is "
         "withheld here on purpose — read it from the store yourself "
         f"(`app/agent/store-read issue {n} --section analysis`) "
         "AFTER you've formed your own view. Use "
         f"`gh issue view {n} --comments` for the live thread."),
    ]
    acts = activity.for_issue(n)
    if acts:
        lines.append("Actions already executed on this issue through Prospector "
                     "(the activity log — what actually happened upstream; "
                     "anything the pipeline recommends is only a proposal until "
                     "it appears here):")
        lines.extend(_activity_lines(acts))
    dups = d.get("duplicates") or []
    if dups:
        lines.append(f"Dedup cluster {d.get('cluster')} groups it with "
                     f"{', '.join(f'#{m}' for m in dups)}.")
    linked = d.get("linked_prs") or []
    if linked:
        lines.append("PRs that may address it:")
        for c in linked:
            how = _HOW_LABEL.get(c.get("how") or "", c.get("how") or "?")
            state = c.get("state") or "open?"
            lines.append(f"  - #{c.get('pr')} \"{c.get('title')}\" — {how}, {state}")
    body = (d.get("body") or "").strip()
    if body:
        if len(body) > _ISSUE_BODY_BUDGET:
            body = body[:_ISSUE_BODY_BUDGET] + "\n…(body truncated)…"
        lines.append(f"\n--- issue body ---\n{body}")
    return "\n".join(lines)


# Cap how many of the operator's currently-visible PRs get a detail line in the
# prompt — keeps the context block's token cost bounded even when the Explorer
# filter matches hundreds of PRs. Mirrors the frontend's VISIBLE_PRS_CAP.
_VISIBLE_PRS_CAP = 150


def _visible_prs_context(numbers: list[int], total: int | None = None) -> str:
    """Neutral facts about the PRs currently visible in the operator's PR
    Explorer (after their active filters/search) — #355, so a 'review these'
    question doesn't require the operator to re-list numbers already on screen.
    Sent alongside a PR/cluster subject's own context too, not just general
    questions (#507): a PR's flyout can be open on top of the filtered list
    the operator is still browsing, and it isn't itself a signal that they've
    switched to asking about only that one PR — the question's own wording is.
    Same neutral-signals convention as _cluster_context: the pipeline's recorded
    disposition/rationale verdict is withheld so the agent forms its own view."""
    n_total = total if total is not None else len(numbers)
    lines = [
        f"CONTEXT: the operator is looking at {n_total} PR(s) in the PR Explorer "
        "right now, after their active filters/search. Neutral signals only — "
        "the pipeline's recorded disposition/rationale is withheld here on "
        "purpose, same as cluster context:",
    ]
    for n in numbers:
        row = service.pr_row(n)
        if row is None:
            continue
        s = row.get("signals") or {}
        g = s.get("greptile")
        lines.append(
            f"  - #{n} \"{row.get('title')}\" @{row.get('author')} — "
            f"greptile {g if g is not None else '?'}/5, CI {s.get('ci')}, "
            f"mergeable {not s.get('conflicts')}, tests {s.get('has_tests')}, "
            f"+{s.get('additions')}/-{s.get('deletions')} over "
            f"{s.get('changed_files')} files"
        )
    if n_total > len(numbers):
        lines.append(f"  …(+{n_total - len(numbers)} more not shown — ask the "
                      "operator to narrow the filter for full coverage)")
    return "\n".join(lines)


def _build_context(pr: int | None, cluster: int | None, issue: int | None,
                   file: str | None, line: int | None) -> str:
    if pr:
        return _pr_context(pr, file, line)
    if cluster:
        return _cluster_context(cluster)
    if issue:
        return _issue_context(issue)
    return (f"CONTEXT: Prospector, triaging the open PRs on {REPO} "
            "grouped into clusters. Answer the operator's general question about the codebase or triage.")


async def stream_chat(question: str, pr: int | None = None, cluster: int | None = None,
                      issue: int | None = None,
                      file: str | None = None, line: int | None = None,
                      prs: list[int] | None = None, prs_total: int | None = None,
                      chat_id: str | None = None):
    ctx_id = _thread_key(chat_id, pr, cluster, issue)
    thread = load_thread(ctx_id)
    sid = _session_id(ctx_id)
    is_first = sid is None and not thread
    # A stopped or silently-forked prior turn leaves the live claude session
    # unresumable (see the end of this function). When flagged, this turn re-grounds
    # from the persisted transcript rather than trusting `-r`: re-inject the subject
    # context and replay the thread into a fresh session.
    needs_reground = not is_first and _consume_reground(ctx_id)
    ground = is_first or needs_reground

    anchored = f"[about {file}:{line}] " if file and line else ""
    # Recall durable learnings at thread start so the agent doesn't start cold
    # and re-learn the same repository-specific corrections.
    memory = agent_memory.context_block() if is_first else ""
    intro = f"{memory}\n\n" if memory else ""
    # The Explorer's filtered set (#355) can change between turns of the same
    # thread, unlike a pr/cluster subject's stable facts — re-stated on every
    # question, not just the thread's first, so the agent never answers against
    # a stale view. Sent alongside a pr/cluster subject too (#507), not just
    # general ones — a PR flyout open on top of the filtered list doesn't mean
    # the operator has stopped browsing it.
    visible = f"{_visible_prs_context(prs, prs_total)}\n\n" if prs else ""
    # The subject's facts (a PR/cluster's context) are injected at thread start and
    # re-injected on a re-ground, since the fresh session has lost them.
    base = f"{_build_context(pr, cluster, issue, file, line)}\n\n" if ground else ""
    # On a re-ground the session starts cold, so replay the prior turns to continue.
    replay = f"{_thread_digest(thread)}\n\n" if needs_reground else ""
    if is_first:
        prompt = f"{intro}{base}{visible}REVIEWER QUESTION: {anchored}{question}"
    elif needs_reground:
        prompt = f"{base}{replay}{visible}REVIEWER QUESTION: {anchored}{question}"
    else:
        prompt = f"{visible}{anchored}{question}"

    # When a bot token can be minted, the agent's curated upstream
    # writes are unlocked AND the whole gh subprocess is authenticated as the bot
    # (bot_env). With no token it stays read-only on the operator's local login.
    token = _bot_token()
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        *isolation_flags(can_write=bool(token)),
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        "--append-system-prompt", system_prompt(),
    ]
    # Resume the live session on a normal turn. On a re-ground the session is
    # unreliable, so start fresh — the context is replayed into the prompt above.
    resumed_sid = sid if (sid and not needs_reground) else None
    if resumed_sid:
        cmd += ["-r", resumed_sid]

    _save(ctx_id, "user", anchored + question, None)

    # start_new_session=True puts claude (and its children) in their own process
    # group so stop_chat() can signal the whole tree, not just the parent.
    proc = await subproc.spawn(
        cmd, cwd=REPO_ROOT, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        env=safety_guard.bot_env(token) if token else None,
    )
    _RUNNING[ctx_id] = proc
    captured_sid: str | None = None
    parts: list[str] = []
    last_pw = 0.0  # last partial-sidecar write time (throttled, #191)
    saw_result = False  # claude's own "this turn is done" signal
    stopped = False
    try:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            s = raw.decode("utf-8", "replace").strip()
            if not s:
                continue
            try:
                e = json.loads(s)
            except json.JSONDecodeError:
                continue
            captured_sid = captured_sid or e.get("session_id")
            t = e.get("type")
            if t == "stream_event":
                ev = e.get("event", {})
                if ev.get("type") == "content_block_delta":
                    dd = ev.get("delta", {})
                    if dd.get("type") == "text_delta" and dd.get("text"):
                        parts.append(dd["text"])
                        yield {"event": "delta", "data": dd["text"]}
            elif t == "assistant" and not parts:
                for c in e.get("message", {}).get("content", []):
                    if c.get("type") == "text" and c.get("text"):
                        parts.append(c["text"])
                        yield {"event": "delta", "data": c["text"]}
            elif t == "result":
                saw_result = True
                break
            # Persist the in-progress reply to its sidecar (throttled) so a backend
            # hot-reload mid-answer doesn't lose it (#191).
            if parts and time.monotonic() - last_pw > 0.4:
                _write_partial(ctx_id, "".join(parts))
                last_pw = time.monotonic()
    finally:
        # Runs on a clean finish AND on an abnormal teardown — an operator Stop
        # (the frontend closes its EventSource before it even calls /chat/stop)
        # or a plain dropped connection tears this generator down via
        # sse_starlette calling aclose()/cancelling the task mid-iteration
        # (#547). That used to skip straight past everything below, which sat
        # AFTER this try/finally: the assistant's partial reply was never
        # saved, and — critically — the thread was never flagged to re-ground,
        # so the next turn either silently resumed a dead `claude -r` session
        # or started a brand-new one with no context and no transcript replay
        # (the agent "forgetting" a cut-off turn). Persisting and deciding
        # re-ground BEFORE reaping the subprocess means a second cancellation
        # landing on `proc.wait()` can't skip them too.
        stopped = ctx_id not in _RUNNING
        _RUNNING.pop(ctx_id, None)
        _save(ctx_id, "assistant", "".join(parts), captured_sid)
        _clear_partial(ctx_id)
        # The live session's resumability is only confirmed when the turn ended
        # via claude's own "result" event; anything else (operator stop, dropped
        # connection, a crashed subprocess) — or a resume that silently forked
        # to a new session id — means the next turn must re-ground from the
        # persisted transcript rather than trust `-r`.
        resume_lost = bool(resumed_sid and captured_sid and captured_sid != resumed_sid)
        if not saw_result or resume_lost:
            _mark_reground(ctx_id)
        if proc.returncode is None:
            _terminate(proc)
        await proc.wait()

    yield {"event": "done", "data": json.dumps({"session_id": captured_sid, "stopped": stopped})}


def _terminate(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the subprocess's whole process group; fall back to killing the proc."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            pass


def stop_chat(pr: int | None = None, cluster: int | None = None, issue: int | None = None,
              chat_id: str | None = None) -> bool:
    """Interrupt the in-flight answer for a thread (#14). Returns True if one was
    running. Popping from _RUNNING first signals stream_chat that this was an
    operator stop (not a natural finish)."""
    ctx_id = _thread_key(chat_id, pr, cluster, issue)
    proc = _RUNNING.pop(ctx_id, None)
    if proc is None:
        return False
    if proc.returncode is None:
        _terminate(proc)
    return True
