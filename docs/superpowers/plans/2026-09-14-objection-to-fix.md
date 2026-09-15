# Objections Become Fix Goals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a machine judgment names a defect in an agent-authored change or a YELLOW-flagged PR, the fix worker hands that judgment to the authoring agent as its goal, re-judges the result under the same bar, and parks or pushes, so the operator sees outcomes rather than rejected intermediate steps.

**Architecture:** An `objection` is a small dict stored on the `fix_request` (`{kind, signature, text, from}`) and stamped into every ledger ending. Three flows consume it: an inline *continuation* of a parked resolve inside its kept merge worktree, a one-shot *retry* of an authored fix, and a hunted `fix` request with `source="objection"` (compile failures on mechanical actions, YELLOW security findings). Policy lives in `pipeline/gates.py` (eligibility, the new `fix_autopush_bar`) and `pipeline/objections.py` (signature, once-per-head, daily budget); `prospector_app/backend/fix_worker.py` does the mechanics; `prospector_app/agent/resubmit` gains a `commit` subcommand so a continuation lands as a commit on top of the kept merge.

**Tech Stack:** Python 3.14 (uv), SQLAlchemy store, headless `claude -p` agents, bash resubmit tool, React/TypeScript frontend (pnpm).

**Spec:** `docs/superpowers/specs/2026-09-14-objection-to-fix-design.md`

## Global Constraints

- Every function signature fully typed; `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness` at 0 errors; `uv run ruff check .` clean; `uv run pytest` green.
- Comments describe the code as it stands; no history, no counterfactuals.
- Imports qualified (`from pipeline import …`); no quoted annotations.
- `schema.STORE_SCHEMA_VERSION` bumps once in this plan (Task 1), because `fix_request.source` gains a value older writers reject.
- The release guard forbids the upstream project's name in tracked files; tests use fake names.
- An objection is never human authorization: `gates.fix_eligibility` treats an objection fix as unguided, and the profile must name `objection` in `autofix.fixable_gates`.
- One continuation per (PR, head, objection signature). Daily budget `TRIAGE_FIX_OBJECTION_BUDGET`, default 20, per worker per UTC day.
- Frontend: `pnpm run build` passes with 0 `tsc` errors; `pnpm exec eslint <touched files>` adds no errors.

---

### Task 1: Vocabulary — the `objection` field, source, gate, settings, schema bump

**Files:**
- Modify: `pipeline/store.py:92` (`FIX_REQUEST_SOURCES`)
- Modify: `pipeline/model.py:433-467` (`record_fix_request`)
- Modify: `pipeline/profile.py:62` (`AUTOFIX_GATES`)
- Modify: `pipeline/settings.py` (after `fix_hunt_limit`)
- Modify: `pipeline/schema.py:64`
- Test: `pipeline/tests/test_store_fix_request.py` (create), `pipeline/tests/test_settings_accessors.py`

**Interfaces:**
- Produces: `Pr.record_fix_request(..., objection: dict | None = None)` storing `fix_request.objection`; `store.FIX_REQUEST_SOURCES` includes `"objection"`; `profile.AUTOFIX_GATES` includes `"objection"`; `settings.fix_objection_budget() -> int` (env `TRIAGE_FIX_OBJECTION_BUDGET`, default 20, non-positive or unparseable reads as default); `settings.fix_hunt_security() -> bool` (env `TRIAGE_FIX_HUNT_SECURITY == "1"`); `settings.fix_autopush_min_tier() -> int` (env `TRIAGE_FIX_AUTOPUSH_MIN_TIER`, default 2); `settings.fix_autopush_max_lines() -> int` (env `TRIAGE_FIX_AUTOPUSH_MAX_LINES`, default 300).

- [ ] **Step 1: Write the failing tests**

```python
# pipeline/tests/test_store_fix_request.py
"""fix_request carries an objection and the objection source."""
from pipeline import profile, settings
from pipeline.store import Store


def _store(tmp_path):
    st = Store(tmp_path / "db")
    st.save_pr({"pr": 1, "meta": {"title": "t", "state": "open", "head_sha": "a" * 40}})
    return st


def test_an_objection_round_trips_with_its_source(tmp_path):
    st = _store(tmp_path)
    obj = {"kind": "resolve-review", "signature": "resolve-review:behavior",
           "text": "the merge drops the base's deletion", "from": {"lens": "behavior"}}
    st.edit_pr(1).record_fix_request("queued", "fix", source="objection", objection=obj,
                                     head_sha="a" * 40)
    req = st.load_pr(1).fix_request
    assert req["source"] == "objection" and req["objection"] == obj


def test_the_objection_gate_is_a_known_fixable_gate():
    assert "objection" in profile.AUTOFIX_GATES


def test_budget_and_flags_read_the_environment(monkeypatch):
    monkeypatch.delenv("TRIAGE_FIX_OBJECTION_BUDGET", raising=False)
    assert settings.fix_objection_budget() == 20
    monkeypatch.setenv("TRIAGE_FIX_OBJECTION_BUDGET", "5")
    assert settings.fix_objection_budget() == 5
    monkeypatch.setenv("TRIAGE_FIX_OBJECTION_BUDGET", "zero")
    assert settings.fix_objection_budget() == 20
    monkeypatch.setenv("TRIAGE_FIX_HUNT_SECURITY", "1")
    assert settings.fix_hunt_security() is True
    monkeypatch.delenv("TRIAGE_FIX_AUTOPUSH_MIN_TIER", raising=False)
    assert settings.fix_autopush_min_tier() == 2
    monkeypatch.setenv("TRIAGE_FIX_AUTOPUSH_MAX_LINES", "120")
    assert settings.fix_autopush_max_lines() == 120
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q pipeline/tests/test_store_fix_request.py`
Expected: FAIL (`record_fix_request() got an unexpected keyword argument 'objection'`, `AttributeError: fix_objection_budget`).

- [ ] **Step 3: Implement**

`pipeline/store.py`:
```python
FIX_REQUEST_SOURCES = {"operator", "auto", "objection"}
```

`pipeline/model.py` `record_fix_request`: add parameter `objection: dict | None = None` after `guidance`, add `("objection", objection)` to the field tuple, and extend the docstring with: `objection carries the machine judgment (a reviewer's rejection, a compile excerpt, a security finding) a continuation authors from; it is never human authorization.`

`pipeline/profile.py`:
```python
AUTOFIX_GATES: tuple[str, ...] = ("ci", "review", "objection")
```

`pipeline/settings.py`, after `fix_hunt_limit`:
```python
def _positive_int(name: str, default: int) -> int:
    try:
        n = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return n if n > 0 else default


def fix_objection_budget() -> int:
    """Continuations an objection may start per worker per UTC day."""
    return _positive_int("TRIAGE_FIX_OBJECTION_BUDGET", 20)


def fix_hunt_security() -> bool:
    """Whether an idle fix worker may queue an objection fix against a current
    YELLOW security verdict on a mergeable, CI-green PR."""
    return os.environ.get("TRIAGE_FIX_HUNT_SECURITY", "") == "1"


def fix_autopush_min_tier() -> int:
    """The lowest risk tier a fix may touch and still push unattended."""
    return _positive_int("TRIAGE_FIX_AUTOPUSH_MIN_TIER", 2)


def fix_autopush_max_lines() -> int:
    """The most changed lines a fix may carry and still push unattended."""
    return _positive_int("TRIAGE_FIX_AUTOPUSH_MAX_LINES", 300)
```
Rewrite `fix_hunt_limit` to use `_positive_int("TRIAGE_FIX_HUNT_LIMIT", 3)`.

`pipeline/schema.py`: `STORE_SCHEMA_VERSION = 22`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q pipeline/tests/test_store_fix_request.py pipeline/tests/test_settings_accessors.py && uv run pyright pipeline`
Expected: PASS, 0 errors.

- [ ] **Step 5: Commit**

```bash
git add pipeline/store.py pipeline/model.py pipeline/profile.py pipeline/settings.py pipeline/schema.py pipeline/tests/test_store_fix_request.py
git commit -m "Carry an objection on a fix request, with its source, gate, and settings"
```

---

### Task 2: `pipeline/objections.py` — shape, signature, once-per-head, daily budget

**Files:**
- Create: `pipeline/objections.py`
- Test: `pipeline/tests/test_objections.py`

**Interfaces:**
- Produces:
  - `class Objection(TypedDict): kind: str; signature: str; text: str; from_: NotRequired[dict]` (stored under key `"from"`; use a plain `dict` for the field list, typed `dict[str, object]`).
  - `KINDS = ("resolve-review", "compile", "fix-review", "security")`
  - `build(kind: str, text: str, *, origin: dict[str, object] | None = None) -> dict` returning `{"kind", "signature", "text", "from"}`; `signature = f"{kind}:{sha1(normalized text)[:12]}"` where normalization lowercases, strips digits and hex runs, collapses whitespace.
  - `spent(pr: Pr, signature: str) -> bool`: True when `pr.fix_request` carries this `objection.signature` at `against_head_sha == pr.head_sha` in any status other than `queued`, or when `pr.fix_request.result.rounds` lists it.
  - `used_today(store: Store, worker: str, now: datetime | None = None) -> int`: count of `fix:single` ledger entries since UTC midnight whose `stats.host == worker` and `stats.objection` is set.
  - `budget_left(store: Store, worker: str, now: datetime | None = None) -> int`: `max(0, settings.fix_objection_budget() - used_today(...))`.
  - `goal_text(objection: dict) -> str`: the goal handed to the author agent: `"A reviewer rejected the previous change for this reason; make the smallest change that resolves it without undoing the resolution:\n\n<text>"` for `resolve-review` and `fix-review`; `"The compile check failed with this error; make the smallest change that makes it pass:\n\n<text>"` for `compile`; `"A security review flagged this finding; make the smallest change that removes it:\n\n<text>"` for `security`.

- [ ] **Step 1: Write the failing tests**

```python
# pipeline/tests/test_objections.py
"""objections: the shape, its signature, once-per-head, and the daily budget."""
from datetime import datetime, timezone

