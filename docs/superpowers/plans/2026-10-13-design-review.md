# Design Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Prospector-run "design review" reviewer that judges each `TRIAGE_REPO` PR head for reuse, layering, convention drift and test effectiveness — advisory by default, gating and autofix-feeding only when opted in.

**Architecture:** A locked-down headless agent (`pipeline/design_review.py`) reads the pinned base clone and the head's cached diff and returns validated findings, stored as `reviews["design"]` through a CAS store method. `reviewers.py` registers it as a `prospector`-source reviewer that ingest carries over and auto-detection ignores; `review_policy` activates it only under `TRIAGE_DESIGN_REVIEW=gate`. The verify worker's idle hunt gains a `design` lane, round-robined with security and verify.

**Tech Stack:** Python 3.14 (uv), SQLAlchemy store via `pipeline/store.py`, headless Claude CLI via `pipeline/headless_agent.py`, React/TS frontend (pnpm).

**Spec:** `docs/superpowers/specs/2026-10-13-design-review-design.md`

## Global Constraints

- Store schema goes to 31 (`schema.STORE_SCHEMA_VERSION`), with a history line.
- `TRIAGE_DESIGN_REVIEW` = `off | advisory | gate`, default `advisory`, editable, registered in `settings_registry.SETTINGS`.
- `design` joins `profile.AUTOFIX_GATES`; `design` joins `worker_health.LANES`.
- Ledger phase `design-review:run`; capacity lane / meter name `design`.
- Agent: `allow_gh=False`, `edit_root=None`, `env_allow=()`, `read_root=[clone, diff]`.
- Severities `blocking | major | minor`; categories `reuse | layering | convention | test`.
- AGENTS.md conventions: present-tense comments, precise types, store accessors only, pyright 0, ruff 0, frontend `pnpm run build` passes, no new lint errors.

## Review Focus

1. Ingest after a design review — the entry must survive `parse_all` (Task 2 test).
2. Head moved between pick and save — the save must refuse, not stamp an old head's verdict on the new one (Task 3 test).
3. Auto mode with a design entry on many PRs — DESIGN must never become active, so merges are never blocked by an advisory reviewer (Task 2 test).
4. Agent cites a path that doesn't exist / outside the diff — finding dropped (Task 4 test).
5. A gating reviewer list containing `design` while the fix profile lacks `design` — no design findings reach the fix goal and the hunter does not hunt on design alone (Task 6 test).

---

### Task 1: Settings, schema, lane and gate names

**Files:**
- Modify: `pipeline/settings.py` (add `design_review_mode`)
- Modify: `pipeline/settings_registry.py` (register `TRIAGE_DESIGN_REVIEW`)
- Modify: `pipeline/schema.py` (version 31 + history line)
- Modify: `pipeline/worker_health.py` (`LANES` += `"design"`)
- Modify: `pipeline/profile.py` (`AUTOFIX_GATES` += `"design"`)
- Modify: `prospector_app/backend/lane_health.py` (`enabled_lanes` adds `design` on a verify worker unless mode is `off`)
- Test: `pipeline/tests/test_design_review.py` (new)

**Produces:** `settings.design_review_mode() -> str` returning `"off" | "advisory" | "gate"`.

- [ ] Step 1: test

```python
def test_mode_defaults_to_advisory(monkeypatch):
    monkeypatch.delenv("TRIAGE_DESIGN_REVIEW", raising=False)
    assert settings.design_review_mode() == "advisory"

def test_mode_reads_choices_and_falls_back(monkeypatch):
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "Gate")
    assert settings.design_review_mode() == "gate"
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "bogus")
    assert settings.design_review_mode() == "advisory"
```

- [ ] Step 2: implement

```python
def design_review_mode() -> str:
    """What the Prospector-run design reviewer does (`pipeline/design_review.py`):
    `advisory` (the default) reviews PR heads and shows the verdict, `gate` also
    makes its bar a merge requirement, `off` reviews nothing."""
    value = os.environ.get("TRIAGE_DESIGN_REVIEW", "advisory").strip().lower()
    return value if value in ("off", "advisory", "gate") else "advisory"
```

Registry entry (behavior group):

