"""Prospector backend — FastAPI.

Read-only API over the existing triage artifacts. Serves the built SPA from
frontend/dist when present; otherwise the SPA runs from the Vite dev server and
talks to this API via a proxy.

Run:  uv run uvicorn prospector_app.backend.app:app --reload --port 8787   (from the repo root)
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from prospector_app.backend import activity
from prospector_app.backend import advisories as advisories_mod
from prospector_app.backend import alerts as alerts_mod
from prospector_app.backend import autohunt_view
from prospector_app.backend import worker_control
from prospector_app.backend import worker_readiness
from prospector_app.backend import bulk
from prospector_app.backend import caps
from prospector_app.backend import chat
from prospector_app.backend import data
from prospector_app.backend import deep_search
from prospector_app.backend import decisions
from prospector_app.backend import executor
from prospector_app.backend import feedback
from prospector_app.backend import instance
from prospector_app.backend import issue_data
from prospector_app.backend import issues as issues_mod
from prospector_app.backend import jobs
from prospector_app.backend import freshness_live
from prospector_app.backend import models
from prospector_app.backend import onboarding
from prospector_app.backend import push_identity
from prospector_app.backend import pipeline_status
from prospector_app.backend import pr_history
from prospector_app.backend import pr_search
from prospector_app.backend import repo_meta
from prospector_app.backend import review_refresh
from prospector_app.backend import responses as responses_mod
from prospector_app.backend import service
from prospector_app.backend import suggested_actions
from prospector_app.backend import tables
from prospector_app.backend import training
from prospector_app.backend import fix_queue
from prospector_app.backend import fix_worker
from prospector_app.backend import verify_queue
from prospector_app.backend import verify_worker
from prospector_app.backend import work_status

from pipeline import reviewers
from pipeline import settings

class SurrogateSafeJSONResponse(JSONResponse):
    """JSON render that survives unpaired UTF-16 surrogates. GitHub text (review
    bodies, comments) can carry lone "\\ud800"-style escapes, which json.loads
    keeps as unencodable surrogate characters; each is emitted as "?" so the
    payload stays valid UTF-8 and the endpoint never 500s over broken text."""

    def render(self, content: object) -> bytes:
        return json.dumps(content, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8", errors="replace")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Launch background services without blocking application startup."""
    _launch_live_sweep()
    _launch_verify_worker()
    _launch_fix_worker()
    yield


app = FastAPI(title="Prospector", version="0.1.0",
              default_response_class=SurrogateSafeJSONResponse,
              lifespan=lifespan)

# Allow the Vite dev server during development: the default port 5173 plus
# this worktree's configured VITE_PORT (setup.sh assigns one per worktree).
_vite_ports = {"5173", os.environ.get("VITE_PORT", "5173")}
app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"http://{h}:{p}" for p in sorted(_vite_ports)
                   for h in ("localhost", "127.0.0.1")],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Paths that answer before this checkout has a deployment target: the wizard's
# own surface, the metadata the SPA routes on, and the liveness probe — the
# frontend reads 503 as "the server is down", which an unconfigured but running
# backend is not.
_UNCONFIGURED_OK = ("/api/onboarding/", "/api/meta", "/api/health")


@app.middleware("http")
async def require_configured(request, call_next):
    """Refuse every API call until a deployment target exists.

    An unconfigured process reaches the local SQLite fallback and answers list
    routes with empty results, which reads as a configured Prospector watching
    an empty repository. Refusing in one place means a route added later
    inherits it.

    409 rather than 503: the server is up and its state conflicts with the
    request. The frontend reads 502/503/504 as the backend being unreachable
    and covers the page with an outage banner, which would be the wrong story
    on a checkout that is merely waiting to be set up."""
    path = request.url.path
    if (path.startswith("/api/") and not path.startswith(_UNCONFIGURED_OK)
            and not settings.configured()):
        return JSONResponse({"unconfigured": True}, status_code=409)
    return await call_next(request)


@app.middleware("http")
async def no_store_api(request, call_next):
    """The app is a live dashboard over changing artifacts — browsers must
    never serve a cached /api response (it's caused stale 'assessed' counts after
    a sweep). Force revalidation on every API call; let static assets cache."""
    resp = await call_next(request)
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


def _launch_live_sweep():
    """On launch, refresh live PR state into the shared store in the background so
    the view converges on GitHub's truth within seconds — without blocking startup.

    Reuses a recent sweep (shared across operators via the store) instead of
    re-querying GitHub on every relaunch: the sweep only runs when none is on record
    or the last is older than PROSPECTOR_LIVE_TTL_MIN (default 60). A sweep is ~70
    GraphQL calls, so within the TTL a relaunch costs zero upstream calls — the
    manual "Refresh live state" button always forces one. Skipped entirely under
    pytest or when explicitly disabled."""
    import os
    import sys
    import threading
    if "pytest" in sys.modules or os.environ.get("PROSPECTOR_NO_LAUNCH_SWEEP"):
        return
    ttl = float(os.environ.get("PROSPECTOR_LIVE_TTL_MIN", "60"))
    if not freshness_live.stale(ttl):
        return  # a recent sweep is on record — reuse it, no GitHub calls this launch

    def run():
        try:
            freshness_live.sweep()
            data.refresh()
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


def _launch_verify_worker():
    """Start the sandbox-verification worker when this backend is the runner
    (TRIAGE_VERIFY_WORKER=1 — the machine with the Docker sandbox). Every other
    backend serves the queue API only. Skipped under pytest — tests drive the
    worker's functions directly."""
    import sys
    if "pytest" in sys.modules:
        return
    verify_worker.startup()


def _launch_fix_worker():
    """Start the autofix worker when this backend is the runner
    (TRIAGE_FIX_WORKER=1 — the machine holding the machine user's push key).
    Every other backend serves the queue API only. Skipped under pytest — tests
    drive the worker's functions directly."""
    import sys
    if "pytest" in sys.modules:
        return
    fix_worker.startup()


