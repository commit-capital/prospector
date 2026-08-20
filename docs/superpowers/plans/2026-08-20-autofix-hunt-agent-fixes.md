# Autofix Hunting for Agent-Authored Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The idle autofix hunter queues agent-authored `fix` actions for below-bar, CI-passing, mergeable PRs; fixes autopush when `TRIAGE_FIX_AUTOPUSH` names `fix`; every pushed fix re-triggers the review provider so the new score can clear the bar.

**Architecture:** Three layers change: `pipeline/settings.py` gains two live-read env switches; `pipeline/gates.py` extends `fix_huntable` with per-action logic (`fix` inverts the review-bar requirement); `prospector_app/backend/fix_worker.py` grows a hunt arm for `fix` (with a one-attempt-per-head guard and an in-flight cap), an autopush branch in `_author_fix`, and a best-effort post-push review retrigger. Deployment config (`profile.json`, `.env`) activates it.

**Tech Stack:** Python 3.14 (uv-locked), pytest, pyright, ruff. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-20-autofix-hunting-agent-fixes-design.md`

## Global Constraints

- `uv run pytest` (all four suites), `uv run ruff check .`, and `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness` must stay at 0 errors after every task.
- Comments/docstrings describe the code as it stands — no "previously/now/instead of" phrasing (CLAUDE.md convention).
- Every function signature fully typed, `from __future__ import annotations` already present in all touched modules.
- Env switches are read live (function per read), matching `fix_autohunt()` / `fix_autopush()` in `pipeline/settings.py:212-227`.
- `fix_huntable` stays a pure policy function over stored facts — no settings/env reads inside `pipeline/gates.py`.

---

### Task 1: Settings switches

**Files:**
- Modify: `pipeline/settings.py` (after `fix_autohunt()`, ~line 227)
- Test: `pipeline/tests/test_settings.py` (create if absent; check first — autopush parsing tests may live in `pipeline/tests/`; put these tests beside them)

**Interfaces:**
- Produces: `settings.fix_hunt_fix() -> bool` (env `TRIAGE_FIX_HUNT_FIX == "1"`), `settings.fix_hunt_limit() -> int` (env `TRIAGE_FIX_HUNT_LIMIT`, default 3; invalid/non-positive values fall back to 3).

- [ ] **Step 1: Write the failing tests**

```python
# in the pipeline test module that already covers settings parsing (or a new
# pipeline/tests/test_settings.py with the standard docstring + __future__ import)
from pipeline import settings


def test_fix_hunt_fix_defaults_off(monkeypatch):
    monkeypatch.delenv("TRIAGE_FIX_HUNT_FIX", raising=False)
    assert settings.fix_hunt_fix() is False


def test_fix_hunt_fix_exact_opt_in(monkeypatch):
    monkeypatch.setenv("TRIAGE_FIX_HUNT_FIX", "yes")
    assert settings.fix_hunt_fix() is False
    monkeypatch.setenv("TRIAGE_FIX_HUNT_FIX", "1")
    assert settings.fix_hunt_fix() is True


def test_fix_hunt_limit_default_and_override(monkeypatch):
    monkeypatch.delenv("TRIAGE_FIX_HUNT_LIMIT", raising=False)
    assert settings.fix_hunt_limit() == 3
    monkeypatch.setenv("TRIAGE_FIX_HUNT_LIMIT", "7")
    assert settings.fix_hunt_limit() == 7
    monkeypatch.setenv("TRIAGE_FIX_HUNT_LIMIT", "junk")
    assert settings.fix_hunt_limit() == 3
    monkeypatch.setenv("TRIAGE_FIX_HUNT_LIMIT", "0")
    assert settings.fix_hunt_limit() == 3
```

- [ ] **Step 2: Run tests to verify they fail** — `uv run pytest pipeline/tests/test_settings.py -v` → FAIL (`AttributeError: fix_hunt_fix`)

- [ ] **Step 3: Implement** (append near `fix_autohunt`, same live-read style):

```python
def fix_hunt_fix() -> bool:
    """Whether an idle fix worker may queue agent-authored `fix` actions on its
    own. A separate opt-in from fix_autohunt: mechanical hunting and unattended
    code authoring are different amounts of trust."""
    return os.environ.get("TRIAGE_FIX_HUNT_FIX", "") == "1"


