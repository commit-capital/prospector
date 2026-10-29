# Feature Request Screening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Screen open feature requests with a read-only agent, list the ones it likes on a Home card with a reason and a sandboxed HTML mockup, and let a maintainer send any feature request into the issue-fix lane in feature mode.

**Architecture:** A new `issue_triage/feature_screen.py` judges each feature request against the repository's docs and code and a digest of past maintainer decisions (`issue_triage/feature_history.py`). It writes a stamped `feature_screen` issue section. A maintainer's decision is a separate `feature_decision` section. "Implement" queues the existing `solve` request with `feature: true`. That flag rides `LaneSpec.feature` into the cross-tested lane, which appends the approved spec to the report every stage reads and stops treating `not-a-defect` as an ending.

**Tech Stack:** Python 3 (uv, pytest, pyright), FastAPI backend, React + TypeScript frontend (pnpm, `node --test`).

**Spec:** `docs/superpowers/specs/2026-10-28-feature-request-screening-design.md`

## Global Constraints

- Every module starts with `from __future__ import annotations`. Imports are qualified. Signatures are typed precisely. Comments describe the code as it is now.
- Agents that read issue text run through `headless_agent.run_agent` with `allow_gh=False` and `env_allow=()`. Outsiders' text goes in as data with fixability's trust section.
- Nothing in this plan writes upstream. Screening, decisions and mockups stay in the store.
- The store is the only source of truth. Use `issue_store` / `issue_model` only.
- Register every new environment variable in `pipeline/settings_registry.py`.
- Repository name in prompts and UI comes from `TRIAGE_REPO` (`issue_triage.config.repo_name()`). Never hard-code a repository name.
- Mockups: one HTML document, inline styles only, at most `MOCKUP_MAX = 200_000` bytes. Rendered only in `<iframe sandbox srcdoc>` with no `allow-*` tokens, behind a CSP meta forbidding every fetch.
- Gates: `uv run pytest`, `uv run ruff check .`, `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness`, and `cd prospector_app/frontend && pnpm run build && pnpm exec eslint <changed files>`.

## Calls this plan makes beyond the spec

- **Capacity:** screening meters under the existing `issue-fix` lane (`capacity.metered(LANE)` in `issue_fix_worker`) and books ledger phase `feature-screen:run`. This avoids registering a new lane in `lane_health`/`control_overview`. It runs in that worker anyway.
- **Feature mode reaches every stage through the report:** the cross-tested lane's stages all read `spec.title`/`spec.body` through `reproduce_issue.report_block`. In feature mode, `spec.body` is the issue body plus an approved-spec section that tells every reader to treat the requested behavior as what the fix must add. That replaces a per-prompt rewrite. The host's part is the `spec.feature` flag: `not-a-defect` reads `no-fix`.
- **Switch:** `TRIAGE_FEATURE_SCREEN=1` turns on unattended screening in the issue-fix worker. The CLI and Control job run regardless.

## Review Focus

1. **A mockup that tries to escape:** `<script>`, `on*=` handlers, `<link>`, `<iframe>`, `<base>`, `<meta http-equiv>`, `url(http…)` or `src="http…"`. Expected: the host refuses it at write time (`check_mockup`), and the frame's CSP blocks it at render time anyway. Tests in Task 3 and Task 7.
2. **A request with an author edit after a decision:** re-screening must not touch `feature_decision`. A declined request stays off the card even when it re-screens as `consider`. Tests in Task 1 and Task 5.
3. **Implement on a closed issue, or while a fix request is in flight:** Expected: 409, with no `feature_decision` written. Test in Task 5.
4. **A screener answer that cites precedents not in the digest, or says `consider` with an empty `why`.** Expected: unknown precedents are dropped, and an empty `why` reads `unclear`. Tests in Task 3.
5. **A feature run where every candidate gives up `not-a-defect`.** Expected: the ending is `no-fix`, never `not-a-defect`, so the issue isn't recorded as "deliberate". Test in Task 6.

---

### Task 1: Store sections, profile labels, setting