@app.get("/api/health")
def health():
    """Liveness, plus what the snapshot holds. Answers without loading the
    snapshot when there is no deployment target: the frontend polls this to
    decide whether the backend is up, so it has to be cheap and truthful on a
    checkout whose store is whatever a stale .env last named."""
    if not settings.configured():
        return {"ok": True, "configured": False, "clusters": 0, "prs": 0}
    return {"ok": True, "configured": True,
            "clusters": len(data.clusters()), "prs": len(data.prs())}


@app.get("/api/instance")
def instance_info():
    return instance.instance()


@app.get("/api/meta")
def meta() -> repo_meta.RepoMeta:
    return repo_meta.meta()


@app.get("/api/feedback/target")
def feedback_target() -> feedback.FeedbackTarget:
    return feedback.target()


@app.post("/api/feedback/generate")
async def feedback_generate(payload: dict = Body(...)) -> feedback.GenerateResult:
    """Generate a polished GitHub issue title + body from a raw description.
    Calls Claude; falls back to empty title + raw body on error."""
    desc = (payload.get("description") or "").strip()
    if not desc:
        raise HTTPException(400, "description required")
    return await feedback.generate_issue(desc)


@app.post("/api/refresh")
def refresh():
    data.refresh()
    return {"ok": True}


@app.get("/api/clusters")
def clusters():
    return {"items": service.cluster_summaries()}


@app.get("/api/clusters/{cid}")
def cluster(cid: int):
    detail = service.cluster_detail(cid)
    if detail is None:
        raise HTTPException(404, f"cluster {cid} not found")
    return detail


MAX_PR_QUERY_LIMIT = 5000  # covers the full open-PR corpus so the frontend's "All" page size works


@app.post("/api/prs/query")
def prs_query(payload: dict = Body(...)):
    """The one PR-list endpoint. Body: {spec, sort?, direction?, offset?, limit?}."""
    return service.query_prs(
        payload.get("spec") or {},
        sort=payload.get("sort"), direction=payload.get("direction"),
        offset=int(payload.get("offset", 0)), limit=min(int(payload.get("limit", 50)), MAX_PR_QUERY_LIMIT),
    )


MAX_PR_COUNT_SPECS = 20  # bounds one counts request; the Home screen sends a handful of specs


@app.post("/api/prs/counts")
def prs_counts(payload: dict = Body(...)):
    """Match totals for a batch of filter specs. Body: {specs: [spec, ...]}.
    While the store snapshot is still cold-loading, answers immediately with
    {"counts": null, "loading": true} — the load continues in the background
    and the Home screen polls, showing a loading state, so the request never
    pins a connection for the duration of the load."""
    specs = payload.get("specs")
    if not isinstance(specs, list) or len(specs) > MAX_PR_COUNT_SPECS:
        raise HTTPException(422, f"specs must be a list of at most {MAX_PR_COUNT_SPECS} filter specs")
    if not all(isinstance(s, dict) for s in specs):
        raise HTTPException(422, "each spec must be a filter-spec object")
    if data.snapshot_loading():
        return {"counts": None, "loading": True}
    return {"counts": service.count_prs(specs)}


@app.post("/api/prs/search")
async def prs_search(payload: dict = Body(...)):
    """NL query -> validated filter spec the Explorer applies. Returns {spec, note}."""
    query = (payload.get("query") or "").strip()
    if not query:
        return {"spec": {}, "note": ""}
    spec = await pr_search.search_to_spec(query)
    note = "interpreted as the filters below" if spec else "couldn't parse that into filters — try rephrasing"
    return {"spec": spec, "note": note}


@app.post("/api/prs/deep-search")
async def prs_deep_search(payload: dict = Body(...)):
    """Agentic deep search over a candidate PR set, streamed SSE. Body: {query, prs}.
    Emits `progress` {done,total} frames, then a final `result` with the matched
    PRs + per-PR reasons. The agent judges each PR on compact facts; output is
    coerced so only real candidates can match (see deep_search)."""
    query = (payload.get("query") or "").strip()
    prs = [int(n) for n in (payload.get("prs") or [])]

    async def gen():
        async for ev in deep_search.stream(query, prs):
            yield {"event": ev.pop("type"), "data": json.dumps(ev)}
    return EventSourceResponse(gen())


@app.get("/api/prs/{n}")
def pr(n: int):
    detail = service.pr_detail(n)
    if detail is None:
        raise HTTPException(404, f"PR {n} not in cache")
    return detail


@app.post("/api/prs/{n}/verify/queue")
def verify_queue_pr(n: int):
    """Queue PR #n for sandbox verification. The request lands in the shared
    store; the verification worker on the sandbox machine picks it up."""
    try:
        return verify_queue.queue_pr(n)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/prs/{n}/verify/dequeue")
def verify_dequeue_pr(n: int):
    """Cancel PR #n's still-queued verification request."""
    try:
        return verify_queue.dequeue_pr(n)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/verify/runner")
def verify_runner():
    """Verification-runner liveness (the verify_worker heartbeat registry) —
    lets any app warn when PRs are queued but no runner is online."""
    return verify_queue.runner_status()


