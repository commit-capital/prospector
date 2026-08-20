"""Run the full VERIFY sequence on ONE pull request, headlessly.

This is the per-PR runner the app's verification queue drives: the blind
adequacy, author, and post-run judge agents execute as locked-down headless
`claude -p` subprocesses (pipeline/headless_agent.py), the sandbox phases run
via verify_driver.verify_pr, and gates.verify_outcome computes the outcome —
the one policy.

Selection is the operator's: a queued PR runs whatever its disposition or
analysis currency. The safety floor stays hard — a malicious threat verdict, a
deps-touching diff, or an unreadable diff refuses the run — and the outcome
policy is untouched. A request whose source is "auto" (the idle hunter's own
pick) carries one more floor check: it refuses without a current GREEN
security verdict, re-checked at run time rather than trusted from queue time.
Evidence stays in memory: the sandbox phases hand their results straight to
the commit path, with no intermediate files on disk.

Progress goes to stdout one line per step; the app's verification worker
captures it. The PR's `verify_request` section records each transition
(running → done / error) so every app shows the run's state live.

  uv run python pipeline/verify_pr.py --pr N [--store DIR] [--from-queue]

`--from-queue` is the worker's flag: it runs only a PR whose request is
currently `queued` or `waiting-for-base`, so a cancel that lands before pickup
is honored.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import socket
import sys
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from pipeline import diff_cache
from pipeline import gates
from pipeline import headless_agent
from pipeline import verify_driver
from pipeline.settings import REPO_ROOT
from pipeline.store import Store
from pipeline.storekit import now as _now
from pipeline.verify_driver import AUTHOR_PROMPT, BLIND_PROMPT, JUDGE_PROMPT
from pipeline.wire import AuthorItem, BlindItem, JudgeItem

if TYPE_CHECKING:
    from pipeline.model import Pr

# The blind, author, and judge prompts are the canonical copies owned by
# verify_driver.py, consumed here and filled per call — never restated. This
# path adds only the per-call placeholder fills and the fenced-block output tails.

# Output-delivery tails for the headless channel: the field set each agent must
# return, delivered as a fenced JSON block. None carries an outcome field: the
# outcome is gates.verify_outcome's alone.
BLIND_FENCED_TAIL = """

Return ONLY a JSON object (no prose): {"has_test": <bool>, "test_cmd": "<cmd>"|null, \
"faithful": <bool>, "confidence": "high|medium|low", "claimed_symptom": "..."|null, \
"expected_red_signature": "..."|null, "repro_command": "<cmd>"|null, \
"expected_repro_signature": "..."|null, "from_linked_issue": <bool>, \
"requires_live_agent": <bool>, "reasoning": "..."}. Output it as a ```json fenced block."""

AUTHOR_FENCED_TAIL = """

Return ONLY a JSON object (no prose): {"can_author": <bool>, \
"files": [{"path": "<repo-relative test file>", "contents": "<full file body>"}], \
"expected_red_signature": "..."|null, "confidence": "high|medium|low", \
"reasoning": "..."}. Output it as a ```json fenced block."""

JUDGE_FENCED_TAIL = """