**Files:**
- Modify: `issue_triage/issue_store.py:39-42` (`ISSUE_SECTIONS`)
- Modify: `issue_triage/issue_freshness.py:31` (`UPDATED_BOUND`)
- Modify: `issue_triage/issue_model.py` (properties and record methods beside `fix_triage`, ~line 234)
- Modify: `pipeline/profile.py` (`RepoProfile`, the allowed keys at ~447, the parser at ~467)
- Modify: `pipeline/settings.py`, `pipeline/settings_registry.py`
- Test: `issue_triage/tests/test_feature_store.py`, `pipeline/tests/test_profile.py`

**Interfaces:**
- Produces:
  - `Issue.feature_screen -> dict | None`
  - `Issue.feature_decision -> dict | None`
  - `Issue.record_feature_screen(section: dict) -> None` (writes only that section, like `record_fix_triage`)
  - `Issue.record_feature_decision(section: dict) -> None`
  - `RepoProfile.feature_labels: tuple[str, ...] = ("enhancement",)`
  - `settings.feature_screen() -> bool`
- `feature_decision` shape: `{"decision": "implement" | "decline", "by": str, "at": str, "guidance"?: str}`. It is not in `UPDATED_BOUND`, so it never goes stale.

- [ ] **Step 1: Write failing tests**

```python
def test_feature_screen_goes_stale_on_edit(tmp_store):
    issue = seed_issue(tmp_store, 7, updated_at="2026-10-01T00:00:00Z")
    tmp_store.edit_issue(7).record_feature_screen({"verdict": "consider", "why": "w"})
    assert issue_freshness.is_current(tmp_store.issue(7), "feature_screen")
    bump_content(tmp_store, 7, "2026-10-02T00:00:00Z")
    assert not issue_freshness.is_current(tmp_store.issue(7), "feature_screen")

def test_rescreen_keeps_decision(tmp_store):
    seed_issue(tmp_store, 7)
    tmp_store.edit_issue(7).record_feature_decision({"decision": "decline", "by": "m", "at": "t"})
    tmp_store.edit_issue(7).record_feature_screen({"verdict": "consider", "why": "w"})
    assert tmp_store.issue(7).feature_decision["decision"] == "decline"
```

In `pipeline/tests/test_profile.py`: a profile without `feature_labels` reads `("enhancement",)`; one with `"feature_labels": ["idea", "feature"]` reads `("idea", "feature")`; one with `"feature_labels": "x"` raises `SystemExit`.

Use the fixtures the existing `issue_triage/tests` use for a temp issue store. Match their names.

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest issue_triage/tests/test_feature_store.py pipeline/tests/test_profile.py -q`
Expected: FAIL (no `record_feature_screen`, no `feature_labels`).

- [ ] **Step 3: Implement**

- Add `"feature_screen"` and `"feature_decision"` to `ISSUE_SECTIONS`, and `"feature_screen"` to `UPDATED_BOUND`.
- Add the two properties (via `_fix_section`) and two record methods, mirroring `record_fix_triage`.
- Add `feature_labels`, parsed with `_parse_str_list`.
- Add `_b("TRIAGE_FEATURE_SCREEN", "workers", "Feature request screening", "An idle issue-fix worker screens open feature requests for the maintainers' shortlist.")` and `settings.feature_screen()` reading `== "1"`.

- [ ] **Step 4: Run the tests and `uv run pytest pipeline/tests/test_settings_registry.py -q`**

Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "Store feature-request screening and decisions on issues"`

---

### Task 2: Taste digest

**Files:**
- Create: `issue_triage/feature_history.py`
- Test: `issue_triage/tests/test_feature_history.py`

**Interfaces:**
- Consumes: `Issue.feature_decision` (Task 1), `profile.active().feature_labels`, `gh.gh_list`, `gh.issue_comments`, `settings.verify_scratch()`.
- Produces:
  - `history_dir() -> Path`, which is `settings.verify_scratch() / "feature-screen" / "history"`
  - `render_closed(raw: dict, comments: list[dict]) -> str`
  - `render_decisions(issues: Iterable[Issue]) -> str`
  - `refresh(issues: Iterable[Issue], *, force: bool = False) -> Path`, which returns `history_dir()`
  - `precedents() -> set[int]`, the issue numbers that have a file in `history_dir()`