@app.post("/api/prs/{n}/fix/queue")
def fix_queue_pr(n: int, action: str, payload: dict = Body(default={})):
    """Queue PR #n for an autofix action (update / rebase / fix). The request
    lands in the shared store; the autofix worker on the machine holding the
    push identity picks it up.

    `payload.guidance` is the operator's own instruction for a `fix`: it becomes
    the authoring agent's goal, and it is what authorizes the action where the
    profile names no fixable gates. It travels in the body because it is prose."""
    try:
        return fix_queue.queue_pr(n, action, guidance=payload.get("guidance"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/prs/{n}/fix/dequeue")
def fix_dequeue_pr(n: int):
    """Cancel PR #n's queued fix request, or discard one awaiting review."""
    try:
        return fix_queue.dequeue_pr(n)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/prs/{n}/fix/approve")
def fix_approve_pr(n: int, dry_run: bool = True):
    """Approve PR #n's authored fix for pushing. The worker re-derives a
    mechanical change against current base and pushes it on its next tick; an
    agent-authored fix is pushed from the worktree it prepared. With `dry_run`
    the click previews what a live approval would push, recording nothing."""
    try:
        return fix_queue.approve_pr(n, dry_run=dry_run)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/fix/queue")
def fix_queue_status(days: int = Query(7, ge=1, le=400), all_time: bool = False,
                     limit: int = Query(100, ge=1, le=autohunt_view.HISTORY_LIMIT_CAP)):
    """The autofix queue: every PR with a request in flight, the ones proven and
    waiting for a decision first, then the requests that ended in the last half
    hour. This is the browsable pre-tested backlog — approving a row re-runs its
    merge or rebase against current base before anything reaches the
    contributor's branch.

    `history` is fix-only run history from the runs ledger over the selected
    window, which is where an ending goes once it ages out of the queue itself.
    Pass `all_time=true` to span the whole ledger regardless of `days`."""
    window = None if all_time else days
    return {
        "queue": fix_queue.queue_entries(),
        "runner": fix_queue.runner_status(),
        "history": autohunt_view.history_window(window, limit=limit,
                                                lanes=frozenset({"fix"})),
    }


@app.get("/api/fix/runner")
def fix_runner():
    """Autofix-runner liveness plus this backend's push-identity configuration —
    what the app renders the actions' disabled reason from."""
    return fix_queue.runner_status()


@app.get("/api/onboarding/state")
def onboarding_state():
    """Where this checkout stands on the setup ladder. Answers on an
    unconfigured checkout — it is what the wizard reads to know what to ask."""
    return onboarding.state()


@app.post("/api/onboarding/probe")
def onboarding_probe(body: models.OnboardingProbe):
    """Check candidate configuration without writing any of it."""
    return onboarding.probe(body.store_url, body.repo, body.key_file,
                            agent=body.agent, agent_provider=body.agent_provider)


@app.post("/api/onboarding/apply")
def onboarding_apply(body: models.OnboardingApply):
    """Write one step's configuration and adopt it in this process."""
    try:
        if body.bundle is not None:
            return onboarding.apply_bundle(body.step, onboarding.parse_bundle(body.bundle))
        return onboarding.apply(body.step, body.env, body.profile)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"could not write configuration: {e}")


@app.get("/api/onboarding/push-identity/account")
def push_identity_account(login: str | None = None):
    """A GitHub user's login, id, and no-reply email: the operator's own `gh`
    login with no `login`, else the named account. 404 when there is no such
    user or gh cannot ask."""
    found = push_identity.account(login) if login else push_identity.operator_account()
    if found is None:
        raise HTTPException(404, "no such GitHub user, or gh is not signed in" if login
                            else "gh is not signed in on this machine")
    return found


@app.post("/api/onboarding/push-identity/key")
def push_identity_key(body: models.PushKeyRequest):
    """Generate this machine's contributor-push key for `login` — or show the
    one already there — and return its path and public half for the operator
    to add to the account."""
    try:
        return push_identity.generate_key(body.login.strip())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/onboarding/push-identity/probe")
def push_identity_probe(body: models.PushProbe):
    """Ask GitHub which account a key authenticates and whether it is `login`.
    Read-only: writing the identity is the `worker` onboarding step."""
    login = body.login.strip()
    try:
        path = (push_identity.operator_key_file(body.key_file)
                if body.key_file and body.key_file.strip()
                else push_identity.key_path(login))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return push_identity.probe_key(path, login)


@app.get("/api/status/now")
def status_now():
    """What the system is doing right now, across every machine on this store —
    the header status label's feed: active verify/fix runs with their step and
    host, queued/parked counts, worker liveness, and this backend's running
    jobs."""
    return work_status.now()


@app.get("/api/setup/readiness")
def setup_readiness():
    """This machine's worker and GitHub App readiness, plus its lane switches."""
    permissions = executor.bot_permissions() if settings.bot_login() else None
    return {
        "readiness": worker_readiness.report(),
        "flags": worker_control.flags(),
        "bot_permissions": {
            "configured": bool(settings.bot_login()),
            "available": permissions is not None,
            "actions": permissions.get("actions") if permissions is not None else None,
        },
    }


@app.post("/api/setup/flags")
def setup_flags(body: models.WorkerFlags):
    """Write this machine's worker lane switches to .env and reconcile the
    running threads with them. Only the five lane switches are writable; any
    other key is refused rather than skipped, so nothing here can reach the
    store password or either credential path."""
    try:
        worker_control.set_flags(body.flags)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"applied": worker_control.apply(),
            "readiness": worker_readiness.report()}


@app.post("/api/setup/share")
def setup_share(body: models.SetupShare | None = None):
    """Everything a teammate's fresh checkout needs to join this deployment,
    as one thing they paste into their setup wizard. POST, so the store URL is
    never in a request line. `include_key` adds the bot's private key and
    `include_push_key` the contributor-push identity; either is refused with a
    400 when this machine has none to give."""
    include_key = bool(body and body.include_key)
    include_push_key = bool(body and body.include_push_key)
    try:
        bundle = onboarding.build_bundle(include_key=include_key,
                                         include_push_key=include_push_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"bundle": json.dumps(bundle, indent=2)}


@app.get("/api/autohunt")
def autohunt(days: int = Query(7, ge=1, le=400), all_time: bool = False,
             limit: int = Query(100, ge=1, le=autohunt_view.HISTORY_LIMIT_CAP)):
    """The idle auto-hunter's status (worker opt-in, pools, failure memory), a
    result summary over the selected window, and up to `limit` individual runs
    from that same window, newest first — a fixed-size digest in place of an
    ever-growing table. Pass `all_time=true` to span the whole ledger
    regardless of `days`."""
    window = None if all_time else days
    return {
        "status": autohunt_view.status(),
        "summary": autohunt_view.summary(window),
        "history": autohunt_view.history_window(window, limit=limit,
                                                lanes=autohunt_view.HUNT_LANES),
    }


@app.get("/api/verify/queue")
def verify_queue_status(days: int = Query(7, ge=1, le=400), all_time: bool = False,
                         limit: int = Query(100, ge=1, le=autohunt_view.HISTORY_LIMIT_CAP)):
    """The sandbox-verification queue: every PR currently queued, waiting on a
    base refresh, or running, plus verify-only run history from the runs
    ledger over the selected window — separate from the Auto-hunt panel's
    combined security+verify feed. Pass `all_time=true` to span the whole
    ledger regardless of `days`."""
    window = None if all_time else days
    return {
        "queue": verify_queue.queue_entries(),
        "history": autohunt_view.history_window(window, limit=limit, lanes=frozenset({"verify"})),
    }