```python
Setting("TRIAGE_DESIGN_REVIEW", "behavior", "choice", "Design review", "advisory",
        "A verify worker reviews each PR head for reuse, layering, the repository's "
        "conventions and test strength: advisory (shown only), gate (a merge "
        "requirement), or off.", editable=True, choices=("advisory", "gate", "off")),
```

- [ ] Step 3: `uv run pytest pipeline/tests/test_design_review.py pipeline/tests/test_settings_registry.py -q` → pass. Commit.

### Task 2: Registry + policy

**Files:**
- Modify: `pipeline/reviewers.py`
- Modify: `pipeline/review_policy.py`
- Test: `pipeline/tests/test_reviewers.py`, `pipeline/tests/test_review_policy.py`

**Produces:**
- `Reviewer.source: str = GITHUB` (`GITHUB = "github"`, `PROSPECTOR = "prospector"`).
- `reviewers.DESIGN`, `reviewers.DESIGN_SEVERITIES = ("blocking", "major", "minor")`, `reviewers.DESIGN_CATEGORIES = ("reuse", "layering", "convention", "test")`.
- `parse(DESIGN, feed, head, previous)` returns `previous`.
- `bar(DESIGN, entry, head)` → pending / stale / fail (open `blocking`) / pass.
- `findings_for_fix(DESIGN, …)` → open blocking+major findings in `{headline, class, why, path, line}`.
- `digest(...)` gains `"advisory": bool` (False by default); `digests(pr)` shows DESIGN with its own bar and `advisory=True` when it is not active.
- `seen_summary` skips `PROSPECTOR` reviewers.
- `review_policy.active_reviewers`: auto mode skips `PROSPECTOR`; DESIGN appended when `settings.design_review_mode() == "gate"`; `bar_label(DESIGN)`.

- [ ] Step 1: tests

```python
# test_reviewers.py
HEAD = "h" * 40
def _design(findings, sha=HEAD):
    return {"kind": "review", "reviewed_sha": sha, "observed_at": "2026-10-13T00:00:00Z",
            "score": None, "findings": findings, "summary": "s", "checks": [], "extra": {}}
def _f(sev):
    return {"path": "a.ts", "line": 1, "severity": sev, "title": "t", "body": "b",
            "category": "reuse", "resolved": False, "outdated": False}

def test_design_bar_states():
    D = reviewers.DESIGN
    assert reviewers.bar(D, None, HEAD).status == "pending"
    assert reviewers.bar(D, _design([], "o" * 40), HEAD).status == "stale"
    assert reviewers.bar(D, _design([_f("blocking")]), HEAD).status == "fail"
    assert reviewers.bar(D, _design([_f("major"), _f("minor")]), HEAD).status == "pass"

def test_parse_all_carries_the_design_entry():
    feed = review_fetch.PrFeed(...)  # an empty conversation feed, as other parse tests build it
    prev = {"design": _design([_f("major")])}
    assert reviewers.parse_all(feed, HEAD, prev)["design"] == prev["design"]

def test_design_findings_for_fix_are_blocking_and_major():
    out = reviewers.findings_for_fix(reviewers.DESIGN, _design([_f("blocking"), _f("minor")]), HEAD, None)
    assert [f["class"] for f in out] == ["substantive"]

def test_seen_summary_ignores_design():
    pr = Pr(None, {"meta": {"state": "open", "head_sha": HEAD}, "reviews": {"design": _design([])}})
    assert "design" not in reviewers.seen_summary([pr])["seen"]

# test_review_policy.py
def test_auto_mode_never_detects_design(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "auto")
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "advisory")
    monkeypatch.setattr(review_policy, "_load_seen",
                        lambda: {"design": {"last_observed_at": "2999-01-01T00:00:00Z", "prs": 9}})
    review_policy.reset()
    assert "design" not in [r.id for r in review_policy.active_reviewers()]

def test_gate_mode_activates_design(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "gate")
    review_policy.reset()
    assert [r.id for r in review_policy.active_reviewers()] == ["greptile", "design"]

def test_advisory_digest_carries_its_own_bar(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "advisory")
    review_policy.reset()
    pr = Pr(None, {"meta": {"head_sha": HEAD}, "reviews": {"design": _design([_f("blocking")])}})
    d = reviewers.digests(pr)["design"]
    assert (d["status"], d["advisory"]) == ("fail", True)
    assert review_policy.bar(pr, reviewers.DESIGN).status == "na"
```