from pipeline import objections
from pipeline.model import Pr
from pipeline.store import Store

HEAD = "a" * 40


def test_build_signs_by_kind_and_flattened_text():
    a = objections.build("compile", "error TS2345 in file 12 at line 33")
    b = objections.build("compile", "error TS9999 in file 99 at line 1")
    assert a["signature"] == b["signature"]
    assert a["signature"].startswith("compile:")
    assert objections.build("security", "x")["signature"] != a["signature"]


def test_spent_when_the_head_already_carried_this_objection():
    obj = objections.build("resolve-review", "drops the base's deletion")
    pr = Pr(None, {"pr": 1, "meta": {"head_sha": HEAD},
                   "fix_request": {"status": "refused", "action": "fix", "objection": obj,
                                   "against_head_sha": HEAD}})
    assert objections.spent(pr, obj["signature"])
    moved = Pr(None, {"pr": 1, "meta": {"head_sha": "b" * 40},
                      "fix_request": {"status": "refused", "action": "fix", "objection": obj,
                                      "against_head_sha": HEAD}})
    assert not objections.spent(moved, obj["signature"])
    queued = Pr(None, {"pr": 1, "meta": {"head_sha": HEAD},
                       "fix_request": {"status": "queued", "action": "fix", "objection": obj,
                                       "against_head_sha": HEAD}})
    assert not objections.spent(queued, obj["signature"])


def test_spent_when_a_round_already_answered_it():
    obj = objections.build("resolve-review", "r")
    pr = Pr(None, {"pr": 1, "meta": {"head_sha": HEAD},
                   "fix_request": {"status": "awaiting-review", "action": "resolve",
                                   "against_head_sha": HEAD,
                                   "result": {"rounds": [{"objection": obj}]}}})
    assert objections.spent(pr, obj["signature"])


def test_budget_counts_todays_objection_endings_for_this_worker(tmp_path, monkeypatch):
    st = Store(tmp_path / "db")
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    for host, hours_ago in (("w1", 1), ("w1", 2), ("w2", 1), ("w1", 20)):
        st.append_run({"phase": "fix:single", "pr": 1,
                       "started": "2026-09-15T00:00:00+00:00",
                       "finished": now.isoformat(),
                       "ts": now.replace(hour=12 - hours_ago if hours_ago < 12 else 0).isoformat()
                       if hours_ago < 12 else "2026-09-14T16:00:00+00:00",
                       "stats": {"host": host, "status": "pushed", "action": "fix",
                                 "objection": "compile:abc"}})
    st.append_run({"phase": "fix:single", "pr": 2, "started": None, "finished": now.isoformat(),
                   "ts": now.isoformat(), "stats": {"host": "w1", "status": "refused", "action": "fix"}})
    monkeypatch.setenv("TRIAGE_FIX_OBJECTION_BUDGET", "3")
    assert objections.used_today(st, "w1", now) == 2
    assert objections.budget_left(st, "w1", now) == 1