The digest is plain files the screener reads as a read root:
- one `closed-<n>.md` per closed feature request: title, `state_reason` (completed / not_planned), labels, the body cut to 2,000 chars, and maintainers' comments (`author_association` in `OWNER`, `MEMBER`, `COLLABORATOR`), each cut to 1,500 chars;
- one `decisions.md` listing this deployment's `feature_decision`s with each issue's title.

`refresh` skips the fetch when `history_dir()/".fetched"` is younger than `HISTORY_MAX_AGE = timedelta(hours=24)`, unless `force`. It fetches at most `HISTORY_MAX = 150` most recently closed issues across `feature_labels` (REST `repos/{repo}/issues?state=closed&labels=<label>&sort=updated&per_page=100`, dropping pull requests). It always rewrites `decisions.md`. A failed fetch keeps the old files.

- [ ] **Step 1: Write failing tests**

```python
def test_render_closed_keeps_only_maintainer_comments():
    raw = {"number": 12, "title": "Dark mode", "state_reason": "not_planned",
           "labels": [{"name": "enhancement"}], "body": "please"}
    comments = [{"author_association": "MEMBER", "user": {"login": "m"}, "body": "Not for us: theming is out of scope"},
                {"author_association": "NONE", "user": {"login": "u"}, "body": "+1"}]
    text = feature_history.render_closed(raw, comments)
    assert "not_planned" in text and "theming is out of scope" in text and "+1" not in text

def test_refresh_is_fresh_for_a_day(tmp_path, monkeypatch):  # gh_list called once across two refreshes
def test_refresh_keeps_old_files_when_fetch_fails(tmp_path, monkeypatch):  # gh_list -> None
def test_render_decisions_lists_declines_and_implements(tmp_store): ...
```

- [ ] **Step 2: Run them and confirm they fail.** `uv run pytest issue_triage/tests/test_feature_history.py -q`
- [ ] **Step 3: Implement `feature_history.py`** with the signatures above.
- [ ] **Step 4: Run the tests. Expected: PASS.**
- [ ] **Step 5: Commit** — `git commit -m "Build a digest of past feature-request decisions"`

---

### Task 3: The screener and the mockup

**Files:**
- Create: `issue_triage/feature_screen.py`
- Test: `issue_triage/tests/test_feature_screen.py`

**Interfaces:**
- Consumes: `fixability.current`, `fixability._replies_block` (make it public as `fixability.replies_block`), `reproduce_issue.report_block`, `feature_history.precedents`, `headless_agent.run_agent` / `json_reply` / `fill`, `verdict_log`.
- Produces:
  - `VERDICTS = ("consider", "not-a-fit", "unclear")`
  - `MOCKUP_MAX = 200_000`
  - `is_feature_request(issue: Issue) -> bool`: open, and either `fixability.current(issue)` has `kind == "feature"` or the issue's labels meet `profile.active().feature_labels`
  - `review(clone: Path, history: Path, *, title: str, body: str, replies_block: str) -> dict`, returning `{"verdict", "why", "precedents": list[int], "size": "small"|"medium"|"large"|None, "ui": bool}`
  - `check_mockup(html: str) -> str | None`: the reason it is refused, or None
  - `mockup(clone: Path, history: Path, workdir: Path, *, title: str, body: str, why: str) -> str | None`: the HTML, or None when the agent wrote nothing usable
  - `screen(issue: Issue, clone: Path, history: Path, *, base_sha: str, replies: Sequence[Reply], subject: verdict_log.Subject | None) -> dict`: the `feature_screen` section, which adds `"mockup"?: str`, `"mockup_refused"?: str`, `"model"` and `"base_sha"`
  - `current(issue: Issue) -> dict | None`: the section while it is fresh

**Screener prompt (`PROMPT`):** frame it as "You judge whether a feature request is worth a maintainer's time for __REPO__". It reads the README, docs and code under __CLONE__ and the digest under __HISTORY__. It has the same Trust section as fixability. Its questions:
1. What does this product do, and for whom (from the README/docs)?
2. Does the request serve that, or pull it somewhere else?
3. How did maintainers treat similar requests (cite digest issue numbers)?
4. How big is it? Does it change the UI?