def fix_hunt_limit() -> int:
    """The most auto-queued `fix` requests allowed in flight at once. Each fix
    spends two agents plus a compile preflight, so the hunter feeds them in
    small batches; an unparseable or non-positive value reads as the default."""
    raw = os.environ.get("TRIAGE_FIX_HUNT_LIMIT", "")
    try:
        n = int(raw)
    except ValueError:
        return 3
    return n if n > 0 else 3
```

- [ ] **Step 4: Run tests** → PASS
- [ ] **Step 5: Commit** — `git add -A && git commit -m "Settings: TRIAGE_FIX_HUNT_FIX and TRIAGE_FIX_HUNT_LIMIT"`

---

### Task 2: `gates.fix_huntable` learns the `fix` action

**Files:**
- Modify: `pipeline/gates.py:504-538` (`HUNTABLE_ACTIONS` comment + tuple, `fix_huntable`)
- Test: `pipeline/tests/test_gates.py` (`TestFixHuntable`, ~line 2107)

**Interfaces:**
- Consumes: `profile.active().autofix.fixable_gates`, `review_policy.active().clean_blocker(pr)`, `pr.ci`, `pr.mergeable`, `pr.review_stale` (all existing).
- Produces: `gates.fix_huntable(pr, "fix", changed_paths)` returns `(True, ...)` for a signals-current, CI-passing, mergeable PR with a non-stale below-bar review score when the profile names `"review"` in `fixable_gates` — then delegates to `fix_eligibility(pr, "fix", changed_paths, guided=False)` unchanged. `HUNTABLE_ACTIONS == ("update", "rebase", "fix")`.

- [ ] **Step 1: Update the existing contradicting test and add the new cases.** Replace `test_never_hunts_an_agent_authored_fix` (test_gates.py ~2174) and extend `TestFixHuntable`:

```python
    def _below_bar(self, **over):
        """A CI-passing, mergeable PR whose review score sits below the bar —
        the population an agent-authored fix targets."""
        return _pr(signals={"greptile": 4, "greptile_reviewed_sha": HEAD,
                            "ci": "passing", "mergeable": True,
                            "has_tests": True, "checked_at": NOW,
                            "against_head_sha": HEAD}, **over)

    def test_below_bar_clean_pr_is_fix_huntable(self, monkeypatch):
        self._profile(monkeypatch, fixable_gates=("review",))
        ok, why = gates.fix_huntable(self._below_bar(), "fix")
        assert ok is True, why

    def test_fix_hunt_requires_ci_passing(self, monkeypatch):
        self._profile(monkeypatch, fixable_gates=("review",))
        pr = self._below_bar()
        pr.raw["signals"]["ci"] = "failing"
        ok, why = gates.fix_huntable(pr, "fix")
        assert ok is False
        assert "CI" in why

    def test_fix_hunt_requires_mergeable(self, monkeypatch):
        self._profile(monkeypatch, fixable_gates=("review",))
        pr = self._below_bar()
        pr.raw["signals"]["mergeable"] = False
        ok, why = gates.fix_huntable(pr, "fix")
        assert ok is False
        assert "merge" in why

    def test_fix_hunt_skips_a_stale_score(self, monkeypatch):
        # A stale score describes a head the author moved past; the remedy is a
        # re-review, not a fix authored against outdated findings.
        self._profile(monkeypatch, fixable_gates=("review",))
        pr = self._below_bar()
        pr.raw["signals"]["greptile_reviewed_sha"] = "b" * 40
        ok, why = gates.fix_huntable(pr, "fix")
        assert ok is False
        assert "stale" in why.lower()

    def test_fix_hunt_skips_a_pr_already_at_the_bar(self, monkeypatch):
        self._profile(monkeypatch, fixable_gates=("review",))
        pr = self._below_bar()
        pr.raw["signals"]["greptile"] = 5
        ok, why = gates.fix_huntable(pr, "fix")
        assert ok is False

    def test_fix_hunt_needs_the_profile_gate_named(self, monkeypatch):
        self._profile(monkeypatch)  # no fixable_gates
        ok, why = gates.fix_huntable(self._below_bar(), "fix")
        assert ok is False

    def test_never_hunts_a_resolve(self, monkeypatch):
        self._profile(monkeypatch, fixable_gates=("review",))
        ok, why = gates.fix_huntable(self._below_bar(), "resolve")
        assert ok is False
        assert "resolve" in why