def test_goal_text_names_the_kind():
    assert "compile check failed" in objections.goal_text(objections.build("compile", "boom"))
    assert "security review flagged" in objections.goal_text(objections.build("security", "s"))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q pipeline/tests/test_objections.py`
Expected: FAIL with `ModuleNotFoundError: pipeline.objections`.

- [ ] **Step 3: Implement `pipeline/objections.py`**

```python
"""An objection: a machine judgment that names a defect an agent may fix.

A reviewer's rejection of a resolution, a compile excerpt the base passes, a
fix reviewer's rejection, or a security finding is handed to the authoring
agent as its goal. The signature keys once-per-head and issue-style dedup;
the budget bounds unattended spend per worker per UTC day.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pipeline import settings

if TYPE_CHECKING:
    from pipeline.model import Pr
    from pipeline.store import Store

KINDS = ("resolve-review", "compile", "fix-review", "security")

_GOALS = {
    "resolve-review": ("A reviewer rejected the previous change for this reason; make the "
                       "smallest change that resolves it without undoing the resolution:"),
    "fix-review": ("A reviewer rejected the previous change for this reason; make the "
                   "smallest change that resolves it without undoing the resolution:"),
    "compile": ("The compile check failed with this error; make the smallest change "
                "that makes it pass:"),
    "security": ("A security review flagged this finding; make the smallest change "
                 "that removes it:"),
}


def _flatten(text: str) -> str:
    text = re.sub(r"[0-9a-f]{7,}", "#", text.lower())
    text = re.sub(r"\d+", "#", text)
    return re.sub(r"\s+", " ", text).strip()


def build(kind: str, text: str, *, origin: dict[str, object] | None = None) -> dict:
    """The stored objection for `kind` and `text`."""
    if kind not in KINDS:
        raise ValueError(f"unknown objection kind {kind!r}")
    digest = hashlib.sha1(_flatten(text).encode()).hexdigest()[:12]
    return {"kind": kind, "signature": f"{kind}:{digest}", "text": text[:2000],
            "from": dict(origin or {})}


def goal_text(objection: dict) -> str:
    return f"{_GOALS[str(objection['kind'])]}\n\n{objection['text']}"


def spent(pr: Pr, signature: str) -> bool:
    """Whether this head already answered `signature`: a fix_request carrying
    it past `queued`, or a continuation round stamped with it."""
    req = pr.fix_request or {}
    if req.get("against_head_sha") != pr.head_sha:
        return False
    obj = req.get("objection") or {}
    if obj.get("signature") == signature and req.get("status") != "queued":
        return True
    rounds = (req.get("result") or {}).get("rounds") or []
    return any((r.get("objection") or {}).get("signature") == signature
               for r in rounds if isinstance(r, dict))


def used_today(store: Store, worker: str, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    count = 0
    for run in store.runs(since=midnight.isoformat()):
        if getattr(run, "phase", None) != "fix:single":
            continue
        stats = run.raw.get("stats") or {}
        if stats.get("host") == worker and stats.get("objection"):
            count += 1
    return count


def budget_left(store: Store, worker: str, now: datetime | None = None) -> int:
    return max(0, settings.fix_objection_budget() - used_today(store, worker, now))
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q pipeline/tests/test_objections.py && uv run pyright pipeline && uv run ruff check pipeline`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pipeline/objections.py pipeline/tests/test_objections.py
git commit -m "Add the objection shape, its signature, once-per-head, and the daily budget"
```

---

### Task 3: Gates — objection eligibility and the fix autopush bar

**Files:**
- Modify: `pipeline/gates.py:544-600` (`fix_eligibility`), `pipeline/gates.py:704-735` (`fix_huntable`), and add `fix_autopush_bar` after `resolve_autopush_bar`
- Test: `pipeline/tests/test_gates.py`

**Interfaces:**
- Produces:
  - `fix_eligibility(pr, action, changed_paths=None, *, guided=False, objection=False)`: with `objection=True` and `action == "fix"`, the profile must name `"objection"` in `autofix.fixable_gates` (message: `the active profile does not name objection in autofix.fixable_gates, so agent continuations are not enabled for this repository`); `guided` and `objection` are never both True; every other block unchanged.
  - `fix_huntable(pr, action, changed_paths=None, *, objection=False)`: with `objection=True` the review-blocker requirement for `fix` is skipped (CI passing, mergeable, and no scanner blocker still required) and eligibility is asked with `objection=True`.
  - `fix_autopush_bar(result: dict, changed_paths: list[str]) -> tuple[bool, str]`: pass requires all of: `result["review_verdict"]["verdict"] == "safe"` and not failed; `result["compile_preflight"]` is `None` or `compile_preflight_gate` ok; `risktier.pr_tier(changed_paths)` not None and `>= settings.fix_autopush_min_tier()`; changed lines in `result["patch"]` (count of lines starting with `+` or `-` excluding `+++`/`---`) `<= settings.fix_autopush_max_lines()`.

- [ ] **Step 1: Write the failing tests** (append to `pipeline/tests/test_gates.py`)

```python
class TestObjectionEligibility:
    def test_an_objection_fix_needs_the_objection_gate(self, monkeypatch):
        from pipeline import profile
        pr = _pr(1)  # the module's clean open-PR factory
        monkeypatch.setattr(profile.active().autofix, "fixable_gates", ("review",))
        ok, why = gates.fix_eligibility(pr, "fix", ["src/a.ts"], objection=True)
        assert not ok and "objection" in why
        monkeypatch.setattr(profile.active().autofix, "fixable_gates", ("objection",))
        assert gates.fix_eligibility(pr, "fix", ["src/a.ts"], objection=True)[0]

    def test_huntable_with_an_objection_skips_the_review_blocker(self, monkeypatch):
        from pipeline import profile
        monkeypatch.setattr(profile.active().autofix, "fixable_gates", ("objection",))
        pr = _pr(1)  # CI passing, mergeable, reviews at bar
        assert not gates.fix_huntable(pr, "fix", ["src/a.ts"])[0]
        assert gates.fix_huntable(pr, "fix", ["src/a.ts"], objection=True)[0]


class TestFixAutopushBar:
    def _result(self, verdict="safe", lines=10, failed=False):
        patch = "diff --git a/x.ts b/x.ts\n--- a/x.ts\n+++ b/x.ts\n" + "\n".join(
            f"+line {i}" for i in range(lines))
        return {"patch": patch, "review_verdict": {"verdict": verdict, "failed": failed, "reason": "r"},
                "compile_preflight": {"exit": 0, "base_sha": "b" * 40}}

    def test_passes_a_small_safe_compiled_change_off_tier_zero(self, monkeypatch):
        monkeypatch.setattr(gates.risktier, "pr_tier", lambda paths: 2)
        assert gates.fix_autopush_bar(self._result(), ["src/a.ts"])[0]

    def test_blocks_on_each_missing_condition(self, monkeypatch):
        monkeypatch.setattr(gates.risktier, "pr_tier", lambda paths: 2)
        assert "reviewer" in gates.fix_autopush_bar(self._result(verdict="unsafe"), ["a"])[1]
        assert "lines" in gates.fix_autopush_bar(self._result(lines=400), ["a"])[1]
        monkeypatch.setattr(gates.risktier, "pr_tier", lambda paths: 1)
        assert "tier" in gates.fix_autopush_bar(self._result(), ["a"])[1]
        r = self._result(); r["compile_preflight"] = {"exit": 20}
        monkeypatch.setattr(gates.risktier, "pr_tier", lambda paths: 2)
        assert "compile" in gates.fix_autopush_bar(r, ["a"])[1]
```
If `test_gates.py` has no `_pr` factory that yields a CI-passing, mergeable, review-at-bar open PR, build one inline with `Pr(None, {...})` following `test_autohunt._clean_merge_pr`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q pipeline/tests/test_gates.py -k "Objection or FixAutopush"`
Expected: FAIL (`unexpected keyword argument 'objection'`, `no attribute fix_autopush_bar`).

- [ ] **Step 3: Implement**

In `fix_eligibility`: add `objection: bool = False` to the signature; extend the docstring with one sentence: `objection says a machine judgment, not a person, chose this fix; it authorizes nothing by itself, so the profile must name objection in fixable_gates.` Replace the fixable-gates check:
```python
    if action == "fix" and objection and "objection" not in profile.active().autofix.fixable_gates:
        return False, ("the active profile does not name objection in autofix.fixable_gates, "
                       "so agent continuations are not enabled for this repository")
    if action == "fix" and not guided and not objection and not profile.active().autofix.fixable_gates:
        return False, ("the active profile names no autofix.fixable_gates, so "
                       "agent-authored fixes are not enabled for this repository")
```

In `fix_huntable`: add `objection: bool = False`; in the `elif action == "fix":` branch, after the scanner check, wrap the fixable-gate block in `if not objection:`; end with `return fix_eligibility(pr, action, changed_paths, objection=objection)`.

Add:
```python
def fix_autopush_bar(result: dict, changed_paths: list[str]) -> tuple[bool, str]:
    """May an authored `fix` be pushed unattended? The ONE policy over the
    request's recorded evidence, judged only when TRIAGE_FIX_AUTOPUSH names
    `fix`; an operator's manual approval never consults it.

    Pass requires all of: the refuting reviewer's affirmative `safe`; a compile
    preflight that cleared (or none configured); every touched path at or above
    TRIAGE_FIX_AUTOPUSH_MIN_TIER; and a patch within TRIAGE_FIX_AUTOPUSH_MAX_LINES
    changed lines."""
    review = result.get("review_verdict") or {}
    if review.get("failed") or review.get("verdict") != "safe":
        return False, f"the reviewer did not clear it: {review.get('reason') or 'no verdict'}"
    pf = result.get("compile_preflight")
    if pf is not None:
        ok, why = compile_preflight_gate(pf)
        if not ok:
            return False, f"the compile preflight did not clear it: {why}"
    tier = risktier.pr_tier(changed_paths)
    if tier is None:
        return False, "the touched paths are unknown, so the risk tier is too"
    if tier < settings.fix_autopush_min_tier():
        return False, (f"touched paths reach risk tier {tier}, below the unattended "
                       f"floor of {settings.fix_autopush_min_tier()}")
    lines = sum(1 for ln in str(result.get("patch") or "").splitlines()
                if (ln.startswith("+") and not ln.startswith("+++"))
                or (ln.startswith("-") and not ln.startswith("---")))
    if lines > settings.fix_autopush_max_lines():
        return False, (f"the change touches {lines} lines, past the unattended cap of "
                       f"{settings.fix_autopush_max_lines()}")
    return True, "cleared for unattended push"
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q pipeline/tests/test_gates.py && uv run pyright pipeline`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pipeline/gates.py pipeline/tests/test_gates.py
git commit -m "Gate objection fixes on the profile's objection gate and add the fix autopush bar"
```

---

### Task 4: resubmit `commit` — a follow-up commit on a kept merge

**Files:**
- Modify: `prospector_app/agent/resubmit` (add `cmd_commit`, wire the `commit` action with `-m/--message`, list it in the usage docstring)
- Test: the existing resubmit test module under `prospector_app/backend/tests/` that exercises `cmd_push` with a temp repo (find it with `grep -ln "cmd_push\|def test_push" prospector_app/backend/tests/test_resubmit*.py`); add tests beside them.

**Interfaces:**
- Produces: `resubmit <pr> commit -m <message>` → exit 0 after `git add -A && git commit -m <message>` in the kept worktree when `meta.mode == "merge"` and the tree has changes; exit 2 when not prepared or not in merge mode; exit 5 when nothing changed. Prints `committed <sha8> on the kept merge`. The worktree and meta are kept (no `_cleanup`).

- [ ] **Step 1: Write the failing test**

```python
def test_commit_adds_a_follow_up_commit_on_a_kept_merge(prepared_merge_worktree, capsys):
    pr, wt = prepared_merge_worktree  # fixture yielding a merge-mode worktree, as the push tests use
    (wt / "follow.txt").write_text("fix\n")
    assert resubmit.cmd_commit(pr, "Address the reviewer's objection") == 0
    log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=wt,
                         capture_output=True, text=True).stdout.strip()
    assert log == "Address the reviewer's objection"
    assert resubmit._read_meta(pr)["mode"] == "merge"


def test_commit_refuses_a_clean_tree_and_a_non_merge_worktree(prepared_merge_worktree,
                                                              prepared_head_worktree):
    pr, _ = prepared_merge_worktree
    assert resubmit.cmd_commit(pr, "nothing") == 5
    pr2, _ = prepared_head_worktree
    assert resubmit.cmd_commit(pr2, "nope") == 2
```
Use the module's existing fixtures for a prepared merge worktree and a prepared head worktree; if they are named differently, use those names.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q prospector_app/backend/tests/test_resubmit*.py -k commit`
Expected: FAIL with `AttributeError: cmd_commit`.

- [ ] **Step 3: Implement** (in `prospector_app/agent/resubmit`, beside `cmd_push`)

```python
def cmd_commit(pr: int, message: str) -> int:
    """Commit the kept merge worktree's outstanding edits as one follow-up
    commit on top of its merge commit, pushing nothing and keeping the tree.
    A continuation authored after a reviewer's objection lands this way, so
    the merge push that follows carries the resolution and its follow-up."""
    wt = _worktree(pr)
    meta = _read_meta(pr)
    if not meta or meta.get("mode") != "merge":
        print(f"resubmit: PR #{pr} holds no kept merge to commit onto", file=sys.stderr)
        return 2
    status = _git(["status", "--porcelain"], wt)
    if status.returncode != 0:
        print(f"resubmit: git status failed: {status.stderr.strip()}", file=sys.stderr)
        return 4
    if not status.stdout.strip():
        print(f"resubmit: no changes in {wt} — nothing to commit.", file=sys.stderr)
        return 5
    add = _git(["add", "-A"], wt)
    if add.returncode != 0:
        print(f"resubmit: git add failed: {add.stderr.strip()}", file=sys.stderr)
        return 4
    commit = _git(["commit", "-m", message], wt)
    if commit.returncode != 0:
        print(f"resubmit: commit failed: {commit.stderr.strip()}", file=sys.stderr)
        return 4
    sha = _git(["rev-parse", "HEAD"], wt).stdout.strip()
    print(f"committed {sha[:8]} on the kept merge")
    return 0
```
Wire it in the argparse block next to `push`: a `commit` subparser with required `-m/--message`, dispatching `cmd_commit(args.pr, args.message)`. Add `resubmit <pr> commit -m <message>` to the usage docstring under the merge flow.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q prospector_app/backend/tests/test_resubmit*.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add prospector_app/agent/resubmit prospector_app/backend/tests/
git commit -m "Add resubmit commit for a follow-up on a kept merge"
```

---

### Task 5: Resolve continuation in the fix worker

**Files:**
- Modify: `prospector_app/backend/fix_worker.py:1284-1350` (`_judge_claimed_resolve`), add `_continue_resolve`
- Test: `prospector_app/backend/tests/test_fix_worker.py`

**Interfaces:**
- Consumes: `objections.build/goal_text/spent/budget_left`, `gates.fix_eligibility(..., objection=True)`, `author_fix.author`, `review_resolve.review`, `_related_tests_run`, `_resubmit(n, "commit", "-m", msg)`.
- Produces: `_continue_resolve(n: int, rec: Pr, claimed: dict, head: str, worktree: str, result: dict, stamp: dict, objection: dict) -> None`. On success it writes `approved`/`awaiting-review` with `result["auto_review"]` = the second round and `result["rounds"] = [first_round_stamp_with_objection, second_round]`; every ending stamps `objection` on the request and passes `objection=objection["signature"]` to `_log_run`.

Behavior of `_judge_claimed_resolve` after a judged rejection (`judged_rejection` True): if `"objection" in profile.active().autofix.fixable_gates`, `not objections.spent(rec, sig)`, and `objections.budget_left(store, worker) > 0`, call `_continue_resolve`; otherwise the existing park path.

`_continue_resolve`:
1. `objection = objections.build("resolve-review", f"{lens}-lens: {reason}\n" + "\n".join(concerns), origin={"lens": lens})`; `first = {**stamp, "objection": objection}`.
2. `_running_step(n, claimed, "continuing after the reviewer's objection")`.
3. Author: `author_fix.author(worktree, pr=n, title=rec.title or "", body=rec.body or "", goal=objections.goal_text(objection), findings=[{"path": p, "note": c} for c in concerns for p in paths[:1]] or [], ci_failures=[], diff_path=str(verify_driver.fetch_patch(n, rec.head_sha)), head_sha=rec.head_sha)`. `AgentUnavailable` → `_fail(kind="agent-unavailable")` after `_resubmit(n,"abort")`; other `RuntimeError/ValueError` → park with the first round only (`_park_resolve_rounds`); `give_up` → park with rounds `[first]` and detail `the continuation agent declined: <reason>`.
4. `diff = _resubmit(n, "diff")`; new patch; `author_fix.assert_disclosed(verdict["changes"], diffpaths.changed_paths(patch))` (ValueError → park); `recheck_eligibility(n, "resolve", paths ∪ conflict_paths)` (fail → park with the reason).
5. `_resubmit(n, "commit", "-m", verdict["summary"] or "Address the reviewer's objection")`; non-zero → `_fail`.
6. Second round: run both lenses again with the new `patch`, `related tests`, exactly as `_judge_claimed_resolve` does, into `second = {..., "objection": objection}`; then `result["rounds"] = [first, second]; result["auto_review"] = second; ok, why = gates.resolve_autopush_bar(result)`; write `approved` or `awaiting-review` with `objection=objection`; `_log_run(..., objection=sig)` is called from the endings only (approved is not an ending; the eventual push or cancel logs).

Add `objection: str | None = None` to `_log_run` (`stats["objection"] = objection` when set) and to `_fail`, `_refuse`, `_cancel`, `_park`, `_finish_pushed`: each reads `req.get("objection")` and passes `objection=(req.get("objection") or {}).get("signature")` through, and forwards `objection=req.get("objection")` into `record_fix_request` so the field survives every transition.

- [ ] **Step 1: Write the failing tests**

```python
class TestResolveContinuation:
    """A reviewer's judged rejection becomes the author agent's goal inside the
    kept merge worktree; the result is re-reviewed under the same bar."""

    def _parked(self, store, tmp_path):
        from pipeline import settings
        wt = tmp_path / "wt"; wt.mkdir()
        store.edit_pr(1).record_fix_request(
            "awaiting-review", "resolve", queued_at="2026-09-14T00:00:00+00:00",
            source="auto", host=settings.worker_id(), head_sha=HEAD,
            result={"conflict_paths": ["one.txt"], "merge_diff": "diff --cc one.txt",
                    "resolutions": [{"path": "one.txt", "rationale": "kept both"}]})
        data.refresh()
        return wt

    def _fakes(self, monkeypatch, wt, verdicts):
        """Reviews answer from `verdicts` in call order; the author writes one file."""
        from pipeline import profile
        monkeypatch.setattr(profile.active().autofix, "fixable_gates", ("objection",))
        calls = {"author": 0, "review": 0, "resubmit": []}
        seq = iter(verdicts)

        def review(worktree, **kw):
            calls["review"] += 1
            return next(seq)
        monkeypatch.setattr(fix_worker.review_resolve, "review", review)

        def author(worktree, **kw):
            calls["author"] += 1
            calls["goal"] = kw["goal"]
            return {"summary": "Keep the base's deletion", "changes": [{"path": "one.txt", "note": "n"}]}
        monkeypatch.setattr(fix_worker.author_fix, "author", author)
        monkeypatch.setattr(fix_worker.author_fix, "assert_disclosed", lambda changes, paths: None)
        monkeypatch.setattr(fix_worker.verify_driver, "fetch_patch", lambda pr, head: wt / "pr.patch")
        monkeypatch.setattr(fix_worker, "_related_tests_run", lambda *a, **k: None)
        monkeypatch.setattr(fix_worker.risktier, "tier_facet", lambda paths: {"tier": 2, "pinned_by": []})
        monkeypatch.setattr(fix_worker.resolve_evidence, "history", lambda wt, paths: "")
        monkeypatch.setattr(fix_worker.resolve_evidence, "store_context", lambda rec: "")
        monkeypatch.setattr(fix_worker.resolve_evidence, "related_tests", lambda wt, paths: [])

        def resubmit(n, *args, stdin=None):
            calls["resubmit"].append(args)
            out = ""
            if args[0] == "state":
                out = json.dumps({"phase": "ready", "mode": "merge", "conflicts": [],
                                  "worktree": str(wt), "base_branch": "master"})
            elif args[0] == "diff":
                out = "diff --git a/one.txt b/one.txt\n+resolved"
            return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()
        monkeypatch.setattr(fix_worker, "_resubmit", resubmit)
        return calls

    def test_a_rejection_is_continued_and_re_reviewed(self, store, monkeypatch, tmp_path):
        wt = self._parked(store, tmp_path)
        unsafe = {"verdict": "unsafe", "reason": "drops the base's deletion", "concerns": ["c1"]}
        safe = {"verdict": "safe", "reason": "ok", "concerns": []}
        calls = self._fakes(monkeypatch, wt, [unsafe, safe, safe])
        fix_worker.review_parked_resolve(1)
        req = store.load_pr(1).fix_request
        assert req["status"] == "approved"
        assert calls["author"] == 1 and "drops the base's deletion" in calls["goal"]
        assert ("commit", "-m", "Keep the base's deletion") in calls["resubmit"]
        assert [r["objection"]["kind"] for r in req["result"]["rounds"]] == ["resolve-review", "resolve-review"]
        assert req["objection"]["signature"].startswith("resolve-review:")

    def test_a_second_rejection_parks_with_both_rounds(self, store, monkeypatch, tmp_path):
        wt = self._parked(store, tmp_path)
        unsafe = {"verdict": "unsafe", "reason": "still wrong", "concerns": []}
        self._fakes(monkeypatch, wt, [unsafe, unsafe])
        fix_worker.review_parked_resolve(1)
        req = store.load_pr(1).fix_request
        assert req["status"] == "awaiting-review"
        assert len(req["result"]["rounds"]) == 2
        assert not req["result"]["auto_review"]["bar"]["ok"]

    def test_without_the_gate_the_rejection_parks_as_before(self, store, monkeypatch, tmp_path):
        from pipeline import profile
        wt = self._parked(store, tmp_path)
        calls = self._fakes(monkeypatch, wt, [{"verdict": "unsafe", "reason": "r", "concerns": []}])
        monkeypatch.setattr(profile.active().autofix, "fixable_gates", ("review",))
        fix_worker.review_parked_resolve(1)
        assert store.load_pr(1).fix_request["status"] == "awaiting-review"
        assert calls["author"] == 0

    def test_an_exhausted_budget_parks_as_before(self, store, monkeypatch, tmp_path):
        wt = self._parked(store, tmp_path)
        calls = self._fakes(monkeypatch, wt, [{"verdict": "unsafe", "reason": "r", "concerns": []}])
        monkeypatch.setattr(fix_worker.objections, "budget_left", lambda st, w, now=None: 0)
        fix_worker.review_parked_resolve(1)
        assert calls["author"] == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q prospector_app/backend/tests/test_fix_worker.py -k ResolveContinuation`
Expected: FAIL (`author == 0`, no `rounds`).

- [ ] **Step 3: Implement** in `fix_worker.py`

Add `from pipeline import objections` to the pipeline import tuple. Thread `objection` through the ending writers as described in Interfaces. Replace the end of `_judge_claimed_resolve` (from `judged_rejection = ...`) with:

```python
        judged_rejection = next((r for r in reviews
                                 if r.get("verdict") != "safe" and not r.get("failed")), None)
        if any(r.get("failed") for r in reviews) and judged_rejection is None:
            restore("a reviewer failed as a machine, with no judged rejection "
                    "beside it")
            return
        stamp["reviews"] = reviews
        if judged_rejection is not None:
            objection = objections.build(
                "resolve-review",
                f"{judged_rejection.get('lens')}-lens: {judged_rejection.get('reason')}\n"
                + "\n".join(str(c) for c in judged_rejection.get("concerns") or []),
                origin={"lens": judged_rejection.get("lens")})
            if _may_continue(rec, objection):
                _continue_resolve(n, rec, claimed, head, worktree, result, stamp, objection)
                return
        # (existing: tests run, stale check) …
```
and add:
```python
def _may_continue(rec: Pr, objection: dict) -> bool:
    """Whether a continuation may start: the profile opted in, this head has
    not answered this objection, and today's budget is not spent."""
    if "objection" not in profile.active().autofix.fixable_gates:
        return False
    if objections.spent(rec, objection["signature"]):
        return False
    return objections.budget_left(data.store(), settings.worker_id()) > 0