It ends: "Say `consider` rarely: only for a request you would defend to the maintainers as plainly theirs. Most requests are `not-a-fit` or `unclear`." The output is the JSON shape in the spec. Model `MODEL = "sonnet"`.

**Host rules in `review`:**
- A verdict not in `VERDICTS` raises `ValueError`.
- `why` is cut to 800 chars.
- `precedents` keeps only numbers in `feature_history.precedents()`.
- `consider` with an empty `why` reads `unclear`.

**Mockup:** `MOCKUP_PROMPT` tells the agent to read the repository's UI components and styles and to write `__WORKDIR__/mockup.html`: one HTML document showing the proposed UI in the product's look, with the new parts outlined. Inline `<style>` only, no script, no external URL. Run it with `edit_root=workdir` and `read_root=[clone, history, workdir]`. The host reads the file and returns it only when `check_mockup` passes. Otherwise `screen` records `mockup_refused`.

`check_mockup` refuses:
- over `MOCKUP_MAX`;
- no `<html` or `<body`;
- `<script`, `<link`, `<iframe`, `<object`, `<embed`, `<base`, `<form`, `<meta` with `http-equiv`, or `javascript:`;
- any `on[a-z]+\s*=` attribute;
- any `http:`, `https:` or `//` URL in `src`, `href`, `srcset` or CSS `url(`.

Matching is case-insensitive.

`screen` runs the mockup only for `consider` with `ui` true, and records to `verdict_log.record("feature-screen", …)`. Shape `screen` like `fixability.judge`: agent outages propagate, and an unusable answer is recorded `failed` and raises.

- [ ] **Step 1: Write failing tests** (monkeypatch `headless_agent.run_agent` to return canned JSON, like `test_fixability.py` does)

```python
@pytest.mark.parametrize("html", [
    "<html><body><script>1</script></body></html>",
    "<html><body><img src=x onerror=alert(1)></body></html>",
    "<html><body><img src='https://evil/x.png'></body></html>",
    "<html><body><div style=\"background:url(//evil/x)\"></div></body></html>",
    "<html><head><link rel=stylesheet href=a.css></head><body></body></html>",
    "<html><head><META HTTP-EQUIV=refresh content='0;url=x'></head><body></body></html>",
    "<html><body><a href='javascript:x'>x</a></body></html>",
])
def test_check_mockup_refuses_escapes(html):
    assert feature_screen.check_mockup(html) is not None

def test_check_mockup_accepts_inline_styles_and_data_images():
    assert feature_screen.check_mockup(
        "<html><head><style>.a{color:red}</style></head><body><img src='data:image/png;base64,AA=='></body></html>") is None

def test_check_mockup_refuses_oversize(): ...
def test_review_drops_unknown_precedents(...): ...   # precedents() == {12}; agent cites [12, 99] -> [12]
def test_consider_without_why_reads_unclear(...): ...
def test_screen_skips_mockup_unless_consider_with_ui(...): ...   # run_agent called once for not-a-fit
def test_screen_records_refused_mockup(...): ...   # mockup agent writes a <script> page -> "mockup_refused" set, no "mockup"
def test_is_feature_request_by_label_or_kind(...): ...
```

- [ ] **Step 2: Run them and confirm they fail.** `uv run pytest issue_triage/tests/test_feature_screen.py -q`
- [ ] **Step 3: Implement `feature_screen.py`** and rename `fixability._replies_block` to `replies_block`, updating its callers.
- [ ] **Step 4: Run the tests plus `uv run pytest issue_triage/tests/test_fixability.py -q`. Expected: PASS.**
- [ ] **Step 5: Commit** — `git commit -m "Screen feature requests against the product and past decisions"`

---

### Task 4: Run it: worker, CLI backfill, Control job

**Files:**
- Modify: `prospector_app/backend/issue_fix_worker.py` (beside `triage` / `_judge`, and the loop at ~529)
- Modify: `pipeline/cli.py` (beside `_triage_backfill`)
- Modify: `prospector_app/backend/jobs.py` (beside `"issue-find-fixed"`)
- Test: `prospector_app/backend/tests/test_issue_fix_worker.py`, `pipeline/tests/test_cli.py` (or the CLI test file the repo uses)

