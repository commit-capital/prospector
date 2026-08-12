# Agent Conflict Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the "Resolve merge conflicts" (rebase) autofix pauses on real conflicts, an agent authors a best-attempt resolution as a merge commit, parks it for operator approve/reject, and an approval pushes it to the contributor's branch.

**Architecture:** The existing fix queue/worker lifecycle is reused end to end. `resubmit` gains a `--merge` prepare mode (merge base into head, pause on conflicts). The fix worker, on a paused mechanical rebase from an operator-clicked request, aborts the rebase, prepares the merge, runs a locked-down headless agent (`pipeline/resolve_conflicts.py` over `pipeline/headless_agent.py`) to resolve only the conflicted paths, commits the merge, compile-preflights, and parks as action `resolve` keeping its worktree (like `fix`). Approval pushes the kept tree; no history rewrite.

**Tech Stack:** Python 3.14 (uv), stdlib-only `resubmit` script, headless `claude -p`, React/TS frontend (pnpm).

## Global Constraints

- `uv run pyright pipeline issue_triage prospector_app/backend review-new-pr/harness` must stay at 0 errors.
- `uv run ruff check .` must stay at 0 findings.
- `uv run pytest` (all three suites) must pass.
- Frontend: `pnpm run build` (tsc must pass 0 errors) from `prospector_app/frontend/`; lint only your changed files (`pnpm exec eslint <files>`), add no new errors. pnpm, never npm.
- Comments describe the code as it stands — no "previously/now/instead of" phrasing (see CLAUDE.md).
- Every function signature fully typed, most-specific types. No quoted annotations.
- `resubmit` stays stdlib-only (it runs under bare python3).
- Store record shape changes require a `STORE_SCHEMA_VERSION` bump (10 → 11 here, exactly once for this whole plan).
- The agent resolution never autopushes: the fallback path always parks, regardless of `TRIAGE_FIX_AUTOPUSH`.
- Autohunt-queued (`source == "auto"`) rebases never invoke the agent.

---

### Task 1: `resolve` joins the action vocabulary (settings, store, schema)

**Files:**
- Modify: `pipeline/settings.py:156-159` (FIX_ACTIONS)
- Modify: `pipeline/schema.py:37` (STORE_SCHEMA_VERSION)
- Modify: `pipeline/store.py:61-66` (comment on FIX_ACTIONS)
- Test: `pipeline/tests/test_store.py` (add to existing file)

**Interfaces:**
- Produces: `settings.FIX_ACTIONS == ("update", "rebase", "fix", "resolve")`; store validation accepts `fix_request.action == "resolve"`. Later tasks rely on the literal string `"resolve"`.

- [ ] **Step 1: Write the failing test**

In `pipeline/tests/test_store.py`, find how existing tests construct a store (`S.Store(tmp_path / "store")` pattern used across `pipeline/tests`) and add:

```python
def test_fix_request_accepts_resolve_action(tmp_path):
    st = S.Store(tmp_path / "store")
    st.save_pr({"pr": 7, "meta": {"title": "t", "state": "open", "head_sha": "a" * 40},
                "fix_request": {"status": "awaiting-review", "action": "resolve"}})
    assert st.load_pr(7).raw["fix_request"]["action"] == "resolve"
```

Match the module's existing import alias for the store (`from pipeline import store as S` or whatever the file already uses).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest pipeline/tests/test_store.py::test_fix_request_accepts_resolve_action -v`
Expected: FAIL with `ValidationError: fix_request.action: 'resolve' not in ['fix', 'rebase', 'update']`

- [ ] **Step 3: Implement**

In `pipeline/settings.py`, replace the FIX_ACTIONS block:

```python
# The autofix actions a fix request may carry. `update` merges the base branch
# into the PR head, `rebase` rebases onto current base behind a pinned lease,
# `fix` has an agent author a change against a failing gate, and `resolve` has
# an agent resolve merge conflicts inside a merge of the base into the head —
# recorded on requests the worker escalates from a conflicted rebase, never
# queued directly.
FIX_ACTIONS: tuple[str, ...] = ("update", "rebase", "fix", "resolve")
```

In `pipeline/store.py`, extend the comment above `FIX_ACTIONS = set(settings.FIX_ACTIONS)` with a clause for resolve: "…, or resolve the conflicts of a base merge with agent-authored content parked for review."

In `pipeline/schema.py`, bump:

```python
STORE_SCHEMA_VERSION = 11
```

and add a line to whatever changelog comment sits beside it (read the surrounding comment style first): `# 11: fix_request.action gains "resolve" (agent-resolved base merge).`

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest pipeline/tests/test_store.py::test_fix_request_accepts_resolve_action -v`
Expected: PASS

- [ ] **Step 5: Run the full store + settings tests, pyright, ruff**

Run: `uv run pytest pipeline/tests/test_store.py -q && uv run pyright pipeline && uv run ruff check pipeline`
Expected: all pass. (`parse_fix_autopush` now accepts `resolve` as a *name*; that is harmless because Task 5's fallback parks unconditionally.)

- [ ] **Step 6: Commit**

```bash
git add pipeline/settings.py pipeline/schema.py pipeline/store.py pipeline/tests/test_store.py
git commit -m "feat: add resolve to FIX_ACTIONS, bump store schema to 11"
```

---

### Task 2: `gates.fix_eligibility` knows `resolve`

**Files:**
- Modify: `pipeline/gates.py:429-489` (fix_eligibility)
- Test: `pipeline/tests/test_gates.py` (the class/section around line 1984 with existing fix_eligibility tests)

**Interfaces:**
- Consumes: `settings.FIX_ACTIONS` including `"resolve"` (Task 1).
- Produces: `gates.fix_eligibility(pr, "resolve", conflicted_paths)` — CODEOWNERS-gated or deny-globbed conflicted paths block; **no** `autofix.fixable_gates` requirement. Callers pass the *conflicted* paths as `changed_paths`.

- [ ] **Step 1: Write the failing tests**

Read the existing fix_eligibility tests around `pipeline/tests/test_gates.py:1980-2050` to reuse their `_pr()` helper and profile monkeypatching idiom (there will be an existing test for `fix` + CODEOWNERS; mirror its setup exactly). Add:

```python
def test_resolve_is_eligible_without_fixable_gates(self):
    # resolve parks for operator approval, so it needs no profile opt-in.
    ok, why = gates.fix_eligibility(_pr(), "resolve", ["src/app.ts"])
    assert ok is True

def test_resolve_blocked_on_codeowners_gated_conflict_path(self):
    # Use the same profile/codeowners monkeypatch the existing
    # fix-on-gated-path test uses, with a conflicted path that matches.
    ok, why = gates.fix_eligibility(_pr(), "resolve", ["gated/path.ts"])
    assert ok is False
    assert "CODEOWNERS" in why

def test_resolve_blocked_on_deny_glob(self):
    # Same deny_globs monkeypatch as the existing update/fix deny test.
    ok, why = gates.fix_eligibility(_pr(), "resolve", ["denied/path.ts"])
    assert ok is False
```

Adapt names/fixtures to the surrounding test class so they run under its setup (self vs module-level functions — copy whichever the neighbors use).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/tests/test_gates.py -k resolve -v`
Expected: `test_resolve_is_eligible_without_fixable_gates` FAILS with "unknown autofix action" — no wait, Task 1 added it to FIX_ACTIONS, so it will PASS through the action check; it should PASS entirely. The CODEOWNERS test FAILS (resolve is not yet in the `action == "fix"` branch). Confirm at least the CODEOWNERS test fails.