```

Check the `_pr` helper's signals shape first: the stale-score mechanism is whatever `pr.review_stale` reads (`greptile_reviewed_sha` vs `head_sha` — confirm in `pipeline/model.py` around the `greptile_stale` property and mirror the fixture accordingly).

- [ ] **Step 2: Run** — `uv run pytest pipeline/tests/test_gates.py::TestFixHuntable -v` → new cases FAIL

- [ ] **Step 3: Implement.** Replace `HUNTABLE_ACTIONS` block and rework `fix_huntable`:

```python
# The autofix actions the idle hunter may queue on its own. `update` and
# `rebase` are mechanical; `fix` has an agent author a change, and the worker
# additionally holds it behind the deployment's TRIAGE_FIX_HUNT_FIX opt-in and
# an in-flight cap. A `resolve` is never queued directly by anyone.
HUNTABLE_ACTIONS = ("update", "rebase", "fix")


def fix_huntable(pr: Pr, action: str,
                 changed_paths: list[str] | None = None) -> tuple[bool, str]:
    """May the idle hunter queue `action` for this PR without being asked?

    fix_eligibility bounds which branches the push bot may touch at all. This is
    the narrower question of which PRs are worth spending sandbox time on
    unprompted, and its bar is per-action:

    - `update`/`rebase` exist to clear conflicts and a stale CI run, so they ask
      for a human-facing quality signal — the review provider's bar — and for
      nothing that being out of date is what causes (`pr_clean` requires
      `mergeable` and passing CI, both false on exactly the PRs this hunts).
    - `fix` targets the opposite population: a PR already mergeable with CI
      passing whose review score sits below the bar, scored at the current head.
      A stale score is excluded because its findings describe a head the author
      moved past — the remedy there is a re-review, not authored code.

    The operator's own click answers to fix_eligibility alone; this bar governs
    only what the hunter starts by itself.
    """
    if action not in HUNTABLE_ACTIONS:
        return False, (f"the hunter queues only {', '.join(HUNTABLE_ACTIONS)}; "
                       f"a {action} is an operator's call")
    if not is_current(pr, "signals"):
        return False, "signals stale or missing, so the review bar is unknowable"
    blocker = review_policy.active().clean_blocker(pr)
    if action == "fix":
        if pr.ci != "passing":
            return False, f"CI is {pr.ci or 'unknown'}, not passing"
        if pr.mergeable is not True:
            return False, "the PR does not merge cleanly"
        fixable = profile.active().autofix.fixable_gates
        review_fixable = ("review" in fixable and blocker is not None
                          and not pr.review_stale)
        ci_fixable = "ci" in fixable and pr.ci == "failing"
        if not (review_fixable or ci_fixable):
            if blocker is not None and pr.review_stale:
                return False, ("the review score is stale — a re-review, not a "
                               "fix, is what moves it")
            return False, "no gate a fix could clear is failing"
    elif blocker:
        return False, blocker
    return fix_eligibility(pr, action, changed_paths)
```

(`profile` is already imported in gates.py — verify; add to the existing import if not.) Note the `ci_fixable` arm is unreachable while the entry conditions require CI passing; it is written as a disjunction so the arm activates if the entry conditions are widened, per the spec.

- [ ] **Step 4: Run the full gates suite** — `uv run pytest pipeline/tests/test_gates.py -v` → PASS (including untouched update/rebase cases)
- [ ] **Step 5: Commit** — `"Gates: fix_huntable learns the agent-authored fix action"`

---

### Task 3: Terminal fix records keep their head stamp

**Files:**
- Modify: `prospector_app/backend/fix_worker.py` — `_refuse`, `_fail`, `_finish_pushed`
- Test: `prospector_app/backend/tests/test_fix_worker.py`

**Interfaces:**
- Produces: every terminal `fix_request` written by the worker (`refused`, `failed`, `pushed`) carries `against_head_sha` from the claimed request, so `_fix_attempted` (Task 4) can compare it to the current head. `record_fix_request` already accepts `head_sha=` and stamps it as `against_head_sha` (`pipeline/model.py:456-489`); the claimed dict already carries it (`store.claim_fix_request`, `pipeline/store.py:722`).

- [ ] **Step 1: Write the failing test**

```python
def test_terminal_records_carry_the_head_they_ran_against(store, monkeypatch):
    # The one-attempt-per-head guard reads the stamp off refused/failed/pushed
    # records; a terminal record that dropped it would re-arm the PR instantly.
    fix_queue.queue_pr(1, "update")
    probe = _Probe(rc=8)  # base conflicts → refused
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    fix_worker.run_one(1)
    req = store.load_pr(1).fix_request
    assert req["status"] == "refused"
    assert req["against_head_sha"] == HEAD