@app.get("/api/prs/{n}/actions")
def pr_actions(n: int):
    """Every real action the configured bot has taken on this PR (with the human
    operator who initiated each), newest-first — shown in the PR-detail panel."""
    return {"items": activity.for_pr(n)}


@app.get("/api/prs/{n}/history")
def pr_history_endpoint(n: int):
    """Condensed upstream activity for this PR — comments, reviews (each
    automated reviewer's flagged), commits, and reopen/close/force-push/rename
    events — read live from GitHub, oldest first. Powers the PR-detail history
    panel so a reviewer doesn't have to open GitHub to see the back-and-forth."""
    return {"items": pr_history.fetch_pr_history(n)}


@app.get("/api/prs/{n}/diff")
def pr_diff(n: int):
    return service.get_diff(n)


@app.get("/api/prs/{n}/reviews")
def pr_reviews(n: int):
    """Every automated reviewer's and scanner's stored entry + digest on this PR,
    keyed by reviewer id — the column hover and the PR page's per-reviewer
    blocks. {} when the PR is unknown."""
    return {"reviews": service.reviews_detail(n)}


@app.post("/api/freshness")
def freshness_check(payload: dict = Body(...)):
    """Live freshness check (#25): given a list of PR numbers, re-fetch their
    current GitHub state and report where it diverges from the store snapshot."""
    prs = payload.get("prs") or []
    return freshness_live.check([int(n) for n in prs if str(n).strip()])


@app.post("/api/actions/run-state")
def actions_run_state(payload: dict = Body(...)):
    """Run-state (#10): which of these PRs already have a landed live action,
    so the UI can mark them done and guard a re-fire. Backed by the activity
    log, so it survives a refresh."""
    prs = payload.get("prs") or []
    return {"states": activity.run_state([int(n) for n in prs if str(n).strip()])}


@app.post("/api/live/refresh")
def live_refresh(payload: dict = Body(default={})):
    """Sweep PRs' live upstream state (open/closed/merged) into the shared store so
    every operator's view reflects what GitHub shows — without ever writing
    the upstream repo. With a `prs` list, sweep just those; otherwise every PR the store
    still thinks is open. Read-only upstream."""
    prs_arg = payload.get("prs")
    targets = [int(n) for n in prs_arg if str(n).strip()] if prs_arg else None
    res = freshness_live.sweep(targets)
    data.refresh()
    return res


@app.get("/api/live/status")
def live_status():
    """When the live sweep last ran (shared across operators) — drives the
    'live as of …' UI."""
    return {"fetched_at": freshness_live.last_swept_at()}


@app.post("/api/responses/scan")
def responses_scan(payload: dict = Body(default={})):
    """Sweep the PRs we've acted on and classify how the community responded
    since (replies, reopens, new commits). Read-only upstream (GraphQL timeline)
    + an app-local registry write. With a `prs` list, scan just those."""
    prs_arg = payload.get("prs")
    targets = [int(n) for n in prs_arg if str(n).strip()] if prs_arg else None
    return responses_mod.scan(targets)


@app.post("/api/responses/{n}/ack")
def responses_ack(n: int):
    """Mark PR `n`'s community-response signal as seen (#537) — for every
    operator, until a newer response supersedes it."""
    return {"pr": n, "ack": responses_mod.ack(n)}


@app.get("/api/suggest/pr/{n}")
def suggest_pr(n: int, disposition: str | None = None):
    """Suggestion (incl. the exact bot comment) for a PR, optionally as if its
    disposition were `disposition` — lets the UI refresh the comment when the
    operator changes the selected action (#13)."""
    s = service.suggestion_for(n, disposition)
    if s is None:
        raise HTTPException(404, f"PR {n} not in store")
    return s


@app.get("/api/default-comment")
def default_comment(action: str, canonical: int | None = None,
                    upstream_pr: int | None = None, upstream_commit: str | None = None,
                    upstream_date: str | None = None, dup_reason: str | None = None):
    """The exact closing comment the executor would post for a manual close
    action — so the PR-detail Disposition panel can show & let you edit it before
    closing, instead of silently posting the default (#77). Single source of
    truth: the same decisions.default_comment the executor falls back to.
    `dup_reason` is an optional, genuine clause stating why the canonical wins —
    included only when supplied, never as boilerplate (#184)."""
    a = models.CloseAction(action=action, canonical=canonical, upstream_pr=upstream_pr,
                           upstream_commit=upstream_commit, upstream_date=upstream_date,
                           dup_reason=dup_reason)
    return {"comment": decisions.default_comment(a)}


# ---------------------------------------------------------------------------
# Live agent chat (M3).
# ---------------------------------------------------------------------------
@app.get("/api/chat/ready")
def chat_ready():
    """Whether this machine can run the agent pane — the provider plus its
    local CLI's presence and login. The pane renders its fix-it empty state
    from this."""
    return chat.readiness()


@app.get("/api/chat/history")
def chat_history(pr: int | None = None, cluster: int | None = None, issue: int | None = None,
                 advisory: str | None = None, alert_source: str | None = None,
                 alert: int | None = None,
                 chat_id: str | None = None) -> dict[str, object]:
    # `chat_id` selects an operator-named session's own thread (#343). Without
    # one, the subject selects its default thread.
    ctx_id = chat._thread_key(chat_id, pr, cluster, issue, advisory, alert_source, alert)
    return {"messages": chat.load_thread(ctx_id), "session": chat._session_id(ctx_id), "ctx": ctx_id}


@app.get("/api/chat")
async def chat_stream(q: str, pr: int | None = None, cluster: int | None = None,
                      issue: int | None = None,
                      advisory: str | None = None, alert_source: str | None = None,
                      alert: int | None = None,
                      file: str | None = None, line: int | None = None,
                      prs: str | None = None, prs_total: int | None = None,
                      chat_id: str | None = None) -> EventSourceResponse:
    # `prs` is a comma-separated PR-number list — the operator's currently
    # visible/filtered view (#355), e.g. from PR Explorer. `prs_total` carries
    # the true match count when the frontend truncated the list before sending.
    pr_list = [int(x) for x in prs.split(",") if x.strip().isdigit()] if prs else None
    async def gen() -> AsyncIterator[dict[str, str]]:
        async for ev in chat.stream_chat(q, pr=pr, cluster=cluster, issue=issue,
                                         advisory=advisory, alert_source=alert_source,
                                         alert=alert,
                                         file=file, line=line,
                                         prs=pr_list, prs_total=prs_total, chat_id=chat_id):
            yield ev
    return EventSourceResponse(gen())