**Interfaces:**
- Consumes: `feature_screen.is_feature_request`, `feature_screen.current`, `feature_screen.screen`, `feature_history.refresh`, `Issue.record_feature_screen`.
- Produces:
  - `issue_fix_worker.screen_features(store: IssueStore, limit: int | None, *, trigger: str = "worker") -> list[int]`
  - `SCREEN_BATCH = 3`
  - CLI `uv run prospector feature-screen-backfill [--limit N]`
  - Control job kind `"feature-screen"`

`screen_features`:
- Picks open issues where `is_feature_request` is true, `feature_screen.current` is None, and there is no `feature_decision`, newest first. Ignore `HUNT_MAX_AGE` and `HUNT_SKIP_LABELS`.
- Refreshes the digest once per call.
- Screens each under `capacity.metered(LANE)` while `lane_health.capacity_open(LANE)`, with replies from `public_loop.read_replies` as `_judge` does.
- Records the section.
- Appends a run `{"phase": "feature-screen:run", "issue": n, …, "stats": {"verdict", "size", "mockup": bool}}`.
- A failed screen backs off with the same `TRIAGE_RETRY_SECONDS` pattern, in its own dict.

The worker loop calls `screen_features(store, SCREEN_BATCH)` after `triage` when `settings.feature_screen()` is on and the budget isn't spent.

The CLI prints `screened N, consider K (K/N%)`. That is the yes-rate check from the spec.

The job runs the CLI with `--limit n` (`needs_count`, `count_default: 50`, `count_noun: "requests"`, `ledger: ("issue", ("feature-screen",))`, `agentic: True`).

- [ ] **Step 1: Write failing tests:**
  - `test_screen_features_picks_unscreened_feature_requests_only`: a defect, a decided request and a fresh-screened one are skipped, while an old `enhancement` issue is screened.
  - `test_screen_features_books_ledger_phase`.
  - `test_screen_features_stops_when_capacity_closes`.
  - `test_feature_screen_backfill_prints_yes_rate`.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `uv run pytest prospector_app/backend/tests/test_issue_fix_worker.py -q` and the CLI test. Expected: PASS.**
- [ ] **Step 5: Commit** — `git commit -m "Screen feature requests in the issue-fix worker, CLI and Control"`

---

### Task 5: Decisions and the card's API

**Files:**
- Modify: `issue_triage/fix_review.py` (`queue` gets `feature: bool = False`, which stores `"feature": True` on the request section)
- Modify: `prospector_app/backend/models.py` (add `FeatureDecisionBody`)
- Modify: `prospector_app/backend/app.py` (two routes placed before `/api/issues/{n}`, one after)
- Modify: `prospector_app/backend/issues.py` (`get_issue` adds `feature_screen` and `feature_decision`)
- Test: `prospector_app/backend/tests/test_feature_routes.py`

**Interfaces:**
- Consumes: Task 1 sections, `feature_screen.current`, `feature_screen.is_feature_request`, `activity.record_issue_fix`, `activity.operator`.
- Produces:
  - `FeatureDecisionBody(decision: Literal["implement", "decline", "reopen"], guidance: str | None = None)`
  - `GET /api/issues/features` returns `{"items": [{"issue", "title", "url", "why", "size", "precedents", "has_mockup", "screened_at"}], "count": int}`. It lists open issues whose current screen is `consider` and that have no `feature_decision`, ordered by size (small, medium, large, None), then newest.
  - `GET /api/issues/{n}/feature-mockup` returns `{"html": str}`, or 404.
  - `POST /api/issues/{n}/feature-decision`:
    - `implement` calls `fix_review.queue(store, n, "solve", by=operator, guidance=guidance, feature=True)`. On failure it returns 409 with the reason and writes no decision. On success it records `feature_decision`.
    - `decline` records the decision.
    - `reopen` clears `feature_decision`, so a maintainer can reverse a decline.
    - A non-feature request (per `is_feature_request`) returns 409.
    - Each decision logs `activity.record_issue_fix(n, "feature-" + decision, detail)` (actions `FIX_FEATURE-IMPLEMENT` etc., as `record_issue_fix` upper-cases).
    - Neither route writes upstream.
  - `IssueDetail` JSON gains `feature_screen: dict | None` (without the `mockup` body, plus `has_mockup: bool`) and `feature_decision: dict | None`.