- [ ] **Step 3: Implement**

In `pipeline/gates.py` `fix_eligibility`, change the CODEOWNERS branch condition:

```python
    if changed_paths is not None:
        if action in ("fix", "resolve"):
            hm = codeowners.human_merge(changed_paths)
            if hm:
                owners = " ".join(hm["owners"])
                return False, (f"authoring a fix on a CODEOWNERS-gated path owned by "
                               f"{owners} needs a human: {', '.join(hm['paths'][:5])}")
```

Leave the `fixable_gates` check as `action == "fix"` only. Update the docstring's hard-block list: change the two `fix`-specific bullets to say `fix`/`resolve` where CODEOWNERS is concerned, and add one sentence: "`resolve` carries agent-authored conflict resolutions; callers pass the conflicted paths, and it needs no profile opt-in because every resolution parks for operator approval." Keep the CODEOWNERS paragraph's reasoning intact but include resolve alongside fix in its first sentence.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/tests/test_gates.py -k "resolve or fix_eligib or eligibility" -v`
Expected: PASS (including all pre-existing eligibility tests).

- [ ] **Step 5: Commit**

```bash
git add pipeline/gates.py pipeline/tests/test_gates.py
git commit -m "feat: fix_eligibility gates resolve like fix for CODEOWNERS, no opt-in"
```

---

### Task 3: `resubmit` merge mode (prepare --merge / continue / diff / push / state)

**Files:**
- Modify: `prospector_app/agent/resubmit`
- Test: `prospector_app/backend/tests/test_resubmit_cli.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (used by Task 5's worker code):
  - `resubmit <pr> prepare --merge` — clones, merges `upstream/<base>` into head; exit 0 with meta `phase: "conflicted"` when paused on conflicts; exit 0 `phase: "ready"` when the merge applied cleanly; exit 5 (cleaned up) when already up to date.
  - `resubmit <pr> continue` — in merge mode: verifies markers gone, stages only conflicted paths, refuses stray edits (exit 10), commits the merge, `phase: "ready"`.
  - `resubmit <pr> diff` — in merge mode, when ready: prints `git diff <pinned base_sha> HEAD` (the PR's whole change relative to current base — the shape the compile preflight applies). When conflicted: the working conflict diff.
  - `resubmit <pr> push` — merge mode takes neither `-m` nor `--confirm-rewrite`; lease-pinned to the old head; refuses stray edits or a moved head.
  - `resubmit <pr> state` — JSON gains `"worktree"` (absolute path or null) and includes merge-mode conflicts.

- [ ] **Step 1: Write the failing tests**

Add to `prospector_app/backend/tests/test_resubmit_cli.py`, reusing `_make_rebase_repos` (the same fixture works: the fork's `fix` branch conflicts with advanced master) and a `_wire_merge` helper modeled on `_wire_rebase` (identical body — copy it, it needs no rebase-specific parts; keep `_log_rebase` silenced and also silence `_log_merge`):

```python
def _wire_merge(monkeypatch, tmp_path: Path, repos: dict) -> None:
    _wire_rebase(monkeypatch, tmp_path, repos)
    monkeypatch.setattr(resubmit, "_log_merge", lambda *args, **kwargs: None)


def test_prepare_merge_pauses_on_conflicts_and_state_reports_them(monkeypatch, tmp_path):
    repos = _make_rebase_repos(tmp_path)
    _wire_merge(monkeypatch, tmp_path, repos)
    assert resubmit.cmd_prepare(42, merge=True) == 0
    meta = resubmit._read_meta(42)
    assert meta["mode"] == "merge"
    assert meta["phase"] == "conflicted"
    state = json.loads(_capture_state(monkeypatch, 42))
    assert state["phase"] == "conflicted"
    assert state["conflicts"] == ["one.txt"]
    assert state["worktree"] == str(resubmit._worktree(42))


def test_merge_continue_refuses_markers_and_stray_edits(monkeypatch, tmp_path):
    repos = _make_rebase_repos(tmp_path)
    _wire_merge(monkeypatch, tmp_path, repos)
    resubmit.cmd_prepare(42, merge=True)
    wt = resubmit._worktree(42)
    # markers still present
    assert resubmit.cmd_continue(42) == 10
    (wt / "one.txt").write_text("resolved one\n")
    (wt / "stray.txt").write_text("agent wandered\n")
    assert resubmit.cmd_continue(42) == 10
    (wt / "stray.txt").unlink()
    assert resubmit.cmd_continue(42) == 0
    assert resubmit._read_meta(42)["phase"] == "ready"


def test_merge_diff_when_ready_is_change_relative_to_base(monkeypatch, tmp_path, capsys):
    repos = _make_rebase_repos(tmp_path)
    _wire_merge(monkeypatch, tmp_path, repos)
    resubmit.cmd_prepare(42, merge=True)
    (resubmit._worktree(42) / "one.txt").write_text("resolved one\n")
    resubmit.cmd_continue(42)
    capsys.readouterr()
    assert resubmit.cmd_diff(42) == 0
    out = capsys.readouterr().out
    assert "resolved one" in out
    assert out.startswith("diff ")


def test_merge_push_needs_no_flags_and_lands_a_merge_commit(monkeypatch, tmp_path):
    repos = _make_rebase_repos(tmp_path)
    _wire_merge(monkeypatch, tmp_path, repos)
    resubmit.cmd_prepare(42, merge=True)
    (resubmit._worktree(42) / "one.txt").write_text("resolved one\n")
    resubmit.cmd_continue(42)
    assert resubmit.cmd_push(42, None, False, None) == 0
    check = tmp_path / "check"
    _git(tmp_path, "clone", "--branch", "fix", repos["fork"].as_uri(), str(check))
    head_parents = _git(check, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(head_parents) == 3  # merge commit: itself + two parents
    assert repos["old_head"] in head_parents
    assert (check / "one.txt").read_text() == "resolved one\n"


def test_merge_push_refuses_when_head_moved(monkeypatch, tmp_path):
    repos = _make_rebase_repos(tmp_path)
    _wire_merge(monkeypatch, tmp_path, repos)
    resubmit.cmd_prepare(42, merge=True)
    (resubmit._worktree(42) / "one.txt").write_text("resolved one\n")
    resubmit.cmd_continue(42)
    monkeypatch.setattr(resubmit, "_gh_json",
                        lambda pr: _pr(headRefOid="f" * 40, baseRefOid=repos["stale_base"]))
    assert resubmit.cmd_push(42, None, False, None) == 6
```

`_capture_state` helper (add near the tests): run `cmd_state` under `capsys`, or simpler — make it a real helper:

```python
def _capture_state(monkeypatch, pr: int) -> str:
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert resubmit.cmd_state(pr) == 0
    return buf.getvalue()
```

Check the existing `_pr()` helper's `baseRefName` is `"master"` (it is) — `_make_rebase_repos` uses `master`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest prospector_app/backend/tests/test_resubmit_cli.py -k merge_ -v`
Expected: FAIL — `cmd_prepare() got an unexpected keyword argument 'merge'` and missing `_log_merge`.

- [ ] **Step 3: Implement merge mode in `prospector_app/agent/resubmit`**

3a. Docstring: after the rebase flow block, add:

```
A conflicting PR can instead take a merge-commit resolution — merge the base
into the head and resolve inside the merge commit, rewriting no history:
    resubmit <pr> prepare --merge      # partial clone + merge base into head
    #   ...if it pauses, resolve only the printed conflicted paths...
    resubmit <pr> continue             # stage those paths + commit the merge
    resubmit <pr> diff                 # the PR's whole change vs current base
    resubmit <pr> push                 # normal push, lease-pinned to the old head
```

3b. Helpers:

```python
def _merge_in_progress(wt: Path) -> bool:
    git_dir = _git_text(["rev-parse", "--absolute-git-dir"], wt)
    return (Path(git_dir) / "MERGE_HEAD").exists()


def _stray_changes(wt: Path) -> list[str]:
    """Porcelain entries that are not cleanly staged: untracked files and
    worktree-side modifications. After a merge's conflicted paths are staged,
    any of these is content beyond the conflict resolution."""
    r = _git(["status", "--porcelain"], wt)
    if r.returncode != 0:
        raise RuntimeError(f"git status failed: {r.stderr.strip()}")
    out: list[str] = []
    for line in r.stdout.splitlines():
        if not line:
            continue
        if line.startswith("??") or (len(line) > 1 and line[1] != " "):
            out.append(line[3:])
    return out
```

3c. Generalize `_print_conflicts` — parameterize the verb (it currently hardcodes "rebase of PR #… paused"): give it `what: str = "rebase"` and print `f"{what} of PR #{pr} paused on …"`; the merge path passes `"merge"`. Update the "then:" line to stay accurate for both (it already points at `continue`).

3d. `cmd_prepare(pr: int, rebase: bool = False, merge: bool = False) -> int`: the merge branch shares the rebase branch's clone/fetch/head-recheck code. Refactor minimally: keep the existing rebase path intact; after the shared blobless clone + upstream fetch + head-moved rechecks (reuse by extracting them if straightforward, or duplicating the ~30 lines if extraction would tangle the rebase path — prefer a small shared helper `_clone_with_upstream(pr, info, wt) -> tuple[str, str] | int` returning `(base, base_sha)` or an exit code), do:

```python
    if merge:
        meta.update({"base_branch": base, "base_sha": base_sha, "phase": "merging"})
        _write_meta(pr, meta)
        print(f"prepared PR #{pr} for a conflict-resolving merge — {reason}")
        print(f"  fork branch: {meta['repo']} @ {branch} ({sha[:8]})")
        print(f"  base: {base} ({base_sha[:8]})")
        r = _git(["merge", "--no-edit", "-m", f"Merge branch '{base}' into {branch}",
                  base_sha], wt, extra_env={"GIT_EDITOR": "true"})
        paths = _conflicted_paths(wt)
        if paths:
            meta["phase"] = "conflicted"
            _write_meta(pr, meta)
            _print_conflicts(pr, wt, paths, what="merge")
            return 0
        if r.returncode != 0:
            print(f"resubmit: merge failed: {r.stderr.strip() or r.stdout.strip()}",
                  file=sys.stderr)
            _cleanup(pr)
            return 9
        after = _git_text(["rev-parse", "HEAD"], wt)
        if after == sha:
            print(f"resubmit: PR #{pr} is already up to date with {base}; nothing to resolve.")
            _cleanup(pr)
            return 5
        return _mark_merge_ready(pr, meta, wt)
```

The meta for merge mode must set `"mode": "merge"` — thread the mode through where the existing code writes `"mode": "rebase" if rebase else "edit"`:

```python
        "mode": "merge" if merge else "rebase" if rebase else "edit",
```

and the clone must take the blobless form when `rebase or merge`.

`_mark_merge_ready`:

```python
def _mark_merge_ready(pr: int, meta: dict, wt: Path) -> int:
    new_sha = _git_text(["rev-parse", "HEAD"], wt)
    meta.update({"phase": "ready", "new_head_sha": new_sha})
    _write_meta(pr, meta)
    print(f"merged {meta['base_branch']} ({meta['base_sha'][:8]}) into PR #{pr}:")
    print(f"  head: {meta['head_sha'][:8]} → {new_sha[:8]} (merge commit, no rewrite)")
    print(f"  inspect: prospector_app/agent/resubmit {pr} diff")
    print(f"  then: prospector_app/agent/resubmit {pr} push")
    return 0
```

3e. `cmd_continue` merge arm — insert after the meta checks, before the rebase-specific logic (restructure the guard):

```python
    if meta.get("mode") == "merge":
        if meta.get("phase") == "ready" and not _merge_in_progress(wt):
            return _mark_merge_ready(pr, meta, wt)
        if not _merge_in_progress(wt):
            print(f"resubmit: PR #{pr} has no merge waiting to continue.", file=sys.stderr)
            return 2
        paths = _conflicted_paths(wt)
        if not paths:
            print(f"resubmit: merge of PR #{pr} is paused but has no conflicted paths; "
                  "abort rather than guessing at Git's state.", file=sys.stderr)
            return 9
        check = _git(["diff", "--check", "--", *paths], wt)
        if check.returncode != 0:
            print("resubmit: conflict markers or whitespace errors remain; nothing was staged:",
                  file=sys.stderr)
            print(check.stdout.strip() or check.stderr.strip(), file=sys.stderr)
            return 10
        add = _git(["add", "--", *paths], wt)
        if add.returncode != 0:
            print(f"resubmit: staging resolved paths failed: {add.stderr.strip()}", file=sys.stderr)
            return 4
        still_unmerged = _conflicted_paths(wt)
        if still_unmerged:
            print(f"resubmit: these paths are still unresolved: {', '.join(still_unmerged)}",
                  file=sys.stderr)
            return 10
        stray = _stray_changes(wt)
        if stray:
            print("resubmit: the worktree carries edits beyond the conflicted paths; "
                  f"refusing to commit them: {', '.join(stray[:10])}", file=sys.stderr)
            return 10
        commit = _git(["commit", "--no-edit"], wt, extra_env={"GIT_EDITOR": "true"})
        if commit.returncode != 0:
            print(f"resubmit: concluding the merge failed: "
                  f"{commit.stderr.strip() or commit.stdout.strip()}", file=sys.stderr)
            return 9
        return _mark_merge_ready(pr, meta, wt)
```

Adjust the existing `mode != "rebase"` guard so it reads: edit mode still errors ("prepared for a content edit"), and the rebase logic below stays untouched.

3f. `cmd_diff` merge arm — insert before the rebase branch:

```python
    if meta.get("mode") == "merge":
        if _merge_in_progress(wt) or _conflicted_paths(wt):
            r = _git(["diff", "--no-color"], wt)
            if r.returncode != 0:
                print(f"resubmit: git diff failed: {r.stderr.strip()}", file=sys.stderr)
                return 4
            print(r.stdout.strip() or "resubmit: merge is paused, but no working diff is visible.")
            return 0
        if meta.get("phase") != "ready":
            print(f"resubmit: merge of PR #{pr} is not ready to inspect.", file=sys.stderr)
            return 9
        r = _git(["diff", "--no-color", meta["base_sha"], "HEAD"], wt)
        if r.returncode != 0:
            print(f"resubmit: git diff failed: {r.stderr.strip()}", file=sys.stderr)
            return 4
        print(r.stdout, end="")
        return 0
```

3g. `_push_merge`:

```python
def _push_merge(pr: int, meta: dict, dry_run: bool) -> int:
    wt = _worktree(pr)
    if meta.get("phase") != "ready" or _merge_in_progress(wt):
        print(f"resubmit: merge of PR #{pr} is not complete; resolve/continue it or abort.",
              file=sys.stderr)
        return 9
    stray = _stray_changes(wt)
    if stray:
        print("resubmit: merged worktree carries unreviewed edits; continue or abort "
              f"rather than pushing them: {', '.join(stray[:10])}", file=sys.stderr)
        return 9
    _, rc = _live_push_preflight(pr, meta, check_base=True, wt=wt)
    if rc:
        return rc
    new_sha = _git_text(["rev-parse", "HEAD"], wt)
    if new_sha != meta.get("new_head_sha"):
        print(f"resubmit: local head changed after the merge "
              f"({meta.get('new_head_sha', '')[:8]} → {new_sha[:8]}); nothing pushed.",
              file=sys.stderr)
        return 9
    ff = _git(["merge-base", "--is-ancestor", meta["head_sha"], new_sha], wt)
    if ff.returncode != 0:
        print("resubmit: merged head does not contain the author's commits; nothing pushed.",
              file=sys.stderr)
        return 9
    lease = f"--force-with-lease=refs/heads/{meta['branch']}:{meta['head_sha']}"
    if dry_run:
        print(f"[dry-run] would push merge commit {new_sha[:8]} to {meta['repo']} "
              f"@ {meta['branch']} (fast-forward from {meta['head_sha'][:8]})")
    else:
        push = _git(["push", lease, "origin", f"HEAD:{meta['branch']}"], wt)
        if push.returncode != 0:
            print(f"resubmit: lease-protected push to {meta['repo']} @ {meta['branch']} "
                  f"failed: {push.stderr.strip()}", file=sys.stderr)
            return 7
    _log_merge(pr, meta, new_sha, dry_run)
    _cleanup(pr)
    if dry_run:
        print(f"[dry-run] conflict-resolving merge for PR #{pr} logged; remote branch untouched.")
    else:
        print(f"resolved PR #{pr}'s conflicts as {actor_label()}: merged "
              f"{meta['base_branch']} into {meta['branch']} ({meta['head_sha'][:8]} → {new_sha[:8]}).")
        print("  Greptile + CI re-run on the push.")
    return 0
```

Note `check_base=True` here reuses `_live_push_preflight`'s rewrite check — the merge commit's base parent must remain real history.

3h. `cmd_push` dispatch — at the top of the existing mode checks:

```python
    if meta.get("mode") == "merge":
        if message or confirm_rewrite:
            print("resubmit: a conflict-resolving merge push takes no -m and no "
                  "--confirm-rewrite; the merge commit already exists and no history "
                  "is rewritten.", file=sys.stderr)
            return 2
        return _push_merge(pr, meta, dry_run)
```

and in `main`, make the `-m`/`--confirm-rewrite` group optional (`required=False`); edit mode's "requires -m" and rebase's confirm checks already enforce per-mode requirements (verify the existing early `if not message` check for edit mode still runs — it does, in `cmd_push`).

3i. `cmd_state`: include the worktree, the base branch, and merge-mode conflicts:

```python
    print(json.dumps({"phase": phase, "mode": meta.get("mode"), "conflicts": conflicts,
                      "worktree": str(wt) if wt.exists() else None,
                      "base_branch": meta.get("base_branch")}))
```

with the conflict scan condition widened to `meta.get("mode") in ("rebase", "merge")`, and the no-meta line gaining `"worktree": None, "base_branch": None`. (Task 6's worker reads `worktree`, `conflicts`, and `base_branch` from this JSON.) Extend the state test's assertions with `assert state["base_branch"] == "master"`.

3j. `_log_merge` beside `_log_rebase`:

```python
def _log_merge(pr: int, meta: dict, new_sha: str, dry_run: bool) -> None:
    """Append the conflict-resolving base merge to the app activity log."""
    try:
        from prospector_app.backend import activity
        activity.record("resubmit", pr=pr, action="RESOLVE_CONFLICTS", repo=meta.get("repo"),
                        branch=meta.get("branch"), base=meta.get("base_branch"),
                        base_sha=meta.get("base_sha"), from_sha=meta.get("head_sha"),
                        to_sha=new_sha, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 — never let audit logging break the push
        print(f"resubmit: warning — merge activity log failed: {e}", file=sys.stderr)
```

3k. argparse: `prep.add_argument("--merge", action="store_true", help="fetch both histories and merge the base into the PR head, pausing on conflicts")`; make `--rebase`/`--merge` mutually exclusive (`prep_mode = prep.add_mutually_exclusive_group()` holding both), and pass `merge=args.merge` through to `cmd_prepare`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest prospector_app/backend/tests/test_resubmit_cli.py -v`
Expected: all PASS — the new merge tests and every pre-existing rebase/update/edit test.

- [ ] **Step 5: Commit**

```bash
git add prospector_app/agent/resubmit prospector_app/backend/tests/test_resubmit_cli.py
git commit -m "feat: resubmit merge mode — prepare --merge, continue, diff, flagless push"
```

---

### Task 4: `headless_agent` editing mode

**Files:**
- Modify: `pipeline/headless_agent.py:36-58,156-166`
- Test: `pipeline/tests/test_headless_agent.py`

**Interfaces:**
- Produces: `run_agent(prompt, *, allow_gh, cwd, edit_root: str | None = None, …)` — when `edit_root` is set, the spawned claude may `Edit`/`Write` under that path only, plus run read-only git (`git diff/log/show/status`); everything else in the lockdown is unchanged.

- [ ] **Step 1: Write the failing test**

Look at `pipeline/tests/test_headless_agent.py` for how `_flags` is currently asserted, then add:

```python
def test_flags_edit_root_scopes_edit_and_write():
    flags = headless_agent._flags(False, edit_root="/tmp/wt")
    allowed = flags[flags.index("--allowedTools") + 1]
    assert "Edit(/tmp/wt/**)" in allowed
    assert "Write(/tmp/wt/**)" in allowed
    assert "Bash(git diff:*)" in allowed
    i = flags.index("--disallowedTools")
    disallowed = flags[i + 1:flags.index("--permission-mode")]
    assert "Edit" not in disallowed
    assert "Write" not in disallowed
    assert "Task" in disallowed


def test_flags_default_stays_read_only():
    flags = headless_agent._flags(False)
    allowed = flags[flags.index("--allowedTools") + 1]
    assert "Edit" not in allowed
    i = flags.index("--disallowedTools")
    disallowed = flags[i + 1:flags.index("--permission-mode")]
    assert "Edit" in disallowed and "Write" in disallowed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/tests/test_headless_agent.py -k edit_root -v`
Expected: FAIL — `_flags() got an unexpected keyword argument 'edit_root'`.

- [ ] **Step 3: Implement**

```python
# Read-only git the resolver may run inside its worktree to see both sides of
# a conflict. No push, no commit — the resubmit tool owns every git write.
_GIT_READ_ALLOW = [
    "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git status:*)",
]


def _flags(allow_gh: bool, edit_root: str | None = None) -> list[str]:
    tools = ["Read", "Grep", "Glob", *(_GH_ALLOW if allow_gh else [])]
    disallowed = list(_DISALLOWED)
    if edit_root:
        root = edit_root.rstrip("/")
        tools += [f"Edit({root}/**)", f"Write({root}/**)", *_GIT_READ_ALLOW]
        disallowed = [t for t in disallowed if t not in ("Edit", "Write")]
    return [
        "--allowedTools", ",".join(tools),
        "--disallowedTools", *disallowed,
        "--permission-mode", "dontAsk",
        "--safe-mode",
        "--setting-sources", "",
    ]
```

Update the module docstring's lockdown paragraph with one sentence: "An opt-in `edit_root` grants Edit/Write scoped to a single worktree plus read-only git, for the conflict resolver." Then thread it through `run_agent`:

```python
def run_agent(prompt: str, *, allow_gh: bool, cwd: str, system_prompt: str | None = None,
              model: str | None = None, on_event=None, timeout: int = 1200,
              edit_root: str | None = None) -> str:
    ...
    cmd = [CLAUDE_BIN, "-p", prompt, *_flags(allow_gh, edit_root),
           "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
```

(keep the rest of `run_agent` byte-identical).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/tests/test_headless_agent.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add pipeline/headless_agent.py pipeline/tests/test_headless_agent.py
git commit -m "feat: headless_agent edit_root mode — worktree-scoped Edit/Write"
```

---

### Task 5: `pipeline/resolve_conflicts.py` — the resolution agent

**Files:**
- Create: `pipeline/resolve_conflicts.py`
- Test: `pipeline/tests/test_resolve_conflicts.py`

**Interfaces:**
- Consumes: `headless_agent.run_agent(..., edit_root=...)` (Task 4), `headless_agent.fill`, `headless_agent.extract_json`.
- Produces (used by Task 6):
  ```python
  class Resolution(TypedDict): ...  # path: str, rationale: str
  def resolve(worktree: str, conflict_paths: list[str], *, pr: int, title: str,
              body: str, base_branch: str,
              on_event: Callable[[tuple], None] | None = None) -> dict
  ```
  Returns the agent's parsed JSON: `{"resolutions": [{"path": ..., "rationale": ...}]}` on success or `{"give_up": "<reason>"}`. Raises `RuntimeError` on agent failure/timeout and `ValueError` on unparseable/malformed output.

- [ ] **Step 1: Write the failing tests**

`pipeline/tests/test_resolve_conflicts.py`:

```python
"""The conflict-resolution agent driver: prompt assembly, output validation,
and the fail-closed shape of what it returns. The agent itself is mocked —
run_agent is a subprocess boundary."""
from __future__ import annotations

import json

import pytest

from pipeline import headless_agent, resolve_conflicts


def _run(monkeypatch, reply: str) -> dict:
    calls: dict = {}

    def fake_run_agent(prompt, *, allow_gh, cwd, edit_root=None, timeout=0, on_event=None,
                       system_prompt=None, model=None):
        calls.update(prompt=prompt, allow_gh=allow_gh, cwd=cwd, edit_root=edit_root)
        return reply

    monkeypatch.setattr(headless_agent, "run_agent", fake_run_agent)
    out = resolve_conflicts.resolve("/wt", ["a.ts", "b.ts"], pr=7, title="T",
                                    body="B", base_branch="master")
    return {"out": out, "calls": calls}


def test_resolve_returns_parsed_resolutions(monkeypatch):
    reply = json.dumps({"resolutions": [{"path": "a.ts", "rationale": "kept both"},
                                        {"path": "b.ts", "rationale": "took base"}]})
    r = _run(monkeypatch, reply)
    assert r["out"]["resolutions"][0]["path"] == "a.ts"
    assert r["calls"]["edit_root"] == "/wt"
    assert r["calls"]["cwd"] == "/wt"
    assert r["calls"]["allow_gh"] is False
    assert "a.ts" in r["calls"]["prompt"] and "master" in r["calls"]["prompt"]


def test_resolve_passes_give_up_through(monkeypatch):
    r = _run(monkeypatch, json.dumps({"give_up": "the sides contradict"}))
    assert r["out"] == {"give_up": "the sides contradict"}


def test_resolve_rejects_resolutions_for_unknown_paths(monkeypatch):
    reply = json.dumps({"resolutions": [{"path": "evil.ts", "rationale": "x"}]})
    with pytest.raises(ValueError):
        _run(monkeypatch, reply)


def test_resolve_rejects_missing_paths(monkeypatch):
    # every conflicted path must be accounted for
    reply = json.dumps({"resolutions": [{"path": "a.ts", "rationale": "x"}]})
    with pytest.raises(ValueError):
        _run(monkeypatch, reply)


def test_resolve_rejects_garbage(monkeypatch):
    with pytest.raises(ValueError):
        _run(monkeypatch, "I could not decide, sorry!")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest pipeline/tests/test_resolve_conflicts.py -v`
Expected: FAIL — `No module named 'pipeline.resolve_conflicts'` (import error).

- [ ] **Step 3: Implement `pipeline/resolve_conflicts.py`**

```python
"""Drive a locked-down headless agent over a paused merge worktree to resolve
its conflicted paths, returning the per-file rationale it records.

The worktree is a `resubmit prepare --merge` clone paused on conflicts. The
agent edits only the conflicted files (headless_agent's edit_root scopes its
Edit/Write to the worktree, and resubmit's `continue` refuses stray edits
fail-closed). The agent stages nothing and commits nothing — git writes belong
to the resubmit tool.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TypedDict

from pipeline import headless_agent

# Generous but bounded: a resolution is a handful of file edits, not a build.
AGENT_TIMEOUT_SECONDS = 900


class Resolution(TypedDict):
    path: str
    rationale: str


PROMPT = """\
You are resolving merge conflicts in the git worktree at __WORKTREE__.
A merge of the base branch '__BASE__' into this pull request's branch is
paused on conflicts. Your job is to edit the conflicted files so both sides'
intent is preserved, then report what you did as JSON.

The pull request (PR #__PR__): __TITLE__

__BODY__

Conflicted files (resolve ALL of these, and ONLY these):
__PATHS__

For each conflicted file:
1. Read it. Conflict regions look like:
   <<<<<<< HEAD          (this PR's branch — "ours")
   ...
   =======
   ...
   >>>>>>> <sha>         (the base branch — "theirs")
   `git diff` in the worktree shows the combined view; `git log` shows both
   histories.
2. Edit the file to remove every conflict marker, keeping BOTH sides' intent
   whenever they do not genuinely contradict — for example, two independent
   additions at the same location are both kept.
3. Do not modify any other file. Do not stage, commit, or run any git command
   that writes.

If the two sides genuinely contradict — the same behavior implemented two
incompatible ways, where choosing is a product decision — do not guess.
Give up instead.

Your final message must be exactly one JSON object, nothing else:
  {"resolutions": [{"path": "<file>", "rationale": "<one or two sentences on
   how you combined the sides>"}, ...]}   — one entry per conflicted file
or
  {"give_up": "<one or two sentences on why a person must decide>"}
"""


def _prompt(worktree: str, conflict_paths: list[str], pr: int, title: str, body: str,
            base_branch: str) -> str:
    return headless_agent.fill(PROMPT, {
        "__WORKTREE__": worktree,
        "__BASE__": base_branch,
        "__PR__": pr,
        "__TITLE__": title,
        "__BODY__": (body or "(no description)").strip()[:4000],
        "__PATHS__": "\n".join(f"  {p}" for p in conflict_paths),
    })


def resolve(worktree: str, conflict_paths: list[str], *, pr: int, title: str,
            body: str, base_branch: str,
            on_event: Callable[[tuple], None] | None = None) -> dict:
    """Run the resolution agent over the paused merge at `worktree`.

    Returns the agent's verdict: {"resolutions": [Resolution, ...]} covering
    exactly the conflicted paths, or {"give_up": reason}. Raises RuntimeError
    when the agent process fails and ValueError when its output is not a
    well-formed verdict — both mean no resolution exists and the caller
    aborts the worktree."""
    text = headless_agent.run_agent(
        _prompt(worktree, conflict_paths, pr, title, body, base_branch),
        allow_gh=False, cwd=worktree, edit_root=worktree,
        timeout=AGENT_TIMEOUT_SECONDS, on_event=on_event)
    verdict = headless_agent.extract_json(text)
    if "give_up" in verdict:
        return {"give_up": str(verdict["give_up"])}
    raw = verdict.get("resolutions")
    if not isinstance(raw, list):
        raise ValueError(f"agent output has no resolutions list: {text[-500:]}")
    resolutions: list[Resolution] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("path"):
            raise ValueError(f"malformed resolution entry: {item!r}")
        resolutions.append({"path": str(item["path"]),
                            "rationale": str(item.get("rationale") or "")})
    reported = {r["path"] for r in resolutions}
    expected = set(conflict_paths)
    if reported != expected:
        raise ValueError(
            f"agent resolutions cover {sorted(reported)} but the conflicted paths "
            f"are {sorted(expected)}")
    return {"resolutions": resolutions}
```

Note `headless_agent.extract_json` raises `ValueError` on garbage already — the garbage test passes through that.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest pipeline/tests/test_resolve_conflicts.py -v`
Expected: all PASS.

- [ ] **Step 5: pyright + ruff the new module, commit**

Run: `uv run pyright pipeline && uv run ruff check pipeline`

```bash
git add pipeline/resolve_conflicts.py pipeline/tests/test_resolve_conflicts.py
git commit -m "feat: resolve_conflicts agent driver over a paused merge worktree"
```

---

### Task 6: fix worker — escalate a paused rebase to an agent resolution

**Files:**
- Modify: `prospector_app/backend/fix_worker.py` (`_probe` lines 362-385, `recheck_eligibility`, `push_approved`, `_push`, module docstring)
- Test: `prospector_app/backend/tests/test_fix_worker.py`

**Interfaces:**
- Consumes: `resolve_conflicts.resolve(...)` (Task 5), `resubmit prepare --merge`/`continue`/`diff`/`state`/`push` (Task 3), `gates.fix_eligibility(pr, "resolve", paths)` (Task 2).
- Produces: a parked `fix_request` with `action: "resolve"`, `status: "awaiting-review"`, result carrying `patch`, `compile_preflight`, `resolutions`, `conflict_paths`, `merge_diff`, `message`; `push_approved` pushes it from the kept worktree with `resubmit push` (no flags).

- [ ] **Step 1: Write the failing tests**

Add to `prospector_app/backend/tests/test_fix_worker.py`. The existing `_Probe` class replays scripted resubmit calls keyed by subcommand via `overrides`; a paused rebase is signaled through the `state` subcommand's stdout. Extend the scripting with per-call sequencing where needed — the fallback calls `state` more than once (after `prepare --rebase`, then after `prepare --merge`). Use a small stateful fake instead of `_Probe` for these tests:

```python
class _ConflictedResubmit:
    """A resubmit whose rebase pauses on conflicts and whose merge prepare
    pauses on the same paths; continue/diff then succeed."""

    def __init__(self, tmp_path):
        self.calls = []
        self.wt = str(tmp_path / "wt")
        self.merged = False

    def __call__(self, n, *args):
        self.calls.append(args)
        rc, out = 0, ""
        if args[0] == "state":
            phase = "conflicted" if not self.merged else "ready"
            out = json.dumps({"phase": phase, "mode": "merge" if self.merged else "rebase",
                              "conflicts": [] if self.merged else ["one.txt"],
                              "worktree": self.wt})
        elif args[0] == "diff":
            out = ("diff --git a/one.txt b/one.txt\n+resolved" if self.merged
                   else "diff --git a/one.txt b/one.txt\n+<<<<<<< conflict")
        elif args == ("prepare", "--merge"):
            pass
        elif args[0] == "continue":
            self.merged = True
        elif args[0] == "abort":
            pass
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": ""})()
```

(import `json` at top if not present). Tests:

```python
def _queue_rebase(store, source=None):
    fix_queue.queue_pr(1, "rebase", source=source)


def test_conflicted_rebase_escalates_to_agent_and_parks_resolve(store, monkeypatch, tmp_path):
    _queue_rebase(store)
    fake = _ConflictedResubmit(tmp_path)
    monkeypatch.setattr(fix_worker, "_resubmit", fake)
    monkeypatch.setattr(fix_worker.resolve_conflicts, "resolve",
                        lambda wt, paths, **kw: {"resolutions": [
                            {"path": "one.txt", "rationale": "kept both"}]})
    fix_worker.run_one(1)
    req = store.load_pr(1).raw["fix_request"]
    assert req["status"] == "awaiting-review"
    assert req["action"] == "resolve"
    assert req["result"]["resolutions"][0]["rationale"] == "kept both"
    assert req["result"]["conflict_paths"] == ["one.txt"]
    assert req["result"]["merge_diff"].startswith("diff ")
    assert "resolved" in req["result"]["patch"]
    assert ("continue",) in fake.calls
    assert not _pushed(fake)
    # the parked worktree is kept: no abort after the merge was prepared
    assert fake.calls.index(("prepare", "--merge")) > fake.calls.index(("abort",))
    assert ("abort",) not in fake.calls[fake.calls.index(("prepare", "--merge")):]


def test_auto_queued_conflicted_rebase_keeps_the_refusal(store, monkeypatch, tmp_path):
    _queue_rebase(store, source="auto")
    fake = _ConflictedResubmit(tmp_path)
    monkeypatch.setattr(fix_worker, "_resubmit", fake)
    called = []
    monkeypatch.setattr(fix_worker.resolve_conflicts, "resolve",
                        lambda *a, **kw: called.append(1))
    fix_worker.run_one(1)
    req = store.load_pr(1).raw["fix_request"]
    assert req["status"] == "refused"
    assert not called
    assert ("prepare", "--merge") not in fake.calls


def test_agent_give_up_refuses_with_reason(store, monkeypatch, tmp_path):
    _queue_rebase(store)
    fake = _ConflictedResubmit(tmp_path)
    monkeypatch.setattr(fix_worker, "_resubmit", fake)
    monkeypatch.setattr(fix_worker.resolve_conflicts, "resolve",
                        lambda wt, paths, **kw: {"give_up": "the sides contradict"})
    fix_worker.run_one(1)
    req = store.load_pr(1).raw["fix_request"]
    assert req["status"] == "refused"
    assert "the sides contradict" in req["refused_reason"]
    assert ("abort",) in fake.calls[fake.calls.index(("prepare", "--merge")):]


def test_resolve_preflight_failure_refuses_and_aborts(store, monkeypatch, tmp_path):
    _queue_rebase(store)
    fake = _ConflictedResubmit(tmp_path)
    monkeypatch.setattr(fix_worker, "_resubmit", fake)
    monkeypatch.setattr(fix_worker.resolve_conflicts, "resolve",
                        lambda wt, paths, **kw: {"resolutions": [
                            {"path": "one.txt", "rationale": "kept both"}]})
    monkeypatch.setattr(fix_worker, "_preflight", lambda n, patch: {"exit": 1})
    fix_worker.run_one(1)
    req = store.load_pr(1).raw["fix_request"]
    assert req["status"] == "refused"
    assert ("abort",) in fake.calls[fake.calls.index(("prepare", "--merge")):]


def test_approved_resolve_pushes_the_kept_tree_without_rederiving(store, monkeypatch, tmp_path):
    rec = store.load_pr(1)
    rec.record_fix_request("approved", "resolve", queued_at=NOW,
                           result={"patch": "diff", "conflict_paths": ["one.txt"]},
                           head_sha=HEAD)
    data.refresh()
    probe = _Probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    fix_worker.push_approved(1)
    assert probe.calls == [("push",)]
    assert store.load_pr(1).raw["fix_request"]["status"] == "pushed"
```

The `store` fixture's PR (mergeable False, drift conflicts) already satisfies rebase queueing. If `fix_eligibility` for `resolve` consults CODEOWNERS/deny globs, the default test profile gates nothing, so the happy paths pass.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest prospector_app/backend/tests/test_fix_worker.py -k "escalates or give_up or resolve or auto_queued" -v`
Expected: FAIL — today's code refuses every paused rebase (`status == "refused"` where the first test expects `awaiting-review`), and `fix_worker.resolve_conflicts` doesn't exist.

- [ ] **Step 3: Implement in `fix_worker.py`**

3a. Import: add `resolve_conflicts` to the `from pipeline import …` line.

3b. Replace the paused-rebase branch of `_probe` (the `if paused is not None:` block) with a handoff:

```python
        paused = _conflicted_state(n)
        if paused is not None:
            _agent_resolve(n, claimed, action, paused)
            return None
```

3c. The refusal message, shared verbatim by both refusal paths:

```python
def _conflict_refusal(paused: list[str]) -> str:
    files = ", ".join(paused[:5])
    more = f" (and {len(paused) - 5} more)" if len(paused) > 5 else ""
    return (f"This PR's changes and the current base both edit the same lines "
            f"in {len(paused)} file(s), and git can't combine them on its own: "
            f"{files}{more}. Resolving that needs a person who knows which "
            f"version is right — ask the author to rebase.")
```

3d. The escalation:

```python
def _agent_resolve(n: int, claimed: dict, action: str, paused: list[str]) -> None:
    """Escalate a rebase that paused on conflicts to an agent-authored merge
    resolution, parking the result as a `resolve` request for operator review.

    Only operator-clicked requests escalate — the hunter's picks refuse, so
    unattended agent time is never spent without a human having asked. Every
    exit path writes a terminal status and leaves no paused git state behind;
    the one worktree that survives is the parked resolution's, kept because an
    agent's edits are not mechanically re-derivable."""
    merge_diff = _conflict_diff(n)
    _resubmit(n, "abort")
    evidence = ({"merge_diff": merge_diff, "conflict_paths": paused}
                if merge_diff else None)
    if claimed.get("source") == "auto":
        _refuse(n, claimed, _conflict_refusal(paused), result=evidence)
        return
    rec = data.store().load_pr(n)
    if rec is None:
        _refuse(n, claimed, f"PR #{n} left the store")
        return
    ok, why = gates.fix_eligibility(rec, "resolve", paused)
    if not ok:
        _refuse(n, claimed,
                f"{_conflict_refusal(paused)} An agent resolution was withheld: {why}.",
                result=evidence)
        return

    prepared = _resubmit(n, "prepare", "--merge")
    if prepared.returncode != 0:
        _settle(n, claimed, prepared.returncode,
                (prepared.stderr or prepared.stdout).strip())
        return
    state_r = _resubmit(n, "state")
    try:
        st = json.loads(state_r.stdout or "{}")
    except ValueError:
        st = {}
    worktree = st.get("worktree")
    conflicts = [str(c) for c in (st.get("conflicts") or [])]
    if not worktree or not conflicts:
        # The merge did not pause where the rebase did; nothing to resolve here.
        _resubmit(n, "abort")
        _refuse(n, claimed, _conflict_refusal(paused), result=evidence)
        return

    data.store().edit_pr(n).record_fix_request(
        "running", "resolve", queued_at=claimed.get("queued_at"),
        started_at=claimed.get("started_at"), step="agent resolving conflicts",
        source=claimed.get("source"), host=socket.gethostname(),
        head_sha=claimed.get("against_head_sha"))
    data.refresh()

    try:
        verdict = resolve_conflicts.resolve(
            worktree, conflicts, pr=n, title=rec.title or "",
            body=(rec.raw.get("meta") or {}).get("body") or "",
            base_branch=str(st.get("base_branch") or ""))
    except (RuntimeError, ValueError) as e:
        _resubmit(n, "abort")
        _refuse(n, claimed,
                f"{_conflict_refusal(paused)} The agent attempt did not land: {e}.",
                result=evidence)
        return

    if "give_up" in verdict:
        _resubmit(n, "abort")
        _refuse(n, claimed,
                f"{_conflict_refusal(paused)} The agent declined to guess: "
                f"{verdict['give_up']}",
                result=evidence)
        return

    cont = _resubmit(n, "continue")
    if cont.returncode != 0:
        _resubmit(n, "abort")
        _refuse(n, claimed,
                f"{_conflict_refusal(paused)} The agent's resolution did not pass the "
                f"merge checks: {(cont.stderr or cont.stdout).strip()[:500]}",
                result=evidence)
        return
    diff = _resubmit(n, "diff")
    if diff.returncode != 0 or not (diff.stdout or "").strip():
        _resubmit(n, "abort")
        _fail(n, claimed, f"reading the resolved diff failed: "
                          f"{(diff.stderr or diff.stdout).strip()[:500]}")
        return
    patch = diff.stdout.strip()

    data.store().edit_pr(n).record_fix_request(
        "running", "resolve", queued_at=claimed.get("queued_at"),
        started_at=claimed.get("started_at"), step="compile preflight",
        source=claimed.get("source"), host=socket.gethostname(),
        head_sha=claimed.get("against_head_sha"))
    data.refresh()
    pf = _preflight(n, patch)
    if pf is not None:
        pf_ok, pf_why = gates.compile_preflight_gate(pf)
        if not pf_ok:
            _resubmit(n, "abort")
            _refuse(n, claimed, plain_preflight(pf),
                    result={"patch": patch[-TAIL_CHARS:], "compile_preflight": pf,
                            "detail": pf_why, "merge_diff": merge_diff,
                            "conflict_paths": paused})
            return

    result = {"patch": patch[-TAIL_CHARS:], "compile_preflight": pf,
              "resolutions": verdict["resolutions"], "conflict_paths": paused,
              "merge_diff": merge_diff,
              "message": "Merge current base, conflicts agent-resolved"}
    _park(n, claimed, "resolve", result, socket.gethostname())
```

Notes for the implementer:
- `_park` keeps the worktree for any action outside `gates.HUNTABLE_ACTIONS` — `resolve` qualifies, so no abort happens on the park path.
- The agent-resolve path parks unconditionally: `settings.FIX_AUTOPUSH` is deliberately not consulted here.
- `rec.title` exists on `Pr`; the PR body lives in the raw meta section — verify the exact accessor (`grep -n "def title\|body" pipeline/model.py`) and use the model property if one exists.
- `resubmit state` must expose `base_branch` for the prompt: in Task 3's `cmd_state`, also include `"base_branch": meta.get("base_branch")` in the JSON (add it there while implementing this task if it was missed; update Task 3's state test accordingly).

3e. `recheck_eligibility` — pass the conflicted paths for a resolve:

```python
def recheck_eligibility(n: int, action: str,
                        conflict_paths: list[str] | None = None) -> tuple[bool, str]:
    """Re-run the autofix gate against the record as it stands now. A request
    can sit in the queue while a threat scan or security review lands, so the
    gate that allowed the queue click is re-asked before the worker acts. A
    `resolve` is judged on its conflicted paths — the only content the agent
    authored."""
    rec = data.store().load_pr(n)
    if rec is None:
        return False, f"PR #{n} left the store"
    paths = conflict_paths if action == "resolve" else service.changed_paths(rec)
    return gates.fix_eligibility(rec, action, paths)
```

and in `push_approved`, thread them through:

```python
    action = claimed.get("action") or "fix"
    conflict_paths = None
    if action == "resolve":
        raw_paths = (claimed.get("result") or {}).get("conflict_paths")
        conflict_paths = [str(p) for p in raw_paths] if raw_paths else []
    ok, why = recheck_eligibility(n, action, conflict_paths)
```

(`run_one`'s call site stays two-argument.)

3f. `_push` — a resolve pushes the kept merge worktree with no flags:

```python
    args = (["update"] if action == "update" else
            ["push"] if action == "resolve" else
            ["push", "--confirm-rewrite", str(req.get("against_head_sha") or "")]
            if action == "rebase" else
            ["push", "-m", str(result.get("message") or _commit_message(action))])
```

3g. Module docstring: in the paragraph about parked worktrees, extend the last sentence: "An agent-authored `fix` or `resolve` is not reproducible, so it keeps its tree and pushes the reviewed change verbatim." Add one sentence to the opening paragraph: "A mechanical rebase that pauses on conflicts escalates, for operator-clicked requests, to an agent-authored merge resolution that parks for review."

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest prospector_app/backend/tests/test_fix_worker.py -v`
Expected: all PASS — the five new tests and every pre-existing one. Two pre-existing tests assert the old refusal on conflicts; if any now fail because the request was operator-queued, re-read them: they should either be updated to queue with `source="auto"` (when they test the mechanical refusal) or their expectations updated to the parked resolve (when they test the operator path). Preserve the *property* each test states in its name/docstring.

- [ ] **Step 5: pyright + ruff, commit**

Run: `uv run pyright prospector_app/backend && uv run ruff check prospector_app`

```bash
git add prospector_app/backend/fix_worker.py prospector_app/backend/tests/test_fix_worker.py
git commit -m "feat: fix worker escalates conflicted rebases to parked agent resolutions"
```

---

### Task 7: Frontend — surface the parked resolution

**Files:**
- Modify: `prospector_app/frontend/src/api.ts:522-560`
- Modify: `prospector_app/frontend/src/components/FixPanel.tsx`
- Modify: `prospector_app/frontend/src/views/PRDetail.tsx:539-561` (only if the merge-diff panel needs the wording tweak below)

**Interfaces:**
- Consumes: the parked record shape from Task 6 (`action: "resolve"`, `result.resolutions`, `result.merge_diff`, `result.conflict_paths`).

- [ ] **Step 1: Extend the API types**

In `api.ts`:

```ts
export type FixAction = "update" | "rebase" | "fix";
/** Every action a request can carry: the three queueable actions plus
 *  `resolve`, which the worker records when it escalates a conflicted rebase
 *  to an agent-authored merge resolution. */
export type FixRequestAction = FixAction | "resolve";
```

Change `FixRequest.action` and `FixQueueEntry.action` to `FixRequestAction`. In `FixResult` add:

```ts
  /** Per-file rationale from the conflict-resolution agent (action `resolve`). */
  resolutions?: { path: string; rationale: string }[] | null;
```

and extend the `FixResult` doc comment's merge_diff sentence: the conflict diff also rides a parked `resolve`, where the Diff panel shows the hunks the agent resolved.

- [ ] **Step 2: FixPanel — awaiting-review copy and rationale**

In `FixPanel.tsx` `RequestStrip`, the awaiting-review branch: special-case resolve in the headline —

```tsx
            {req.status === "approved"
              ? "Approved — waiting for the runner to push"
              : req.action === "resolve"
                ? "An agent resolved the merge conflicts — review & approve"
                : proven
                  ? `Conflicts resolvable — this ${req.action} applies cleanly`
                  : `A ${req.action} is ready for your review`}
```

and in the detail line, after the commit-message sentence, add for resolve:

```tsx
            {req.action === "resolve" &&
              " The conflicted hunks are in the Diff panel below — switch it to “Merge diff”."}
```

In `FixBody`, render the rationale above the patch when present:

```tsx
      {req.result?.resolutions && req.result.resolutions.length > 0 && (
        <div className="small" style={{ marginTop: 8 }}>
          <div className="muted">How each conflict was resolved:</div>
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {req.result.resolutions.map((r) => (
              <li key={r.path}><code>{r.path}</code> — {r.rationale}</li>
            ))}
          </ul>
        </div>
      )}
```

- [ ] **Step 3: PRDetail merge-diff wording**

The Diff panel's merge mode already keys off `pr.fix_request?.result?.merge_diff`, which a parked resolve now carries — no logic change. Update the two title/note strings that say the attempt "paused"/"refused" so they cover both cases, e.g. the segmented button title: `"Where this PR and the current base edit the same lines."` and the note text: `"⚠ Where this PR and the current base collide — the conflicted hunks before any resolution ( … files), not the PR's own change."` Keep the `(#46)` comment references intact.

- [ ] **Step 4: Build + lint**

Run (from `prospector_app/frontend/`): `pnpm run build && pnpm exec eslint src/api.ts src/components/FixPanel.tsx src/views/PRDetail.tsx`
Expected: tsc 0 errors; eslint adds no new errors on these files (compare against `git stash`-free baseline by running eslint on the same files before committing if unsure).

- [ ] **Step 5: Commit**

```bash
git add prospector_app/frontend/src/api.ts prospector_app/frontend/src/components/FixPanel.tsx prospector_app/frontend/src/views/PRDetail.tsx
git commit -m "feat: surface parked agent conflict resolutions in the fix panel"
```

---

### Task 8: Docs + full gates

**Files:**
- Modify: `CLAUDE.md` (the AUTOFIX paragraph)
- Modify: `prospector_app/backend/fix_worker.py` / others only if gates fail

- [ ] **Step 1: Update CLAUDE.md's AUTOFIX paragraph**

After the sentence describing the three actions, add:

```
A mechanical `rebase` that pauses on real conflicts escalates — for
operator-clicked requests only, never the hunter's — to a fourth action,
`resolve`: a locked-down agent resolves the conflicted paths inside a merge
of current base into the head (`resubmit prepare --merge` +
`pipeline/resolve_conflicts.py`), the result passes the compile preflight,
and it parks as `awaiting-review` with a per-file rationale, keeping its
worktree like `fix`. `gates.fix_eligibility` holds `resolve` to the
CODEOWNERS and deny-glob bar over the conflicted paths, with no profile
opt-in; a resolve never autopushes regardless of `TRIAGE_FIX_AUTOPUSH`, and
approval pushes the kept merge commit with no history rewrite.
```

- [ ] **Step 2: Run every gate**

```bash
uv run pytest
```

```bash
uv run pyright pipeline issue_triage prospector_app/backend review-new-pr/harness
```

```bash
uv run ruff check .
```

From `prospector_app/frontend/`: `pnpm run build`.
Expected: pytest all pass, pyright 0 errors, ruff clean, tsc 0 errors. Fix anything that fails before committing.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: describe the resolve autofix action in the operating rules"
```