```

- [ ] **Step 2: Run** — `uv run pytest prospector_app/backend/tests/test_fix_worker.py::test_terminal_records_carry_the_head_they_ran_against -v` → FAIL (no `against_head_sha` key)

- [ ] **Step 3: Implement.** Add `head_sha=req.get("against_head_sha")` to the three `record_fix_request` calls in `_refuse`, `_fail`, and `_finish_pushed` (matching how `_park` and `_running_step` already pass it). `_settle`'s retry re-queue path stays without it — a re-queued request is re-claimed and restamped.

- [ ] **Step 4: Run the fix_worker suite** → PASS
- [ ] **Step 5: Commit** — `"Fix worker: terminal records carry against_head_sha"`

---

### Task 4: Hunter queues `fix` (guard + cap)

**Files:**
- Modify: `prospector_app/backend/fix_worker.py` — `auto_fixable`, new `_fix_attempted`, new `_auto_fixes_in_flight`, `next_auto`
- Test: `prospector_app/backend/tests/test_fix_worker.py`

**Interfaces:**
- Consumes: `settings.fix_hunt_fix()`, `settings.fix_hunt_limit()` (Task 1), `gates.fix_huntable(pr, "fix", paths)` (Task 2), head-stamped terminal records (Task 3).
- Produces: `auto_fixable(pr) -> str | None` may return `"fix"`; `next_auto() -> tuple[str, int] | None` respects the cap. `_fix_attempted(pr) -> bool`, `_auto_fixes_in_flight() -> int` (module-private).

- [ ] **Step 1: Write the failing tests**

```python
# A store fixture PR shaped for fix hunting (add as a helper or second record):
def _fixable_pr(n: int = 2) -> dict:
    return {"pr": n,
            "meta": {"title": "below bar", "state": "open", "head_sha": HEAD},
            "signals": {"greptile": 4, "greptile_reviewed_sha": HEAD,
                        "ci": "passing", "mergeable": True,
                        "checked_at": NOW, "against_head_sha": HEAD}}


@pytest.fixture
def fix_profile(monkeypatch):
    p = profile.RepoProfile(autofix=profile.AutofixPolicy(fixable_gates=("review",)))
    monkeypatch.setattr(profile, "active", lambda: p)


def test_hunter_ignores_fix_without_the_opt_in(store, fix_profile, monkeypatch):
    monkeypatch.delenv("TRIAGE_FIX_HUNT_FIX", raising=False)
    store.save_pr(_fixable_pr()); data.refresh()
    assert fix_worker.auto_fixable(data.prs()[2]) is None


def test_hunter_queues_fix_with_the_opt_in(store, fix_profile, monkeypatch):
    monkeypatch.setenv("TRIAGE_FIX_HUNT_FIX", "1")
    store.save_pr(_fixable_pr()); data.refresh()
    assert fix_worker.auto_fixable(data.prs()[2]) == "fix"


def test_one_fix_attempt_per_head(store, fix_profile, monkeypatch):
    monkeypatch.setenv("TRIAGE_FIX_HUNT_FIX", "1")
    rec = _fixable_pr()
    rec["fix_request"] = {"status": "refused", "action": "fix",
                          "against_head_sha": HEAD}
    store.save_pr(rec); data.refresh()
    assert fix_worker.auto_fixable(data.prs()[2]) is None
    # A moved head re-arms the PR.
    rec["fix_request"] = {"status": "refused", "action": "fix",
                          "against_head_sha": "b" * 40}
    store.save_pr(rec); data.refresh()
    assert fix_worker.auto_fixable(data.prs()[2]) == "fix"