Return ONLY a JSON object (no prose): {"red_reason_match": {"matches": <bool>, \
"confidence": "high|medium|low", "reasoning": "..."}, "repro_reason_match": \
{"applicable": <bool>, "matches": <bool>|null, "confidence": "high|medium|low", \
"reasoning": "..."}, "findings": [{"title": "...", "detail": "...", \
"confidence": "high|medium|low"}]}. Output it as a ```json fenced block."""

# Longest traceback / captured-output tail stored on an errored request.
LOG_TAIL_CHARS = 4000

# How long a queued request may wait for a usable pinned base. A no-base
# preflight failure parks the request as `waiting-for-base` (the verification
# worker retries it on later drain ticks — a prepare-base run in flight lands
# within this window); past this many hours since queueing, the failure is
# terminal.
WAITING_FOR_BASE_MAX_HOURS = 6.0

# Canary self-test results, cached per base image for the worker's lifetime: a
# healthy harness stays healthy until the base is re-pinned (a new image tag), so
# the 3-container self-test runs once per boot, not per PR.
_canary_cache: dict[str, list[str]] = {}


def _harness_problems(image: str, base: str, tier: int) -> list[str]:
    """The canary self-test's problems for a base image — empty when the harness
    reproduces a known bug, confirms its fix, and rejects a non-fix patch. Cached
    per image, so the self-test runs once per worker boot per pinned base."""
    if image not in _canary_cache:
        _canary_cache[image] = verify_driver.run_canaries(image, base, tier)
    return _canary_cache[image]


def _say(msg: str) -> None:
    print(msg, flush=True)


class _Request:
    """This run's verify_request transitions on the PR record. Carries queued_at
    (from the app's queue write), started_at, source, and the prior attempt
    count across transitions, since record_verify_request replaces the whole
    section."""

    def __init__(self, store: Store, n: int, queued_at: str | None,
                 source: str | None = None, started_at: str | None = None,
                 attempts: int = 0):
        self._store = store
        self._n = n
        self._queued_at = queued_at
        self._source = source
        self._started_at: str | None = started_at
        self._attempts = attempts
        self._host = socket.gethostname()

    @property
    def source(self) -> str | None:
        return self._source

    @property
    def attempt(self) -> int:
        """This run's 1-based ordinal among the request's attempts."""
        return self._attempts + 1

    def running(self, step: str) -> None:
        if self._started_at is None:
            self._started_at = _now()
        self._store.edit_pr(self._n).record_verify_request(
            "running", queued_at=self._queued_at, started_at=self._started_at,
            step=step, source=self._source, host=self._host)

    def done(self) -> None:
        self._store.edit_pr(self._n).record_verify_request(
            "done", queued_at=self._queued_at, started_at=self._started_at,
            finished_at=_now(), source=self._source, host=self._host)

    def error(self, kind: str, message: str, log_tail: str | None = None) -> None:
        self._store.edit_pr(self._n).record_verify_request(
            "error", queued_at=self._queued_at, started_at=self._started_at,
            finished_at=_now(), error_kind=kind, error=message,
            log_tail=log_tail[-LOG_TAIL_CHARS:] if log_tail else None,
            source=self._source, host=self._host)

    def waiting_for_base(self, message: str) -> None:
        self._store.edit_pr(self._n).record_verify_request(
            "waiting-for-base", queued_at=self._queued_at,
            started_at=self._started_at, error_kind="no-base", error=message, source=self._source)

    def retrying(self, kind: str, message: str, log_tail: str | None = None) -> None:
        """Park this run's transient failure back in the queue: status `queued`
        with the failure named and the attempt count, so the worker re-picks it
        after a rest and _fail_transient can give up at the cap."""
        self._store.edit_pr(self._n).record_verify_request(
            "queued", queued_at=self._queued_at, error_kind=kind, error=message,
            log_tail=log_tail[-LOG_TAIL_CHARS:] if log_tail else None,
            attempts=self._attempts + 1, source=self._source, host=self._host)

    def hours_since_queued(self) -> float | None:
        """Hours since the app queued this request, or None when it did not
        come from the queue (or the stamp is unparseable)."""
        if not self._queued_at:
            return None
        try:
            queued = datetime.fromisoformat(self._queued_at)
        except ValueError:
            return None
        return (datetime.now(timezone.utc) - queued).total_seconds() / 3600.0


def _fail(req: _Request, kind: str, message: str, log_tail: str | None = None) -> int:
    _say(f"✗ {message}")
    req.error(kind, message, log_tail)
    return 1


# How many runs a transiently failing request gets before its failure is
# recorded as a terminal error for the operator.
TRANSIENT_MAX_ATTEMPTS = 3


def _fail_transient(req: _Request, kind: str, message: str,
                    log_tail: str | None = None) -> int:
    """Handle a transient infrastructure failure — an upstream fetch, a headless
    agent, the sandbox. A queued request under the attempt cap re-queues itself
    for the worker to re-pick after a rest; a direct CLI run (no queued_at) has
    no worker to re-pick it, and an exhausted cap means the failure is not
    passing on its own — both error terminally."""
    if req.hours_since_queued() is None:
        return _fail(req, kind, message, log_tail)
    if req.attempt >= TRANSIENT_MAX_ATTEMPTS:
        return _fail(req, kind,
                     f"{message} — gave up after {req.attempt} attempts", log_tail)
    _say(f"✗ {message} — re-queued for retry (attempt {req.attempt} of "
         f"{TRANSIENT_MAX_ATTEMPTS})")
    req.retrying(kind, message, log_tail)
    return 0


def _no_base(req: _Request, message: str) -> int:
    """Handle a no-base preflight failure. A queued request within its wait
    budget parks as `waiting-for-base` for the worker to retry; a direct CLI
    run (no queued_at) or an exhausted budget errors terminally. Either way,
    nothing runs without a captured baseline."""
    waited = req.hours_since_queued()
    if waited is None:
        return _fail(req, "no-base", message)
    if waited > WAITING_FOR_BASE_MAX_HOURS:
        return _fail(req, "no-base",
                     f"{message} — gave up after waiting {waited:.1f}h for a base")
    _say(f"… {message} — waiting for a base (queued {waited:.1f}h ago; the worker "
         f"retries until {WAITING_FOR_BASE_MAX_HOURS:g}h)")
    req.waiting_for_base(message)
    return 0


def _no_daemon(req: _Request, message: str) -> int:
    """Handle an unreachable Docker daemon. A queued request parks as
    `waiting-for-base` for however long the outage lasts: the wait budget bounds
    a base the operator must prepare, and no re-queue makes a stopped daemon
    answer. A direct CLI run has no worker to re-pick it and errors."""
    if req.hours_since_queued() is None:
        return _fail(req, "sandbox-error", message)
    _say(f"… {message} — waiting for the daemon to come back")
    req.waiting_for_base(message)
    return 0


def _daemon_available() -> bool:
    """True when the local Docker daemon answers this machine."""
    return verify_driver.daemon_available()


def _image_exists(image: str) -> bool:
    """True when the pinned base image is present in the local Docker daemon. A
    missing image (e.g. after a Colima wipe) is a no-base condition — re-run
    prepare-base — not a sandbox error."""
    return verify_driver.image_exists(image)


def _call_agent_json(prompt: str, step: str) -> tuple[dict | None, str | None]:
    """Run one headless agent (read-only tools, no gh — both verify judgments
    are functions of the diff, the claimed defect, and the pinned tree alone).
    Returns (data, failure): the parsed JSON and None, or None and the failure
    detail when the run or extraction failed."""
    try:
        text = headless_agent.run_agent(prompt, allow_gh=False, cwd=str(REPO_ROOT),
                                        on_event=headless_agent.print_progress)
        return headless_agent.extract_json(text), None
    except (RuntimeError, ValueError) as e:
        _say(f"    ! {step} failed: {e}")
        return None, f"{step}: {e}"


_T = TypeVar("_T")


def _retry_once(call: Callable[[], tuple[_T | None, str | None]],
                step: str) -> tuple[_T | None, str | None]:
    """One retry for a headless verdict agent: a transient API failure or a
    one-off malformed answer passes on the second attempt. Returns (item,
    failure): a usable answer and None, or None and every attempt's failure
    detail joined — the errored request's log_tail."""
    item, failure = call()
    if item is not None:
        return item, None
    _say(f"    ! {step} answer unusable — retrying once")
    item, failure2 = call()
    if item is not None:
        return item, None
    return None, "; ".join(f for f in (failure, failure2) if f) or None


def _blind_verdict(rec: Pr, clone_dir: str,
                   addendum: str = "") -> tuple[BlindItem | None, str | None]:
    """Signal 1 for this PR via a headless agent: the canonical BLIND_PROMPT,
    judged from the diff, the linked issues' text, and the pinned base clone —
    before any sandbox boots. `addendum` is appended before the output tail
    (the repro-rejection retry's feedback). Returns (item, failure): the parsed
    verdict and None, or None and the failure detail on an unusable answer."""
    head = rec.head_sha or ""
    linked_ns = {e.get("issue") for e in rec.linked_issues}
    texts = verify_driver.issue_texts({n for n in linked_ns if isinstance(n, int)})
    linked = [{**e, **texts.get(e.get("issue"), {})} for e in rec.linked_issues]
    prompt = headless_agent.fill(BLIND_PROMPT, {
        "__PR__": rec.n, "__TITLE__": rec.title or "",
        "__DIFF_PATH__": str(diff_cache.DIFFS / f"{head}.diff"),
        "__BASE_CLONE__": clone_dir,
        "__LINKED_ISSUES__": json.dumps(linked, indent=1),
    }) + addendum + BLIND_FENCED_TAIL
    data, failure = _call_agent_json(prompt, "blind adequacy")
    if data is None:
        return None, failure
    if not isinstance(data.get("faithful"), bool):
        return None, f"blind adequacy: unusable answer: {json.dumps(data)[:800]}"
    return BlindItem.from_dict({"pr": rec.n, "head_sha": head, **data}), None


def _repro_rejection_addendum(token: str, repro_cmd: str) -> str:
    """The rejection feedback appended to the full blind prompt on the one
    retry: it names the offending token and restates the invariant the first
    answer violated."""
    return f"""

YOUR PREVIOUS repro_command WAS REJECTED — nothing has run. You answered:

  {repro_cmd}

Its token {token!r} names a test this PR's own diff introduces. The repro executes against the pinned base WITHOUT the diff applied, so that target does not exist there and the command's exit code would carry no signal. Author a repro through a surface that exists on the base — an existing test, a script driving the buggy function directly, or the linked issue's repro steps — or return repro_command: null with your justification in reasoning. Answer the full report again."""


def _vet_blind_repro(rec: Pr, item: BlindItem, clone_dir: str) -> BlindItem:
    """The pre-commit repro vetting: a repro_command targeting a test the PR
    itself introduces (gates.repro_targets_pr_test over the cached diff) is
    rejected and the blind agent re-run once with the rejection named. A retry
    that answers a valid repro — or declines with null — is committed as
    given; a retry that is still vacuous, or an agent error, commits with the
    repro fields nulled and `repro_rejected` recording why. Runs before
    commit_blind, so blindness is preserved: nothing has booted, and the
    committed verdict is the vetted one."""
    if item.repro_command is None:
        return item
    diff_text = verify_driver.cached_diff_text(rec)
    token = gates.repro_targets_pr_test(item.repro_command, diff_text)
    if token is None:
        return item
    _say(f"  ✗ repro rejected pre-run: {token!r} names a test this PR's diff "
         f"introduces — re-running the blind agent once with the rejection")
    retry, _ = _blind_verdict(
        rec, clone_dir,
        addendum=_repro_rejection_addendum(token, item.repro_command))
    if retry is None:
        return dataclasses.replace(
            item, repro_command=None, expected_repro_signature=None,
            repro_rejected=f"repro_command targets a test the PR itself "
                           f"introduces ({token!r}); the blind agent failed on "
                           f"the one retry, so the repro fields are nulled")
    if retry.repro_command is None:
        return retry
    token2 = gates.repro_targets_pr_test(retry.repro_command, diff_text)
    if token2 is None:
        return retry
    return dataclasses.replace(
        retry, repro_command=None, expected_repro_signature=None,
        repro_rejected=f"repro_command targets a test the PR itself introduces "
                       f"({token2!r}) even after one rejection retry, so the "
                       f"repro fields are nulled")


def _author_verdict(rec: Pr, clone_dir: str) -> AuthorItem | None:
    """The AUTHOR pass for a PR that ships no test: a headless agent authors a
    reproduction test from the diff, the linked issues' text, and the pinned
    base clone. None on an unusable answer."""
    head = rec.head_sha or ""
    linked_ns = {e.get("issue") for e in rec.linked_issues}
    texts = verify_driver.issue_texts({n for n in linked_ns if isinstance(n, int)})
    linked = [{**e, **texts.get(e.get("issue"), {})} for e in rec.linked_issues]
    prompt = headless_agent.fill(AUTHOR_PROMPT, {
        "__PR__": rec.n, "__TITLE__": rec.title or "",
        "__DIFF_PATH__": str(diff_cache.DIFFS / f"{head}.diff"),
        "__BASE_CLONE__": clone_dir,
        "__LINKED_ISSUES__": json.dumps(linked, indent=1),
    }) + AUTHOR_FENCED_TAIL
    data, _ = _call_agent_json(prompt, "author test")
    if data is None or not isinstance(data.get("can_author"), bool):
        return None
    return AuthorItem.from_dict({"pr": rec.n, **data})


def _judge_verdict(n: int, expected_red: str | None, expected_repro: str | None,
                   red_green: dict,
                   independent_repro: dict) -> tuple[JudgeItem | None, str | None]:
    """Signals 3 and 4 for this PR via a headless agent: the canonical judge
    prompt with the evidence inlined — the pre-committed predictions vs. what
    the sandbox actually captured. Returns (item, failure): the parsed rating
    and None, or None and the failure detail on an unusable answer."""
    evidence = json.dumps({"red_green": red_green,
                           "independent_repro": independent_repro}, indent=1)
    prompt = headless_agent.fill(JUDGE_PROMPT, {
        "__PR__": n,
        "__EXPECTED_RED__": expected_red or "null",
        "__EXPECTED_REPRO__": expected_repro or "null",
        "__EVIDENCE__": evidence,
    }) + JUDGE_FENCED_TAIL
    data, failure = _call_agent_json(prompt, "post-run judge")
    if data is None:
        return None, failure
    if not isinstance(data.get("red_reason_match"), dict):
        return None, f"post-run judge: unusable answer: {json.dumps(data)[:800]}"
    return JudgeItem.from_dict({"pr": n, **data}), None


def _no_run_evidence(rec: Pr, base: str, tier: int) -> dict:
    """The evidence shape for a PR whose blind verdict settles the outcome with
    no sandbox time (requires_live_agent): host facts all unrecorded, in the
    exact shape verify_driver.verify_pr returns, so the commit path reads one
    shape. gates.verify_outcome resolves it from the blind verdict alone."""
    blind = rec.verify_signals.get("blind_adequacy") or {}
    return {
        "pr": rec.n, "head_sha": rec.head_sha or "", "base_sha": base, "tier": tier,
        "blind_adequacy": blind,
        "red_green": {"apply_exit": None, "red_exit": None, "green_exit": None,
                      "red_output_tail": "", "green_output_tail": ""},
        "independent_repro": {"ran": False, "exit_code": None,
                              "from_linked_issue": bool(blind.get("from_linked_issue")),
                              "output_tail": ""},
        "regress": {"ran": False, "skipped_reason": "requires-live-agent"},
    }


def _run_inner(store: Store, rec: Pr, req: _Request) -> int:
    n = rec.n
    head = rec.head_sha or ""
    _say(f"▶ Sandbox verification of PR #{n}: {rec.title or ''}")
    _say(f"  head {head[:7]}")
    req.running("preflight")

    # Safety floor — hard regardless of who queued the PR.
    if rec.state != "open":
        return _fail(req, "refused-safety", f"PR #{n} is {rec.state}, not open")
    if rec.threat_verdict == "malicious":
        return _fail(req, "refused-safety",
                     "threat verdict is malicious — the sandbox never runs flagged code")
    if req.source == "auto" and not gates.security_cleared(rec):
        return _fail(req, "refused-safety",
                     "auto-queued run requires a current GREEN security verdict — "
                     "the sandbox never executes code the idle hunter selected "
                     "itself without an adversarial review clearing it")

    # This machine's pinned base: sha + tier + captured baseline + clone + built
    # image. All five come from one prepare-base run here; missing any of them is
    # a no-base. Each verification machine carries its own pin, so what another
    # machine has prepared says nothing about what this one can boot.
    try:
        base = verify_driver.pinned_base(store)
        tier = verify_driver.pinned_tier(store)
        baseline = verify_driver.pinned_baseline(store)
    except RuntimeError as e:
        return _no_base(req, str(e))
    image = verify_driver.base_image_tag(base, tier)
    clone = verify_driver.base_clone_dir(base)
    if not clone.is_dir():
        return _no_base(req,
                        f"pinned base clone missing at {clone} — run "
                        f"`verify_driver.py prepare-base` on this machine")
    # The daemon answers before its image inventory means anything: a stopped
    # daemon reports the pinned image as absent, which reads as a base this
    # machine never prepared.
    if not _daemon_available():
        return _no_daemon(req, "the local Docker daemon is not answering — start "
                               "it (e.g. `colima start`) to resume verification")
    if not _image_exists(image):
        return _no_base(req,
                        f"base image {image} not found in the local Docker daemon"
                        f" — run `verify_driver.py prepare-base` on this machine")
    if verify_driver.pinned_suite(store):
        try:
            suite_config: Path | None = verify_driver.write_suite_config()
        except RuntimeError as e:
            return _fail(req, "sandbox-error", str(e))
    else:
        suite_config = None

    # Harness self-test (canary): before trusting THIS PR's red→green, prove the
    # harness reproduces a known bug, confirms its fix, and rejects a non-fix
    # patch. A harness that fails its own ground truth cannot produce a trusted
    # verdict, so refuse rather than emit one. Cached per image (once per boot).
    problems = _harness_problems(image, base, tier)
    if problems:
        return _fail(req, "sandbox-error",
                     "harness self-test (canary) failed — verdicts are not "
                     "trustworthy until the harness is fixed: " + "; ".join(problems))

    # The diff, fail-closed: no parseable diff, no sandbox. An unfetchable diff
    # is a transient upstream failure and re-queues; a deps-touching PR is
    # refused and recorded as deps-touched.
    _say("① Caching diff…")
    if not diff_cache.fetch_diff(n, head, store=store):
        return _fail_transient(req, "fetch-error",
                               f"could not fetch the diff for PR #{n} — "
                               f"verification fails closed without one")
    paths = verify_driver.changed_paths_for(rec)
    if not paths:
        return _fail(req, "refused-safety",
                     "the cached diff yields no changed paths — verification fails closed")
    if gates.deps_touched(paths):
        _say("✗ deps-touched: the PR changes dependency manifests; the sandbox "
             "never installs PR-controlled dependencies")
        store.edit_pr(n).record_verify("deps-touched", {}, base_sha=base,
                                       head_sha=rec.head_sha)
        req.done()
        return 0

    # Signal 1 — blind adequacy, committed BEFORE any run (the phase's
    # integrity property; `run` refuses a PR without it).
    _say("② Blind adequacy verdict (committed before any run)…")
    req.running("blind")
    blind_item, blind_fail = _retry_once(
        lambda: _blind_verdict(rec, str(clone)), "blind adequacy")
    if blind_item is None:
        return _fail_transient(req, "agent-failed",
                               "the blind adequacy agent failed or returned "
                               "unusable JSON", log_tail=blind_fail)
    blind_item = _vet_blind_repro(rec, blind_item, str(clone))
    ok, errs = verify_driver.commit_blind(store, [blind_item])
    if errs or not ok:
        return _fail(req, "agent-failed",
                     f"blind verdict commit failed: {'; '.join(errs)}")
    _say(f"  faithful={blind_item.faithful} test_cmd={blind_item.test_cmd!r}")

    rec2 = store.load_pr(n)
    assert rec2 is not None, f"pr {n} was just blind-committed but is gone from the store"
    blind = rec2.verify_signals.get("blind_adequacy") or {}

    # AUTHOR pass — a PR that ships no test gets an agent-authored reproduction
    # test, validated fail-closed and committed BEFORE any sandbox run (the same
    # integrity property as the blind verdict). Best-effort: an authoring
    # failure records the skip and the run continues to its outcome.
    if not blind.get("test_cmd") and not blind.get("requires_live_agent"):
        _say("②b Authoring pass (agent-authored test — corroborating lane)…")
        req.running("author")
        item = _author_verdict(rec2, str(clone))
        if item is None:
            authored_sig: dict[str, object] = {"attempted": True, "can_author": False,
                                  "files": [], "test_cmd": None,
                                  "skipped_reason": "agent-failed"}
        else:
            cmd, skip = verify_driver.validate_authored(
                item, base_clone=clone, pr_paths=paths)
            authored_sig = item.to_signal()
            authored_sig["test_cmd"] = cmd
            if skip is not None:
                authored_sig["skipped_reason"] = skip
        signals0 = dict(rec2.verify_signals)
        signals0["authored_test"] = authored_sig
        store.edit_pr(n).record_verify(None, signals0, base_sha=base,
                                       head_sha=rec2.head_sha)
        rec2 = store.load_pr(n)
        assert rec2 is not None, \
            f"pr {n} was just author-committed but is gone from the store"
        blind = rec2.verify_signals.get("blind_adequacy") or {}
        _say(f"  authored test_cmd={authored_sig.get('test_cmd')!r} "
             f"skipped={authored_sig.get('skipped_reason')!r}")

    # Signal 2 — the sandbox phases, host-observed. A live-agent-only bug
    # spends no sandbox time: the blind verdict settles its outcome.
    _say("③ Sandbox run (apply → red → green → regress)…")
    req.running("sandbox")
    try:
        if blind.get("requires_live_agent"):
            _say("  requires a live agent — no sandbox time spent")
            ev = _no_run_evidence(rec2, base, tier)
        else:
            ev = verify_driver.verify_pr(rec2, image, base, tier, baseline,
                                         suite_config=suite_config)
    except verify_driver.FetchFailure as e:
        return _fail_transient(req, "fetch-error", str(e))
    except verify_driver.ProbeFailure as e:
        return _fail_transient(req, "sandbox-error", str(e))
    except Exception as e:
        return _fail_transient(req, "sandbox-error", f"sandbox run failed: {e}",
                               log_tail=traceback.format_exc())
    assert ev is not None, f"pr {n} has a committed blind verdict, so verify_pr returns evidence"
    rg = ev["red_green"]
    _say(f"  exits: apply={rg['apply_exit']} red={rg['red_exit']} green={rg['green_exit']}")
    contaminated = gates.contained_green_failures(rg)
    if contaminated:
        _say("  dirty green accepted as contamination (failed red too): "
             + ", ".join(contaminated))
    for lane_name, lane in (ev.get("lanes") or {}).items():
        state = (f"exit {lane['exit']}" if "exit" in lane
                 else f"skipped ({lane.get('skipped')})")
        _say(f"  lane {lane_name}: {state}")
    signals = dict(rec2.verify_signals)
    signals["red_green"] = ev["red_green"]
    signals["independent_repro"] = ev["independent_repro"]
    signals["regress"] = ev["regress"]
    if ev.get("authored_test"):
        signals["authored_test"] = ev["authored_test"]
    if ev.get("lanes"):
        signals["lanes"] = ev["lanes"]
    store.edit_pr(n).record_verify(None, signals, tier=tier, base_sha=base,
                                   head_sha=rec2.head_sha)

    # Signals 3+4 — judged only when a red actually ran, off whichever lane
    # produced it (the PR's own test lane takes priority over the authored
    # lane); every other path resolves from blind + host alone, and an empty
    # rating stores no signal.
    judge_item = JudgeItem(pr=n, red_reason_match={}, repro_reason_match={},
                           findings=[])
    authored_ev = ev.get("authored_test") or {}
    if rg.get("red_exit") is not None:
        _say("④ Post-run judgment (red-reason / repro-reason match)…")
        req.running("judge")
        judged, judge_fail = _retry_once(
            lambda: _judge_verdict(n, blind.get("expected_red_signature"),
                                   blind.get("expected_repro_signature"),
                                   ev["red_green"], ev["independent_repro"]),
            "post-run judge")
        if judged is None:
            return _fail_transient(req, "agent-failed",
                                   "the post-run judge agent failed or returned "
                                   "unusable JSON", log_tail=judge_fail)
        judge_item = judged
    elif authored_ev.get("red_exit") is not None:
        _say("④ Post-run judgment (authored-test red-reason match)…")
        req.running("judge")
        judged, judge_fail = _retry_once(
            lambda: _judge_verdict(n, authored_ev.get("expected_red_signature"), None,
                                   {k: authored_ev.get(k) for k in
                                    ("red_exit", "green_exit", "red_output_tail",
                                     "green_output_tail")},
                                   {"ran": False, "exit_code": None, "output_tail": ""}),
            "post-run judge")
        if judged is None:
            return _fail_transient(req, "agent-failed",
                                   "the post-run judge agent failed or returned "
                                   "unusable JSON", log_tail=judge_fail)
        judge_item = judged

    # The outcome — gates.verify_outcome via commit_outcomes (disposition
    # consequence + escalate cluster-reopen included).
    req.running("commit")
    ok, held, errs = verify_driver.commit_outcomes(store, [judge_item])
    if held:
        return _fail(req, "hold",
                     "the run errored (a phase exited outside its sentinel set) — "
                     "no verdict is trusted from it; re-queue to retry")
    if errs or not ok:
        return _fail(req, "agent-failed", f"outcome commit failed: {'; '.join(errs)}")

    after = store.load_pr(n)
    outcome = after.verify_outcome if after is not None else None
    _say(f"✓ outcome: {outcome}")
    req.done()
    return 0


def run(store: Store, pr: int, *, from_queue: bool = False) -> int:
    """Verify one PR end to end. Returns 0 when a non-error state was recorded
    (a verdict, a deps-touched refusal, a waiting-for-base park, or a no-op
    --from-queue pickup of a PR that is no longer queued) and 1 when the
    request ended in `error`."""
    rec = store.load_pr(pr)
    if rec is None:
        _say(f"✗ PR #{pr} not found in store")
        return 1
    prior = rec.verify_request or {}
    if from_queue:
        # An atomic claim, not a read-then-write: the request flips to running
        # only if the row is unchanged since it was read, so two workers
        # against the shared store can never both run one PR.
        claimed = store.claim_verify_request(pr, host=socket.gethostname())
        if claimed is None:
            _say(f"· PR #{pr} is not claimable (status: {prior.get('status')!r}) — "
                 f"nothing to run; a cancel landed or another worker claimed it")
            return 0
        runnable = True
        prior = claimed
    else:
        runnable = prior.get("status") in ("queued", "waiting-for-base")
    started = _now()
    req = _Request(store, pr, prior.get("queued_at") if runnable else None,
                   prior.get("source") if runnable else None,
                   started_at=prior.get("started_at") if from_queue else None,
                   attempts=(prior.get("attempts") or 0) if runnable else 0)
    try:
        rc = _run_inner(store, rec, req)
    except Exception:
        tb = traceback.format_exc()
        _say(f"✗ verify_pr crashed:\n{tb}")
        req.error("exception", "verify_pr crashed — see log tail", log_tail=tb)
        rc = 1
    after = store.load_pr(pr)
    request = (after.verify_request if after is not None else None) or {}
    # `outcome` names what THIS run concluded, so only a request that ended
    # `done` stamps one — the PR's stored outcome outlives the run that wrote
    # it, and a park or a failure reached no conclusion of its own.
    status = request.get("status")
    outcome = (after.verify_outcome
               if after is not None and status == "done" else None)
    entry: dict = {
        "phase": "verify:single", "pr": pr, "started": started, "finished": _now(),
        "stats": {"status": status, "error_kind": request.get("error_kind"),
                  "outcome": outcome}}
    if runnable and prior.get("source") == "auto":
        entry["trigger"] = "autohunt"
    store.append_run(entry)
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--store", default=None)
    ap.add_argument("--from-queue", action="store_true",
                    help="run only if the PR's verify_request is currently queued "
                         "or waiting-for-base (the verification worker's pickup mode)")
    args = ap.parse_args(argv)
    store = Store(args.store) if args.store else Store()
    return run(store, args.pr, from_queue=args.from_queue)


if __name__ == "__main__":
    sys.exit(main())