def _continue_resolve(n: int, rec: Pr, claimed: dict, head: str, worktree: str,
                      result: dict, stamp: dict, objection: dict) -> None:
    """Author the reviewer's objection inside the kept merge worktree, commit
    it on top of the merge, and judge the combined change under the resolve
    bar again. Both rounds are kept on the request."""
    first = {**stamp, "objection": objection}
    paths = [str(p) for p in (result.get("conflict_paths") or [])]
    claimed = {**claimed, "objection": objection}
    _running_step(n, claimed, "continuing after the reviewer's objection")

    def park(detail: str, extra: dict | None = None) -> None:
        res = {**result, **(extra or {}), "rounds": [first],
               "auto_review": {**first, "bar": {"ok": False, "reason": detail}}}
        data.store().edit_pr(n).record_fix_request(
            "awaiting-review", "resolve", queued_at=claimed.get("queued_at"),
            started_at=claimed.get("started_at"), result=res, source=claimed.get("source"),
            host=settings.worker_id(), base_sha=claimed.get("base_sha"), head_sha=head,
            objection=objection)
        data.refresh()
        _log_run(n, claimed, "awaiting-review", detail, objection=objection["signature"])

    try:
        pr_patch = verify_driver.fetch_patch(n, rec.head_sha or "")
        verdict = author_fix.author(
            worktree, pr=n, title=rec.title or "", body=rec.body or "",
            goal=objections.goal_text(objection),
            findings=[{"path": p, "note": str(c)} for c in (objection.get("from") or {}).get("concerns", [])
                      for p in paths[:1]],
            ci_failures=[], diff_path=str(pr_patch), head_sha=rec.head_sha or "")
    except headless_agent.AgentUnavailable as e:
        _resubmit(n, "abort")
        _fail(n, claimed, f"The agent could not run on {settings.worker_id()}: {e}",
              kind="agent-unavailable")
        return
    except (RuntimeError, ValueError, verify_driver.FetchFailure) as e:
        park(f"the continuation did not land: {e}")
        return
    if "give_up" in verdict:
        park(f"the continuation agent declined: {verdict['give_up']}")
        return
    diff = _resubmit(n, "diff")
    patch = (diff.stdout or "").strip()
    if diff.returncode != 0 or not patch.startswith("diff "):
        park("the continued worktree's diff could not be read")
        return
    touched = diffpaths.changed_paths(patch)
    try:
        author_fix.assert_disclosed(verdict["changes"], touched)
    except ValueError as e:
        park(f"the continuation was not trusted: {e}")
        return
    ok, why = recheck_eligibility(n, "resolve", sorted(set(touched) | set(paths)))
    if not ok:
        park(f"the continuation is not one the bot may push: {why}")
        return
    committed = _resubmit(n, "commit", "-m", verdict["summary"] or "Address the reviewer's objection")
    if committed.returncode != 0:
        _fail(n, claimed, f"committing the continuation failed: "
                          f"{(committed.stderr or committed.stdout).strip()[:500]}")
        return
    second: dict = {"against_head_sha": head, "base_sha": claimed.get("base_sha"),
                    "host": settings.worker_id(), "at": _now(),
                    "tier": risktier.tier_facet(sorted(set(touched) | set(paths))),
                    "reviews": [], "tests": None, "objection": objection}
    history = resolve_evidence.history(worktree, paths)
    context = resolve_evidence.store_context(rec)
    related = resolve_evidence.related_tests(worktree, paths)
    for lens in ("behavior", "history"):
        v = review_resolve.review(worktree, pr=n, title=rec.title or "",
                                  merge_diff=str(result.get("merge_diff") or ""), patch=patch,
                                  resolutions=list(result.get("resolutions") or []),
                                  history=history, store_context=context, lens=lens)
        second["reviews"].append({"lens": lens, **v})
        if v.get("verdict") != "safe" and not v.get("failed"):
            break
    if all(r.get("verdict") == "safe" for r in second["reviews"]) and len(second["reviews"]) == 2:
        second["tests"] = _related_tests_run(n, head, patch, related)
    res = {**result, "rounds": [first, second], "auto_review": second}
    ok, why = gates.resolve_autopush_bar(res)
    second["bar"] = {"ok": ok, "reason": why}
    data.store().edit_pr(n).record_fix_request(
        "approved" if ok else "awaiting-review", "resolve",
        queued_at=claimed.get("queued_at"), started_at=claimed.get("started_at"),
        result=res, source=claimed.get("source"), host=settings.worker_id(),
        base_sha=claimed.get("base_sha"), head_sha=head, objection=objection)
    data.refresh()
    if not ok:
        _log_run(n, claimed, "awaiting-review", why, objection=objection["signature"])
    print(f"[fix-worker] resolve continuation for PR #{n}: "
          f"{'cleared for push' if ok else why}", flush=True)