@app.post("/api/chat/stop")
def chat_stop(pr: int | None = None, cluster: int | None = None, issue: int | None = None,
              advisory: str | None = None, alert_source: str | None = None,
              alert: int | None = None,
              chat_id: str | None = None) -> dict[str, bool]:
    """Interrupt the in-flight answer for this thread (#14)."""
    return {"stopped": chat.stop_chat(
        pr=pr, cluster=cluster, issue=issue, advisory=advisory,
        alert_source=alert_source, alert=alert, chat_id=chat_id)}


# ---------------------------------------------------------------------------
# Control-panel jobs (M5). Fixed allowlisted set; read-only upstream.
# ---------------------------------------------------------------------------
@app.get("/api/jobs/specs")
def job_specs():
    return {"specs": jobs.list_specs()}


@app.get("/api/jobs")
def jobs_list():
    return {"jobs": jobs.list_jobs()}


@app.get("/api/jobs/run/{kind}")
async def jobs_run(kind: str, cluster: int | None = None, pr: int | None = None,
                   count: int | None = None):
    try:
        job = jobs.start_job(kind, cluster, pr, count)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # The retained background task runs independently of this request's SSE
    # stream (#683) and completes if the connection closes.
    jobs.schedule_job(job)
    return EventSourceResponse(jobs.attach_job(job))


@app.get("/api/jobs/{job_id}/stream")
async def jobs_stream(job_id: int):
    """Reattach to a job already running (or finished) server-side — the full
    log replays immediately, then the connection follows it live (#683)."""
    job = jobs.JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"no such job: {job_id}")
    return EventSourceResponse(jobs.attach_job(job))


@app.get("/api/jobs/stream/group")
async def jobs_group_stream(job_id: list[int] = Query(...)) -> EventSourceResponse:
    """Follow several background jobs through one browser connection."""
    if not job_id:
        raise HTTPException(400, "at least one job id is required")
    if len(job_id) > bulk.CAP:
        raise HTTPException(400, f"job group exceeds cap of {bulk.CAP}")
    ids = list(dict.fromkeys(job_id))
    group = [jobs.JOBS.get(i) for i in ids]
    missing = [i for i, job in zip(ids, group, strict=True) if job is None]
    if missing:
        raise HTTPException(404, f"no such job(s): {', '.join(map(str, missing))}")
    return EventSourceResponse(jobs.attach_job_group([job for job in group if job is not None]))


# ---------------------------------------------------------------------------
# Upstream execution as the configured bot (M6). Dry-run unless a bot key is present.
# ---------------------------------------------------------------------------
@app.get("/api/identities")
def get_identities():
    return executor.identities()


@app.post("/api/identities/refresh")
def refresh_identities():
    """Re-probe whether this machine can mint a bot token — "retry
    live mode" in the app UI. live_possible() only probes once and caches
    the result for the process's lifetime, so a key file added, a GitHub App
    installed, or a network blip cleared after that first probe otherwise never
    takes effect without a full backend restart. caps.refresh() resets both
    that cache and the derived /api/capabilities cache together."""
    caps.refresh()
    return executor.identities()


@app.get("/api/capabilities")
def get_capabilities():
    c = caps.capabilities()
    return {"login": c.get("login"), "merge_upstream": c.get("merge_upstream", False),
            "reviewers": c.get("reviewers") or [], "store_schema": c.get("store_schema"),
            "write_block": c.get("write_block")}


@app.post("/api/merge/pr/{n}")
def merge_pr(n: int, method: str = "squash", dry_run: bool = True, reason: str | None = None):
    res = executor.merge_pr(n, method, dry_run=dry_run, reason=reason)
    training.capture(n, "MERGE", reason=reason, dry_run=dry_run, result=res)
    return res


@app.post("/api/execute/pr/{n}")
def execute_pr(n: int, payload: models.CloseAction = Body(...), dry_run: bool = True):
    # token minted per-call; None when no key → execute_pr forces dry-run.
    token = None if dry_run else executor.mint_bot_token()
    res = executor.execute_pr(n, payload, token=token, dry_run=dry_run)
    training.capture(n, res.get("action", "?"), reason=payload.reason, tags=payload.tags,
                     public_body=payload.comment, dry_run=dry_run, result=res)
    return res


@app.post("/api/execute/bulk")
async def execute_bulk(payload: models.BulkExecuteBody = Body(...)):
    """Apply one action to many PRs, streamed SSE. Body: {prs, action, comment?,
    comments?, canonical?, method?, reason?, tags?, dry_run}. `comments` maps a PR
    number to its own comment, so a bulk close can post each PR's suggested text
    instead of one shared `comment` (#182). Loops the per-PR executor; every
    per-PR gate is reused (see bulk.run_bulk)."""
    async def gen():
        async for ev in bulk.run_bulk(
            payload.prs, payload.action, comment=payload.comment, comments=payload.comments,
            canonical=payload.canonical, method=payload.method,
            reason=payload.reason, tags=payload.tags, reviewer=payload.reviewer,
            dry_run=payload.dry_run,
        ):
            yield ev
    return EventSourceResponse(gen())


@app.post("/api/execute/cluster")
async def execute_cluster(payload: models.ClusterExecuteBody = Body(...)):
    """Apply each PR's own disposition across a cluster, streamed SSE — the cluster
    page's mixed 'execute all'. One activity-shard commit for the whole group (see
    bulk.run_cluster); every per-PR gate is reused via the same executor functions."""
    async def gen():
        async for ev in bulk.run_cluster(payload.items, dry_run=payload.dry_run):
            yield ev
    return EventSourceResponse(gen())