def test_fix_hunt_respects_the_in_flight_cap(store, fix_profile, monkeypatch):
    monkeypatch.setenv("TRIAGE_FIX_HUNT_FIX", "1")
    monkeypatch.setenv("TRIAGE_FIX_HUNT_LIMIT", "1")
    running = _fixable_pr(3)
    running["fix_request"] = {"status": "running", "action": "fix", "source": "auto"}
    store.save_pr(running)
    store.save_pr(_fixable_pr(2)); data.refresh()
    # PR 1 (mergeable False) fails the mechanical review bar (greptile 5 needed
    # — the fixture's PR 1 already meets it, so drop it below to isolate):
    one = store.load_pr(1).raw; one["signals"]["greptile"] = 4; store.save_pr(one)
    data.refresh()
    assert fix_worker.next_auto() is None
```

- [ ] **Step 2: Run** → FAIL (auto_fixable returns None for PR 2 with opt-in — no fix arm yet; cap test errors)

- [ ] **Step 3: Implement**

```python
# Terminal fix_request statuses. A cancelled request counts: an operator saying
# no to this head is not an invitation to try the same head again.
_TERMINAL = ("pushed", "refused", "failed", "cancelled")


def _fix_attempted(pr: Pr) -> bool:
    """Whether this PR's current head already had its unattended fix attempt.
    The stamp a moved head no longer matches is what re-arms the PR."""
    req = pr.fix_request or {}
    return (req.get("action") == "fix" and req.get("status") in _TERMINAL
            and req.get("against_head_sha") == pr.head_sha
            and pr.head_sha is not None)


def _auto_fixes_in_flight() -> int:
    """How many hunter-queued fix requests are anywhere between queued and
    pushed. Operator-queued fixes are not counted against the hunter's cap."""
    count = 0
    for rec in data.prs().values():
        req = rec.fix_request or {}
        if (req.get("source") == "auto" and req.get("action") == "fix"
                and req.get("status") in fix_queue.IN_FLIGHT):
            count += 1
    return count
```

`auto_fixable` (replace the two-arm dispatch; docstring updated to describe all three arms — drop "an agent-authored fix is an operator's call"):

```python
    if (pr.fix_request or {}).get("status") in fix_queue.IN_FLIGHT:
        return None
    if pr.mergeable is False:
        action = "rebase"
    elif pr.drift_state == "conflicts":
        action = "update"
    elif settings.fix_hunt_fix() and not _fix_attempted(pr):
        action = "fix"
    else:
        return None
    ok, _ = gates.fix_huntable(pr, action, service.changed_paths(pr))
    return action if ok else None
```

`next_auto` grows the cap check (ordering comes in Task 5 — keep first-match-by-number here):

```python
def next_auto() -> tuple[str, int] | None:
    """The idle hunter's next (action, PR), or None when nothing is eligible."""
    fix_slots = settings.fix_hunt_limit() - _auto_fixes_in_flight()
    for n, rec in sorted(data.prs().items()):
        action = auto_fixable(rec)
        if action is None:
            continue
        if action == "fix" and fix_slots <= 0:
            continue
        return action, n
    return None
```

Also update the module docstring's hunter paragraph (fix_worker.py:30-33) and the fix_worker.py:933 docstring to describe the three-arm hunt.

- [ ] **Step 4: Run the whole fix_worker suite** → PASS
- [ ] **Step 5: Commit** — `"Fix worker: the hunter queues agent-authored fixes behind TRIAGE_FIX_HUNT_FIX"`

---

### Task 5: Hunt ordering — mechanical first, then nits, then score tiers, pain-desc within

**Files:**
- Modify: `prospector_app/backend/fix_worker.py` — `next_auto`, new `_hunt_key`
- Test: `prospector_app/backend/tests/test_fix_worker.py`

**Interfaces:**
- Consumes: `service.pr_pain(rec)["score"]` (float), `pr.review_severity`, `pr.review_score`.
- Produces: `_hunt_key(rec: Pr, action: str, n: int) -> tuple[int, int, float, int]`.

- [ ] **Step 1: Write the failing test**

```python
def test_hunt_order_mechanical_then_nits_then_tiers(store, fix_profile, monkeypatch):
    monkeypatch.setenv("TRIAGE_FIX_HUNT_FIX", "1")
    nits = _fixable_pr(2); nits["signals"]["greptile_severity"] = "nits"
    four = _fixable_pr(3)             # score 4, no severity
    three = _fixable_pr(4); three["signals"]["greptile"] = 3
    for rec in (nits, four, three):
        store.save_pr(rec)
    data.refresh()
    # PR 1 (unmergeable, bar met) is mechanical and wins outright.
    assert fix_worker.next_auto() == ("rebase", 1)
    one = store.load_pr(1).raw
    one["fix_request"] = {"status": "running", "action": "rebase"}
    store.save_pr(one); data.refresh()
    assert fix_worker.next_auto() == ("fix", 2)   # nits beat score tiers
    nits2 = store.load_pr(2).raw
    nits2["fix_request"] = {"status": "running", "action": "fix"}
    store.save_pr(nits2); data.refresh()
    assert fix_worker.next_auto() == ("fix", 3)   # 4/5 beats 3/5