```
Also: `_park` and `push_approved`'s resolve path already keep the worktree; nothing else changes for the push.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q -n auto prospector_app/backend/tests/test_fix_worker.py prospector_app/backend/tests/test_fix_queue.py && uv run pyright prospector_app/backend && uv run ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add prospector_app/backend/fix_worker.py prospector_app/backend/tests/test_fix_worker.py
git commit -m "Continue a rejected resolve from the reviewer's objection and re-judge it"
```

---

### Task 6: Fix retry from the fix reviewer's objection

**Files:**
- Modify: `prospector_app/backend/fix_worker.py:856-900` (`_author_fix`, the review block)
- Test: `prospector_app/backend/tests/test_fix_worker.py`

**Interfaces:**
- Consumes: `objections`, `_may_continue`, `review_fix.review`, `author_fix.author`.
- Produces: one retry inside `_author_fix`: on `review["verdict"] != "safe"` (not failed) and `_may_continue(rec, objection)`, re-run the author in the same worktree with `goal = goal + "\n\n" + objections.goal_text(objection)`, re-diff, re-disclose, re-gate, re-review; a second rejection refuses with both reasons; every ending carries `objection`.

- [ ] **Step 1: Write the failing tests**

```python
class TestFixRetry:
    def _setup(self, store, monkeypatch, verdicts):
        from pipeline import profile
        monkeypatch.setattr(profile.active().autofix, "fixable_gates", ("review", "objection"))
        seq = iter(verdicts); calls = {"author": [], "review": 0}
        monkeypatch.setattr(fix_worker.author_fix, "author",
                            lambda wt, **kw: calls["author"].append(kw["goal"]) or
                            {"summary": "s", "changes": [{"path": "a.ts", "note": "n"}]})
        monkeypatch.setattr(fix_worker.author_fix, "assert_disclosed", lambda c, p: None)

        def review(wt, patch, **kw):
            calls["review"] += 1
            return next(seq)
        monkeypatch.setattr(fix_worker.review_fix, "review", review)
        monkeypatch.setattr(fix_worker, "_prepared_worktree", lambda n: "/tmp/wt")
        monkeypatch.setattr(fix_worker, "_resubmit", _Probe(rc=0, stdout="diff --git a/a.ts b/a.ts\n+x"))
        rec = store.load_pr(1).raw
        rec["signals"].update({"ci": "passing", "mergeable": True})
        store.save_pr(rec); data.refresh()
        fix_queue.queue_pr(1, "fix", guidance="tighten the null check")
        return calls

    def test_a_rejected_fix_is_retried_once_with_the_objection(self, store, monkeypatch):
        calls = self._setup(store, monkeypatch, [
            {"verdict": "unsafe", "reason": "it removed the guard", "concerns": []},
            {"verdict": "safe", "reason": "ok", "concerns": []}])
        fix_worker.run_one(1)
        req = store.load_pr(1).fix_request
        assert req["status"] == "awaiting-review"
        assert len(calls["author"]) == 2 and "it removed the guard" in calls["author"][1]
        assert req["objection"]["kind"] == "fix-review"

    def test_two_rejections_refuse_with_both_reasons(self, store, monkeypatch):
        self._setup(store, monkeypatch, [
            {"verdict": "unsafe", "reason": "first", "concerns": []},
            {"verdict": "unsafe", "reason": "second", "concerns": []}])
        fix_worker.run_one(1)
        req = store.load_pr(1).fix_request
        assert req["status"] == "refused"
        assert "first" in req["refused_reason"] and "second" in req["refused_reason"]
```
`_Probe` is the module's scripted resubmit fake.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q prospector_app/backend/tests/test_fix_worker.py -k FixRetry`
Expected: FAIL (`author` called once; status refused on the first test).