@app.post("/api/reopen/pr/{n}")
def reopen_pr(n: int, dry_run: bool = True):
    token = None if dry_run else executor.mint_bot_token()
    res = executor.reopen_pr(n, token=token, dry_run=dry_run)
    training.capture(n, "REOPEN", dry_run=dry_run, result=res)
    return res


# --- GitHub Issues, folded into the app (#192) ---------------------------

@app.get("/api/issues")
def list_issues():
    """Open GitHub issues enriched with their dedup cluster, pain, repro grade,
    and the PRs that may address them (issue_triage projection). While the PR
    snapshot is still cold-loading, linked-PR chips and author stats come back
    unhydrated and pr_states_loading is true."""
    rows, pr_states_loading = issues_mod.list_issues()
    return {"items": rows, "pr_states_loading": pr_states_loading}


@app.post("/api/issues/query")
def issues_query(payload: dict = Body(default_factory=dict)):
    """Paginated Issue-table endpoint. Body: {q?, sort?, direction?, disposition?,
    state?, author?, pain?, repro_grade?, subsystem?, dups?, linked_prs?, labels?,
    offset?, limit?}; disposition "none" selects unanalyzed issues, state
    "open"/"closed" filters by lifecycle ("all"/absent returns both). See
    issues.query_issues for the per-field filter semantics."""
    return issues_mod.query_issues(
        q=payload.get("q") or "",
        sort=payload.get("sort"), direction=payload.get("direction"),
        disposition=payload.get("disposition"), state=payload.get("state"),
        author=payload.get("author") or None,
        pain=payload.get("pain"),
        repro_grade=payload.get("repro_grade"),
        subsystem=payload.get("subsystem"),
        dups=payload.get("dups"),
        linked_prs=payload.get("linked_prs"),
        labels=payload.get("labels") or None,
        offset=int(payload.get("offset", 0)), limit=min(int(payload.get("limit", 50)), 500),
    )


@app.get("/api/issues/duplicates")
def issue_duplicates():
    """The curated close-as-dup worklist, grouped by canonical issue, most painful
    first — with each cluster's candidate PRs cross-linked."""
    return {"groups": issues_mod.duplicate_groups()}


@app.get("/api/issues/already-fixed")
def issues_already_fixed():
    """The already-fixed worklist: tier-1 close-fixed issues with a live-merged
    fixer (ready to close), and tier-2 likely-fixed issues for human review."""
    return issues_mod.already_fixed()


# {n} matches any path segment, so this stays registered after every literal
# /api/issues/* route.
@app.get("/api/issues/{n}")
def issue_detail(n: int):
    """One issue's detail row — meta, analysis (disposition/rationale/asks),
    repro, cluster context, and linked PRs — for the issue flyout."""
    d = issues_mod.get_issue(n)
    if d is None:
        raise HTTPException(404, f"issue {n} not in store")
    return d


@app.post("/api/execute/issue/{n}/close-dup")
def close_issue_dup(n: int, payload: models.IssueCloseDupBody = Body(default_factory=models.IssueCloseDupBody),
                    dry_run: bool = True):
    """Close issue #n as a duplicate of `canonical`, as the configured bot (gated +
    logged). Body: {canonical?, comment?}."""
    token = None if dry_run else executor.mint_bot_token()
    return executor.close_issue(n, payload, token=token, dry_run=dry_run)


@app.post("/api/execute/issue/{n}/close-fixed")
def close_issue_fixed(n: int, payload: models.IssueCloseFixedBody, dry_run: bool = True):
    """Close issue #n as fixed by merged PR `fixed_by`, as the configured bot (gated on a
    live merged-state re-verify + logged). Body: {fixed_by, comment?}."""
    token = None if dry_run else executor.mint_bot_token()
    return executor.close_issue_fixed(n, payload, token=token, dry_run=dry_run)


@app.post("/api/execute/issue/{n}/close")
def close_issue(n: int, payload: models.IssueCloseBody = Body(default_factory=models.IssueCloseBody),
                dry_run: bool = True):
    """Close issue #n as directed by the operator, as the configured bot (gated on
    issues.close_gate + logged). Body: {disposition, comment, fixed_by?, canonical?}."""
    token = None if dry_run else executor.mint_bot_token()
    return executor.close_issue_with_comment(n, payload, token=token, dry_run=dry_run)


@app.post("/api/execute/issue/{n}/comment")
def comment_issue(n: int, payload: models.IssueCommentBody = Body(default_factory=models.IssueCommentBody),
                  dry_run: bool = True):
    """Post a comment on issue #n as the configured bot, without closing (gated +
    logged). Body: {comment}."""
    token = None if dry_run else executor.mint_bot_token()
    return executor.comment_issue(n, payload, token=token, dry_run=dry_run)


@app.post("/api/reopen/issue/{n}")
def reopen_issue(n: int, dry_run: bool = True):
    """Undo an issue close: reopen #n and delete the bot's closing comment(s), as
    the configured bot (gated + logged). The inverse of the issue-close endpoints."""
    token = None if dry_run else executor.mint_bot_token()
    return executor.reopen_issue(n, token=token, dry_run=dry_run)


# --- GitHub security alerts (code scanning / Dependabot / secret scanning) ---

@app.get("/api/alerts")
def list_alerts():
    """Every ingested repository-security alert with its normalized state,
    severity, fix-scan verdict, and candidate PR/issue links. While the PR
    snapshot is still cold-loading, the link chips carry their recorded states
    and pr_states_loading is true."""
    rows, pr_states_loading = alerts_mod.list_alerts()
    return {"items": rows, "pr_states_loading": pr_states_loading}


@app.post("/api/alerts/query")
def alerts_query(payload: dict = Body(default_factory=dict)):
    """Paginated Alerts-table endpoint. Body: {q?, sort?, direction?, source?,
    state?, severity?, verdict?, offset?, limit?}; verdict "none" selects
    unscanned alerts. See alerts.query_alerts for per-field semantics."""
    return alerts_mod.query_alerts(
        q=payload.get("q") or "",
        sort=payload.get("sort"), direction=payload.get("direction"),
        source=payload.get("source"), state=payload.get("state"),
        severity=payload.get("severity"), verdict=payload.get("verdict"),
        offset=int(payload.get("offset", 0)),
        limit=min(int(payload.get("limit", 50)), 500),
    )