- [ ] **Step 1: Write failing tests** (use the app test client the existing `test_issue_fix_routes.py` uses):
  - `test_features_lists_consider_without_decision_ranked_small_first`
  - `test_declined_request_stays_off_card_after_rescreen` (Review Focus 2)
  - `test_implement_queues_feature_solve`: request section has `action == "solve"` and `feature is True`; `feature_decision.decision == "implement"`
  - `test_implement_closed_issue_409_writes_no_decision` and `test_implement_in_flight_409` (Review Focus 3)
  - `test_implement_works_on_not_a_fit_verdict`: the maintainer's decision decides, not the verdict
  - `test_reopen_clears_decline`
  - `test_decision_on_defect_issue_409`
  - `test_feature_mockup_404_without_mockup`
  - `test_decisions_logged_to_activity`
- [ ] **Step 2: Run them and confirm they fail.** `uv run pytest prospector_app/backend/tests/test_feature_routes.py -q`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests plus `uv run pytest prospector_app/backend/tests/test_issue_fix_routes.py issue_triage/tests -q -k "fix_review or feature"`. Expected: PASS.**
- [ ] **Step 5: Commit** — `git commit -m "Let a maintainer implement or decline a feature request"`

---

### Task 6: Feature mode in the cross-tested lane

**Files:**
- Modify: `issue_triage/fix_lane.py:79` (`LaneSpec` gets `feature: bool = False`)
- Modify: `issue_triage/fix_review_runner.py` (`_spec`, `solve`, `_carry_out`: read `req.get("feature")` and build the feature body)
- Modify: `issue_triage/feature_screen.py` (add `approved_report`)
- Modify: `issue_triage/cross_lane.py:144, 219, 662` and `issue_triage/solo_lane.py:355` (endings)
- Test: `issue_triage/tests/test_feature_mode.py`

**Interfaces:**
- Consumes: `fix_request["feature"]` (Task 5), `Issue.feature_screen` / `feature_decision`.
- Produces:
  - `LaneSpec.feature: bool`
  - `feature_screen.approved_report(body: str, screen: dict | None, decision: dict | None, *, repo: str) -> str`

`approved_report` returns `body` followed by a section that starts `## Approved as a feature by a {repo} maintainer`. The section says:
- this is a request for new behavior a maintainer approved, so where a prompt speaks of the reported defect, read the behavior this request asks for that the code lacks;
- a test must show that behavior missing before the change and present after;
- changes the approved spec calls for are requested.

It then gives the screener's `why` (when there is one), the maintainer's guidance, and the mockup HTML last, cut so the whole body stays within `reproduce_issue.REPORT_MAX`. The issue's own text is never cut for the mockup.

`_spec` uses this body when the request carries `feature`. A revision of a feature attempt keeps the flag: `_carry_out` passes `req.get("feature")` through to `solve`.

**Host endings:** in `cross_lane._author` and `solo_lane.run`, when `spec.feature`, a give-up of kind `not-a-defect` reads `no-fix`. `lone_reproduction`'s `not-a-defect` block then never fires for a feature run.

- [ ] **Step 1: Write failing tests**

```python
def test_approved_report_keeps_issue_text_and_cuts_mockup_first():
    body = "x" * 3000
    out = feature_screen.approved_report(body, {"why": "fits", "mockup": "<html>" + "y" * 20000}, None, repo="acme")
    assert out.startswith(body) and "Approved as a feature by a acme maintainer" in out
    assert len(out) <= reproduce_issue.REPORT_MAX

def test_feature_spec_built_from_request(...):  # _spec with req feature -> spec.feature and approved body
def test_feature_run_not_a_defect_reads_no_fix(...):  # Review Focus 5: stub solo_lane.author to give up not-a-defect for all candidates
def test_defect_run_still_ends_not_a_defect(...):
```