```

(Confirm the signals key `greptile_severity` matches what `pr.review_severity` reads in `pipeline/model.py` — mirror the real key.)

- [ ] **Step 2: Run** → FAIL if ordering not implemented (nits/tiers indistinct)

- [ ] **Step 3: Implement**

```python
def _hunt_key(rec: Pr, action: str, n: int) -> tuple[int, int, float, int]:
    """Hunt priority (ascending). Mechanical actions lead: they are cheap and
    unblock the most. Fixes follow by how little they ask of the agent — a
    nits-only review, then one point below the bar, then the rest — and by
    community pain (descending) within a tier."""
    if action != "fix":
        return (0, 0, 0.0, n)
    tier = (1 if rec.review_severity == "nits"
            else 2 if rec.review_score == 4 else 3)
    pain = float(service.pr_pain(rec).get("score") or 0.0)
    return (1, tier, -pain, n)


def next_auto() -> tuple[str, int] | None:
    """The idle hunter's next (action, PR), or None when nothing is eligible."""
    fix_slots = settings.fix_hunt_limit() - _auto_fixes_in_flight()
    best: tuple[tuple[int, int, float, int], str, int] | None = None
    for n, rec in data.prs().items():
        action = auto_fixable(rec)
        if action is None:
            continue
        if action == "fix" and fix_slots <= 0:
            continue
        key = _hunt_key(rec, action, n)
        if best is None or key < best[0]:
            best = (key, action, n)
    return (best[1], best[2]) if best else None
```

- [ ] **Step 4: Run suite** → PASS
- [ ] **Step 5: Commit** — `"Fix worker: hunt ordering — mechanical, nits, score tiers, pain"`

---

### Task 6: `_author_fix` honors autopush

**Files:**
- Modify: `prospector_app/backend/fix_worker.py:610-612` (the `_park` tail of `_author_fix`)
- Test: `prospector_app/backend/tests/test_fix_worker.py`

**Interfaces:**
- Consumes: `settings.fix_autopush()`, `_push(n, claimed, "fix", result)` — for a `fix`, `_push` runs `resubmit push -m <message>` from the still-live prepared worktree (`fix_worker.py:904-918`; `resubmit push` commits the working-tree edits and pushes, refusing if the contributor pushed since prepare).
- Produces: an authored fix pushes unattended when `TRIAGE_FIX_AUTOPUSH` names `fix`; parks otherwise (existing behavior).

- [ ] **Step 1: Write the failing test.** Locate the existing `_author_fix` tests in test_fix_worker.py (they monkeypatch `author_fix.author` and `review_fix.review`) and mirror their setup exactly:

```python
def test_authored_fix_pushes_when_autopush_names_fix(store, monkeypatch):
    monkeypatch.setenv("TRIAGE_FIX_AUTOPUSH", "fix")
    monkeypatch.setattr(fix_worker.author_fix, "author",
                        lambda *a, **k: {"summary": "fix nits",
                                         "changes": [{"path": "a.ts", "why": "nit"}]})
    monkeypatch.setattr(fix_worker.author_fix, "assert_disclosed", lambda *a: None)
    monkeypatch.setattr(fix_worker.review_fix, "review",
                        lambda *a, **k: {"verdict": "safe", "reason": ""})
    monkeypatch.setattr(fix_worker, "_retrigger_review", lambda n: None, raising=False)
    p = profile.RepoProfile(autofix=profile.AutofixPolicy(fixable_gates=("review",)))
    monkeypatch.setattr(profile, "active", lambda: p)
    fix_queue.queue_pr(1, "fix", guidance="fix the nits")
    probe = _Probe(overrides={"state": (0, json.dumps({"worktree": "/tmp/wt"}))})
    monkeypatch.setattr(fix_worker, "_resubmit", probe)

    fix_worker.run_one(1)

    assert store.load_pr(1).fix_request["status"] == "pushed"
    assert _pushed(probe)