@app.get("/api/alerts/caps")
def alerts_caps():
    """Per-source read availability, probed as the bot: false means the feature
    is disabled on the repository or the App lacks the permission."""
    sources = alerts_mod.sources_available()
    return {"available": any(sources.values()), "sources": sources}


@app.post("/api/alerts/caps/refresh")
def alerts_caps_refresh():
    alerts_mod.refresh_sources()
    sources = alerts_mod.sources_available()
    return {"available": any(sources.values()), "sources": sources}


# --- GitHub repository security advisories (read-only) ---

@app.get("/api/advisories")
def list_advisories():
    """Every ingested repository security advisory with its state, severity,
    fix-scan verdict, and candidate PR links."""
    rows, pr_states_loading = advisories_mod.list_advisories()
    return {"items": rows, "pr_states_loading": pr_states_loading}


@app.post("/api/advisories/query")
def advisories_query(payload: dict = Body(default_factory=dict)):
    """Paginated Advisories-table endpoint. Body: {q?, sort?, direction?,
    state?, verdict?, offset?, limit?}; verdict "none" selects unscanned."""
    return advisories_mod.query_advisories(
        q=payload.get("q") or "",
        sort=payload.get("sort"), direction=payload.get("direction"),
        state=payload.get("state"), verdict=payload.get("verdict"),
        offset=int(payload.get("offset", 0)),
        limit=min(int(payload.get("limit", 50)), 500),
    )


@app.get("/api/advisories/{ghsa}")
def advisory_detail(ghsa: str):
    """One advisory's detail — the row plus its description, CWE ids, version
    range, and the full fix-scan section."""
    d = advisories_mod.get_advisory(ghsa)
    if d is None:
        raise HTTPException(404, f"advisory {ghsa} not in store")
    return d


# {source}/{n} matches any two path segments, so this stays registered after
# every literal /api/alerts/* route.
@app.get("/api/alerts/{source}/{n}")
def alert_detail(source: str, n: int):
    """One alert's detail — full meta, fix-scan evidence, links, and the valid
    dismissal reasons for its source — for the alert detail panel."""
    d = alerts_mod.get_alert(source, n)
    if d is None:
        raise HTTPException(404, f"alert {source}#{n} not in store")
    return d


@app.post("/api/execute/alert/{source}/{n}/dismiss")
def dismiss_alert(source: str, n: int, payload: models.AlertDismissBody = Body(...),
                  dry_run: bool = True):
    """Dismiss/resolve alert {source}#{n} upstream as the configured bot (gated
    by alert_gates.dismiss_eligibility + logged). Body: {reason, comment?}."""
    token = None if dry_run else executor.mint_bot_token()
    return executor.dismiss_alert(source, n, payload.reason, payload.comment or "",
                                  token=token, dry_run=dry_run)


@app.post("/api/review/pr/{n}")
def submit_review(n: int, payload: models.ReviewBody = Body(...), dry_run: bool = True):
    token = None if dry_run else executor.mint_bot_token()
    res = executor.submit_review(n, payload.event, payload.body, token=token, dry_run=dry_run,
                                 override_stale=payload.override_stale)
    training.capture(n, f"REVIEW:{payload.event}", reason=payload.reason, tags=payload.tags,
                     public_body=payload.body, dry_run=dry_run, result=res)
    return res


@app.post("/api/comment/pr/{n}")
def comment_line(n: int, payload: models.LineCommentBody = Body(...), dry_run: bool = True):
    token = None if dry_run else executor.mint_bot_token()
    res = executor.comment_line(n, payload.file, payload.line, payload.body,
                                token=token, dry_run=dry_run,
                                override_stale=payload.override_stale)
    training.capture(n, "LINE_COMMENT", reason=payload.reason, public_body=payload.body,
                     dry_run=dry_run, result=res)
    return res


@app.post("/api/reviews/{reviewer}/retrigger/pr/{n}")
def retrigger_review(reviewer: str, n: int, dry_run: bool = True):
    """Post the named reviewer's mention on PR #n as the configured bot to
    re-trigger its review — no new commit needed. Gated + logged like every bot
    write. Once the post lands, a backend task waits for the reviewer's fresh
    verdict and targeted-ingests it into the shared snapshot; no UI session is
    required. 404 for an unknown reviewer id."""
    known = next((r for r in reviewers.REVIEWERS.values() if r.id == reviewer), None)
    if known is None:
        raise HTTPException(status_code=404, detail="unknown reviewer")
    token = None if dry_run else executor.mint_bot_token()
    baseline = review_refresh.capture(n, known.id) if token is not None else None
    result = executor.retrigger_review(n, known.id, token=token, dry_run=dry_run)
    if result.get("status") == "executed" and baseline is not None:
        review_refresh.schedule(n, known.id, baseline)
    return result


@app.get("/api/pipeline/status")
def pipeline_status_get():
    """Pipeline phase timing + PR coverage stats for the Control Panel."""
    return pipeline_status.status()