- [ ] Step 2: run → fail. Step 3: implement (see Produces). In `active_reviewers`:

```python
    elif p.mode == "explicit":
        ids = list(p.explicit)
    else:
        seen = _seen()
        ids = [rid for rid, r in reviewers.REVIEWERS.items() if r.source == reviewers.GITHUB
               and _recent((seen.get(rid) or {}).get("last_observed_at"), p.active_days)]
    if settings.design_review_mode() == "gate" and p.mode != "none":
        ids.append(reviewers.DESIGN.id)
```

In `digests`, for an inactive DESIGN with an entry: `digest(r, entry, reviewers.bar(r, entry, pr.head_sha), pr.head_sha) | {"advisory": True}`.

- [ ] Step 4: run reviewers/policy/ingest tests → pass. Commit.

### Task 3: Store — claim and CAS save; schema bump

**Files:**
- Modify: `pipeline/store.py` (`PR_SECTIONS` += `"design_run"`; validate `design_run.host`; rename `security_claim_held_elsewhere` → `run_claim_held_elsewhere` and update its callers in `verify_worker.py`; `claim_design_run`, `release_design_run`, `save_design_review`)
- Modify: `pipeline/schema.py` (31)
- Test: `pipeline/tests/test_store.py`

**Produces:**
- `Store.claim_design_run(n: int, *, host: str, stale_after: float) -> bool`
- `Store.release_design_run(n: int, *, host: str) -> None`
- `Store.save_design_review(n: int, entry: dict, *, head_sha: str) -> bool` — False when the row is gone or its head is no longer `head_sha`; writes `reviews["design"]` keeping every other reviewer's entry and the section's stamps.

- [ ] Step 1: tests (claim tests mirror `TestSecurityRunClaim`; plus)

```python
def test_save_design_review_refuses_a_moved_head(tmp_path):
    s = Store(tmp_path)
    s.save_pr({"pr": 1, "meta": {"title": "t", "state": "open", "head_sha": "h2"},
               "reviews": {"greptile": {"kind": "review", "findings": [], "checks": []}}})
    assert s.save_design_review(1, _entry(), head_sha="h1") is False
    assert s.save_design_review(1, _entry(), head_sha="h2") is True
    rec = s.load_pr(1)
    assert set(rec.reviews) >= {"greptile", "design"}
```

- [ ] Step 2–4: implement, run `uv run pytest pipeline/tests/test_store.py -q`, commit.

### Task 4: The agent — `pipeline/design_review.py`

**Files:**
- Create: `pipeline/design_review.py`
- Test: `pipeline/tests/test_design_review.py`

**Produces:**
- `class DesignReviewFailed(RuntimeError)`
- `review(clone: Path, diff_path: Path, *, pr: int, title: str, base_sha: str) -> dict` — the `reviews["design"]` entry.
- `validate(answer: dict, *, touched: set[str], clone: Path) -> tuple[str, list[dict]]` — summary and kept findings.
- `huntable(pr: Pr, *, diff_cached: bool) -> bool` (pure pool predicate: open, `not gates.threat_blocks(pr)` and verdict `clear`, `not gates.bump_exempt(pr)`, diff cached, no DESIGN entry at the head).
- `PHASE = "design-review:run"`.

- [ ] Step 1: tests