def test_authored_fix_parks_when_autopush_does_not_name_fix(store, monkeypatch):
    # same setup, TRIAGE_FIX_AUTOPUSH="" → status "awaiting-review", not _pushed
    ...  # (write it out fully in the test file; identical monkeypatching, empty autopush)
```

(The `...` above is shorthand in THIS plan only because the body is the first test with two changed lines — in the test file both bodies are written out in full. If the existing `_author_fix` tests already cover the parked case, extend rather than duplicate.)

- [ ] **Step 2: Run** → FAIL (status is `awaiting-review` even with autopush)

- [ ] **Step 3: Implement.** Replace `fix_worker.py:610-612` tail:

```python
    result = {**evidence, "compile_preflight": pf,
              "message": verdict["summary"] or _commit_message("fix")}
    if "fix" not in settings.fix_autopush():
        _park(n, claimed, "fix", result, socket.gethostname())
        return
    _push(n, claimed, "fix", result)
```

Update `_author_fix`'s docstring ("...and park the result for an operator to approve" → parks unless TRIAGE_FIX_AUTOPUSH names `fix`; describe present behavior only). Also update the module docstring line 17-19 if it asserts everything parks.

- [ ] **Step 4: Run suite** → PASS
- [ ] **Step 5: Commit** — `"Fix worker: authored fixes honor TRIAGE_FIX_AUTOPUSH"`

---

### Task 7: Post-push review retrigger

**Files:**
- Modify: `prospector_app/backend/fix_worker.py` — `_finish_pushed`, new `_retrigger_review`
- Test: `prospector_app/backend/tests/test_fix_worker.py`

**Interfaces:**
- Consumes: `executor.mint_bot_token() -> str | None` (never raises; None forces dry-run), `executor.retrigger_greptile(n, *, token, dry_run) -> dict` (Activity-logged, handles `retrigger_mention is None`), `review_refresh.capture(n) -> Baseline`, `review_refresh.schedule(n, baseline)` (daemon thread that ingests the new score via `ingest.refresh_prs`).
- Produces: `_retrigger_review(n: int) -> None`, called from `_finish_pushed` only when the pushed action is `fix` — covering both the autopush path and an operator-approved parked fix.

- [ ] **Step 1: Write the failing test**

```python
def test_pushed_fix_retriggers_the_review(store, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(fix_worker, "_retrigger_review", calls.append)
    fix_worker._finish_pushed(1, {"action": "fix"}, "pushed ok")
    assert calls == [1]


def test_pushed_update_does_not_retrigger(store, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(fix_worker, "_retrigger_review", calls.append)
    fix_worker._finish_pushed(1, {"action": "update"}, "pushed ok")
    assert calls == []


def test_retrigger_failure_leaves_the_pushed_record(store, monkeypatch):
    def boom(n):
        raise RuntimeError("no network")
    monkeypatch.setattr(fix_worker, "_retrigger_review", boom)
    fix_worker._finish_pushed(1, {"action": "fix"}, "pushed ok")
    assert store.load_pr(1).fix_request["status"] == "pushed"
```

Wait — with `boom` raising, `_finish_pushed` must swallow it; the third test drives that requirement. Structure `_finish_pushed` so the record write precedes the hook and the hook is wrapped.

- [ ] **Step 2: Run** → FAIL (`_retrigger_review` undefined)

- [ ] **Step 3: Implement.** Imports: add `executor` and `review_refresh` to the `prospector_app.backend` import in fix_worker.py — check for an import cycle first (`executor` must not import `fix_worker` at module level; if it does, import inside the function with a comment saying why). Then:

```python
def _finish_pushed(n: int, req: dict, output: str, result: dict | None = None) -> None:
    merged = dict(result or {})
    merged["output"] = output[-TAIL_CHARS:]
    data.store().edit_pr(n).record_fix_request(
        "pushed", req.get("action", "fix"), queued_at=req.get("queued_at"),
        started_at=req.get("started_at"), finished_at=_now(), result=merged,
        source=req.get("source"), guidance=req.get("guidance"),
        host=socket.gethostname(), head_sha=req.get("against_head_sha"))
    data.refresh()
    if req.get("action") == "fix":
        try:
            _retrigger_review(n)
        except Exception:
            traceback.print_exc()


def _retrigger_review(n: int) -> None:
    """Ask the review provider for a fresh score on the head a fix just pushed,
    and start the backend wait that ingests it. The score is what stands between
    the pushed fix and the merge bar, so the push is what asks. Best-effort: no
    mintable token forces the executor's dry-run and the PR waits for the next
    scheduled ingest instead."""
    token = executor.mint_bot_token()
    baseline = review_refresh.capture(n) if token else None
    res = executor.retrigger_greptile(n, token=token, dry_run=token is None)
    if res.get("status") == "executed" and baseline is not None:
        review_refresh.schedule(n, baseline)
    print(f"[fix-worker] review retrigger for PR #{n}: {res.get('status')}",
          flush=True)
```

(`review_refresh.capture` calls `greptile.fetch_greptile_feedback` — network; in tests `_retrigger_review` is always monkeypatched, so no test hits it.)

- [ ] **Step 4: Run suite + pyright** → PASS / 0 errors
- [ ] **Step 5: Commit** — `"Fix worker: a pushed fix re-triggers the review provider"`

---

### Task 8: Docs + deployment config

**Files:**
- Modify: `CLAUDE.md` (AUTOFIX paragraph), `profile.example.json` (autofix block comment already documents shape — confirm it needs no change), `.env.example` (document `TRIAGE_FIX_HUNT_FIX` / `TRIAGE_FIX_HUNT_LIMIT` beside the other fix switches)
- Deployment (NOT committed; operator's machine, `/Users/workyworky/prospector`): `profile.json` autofix block, `.env` additions

**Interfaces:** none — documentation and config.

- [ ] **Step 1: Update CLAUDE.md's AUTOFIX paragraph.** Replace the sentence `An agent-authored \`fix\` is never auto-queued.` with a description of present behavior: `TRIAGE_FIX_HUNT_FIX=1` lets the hunter also queue unguided `fix` actions — held to `fix_huntable`'s fix arm (CI passing, mergeable, non-stale below-bar score, profile `fixable_gates`), one attempt per head, at most `TRIAGE_FIX_HUNT_LIMIT` in flight — and a pushed `fix` re-triggers the review provider as the bot. Also amend the `TRIAGE_FIX_AUTOPUSH` sentence to note `fix` may be named.
- [ ] **Step 2: Update `.env.example`** with the two new vars and one-line explanations.
- [ ] **Step 3: Run all gates** — `uv run pytest && uv run ruff check . && uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness` → all clean. From `prospector_app/frontend`: no frontend changes, skip.
- [ ] **Step 4: Commit** — `"Docs: autofix hunting for agent-authored fixes"`
- [ ] **Step 5 (operator-confirmed, after merge/deploy):** add to `/Users/workyworky/prospector/profile.json`:

```json
"autofix": {
  "fixable_gates": ["review", "ci"],
  "deny_globs": [
    ".github/**",
    "CLAUDE.md", "**/CLAUDE.md", "AGENTS.md", "**/AGENTS.md", ".claude/**",
    "package.json", "**/package.json", "package-lock.json",
    "pnpm-lock.yaml", "pnpm-workspace.yaml", "yarn.lock", "bun.lock", "bun.lockb"
  ]
}
```

and to `/Users/workyworky/prospector/.env`:

```
TRIAGE_FIX_AUTOHUNT=1
TRIAGE_FIX_HUNT_FIX=1
TRIAGE_FIX_AUTOPUSH=update,fix
```

Confirm with the operator before editing the live `.env` — `TRIAGE_FIX_AUTOHUNT=1` + `TRIAGE_FIX_AUTOPUSH=update` activates unattended pushes on the running worker immediately, with current code, on ~651 PRs.

---

## Self-Review Notes

- Spec §2 (settings) → Task 1; §3 (gate) → Task 2; §4 (worker: guard, cap, ordering) → Tasks 3-5; autopush decision → Task 6; §5 (loop closure) → Task 7; §1 + docs → Task 8. Spec's "targeted ingest refresh of signals" rides `review_refresh.wait_and_refresh` (it calls `ingest.refresh_prs`), so no separate step.
- Task 6's second test body is intentionally abbreviated in the plan with instructions to write it in full; every other code block is complete.
- Names used across tasks: `fix_hunt_fix`, `fix_hunt_limit` (1→4,5,6), `_fix_attempted`, `_auto_fixes_in_flight` (4→5), `_retrigger_review` (7, monkeypatched in 6) — consistent.