- [ ] **Step 3: Implement**

Extract the body from `_running_step(n, claimed, "agent authoring the fix", action="fix")` through the review verdict into a helper `_author_and_review(n, claimed, rec, worktree, goal, findings, checks, review_summary, pr_patch) -> tuple[dict, str, dict] | None` returning `(verdict, patch, review)` or `None` after writing an ending. Then in `_author_fix`:
```python
    authored = _author_and_review(n, claimed, rec, worktree, goal, findings, checks,
                                  review_summary, pr_patch)
    if authored is None:
        return
    verdict, patch, review = authored
    if review["verdict"] != "safe":
        objection = objections.build("fix-review", str(review.get("reason") or ""),
                                     origin={"concerns": review.get("concerns") or []})
        if _may_continue(rec, objection):
            claimed = {**claimed, "objection": objection}
            _running_step(n, claimed, "retrying after the reviewer's objection", action="fix")
            retried = _author_and_review(n, claimed, rec, worktree,
                                         goal + "\n\n" + objections.goal_text(objection),
                                         findings, checks, review_summary, pr_patch)
            if retried is None:
                return
            verdict2, patch2, review2 = retried
            if review2["verdict"] != "safe":
                _resubmit(n, "abort")
                _refuse(n, claimed, "The reviewing agent rejected the change twice: "
                                    f"{review['reason']}; then {review2['reason']}",
                        result={"patch": patch2, "changes": verdict2["changes"],
                                "review_verdict": review2, "rounds": [review, review2]})
                return
            verdict, patch, review = verdict2, patch2, review2
        else:
            _resubmit(n, "abort")
            _refuse(n, claimed, f"The reviewing agent rejected the change: "
                                f"{review['reason']}",
                    result={"patch": patch, "changes": verdict["changes"],
                            "review_verdict": review})
            return
```
Inside `_author_and_review`, a `review["failed"]` still ends `_fail` and returns None. Continue with the compile preflight as today; then the autopush decision becomes:
```python
    result = {**evidence, "compile_preflight": pf,
              "message": verdict["summary"] or _commit_message("fix")}
    if "fix" in settings.fix_autopush():
        ok, why = gates.fix_autopush_bar(result, paths)
        result["autopush_bar"] = {"ok": ok, "reason": why}
        if ok:
            _push(n, claimed, "fix", result)
            return
    _park(n, claimed, "fix", result, settings.worker_id())
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q -n auto prospector_app/backend/tests/test_fix_worker.py && uv run pyright prospector_app/backend`
Expected: PASS, including the existing parked-fix tests (autopush unset in their fixture).

- [ ] **Step 5: Commit**

```bash
git add prospector_app/backend/fix_worker.py prospector_app/backend/tests/test_fix_worker.py
git commit -m "Retry a rejected fix once from the reviewer's objection, and push a fix only past the autopush bar"
```

---

### Task 7: Compile objections — a resolve continues in place, an update queues a fix

**Files:**
- Modify: `prospector_app/backend/fix_worker.py` (`run_one` mechanical path around the preflight, `_end_on_preflight`)
- Modify: `prospector_app/backend/fix_queue.py:32-58` (`queue_pr` gains `objection: dict | None = None`)
- Test: `prospector_app/backend/tests/test_fix_worker.py`, `prospector_app/backend/tests/test_fix_queue.py`

**Interfaces:**
- Produces: `fix_queue.queue_pr(n, action, source=None, guidance=None, objection=None)`; with `objection` set, `source` is forced to `"objection"`, eligibility is asked with `objection=True`, and the request stores `objection`. In `run_one`, after a mechanical action's preflight fails with `exit == SENTINEL_TEST_FAIL` and no `error` (the base passes): for a hunted `update`/`rebase`, build `objections.build("compile", pf["error_excerpt"])`; if `_may_continue(rec, objection)`, end the mechanical request `refused` as today and then `fix_queue.queue_pr(n, "fix", objection=objection)`; the refusal detail gains ` A fix has been queued from the compile error.` For a resolve (`_agent_resolve` path where the preflight runs on the kept worktree), the same objection runs `_continue_resolve`-style authoring against the compile excerpt; implement by calling a shared `_continue_resolve` with `stamp = {"reviews": [], "tests": None, "tier": ...}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_queue_pr_with_an_objection_is_an_objection_source(store, monkeypatch):
    from pipeline import objections, profile
    monkeypatch.setattr(profile.active().autofix, "fixable_gates", ("objection",))
    obj = objections.build("compile", "error TS2322")
    out = fix_queue.queue_pr(1, "fix", objection=obj)
    req = store.load_pr(1).fix_request
    assert out["status"] == "queued" and req["source"] == "objection"
    assert req["objection"] == obj


def test_queue_pr_with_an_objection_needs_the_objection_gate(store, monkeypatch):
    from pipeline import objections, profile
    monkeypatch.setattr(profile.active().autofix, "fixable_gates", ("review",))
    with pytest.raises(ValueError, match="objection"):
        fix_queue.queue_pr(1, "fix", objection=objections.build("compile", "e"))