- [ ] **Step 2: Run them and confirm they fail.** `uv run pytest issue_triage/tests/test_feature_mode.py -q`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `uv run pytest issue_triage/tests -q`. Expected: PASS (existing lane tests unchanged).**
- [ ] **Step 5: Commit** — `git commit -m "Run an approved feature request through the fix lane"`

---

### Task 7: The Home card, the mockup frame, and the issue page

**Files:**
- Create: `prospector_app/frontend/src/components/FeatureMockup.tsx`, `prospector_app/frontend/src/components/featureMockup.ts`, `prospector_app/frontend/src/components/featureMockup.test.ts`
- Create: `prospector_app/frontend/src/components/FeatureDecisionButtons.tsx`
- Modify: `prospector_app/frontend/src/api.ts` (types and calls), `prospector_app/frontend/src/views/Home.tsx` (new `HomeFeatureCard`, modeled on `HomeAutoFeedCard` at ~307), `prospector_app/frontend/src/views/IssueDetail.tsx`

**Interfaces:**
- Consumes: Task 5 routes.
- Produces:
  - `api.featureRequests(): Promise<{items: FeatureRequestItem[]; count: number}>`
  - `api.featureMockup(n: number): Promise<{html: string}>`
  - `api.featureDecision(n: number, decision: "implement" | "decline" | "reopen", guidance?: string)`
  - `IssueDetail.feature_screen` / `feature_decision` types
  - `MOCKUP_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"`
  - `mockupSrcdoc(html: string): string`: inserts `<meta http-equiv="Content-Security-Policy" content="${MOCKUP_CSP}">` as the first element of `<head>`, creating `<head>` when absent

`FeatureMockup` renders `<iframe sandbox="" srcDoc={mockupSrcdoc(html)} title=…>`. It has a thumbnail size on the card and opens full size in a modal on click.

`HomeFeatureCard`:
- titled "Feature requests worth considering", showing the count;
- one row per item: the title linked to the issue page, `why`, a size chip, and a mockup thumbnail when `has_mockup`;
- `FeatureDecisionButtons` with "Implement this feature" and "Not for {repo}";
- refetches through a `generation` counter after a decision;
- a 409 shows its reason inline.

The issue page shows the screen (verdict, why, precedents as issue links, mockup) and `FeatureDecisionButtons` on every feature request, whatever the verdict. That includes "Reopen" when declined. It renders nothing for an issue with neither section.

- [ ] **Step 1: Write failing tests** in `featureMockup.test.ts`:

```ts
test("csp meta is the first thing in head", () => {
  const out = mockupSrcdoc("<html><head><style>a{}</style></head><body></body></html>");
  assert.ok(out.indexOf("Content-Security-Policy") < out.indexOf("<style>"));
});
test("adds head when missing", () => {
  assert.match(mockupSrcdoc("<html><body>x</body></html>"), /<head><meta http-equiv="Content-Security-Policy"/);
});
test("csp forbids every fetch", () => {
  assert.match(MOCKUP_CSP, /default-src 'none'/);
  assert.doesNotMatch(MOCKUP_CSP, /http|\*/);
});
```

- [ ] **Step 2: Run them and confirm they fail.** `cd prospector_app/frontend && pnpm test`
- [ ] **Step 3: Implement** the components, API calls and both views.
- [ ] **Step 4: Run `pnpm test`, `pnpm run build`, and `pnpm exec eslint` on the changed files. Expected: 0 errors.**
- [ ] **Step 5: Verify in the app** with `preview_start` and a seeded store: the card lists a `consider` request; the mockup renders in the frame, and `read_console_messages` shows a CSP violation and no request when the seeded HTML holds `<img src="https://example.com/x.png">`; Implement queues and the row leaves the card; the issue page shows the buttons on a `not-a-fit` request. Take a screenshot as proof.
- [ ] **Step 6: Commit** — `git commit -m "Show feature requests worth considering on Home"`

---

### Task 8: Full gates

- [ ] Run `uv run pytest`, `uv run ruff check .`, `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness`, and `cd prospector_app/frontend && pnpm run build`. Expected: all zero.
- [ ] Add a short "Feature requests" section to `issue_triage/README.md`: what is screened, the switch, the CLI, and that nothing posts upstream.
- [ ] Commit — `git commit -m "Document feature-request screening"`