@app.get("/api/actions/suggested")
def actions_suggested(view: str):
    """Per-tab suggested pipeline actions — Control-tab jobs worth running now,
    surfaced on the view whose data they feed."""
    try:
        return {"items": suggested_actions.suggestions(view)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/training/stats")
def training_stats():
    return training.stats()


@app.get("/api/activity")
def get_activity(limit: int = Query(200, le=1000)):
    return {"items": activity.recent(limit)}


@app.get("/api/tables")
def get_tables():
    """Overview grid: every store table with its row count + a short preview."""
    return {"tables": tables.overview()}


@app.get("/api/tables/{name}")
def get_table(request: Request, name: str,
              limit: int = Query(50, le=200), offset: int = Query(0, ge=0),
              order: str | None = None, dir: str = "asc"):
    """One table's rows, paginated and optionally sorted/filtered. Per-column
    filters arrive as `f_<column>` query params (real SQL columns only)."""
    filters = {k[2:]: v for k, v in request.query_params.items() if k.startswith("f_")}
    try:
        return tables.rows(name, limit=limit, offset=offset, order=order,
                           dir=dir, filters=filters)
    except tables.UnknownTable:
        raise HTTPException(404, f"unknown table: {name}")
    except tables.UnknownColumn as e:
        raise HTTPException(400, f"unknown column: {e}")


@app.post("/api/activity/sync")
def activity_sync(limit: int = 200):
    """Return the current activity feed. The ``synced`` key is kept for frontend
    compatibility; activity reads directly from the DB so no sync is needed."""
    return {"synced": False, "items": activity.recent(limit)}


@app.get("/api/activity/summary")
def activity_summary(group_by: str = "day", since: str | None = None,
                     until: str | None = None, include_dry_run: bool = False,
                     identity: str | None = None, operator: str | None = None,
                     pr_author: str | None = None):
    """Aggregate triage metrics for the dashboard (#42): totals + buckets
    grouped by day / week / cluster / identity / operator. Live actions only by
    default; ``operator`` filters to one teammate's work and ``pr_author`` to
    actions on one contributor's PRs."""
    prs = data.prs()
    scope = activity.ActivityScope.from_selection(
        prs, pr_author=pr_author, operator=operator)
    return activity.summarize(scope.events(activity.all_events()), group_by=group_by,
                              include_dry_run=include_dry_run, since=since,
                              until=until, identity=identity)


@app.get("/api/activity/progress")
def activity_progress(pr_author: str | None = None, operator: str | None = None):
    """Backlog progress (#27): landed live actions vs. the open-PR universe, so
    the operator sees how far through the ~3,000-PR backlog they are."""
    prs = data.prs()
    events = activity.all_events()
    scope = activity.ActivityScope.from_selection(
        prs, pr_author=pr_author, operator=operator)
    return activity.progress(scope.prs(prs), scope.events(events))


@app.get("/api/activity/issue-progress")
def activity_issue_progress(operator: str | None = None):
    """Landed issue closes vs. the open-issue universe."""
    scope = activity.ActivityScope(operator=operator)
    return activity.issue_progress(
        issue_data.issues(), scope.events(activity.all_events()))


@app.get("/api/activity/firehose")
def activity_firehose(days: int = Query(30, le=400), all_time: bool = False,
                      pr_author: str | None = None, operator: str | None = None):
    """Firehose comparison (#238): new PRs/issues opened on the upstream repo
    vs our triage actions per day, plus any PRs we closed that were subsequently
    reopened by their authors. Pass ``all_time=true`` to span from the earliest
    PR in the store to today regardless of ``days``. Pass ``pr_author`` to
    restrict PR stats to PRs by a specific GitHub login, or ``operator`` to
    restrict action stats to one teammate."""
    from datetime import datetime as _dt
    prs = data.prs()
    scope = activity.ActivityScope.from_selection(
        prs, pr_author=pr_author, operator=operator)
    scoped_prs = scope.prs(prs)
    all_issues = [] if pr_author else issues_mod.list_issues()[0]
    events = scope.events(activity.all_events())
    start_date = None
    if all_time:
        dates = [
            _dt.fromisoformat(pr.created_at.replace("Z", "+00:00")).date()
            for pr in scoped_prs.values()
            if pr.created_at
        ]
        if dates:
            start_date = min(dates)
    stats = activity.firehose_stats(scoped_prs, all_issues, days, events, start_date=start_date)
    stats["reopened_after_close"] = activity.reopened_after_close(scoped_prs, events)
    stats["iss_action_counts"] = activity.issue_action_counts(events)
    return stats


@app.get("/api/activity/pr-authors")
def pr_authors():
    """Unique PR authors in the store, sorted by PR count descending — for the
    author-filter picker in the velocity chart."""
    prs = data.prs()
    counts: dict[str, int] = {}
    for rec in prs.values():
        if rec.author:
            counts[rec.author] = counts.get(rec.author, 0) + 1
    return {
        "authors": [
            {"login": k, "pr_count": v}
            for k, v in sorted(counts.items(), key=lambda x: -x[1])
        ]
    }


@app.get("/api/activity/people")
def activity_people():
    """Unified list of roles for the person-filter picker: app operators
    (from the activity log) followed by PR authors (from the store).

    Each entry exposes a display name, a GitHub login (inferred from the
    operator email prefix for app operators), whether they are an app
    operator, and their PR count. Operators appear first; PR authors follow
    sorted by PR count descending. A login in both groups gets both roles."""
    events = activity.all_events()
    op_entries = activity.operators_with_login(events)
    prs = data.prs()
    pr_counts: dict[str, int] = {}
    for rec in prs.values():
        if rec.author:
            pr_counts[rec.author] = pr_counts.get(rec.author, 0) + 1

    result: list[dict] = []
    for entry in op_entries:
        result.append({**entry, "is_operator": True, "pr_count": pr_counts.get(entry["login"], 0)})

    for login, count in sorted(pr_counts.items(), key=lambda x: -x[1]):
        result.append({"display": login, "login": login, "is_operator": False, "pr_count": count})

    return {"people": result}


@app.get("/api/action-items")
def action_items(status: str | None = None, kind: str | None = None):
    items = data.action_items()
    prs = data.prs()
    for it in items:  # enrich with the source PR's title/url/author for the worklist
        pr_num = it.get("pr")
        rec = prs.get(pr_num) if isinstance(pr_num, int) else None
        it["pr_title"] = rec.title if rec else None
        it["pr_url"] = rec.url if rec else None
        it["pr_author"] = rec.author if rec else None
        it["pr_summary"] = (rec.section("summary") or {}).get("one_liner") if rec else None
    if status:
        items = [i for i in items if i.get("status") == status]
    if kind:
        items = [i for i in items if i.get("kind") == kind]
    counts: dict[str, int] = {}
    for i in data.action_items():
        counts[i.get("status", "open")] = counts.get(i.get("status", "open"), 0) + 1
    return {"items": items, "counts": counts}


@app.post("/api/action-items/{item_id:path}/status")
def set_action_item_status(item_id: str, payload: dict = Body(...)):
    try:
        return data.set_action_item_status(item_id, payload.get("status", "open"))
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# Serve the built SPA (frontend/dist) if it exists.
# ---------------------------------------------------------------------------
DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # serve real files, else fall back to index.html for client routing
        candidate = DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(DIST / "index.html")