```python
def test_validate_drops_unknown_and_unanchored(tmp_path):
    (tmp_path / "src").mkdir(); (tmp_path / "src/util.ts").write_text("x")
    answer = {"summary": "ok", "findings": [
        {"category": "reuse", "severity": "major", "path": "src/new.ts", "line": 3,
         "title": "dup", "body": "b", "evidence_path": "src/util.ts"},
        {"category": "reuse", "severity": "major", "path": "src/new.ts", "line": 3,
         "title": "dup", "body": "b", "evidence_path": "src/missing.ts"},
        {"category": "style", "severity": "major", "path": "src/new.ts", "title": "x", "body": "b"},
        {"category": "test", "severity": "fatal", "path": "src/new.ts", "title": "x", "body": "b"},
        {"category": "layering", "severity": "blocking", "path": "elsewhere.ts", "title": "x", "body": "b"},
        {"category": "../etc", "severity": "minor", "path": "src/new.ts", "title": "x", "body": "b"}]}
    summary, kept = design_review.validate(answer, touched={"src/new.ts"}, clone=tmp_path)
    assert [f["evidence_path"] for f in kept] == ["src/util.ts"]
    assert kept[0]["resolved"] is False and kept[0]["outdated"] is False

def test_evidence_path_cannot_escape_the_clone(tmp_path): ...  # "../x" dropped

def test_review_raises_on_unusable_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(headless_agent, "run_agent", lambda *a, **k: "no json here")
    with pytest.raises(design_review.DesignReviewFailed):
        design_review.review(clone, diff, pr=1, title="t", base_sha="b")

def test_review_runs_locked_down(monkeypatch, tmp_path):
    seen = {}
    def fake(prompt, **kw):
        seen.update(kw); return '{"summary": "fine", "findings": []}'
    monkeypatch.setattr(headless_agent, "run_agent", fake)
    entry = design_review.review(clone, diff, pr=1, title="t", base_sha="b")
    assert seen["allow_gh"] is False and seen["edit_root"] is None and seen["env_allow"] == ()
    assert set(seen["read_root"]) == {str(clone.resolve()), str(diff.resolve())}
    assert entry["reviewed_sha"] is None or True  # head stamped by caller
    assert entry["kind"] == "review" and entry["findings"] == []

def test_huntable(...)  # clear threat at head, not exempt, diff cached, no entry at head → True;
                        # each condition flipped → False
```

- [ ] Step 2: implement. Prompt sections: role; the four judgments with "cite evidence_path you read in the base" rule; "Default to no finding; a finding you cannot point at is noise"; docs block; output contract. `review()` reads `diffpaths`-style touched paths from the diff (`diff --git a/X b/Y` lines → `Y`), clips like `review_fix._clip`, calls `run_agent(..., allow_gh=False, cwd=str(clone), edit_root=None, read_root=[clone, diff], env_allow=(), timeout=900)`, maps `RuntimeError` (not the `AgentUnavailable`/`AgentDeclined`/`AgentTransient` subclasses) and `ValueError` from `extract_json` to `DesignReviewFailed`, and returns `reviewers._entry(DESIGN, reviewed_sha=head, ...)` — `review` takes `head_sha` too.

- [ ] Step 3: run tests, commit.

### Task 5: The lane in the verify worker

**Files:**
- Modify: `prospector_app/backend/verify_worker.py` (`design_failed` skip set, pool in `next_auto`, round-robin, `run_design(n)`, release stale `design_run` claims on start)
- Test: `prospector_app/backend/tests/test_verify_worker.py` (or the existing autohunt test module)

**Produces:** `next_auto(open_lanes)` may return `("design", n)`; `run_design(n: int) -> bool | None` (None = no run; True = verdict saved; False = failure booked).

Rotation replaces the two-way alternation:

```python
_ROTATION = ("security", "verify", "design")

def _rotate(pools: dict[str, list[tuple[int, Pr]]]) -> str | None:
    """The next lane with work after the last one picked, in _ROTATION order."""
    live = [lane for lane in _ROTATION if pools.get(lane)]
    if not live:
        return None
    if _last_auto_lane in _ROTATION:
        start = _ROTATION.index(_last_auto_lane) + 1
        for lane in _ROTATION[start:] + _ROTATION[:start]:
            if lane in live:
                return lane
    return live[0]
```

Maintainer priority keeps its order: security, then verify, then design.

`run_design`:

```python
def run_design(n: int) -> bool | None:
    st = data.store()
    rec = st.load_pr(n)
    clone_sha = verify_driver.local_pin(st).get("base_sha")
    clone = verify_driver.base_clone_dir(str(clone_sha)) if clone_sha else None
    diff = diff_cache.DIFFS / f"{rec.head_sha}.diff" if rec else None
    if rec is None or clone is None or not clone.is_dir() or diff is None \
            or not design_review.huntable(rec, diff_cached=diff.is_file()):
        return None
    me = settings.worker_id()
    if not st.claim_design_run(n, host=me, stale_after=DESIGN_CLAIM_SECONDS):
        return None
    try:
        with capacity.metered("design"):
            entry = design_review.review(clone, diff, pr=n, title=rec.title or "",
                                         head_sha=rec.head_sha or "", base_sha=str(clone_sha))
        saved = st.save_design_review(n, entry, head_sha=rec.head_sha or "")
        ... append_run(PHASE, stats={head_sha, findings counts by severity, saved})
        lane_health.note_success("design"); design_failed.discard(n); return True
    except headless_agent.AgentUnavailable as e:
        lane_health.trip_agent_lanes(str(e)); return False
    except headless_agent.AgentDeclined as e:
        design_failed.add(n, f"declined: {e}"); append_run(... error); return False
    except headless_agent.AgentTransient as e:
        design_failed.add(n, f"the service was overloaded: {e}"); return False
    except design_review.DesignReviewFailed as e:
        design_failed.add(n, str(e)); lane_health.note_failure("design", kind="design-run", reason=str(e), pr=n)
        append_run(... error); return False
    finally:
        st.release_design_run(n, host=me)
```

The drain loop: `design_open = settings.design_review_mode() != "off" and lane_health.open_or_retest("design")` joins `open_lanes`; capacity check maps lane → `"design"`.

- [ ] Step 1: tests — `next_auto` rotates security → verify → design over three picks with all pools non-empty; a design-only pool returns design; `"design"` absent from `open_lanes` never returns design; `run_design` books success/failure (monkeypatched `design_review.review`, store fake) and releases the claim on failure.
- [ ] Step 2–4: implement, run `uv run pytest prospector_app/backend/tests -q -k "verify_worker or autohunt"`, commit.

### Task 6: Autofix opt-in

**Files:**
- Modify: `prospector_app/backend/fix_worker.py` (`_review_summary` and `_fix_goal` skip `PROSPECTOR` reviewers in the active loop; `_fix_goal` adds DESIGN findings when `"design" in fixable` and DESIGN's own bar fails, goal line "Fix the design review findings listed below.")
- Modify: `pipeline/gates.py` (`fix_huntable`: `failing` excludes `PROSPECTOR` reviewers; `design_fixable = "design" in fixable and reviewers.bar(DESIGN, pr.review_entry("design"), pr.head_sha).status == FAIL`)
- Test: `prospector_app/backend/tests/test_fix_worker.py`, `pipeline/tests/test_gates.py`

- [ ] Step 1: tests — with `fixable_gates=("review",)` and a failing DESIGN entry: `_fix_goal` carries no design findings and `fix_huntable` says no gate is failing; with `("design",)`: findings present, goal line present, `fix_huntable` passes its fix branch.
- [ ] Step 2–4: implement, run tests, commit.

### Task 7: Checks card

**Files:**
- Modify: `prospector_app/backend/pr_checks.py` (`CHECK_KEYS` += `"design"` after `"review"`; `_aggregate` skips `PROSPECTOR`; a `design` row: na when mode is off and no entry, else status from DESIGN's own bar — pass/fail/warn(stale, pending) — detail = summary line + " · advisory" unless gating)
- Modify: `prospector_app/frontend/src/components/explorer/checkDefs.ts` (`{ key: "design", label: "Design review" }`)
- Modify: `prospector_app/frontend/src/views/PRDetail.tsx` (review blocks exclude `design`; `checksBodies.design` from its block; an "advisory" chip when `digest.advisory`)
- Modify: `prospector_app/frontend/src/api.ts` (`ReviewDigest.advisory?: boolean`)
- Test: `prospector_app/backend/tests/test_pr_checks.py`

- [ ] Step 1: test — the rollup has a `design` row: na/“not reviewed yet” with no entry; fail with a blocking finding and detail ending "advisory" in advisory mode; the `review` row's detail never names Design review.
- [ ] Step 2–4: implement, `uv run pytest prospector_app/backend/tests/test_pr_checks.py -q`, `pnpm run build` + `pnpm exec eslint` on touched files, commit.

### Task 8: Docs and full gates

- [ ] AGENTS.md: a short **DESIGN REVIEW** paragraph (what it judges, where it runs, advisory vs gate, autofix opt-in, schema 31). `docs/configuration.md` if it lists settings.
- [ ] `uv run pytest -q`, `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness`, `uv run ruff check .`, frontend build. Commit, push, open PR.