```
and in `test_fix_worker.py`:
```python
def test_a_hunted_update_that_fails_to_compile_queues_a_compile_fix(store, monkeypatch):
    from pipeline import profile
    monkeypatch.setattr(profile.active().autofix, "fixable_gates", ("objection",))
    fix_queue.queue_pr(1, "update", source="auto")
    monkeypatch.setattr(fix_worker, "_resubmit", _Probe(rc=0))
    monkeypatch.setattr(fix_worker, "_preflight",
                        lambda n, patch: {"exit": 20, "error_excerpt": "error TS2322: x", "base_sha": "b" * 40})
    fix_worker.run_one(1)
    req = store.load_pr(1).fix_request
    assert req["status"] == "queued" and req["action"] == "fix"
    assert req["source"] == "objection" and req["objection"]["kind"] == "compile"
    endings = [r for r in store.runs() if getattr(r, "phase", "") == "fix:single"]
    assert endings[-1].raw["stats"]["status"] == "refused"
    assert "A fix has been queued" in endings[-1].raw["stats"]["detail"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q prospector_app/backend/tests/test_fix_queue.py prospector_app/backend/tests/test_fix_worker.py -k "objection or compile_fix"`
Expected: FAIL (`unexpected keyword argument 'objection'`).

- [ ] **Step 3: Implement**

`fix_queue.queue_pr`:
```python
def queue_pr(n: int, action: str, source: str | None = None,
             guidance: str | None = None, objection: dict | None = None) -> dict:
    ...
    guidance = (guidance or "").strip() or None
    if objection is not None:
        source = "objection"
    rec = data.store().load_pr(n)
    ...
    ok, why = gates.fix_eligibility(rec, action, service.changed_paths(rec),
                                    guided=guidance is not None, objection=objection is not None)
    ...
    rec.record_fix_request("queued", action, queued_at=_now(), source=source,
                           guidance=guidance, objection=objection, head_sha=rec.head_sha)
```
Docstring addition: `objection is a machine judgment the fix answers; it forces source "objection" and asks eligibility with the objection gate.`

`fix_worker.run_one`, replacing the preflight failure branch:
```python
            if not pf_ok:
                result = {"patch": patch[-TAIL_CHARS:], "compile_preflight": pf, "detail": pf_why}
                followup = _compile_objection(n, claimed, pf)
                if followup is not None:
                    result["detail"] = f"{pf_why} A fix has been queued from the compile error."
                _end_on_preflight(n, claimed, pf, result)
                if followup is not None:
                    fix_queue.queue_pr(n, "fix", objection=followup)
                return
```
with:
```python
def _compile_objection(n: int, claimed: dict, pf: dict) -> dict | None:
    """The compile objection a hunted mechanical action's failed preflight
    hands to a follow-up fix, or None when the failure is the machine's, the
    request is an operator's, or the continuation may not start."""
    if claimed.get("source") != "auto" or pf.get("error") or pf.get("exit") != gates.SENTINEL_TEST_FAIL:
        return None
    rec = data.store().load_pr(n)
    if rec is None:
        return None
    objection = objections.build("compile", str(pf.get("error_excerpt") or "the compile command failed"))
    return objection if _may_continue(rec, objection) else None
```
`_end_on_preflight` uses `result["detail"]` when present as the refusal text: change its `_refuse(n, claimed, plain_preflight(pf), result=result)` to `_refuse(n, claimed, str(result.get("detail") or plain_preflight(pf)), result=result)`.

For the resolve path: in `_agent_resolve`, where the compile preflight on the resolved worktree fails with exit 20 and no error, build the same objection and, when `_may_continue`, call `_continue_resolve(n, rec, claimed, head, worktree, result, {"reviews": [], "tests": None, "tier": risktier.tier_facet(conflicts), "against_head_sha": head, "host": settings.worker_id(), "at": _now()}, objection)` instead of `_end_on_preflight`. (Locate the preflight call in `_agent_resolve` with `grep -n "_preflight(n" prospector_app/backend/fix_worker.py`.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q -n auto prospector_app/backend/tests && uv run pyright prospector_app/backend`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add prospector_app/backend/fix_worker.py prospector_app/backend/fix_queue.py prospector_app/backend/tests
git commit -m "Hand a compile failure the base passes to a follow-up fix"
```

---

### Task 8: Security continuation lane

**Files:**
- Modify: `prospector_app/backend/fix_worker.py:1551-1578` (`auto_fixable`), `_fix_goal`
- Test: `prospector_app/backend/tests/test_fix_worker.py`

**Interfaces:**
- Consumes: `settings.fix_hunt_security()`, `gates.security_cleared`, `Pr.security_verdict`, `pr.section("security")["findings"]`, `gates.fix_huntable(..., objection=True)`.
- Produces: `auto_fixable` returns `"fix"` for a PR with a current YELLOW verdict, CI passing, mergeable, when the flag is on and the objection is unspent; `next_auto` queues it with `fix_queue.queue_pr(n, "fix", objection=obj)` (the hunter's queue call must pass the objection: change `next_auto` to return `(action, n, objection | None)` and the drain loop to pass it). `_fix_goal` uses `objections.goal_text(claimed["objection"])` as the goal and `[]` findings when `claimed.get("objection")` is set.

- [ ] **Step 1: Write the failing tests**

```python
class TestSecurityLane:
    def _yellow(self, store):
        rec = store.load_pr(1).raw
        rec["signals"].update({"ci": "passing", "mergeable": True})
        rec["security"] = {"verdict": "YELLOW", "checked_at": _now(), "against_head_sha": HEAD,
                           "findings": [{"severity": "yellow", "title": "unbounded retry",
                                         "detail": "the loop never gives up"}]}
        store.save_pr(rec); data.refresh()

    def test_a_yellow_pr_is_hunted_as_an_objection_fix(self, store, monkeypatch):
        from pipeline import profile
        monkeypatch.setenv("TRIAGE_FIX_HUNT_SECURITY", "1")
        monkeypatch.setenv("TRIAGE_FIX_HUNT_FIX", "1")
        monkeypatch.setattr(profile.active().autofix, "fixable_gates", ("objection",))
        self._yellow(store)
        pick = fix_worker.next_auto()
        assert pick is not None and pick[0] == "fix" and pick[2]["kind"] == "security"
        assert "unbounded retry" in pick[2]["text"]

    def test_the_lane_is_off_without_its_flag(self, store, monkeypatch):
        from pipeline import profile
        monkeypatch.delenv("TRIAGE_FIX_HUNT_SECURITY", raising=False)
        monkeypatch.setenv("TRIAGE_FIX_HUNT_FIX", "1")
        monkeypatch.setattr(profile.active().autofix, "fixable_gates", ("objection",))
        self._yellow(store)
        assert fix_worker.next_auto() is None

    def test_the_goal_is_the_finding(self, store, monkeypatch):
        from pipeline import objections
        obj = objections.build("security", "unbounded retry: the loop never gives up")
        brief = fix_worker._fix_goal(store.load_pr(1), {"objection": obj})
        assert "security review flagged" in brief.goal and brief.findings == []
```
Update existing callers/tests of `next_auto()` in `test_fix_worker.py` from `== ("rebase", 1)` to `== ("rebase", 1, None)`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q prospector_app/backend/tests/test_fix_worker.py -k SecurityLane`
Expected: FAIL.

- [ ] **Step 3: Implement**

`auto_fixable` returns `tuple[str, dict | None] | None`:
```python
    if (pr.fix_request or {}).get("status") in fix_queue.IN_FLIGHT:
        return None
    objection: dict | None = None
    if pr.mergeable is False:
        action = "rebase"
    elif pr.drift_state == "conflicts":
        action = "update"
    elif settings.fix_hunt_fix() and settings.fix_hunt_security() and _yellow_objection(pr) is not None:
        action, objection = "fix", _yellow_objection(pr)
    elif settings.fix_hunt_fix():
        action = "describe" if describe_pr.only_description_nits(pr) else "fix"
    else:
        return None
    if objection is not None:
        if objections.spent(pr, objection["signature"]):
            return None
    elif _hunt_attempted(pr, action):
        return None
    ok, _ = gates.fix_huntable(pr, action, service.changed_paths(pr), objection=objection is not None)
    return (action, objection) if ok else None


def _yellow_objection(pr: Pr) -> dict | None:
    """The security objection for a PR whose current verdict is YELLOW: every
    confirmed finding's title and detail, or None."""
    if pr.security_verdict != "YELLOW" or not is_current(pr, "security", max_age_days=gates.SECURITY_MAX_AGE_DAYS):
        return None
    findings = [f for f in ((pr.section("security") or {}).get("findings") or []) if isinstance(f, dict)]
    if not findings:
        return None
    text = "\n".join(f"- {f.get('title')}: {f.get('detail')}" for f in findings)
    return objections.build("security", text, origin={"findings": len(findings)})
```
(`is_current` import: `from pipeline.freshness import is_current`.) Adjust `next_auto` to carry the objection: `best[lane] = (key, action, n, objection)`, return `(action, n, objection)`; the drain loop: `action, n, objection = pick; fix_queue.queue_pr(n, action, source="auto", objection=objection)` (with `objection` set, `queue_pr` records source `objection`). `_fix_goal`: at the top, `if claimed.get("objection"): return _FixBrief(objections.goal_text(claimed["objection"]), [], [], "")`. In `_author_fix`, the guard `if not (claimed.get("guidance") or findings or checks or review_summary)` gains `or claimed.get("objection")`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q -n auto prospector_app/backend/tests && uv run pyright prospector_app/backend && uv run ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add prospector_app/backend/fix_worker.py prospector_app/backend/tests/test_fix_worker.py
git commit -m "Hunt a YELLOW security finding as an objection fix behind its own flag"
```

---

### Task 9: Surfaces — queue rows, ledger, heartbeat budget, frontend

**Files:**
- Modify: `prospector_app/backend/fix_queue.py:219-245` (`_entry`), `prospector_app/backend/fix_worker.py:196-201` (`beat`)
- Modify: `prospector_app/frontend/src/api.ts:713-737`, `prospector_app/frontend/src/views/ControlPanel.tsx:925-1010` (fix queue rows)
- Test: `prospector_app/backend/tests/test_fix_queue.py`

**Interfaces:**
- Produces: `FixQueueEntry` gains `objection: {kind: str, text: str} | null` and `rounds: int` (length of `result.rounds`, 0 when absent); `source` may be `"objection"`. Heartbeat gains `objection_budget: {"used": int, "limit": int}`. Frontend renders a `🔁 continued after review` chip with the objection text as title when `rounds > 0`, a `from objection` source chip when `source === "objection"`, and the budget on the fix runner line.

- [ ] **Step 1: Write the failing test**

```python
def test_queue_entry_carries_the_objection_and_round_count(store):
    from pipeline import objections
    obj = objections.build("resolve-review", "drops the deletion")
    store.edit_pr(1).record_fix_request(
        "awaiting-review", "resolve", source="auto", host="w", head_sha="a" * 40, objection=obj,
        result={"conflict_paths": ["a.ts"], "rounds": [{"objection": obj}, {"objection": obj}],
                "auto_review": {"bar": {"ok": False, "reason": "r"}}})
    data.refresh()
    entry = [e for e in fix_queue.queue_entries() if e["pr"] == 1][0]
    assert entry["objection"] == {"kind": "resolve-review", "text": "drops the deletion"}
    assert entry["rounds"] == 2


def test_beat_carries_the_objection_budget(store, monkeypatch):
    from pipeline import settings
    monkeypatch.setenv("TRIAGE_FIX_OBJECTION_BUDGET", "20")
    fix_worker.beat()
    rec = store.load_fix_worker()["hosts"][settings.worker_id()]
    assert rec["objection_budget"] == {"used": 0, "limit": 20}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q prospector_app/backend/tests/test_fix_queue.py -k "objection or budget"`
Expected: FAIL (`KeyError: 'objection'`).

- [ ] **Step 3: Implement**

`fix_queue._entry`: add
```python
        "objection": ({"kind": str(obj.get("kind")), "text": str(obj.get("text") or "")[:400]}
                      if (obj := req.get("objection")) else None),
        "rounds": len((req.get("result") or {}).get("rounds") or []),
```
and extend the `FixQueueEntry` TypedDict with `objection: dict | None` and `rounds: int`. `source` typing widens to `str | None` if it is a Literal.

`fix_worker.beat`:
```python
    st = data.store()
    st.save_fix_worker({
        "host": settings.worker_id(), "pid": os.getpid(),
        "last_beat": _now(), "current_pr": state["current_pr"],
        "autohunt": enabled_autohunt(),
        "objection_budget": {"used": objections.used_today(st, settings.worker_id()),
                             "limit": settings.fix_objection_budget()}})
```

`api.ts` `FixQueueEntry`: `source?: "operator" | "auto" | "objection" | null;`, add
```ts
  /** The machine judgment a continuation authored from, when this request is one. */
  objection?: { kind: string; text: string } | null;
  /** How many author-and-review rounds the request has been through. */
  rounds: number;
```
`FixRunner` (find `interface FixRunner`): add `objection_budget?: { used: number; limit: number } | null;` and have `fix_queue.runner_status` copy `objection_budget` from the freshest heartbeat.

`ControlPanel.tsx` fix queue row: beside the existing `auto_review` chip add
```tsx
{e.rounds > 0 && (
  <span className="chip chip-blue sm" title={e.objection?.text ?? undefined}>
    🔁 continued after review ×{e.rounds}
  </span>
)}
```
and render the source chip as `{e.source === "objection" ? "from objection" : e.source === "auto" ? "auto" : "manual"}`. On the fix runner status line, when `fixq.runner.objection_budget` exists, append `· continuations today {used}/{limit}`.

- [ ] **Step 4: Run tests and the frontend gate**

Run: `uv run pytest -q -n auto prospector_app/backend/tests/test_fix_queue.py && cd prospector_app/frontend && pnpm run build && pnpm exec eslint src/api.ts src/views/ControlPanel.tsx`
Expected: PASS, build ok, no new lint errors.

- [ ] **Step 5: Commit**

```bash
git add prospector_app/backend/fix_queue.py prospector_app/backend/fix_worker.py prospector_app/frontend/src/api.ts prospector_app/frontend/src/views/ControlPanel.tsx prospector_app/backend/tests/test_fix_queue.py
git commit -m "Show continuations, their objection, and today's budget in the fix queue"
```

---

### Task 10: Docs and deployment configuration

**Files:**
- Modify: `CLAUDE.md` (AUTOFIX paragraph), `.env.example` (after `TRIAGE_FIX_HUNT_LIMIT`), `profile.example.json` (`fixable_gates` gains `"objection"`)
- Deployment (not tracked, done by hand on the worker machine): the e2e checkout's `profile.json` `autofix.fixable_gates` gains `"objection"`; its `.env` gains `TRIAGE_FIX_HUNT_SECURITY=1`, `TRIAGE_FIX_OBJECTION_BUDGET=20`, and `fix` is appended to `TRIAGE_FIX_AUTOPUSH`.

- [ ] **Step 1: CLAUDE.md** — add to the AUTOFIX paragraph, after the `describe` sentence:

> An **objection** (`pipeline/objections.py`) is a machine judgment that names a defect an agent may fix: a resolve reviewer's judged rejection, a compile excerpt the pristine base passes, the fix reviewer's rejection, or a current YELLOW security finding (behind `TRIAGE_FIX_HUNT_SECURITY=1`). It is never human authorization: the profile must name `objection` in `autofix.fixable_gates`, every other block applies unchanged, one continuation runs per PR, head, and objection signature, and `TRIAGE_FIX_OBJECTION_BUDGET` (default 20) bounds continuations per worker per UTC day. A rejected resolve is continued inside its kept merge worktree (`resubmit commit` lands the follow-up on the merge) and re-judged under `resolve_autopush_bar`; a rejected fix is retried once; a hunted mechanical action that fails to compile queues a `fix` with source `objection`. A `fix` pushes unattended only when `TRIAGE_FIX_AUTOPUSH` names it and `gates.fix_autopush_bar` clears it: reviewer `safe`, compile clean, every touched path at or above `TRIAGE_FIX_AUTOPUSH_MIN_TIER` (default 2), and at most `TRIAGE_FIX_AUTOPUSH_MAX_LINES` (default 300) changed lines.

- [ ] **Step 2: .env.example** — after the `TRIAGE_FIX_HUNT_LIMIT` block:

```
# Continuations: when a reviewer, the compile check, or a security review names
# a defect, the fix worker hands it to the authoring agent as its goal and
# re-judges the result. The profile must name "objection" in
# autofix.fixable_gates. TRIAGE_FIX_HUNT_SECURITY=1 also hunts current YELLOW
# findings on mergeable, CI-green PRs. The budget is continuations per worker
# per UTC day. A fix pushes unattended only past gates.fix_autopush_bar.
# TRIAGE_FIX_HUNT_SECURITY=1
# TRIAGE_FIX_OBJECTION_BUDGET=20
# TRIAGE_FIX_AUTOPUSH_MIN_TIER=2
# TRIAGE_FIX_AUTOPUSH_MAX_LINES=300
```

- [ ] **Step 3: profile.example.json** — `"fixable_gates": ["ci", "review", "objection"]`.

- [ ] **Step 4: Run the release guard and the full gates**

Run: `bash .github/scripts/release_tree_guard.sh && uv run ruff check . && uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness && uv run pytest -q -n auto`
Expected: all clean.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md .env.example profile.example.json
git commit -m "Document objections, the continuation budget, and the fix autopush bar"
```

---

### Task 11: Review pass, PR, and deployment switch-on

- [ ] **Step 1:** Dispatch a code review of the branch against the spec (superpowers:requesting-code-review) and fix Critical and Important findings in a follow-up commit.
- [ ] **Step 2:** Push the branch under its own name (`git push -u origin HEAD:refs/heads/<branch>`) and open the PR with the spec's summary; watch CI; merge when every check is green.
- [ ] **Step 3:** On the worker machine after merge: pull, add `"objection"` to `profile.json` `autofix.fixable_gates`, set `TRIAGE_FIX_HUNT_SECURITY=1`, `TRIAGE_FIX_OBJECTION_BUDGET=20`, append `fix` to `TRIAGE_FIX_AUTOPUSH`, restart the worker, and confirm the first continuation in the worker log (`[fix-worker] resolve continuation for PR #…`).

## Self-review notes

- Spec coverage: sources (Task 5, 6, 7, 8), trust model (Task 1, 3), once-per-head and budget (Task 2, used in 5–8), mechanics (4–8), surfaces (9), rollout step 4 autopush bar (3, 6, 10).
- Every ending writer carries `objection` through `record_fix_request` (Task 5 Interfaces); the ledger stamps `stats.objection` (Task 5), which Task 2's budget counts.
- `next_auto` return shape changes in Task 8; its callers and tests are updated in the same task.
