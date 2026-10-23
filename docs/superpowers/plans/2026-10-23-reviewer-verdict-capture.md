# Reviewer verdict capture (ratchet phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every production run of Prospector's six own reviewers leaves one replayable `review_verdicts` row, and a PR fix request's operator guidance survives on its `fix:single` ledger row.

**Architecture:** A new append-only table behind validated `Store` accessors. A small module, `pipeline/verdict_log.py`, owns the reviewer names, verdict vocabularies, `prompt_sha`, and a `record()` that never fails its caller. Each reviewer's entry point takes an explicit `subject` and records itself; callers without a subject (evals) record nothing.

**Tech Stack:** Python 3, SQLAlchemy, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-10-23-reviewer-eval-ratchet-design.md` (sections Concepts, Data, Capture; step 1 of "Order of work").

## Global Constraints

- `from __future__ import annotations` atop every new module; qualified imports (`from pipeline import store`); precise types, no quoted annotations.
- Store access only through `pipeline/store.py` / `storekit.py`; rows validated on write; `storekit.assert_writable` before insert.
- `schema.STORE_SCHEMA_VERSION` goes 37 → 38 with a history comment line in the same style as 30.
- No new environment variable. No pruning of `inputs_gz` in this phase (spec: retention lands with labeling).
- Comments describe the code as it is; match the surrounding comment density.
- Gates: `uv run pytest`, `uv run ruff check .`, `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness` all clean.

## Review Focus

1. **A store write failure inside a review** (store behind schema, DB locked): the review's own result must be unchanged and returned; the miss is printed to stderr. Test in Task 2.
2. **A failed or garbled agent run** (security `INCOMPLETE`, fix-review lens `failed`, trust-boundary `failed`, intake `failed`): still recorded, with verdict `failed` (or `INCOMPLETE` for security), so failure rates are visible later. Tests in Tasks 3–5.
3. **Eval callers** (`pipeline/evals/issue_redteam.py` calls `intake_audit.judge` / `trust_boundary.judge`): no `subject`, no row. Test in Task 5.
4. **Security's prompt filled with the repo at import**: `prompt_sha` must hash the unfilled template so it is the same for every `TRIAGE_REPO`. Test in Task 3.
5. **Outsiders' text in `inputs_gz`**: stored compressed only, never in `data`, so the Tables tab (which shows binary columns as sizes) never renders it. Test in Task 1.

---

### Task 1: The `review_verdicts` table and its store accessors

**Files:**
- Modify: `pipeline/schema.py` (version + comment; new `review_verdicts` Table after `training_decisions`)
- Modify: `pipeline/storekit.py` (add `VerdictRecord` dataclass beside `EvidenceRecord`)
- Modify: `pipeline/store.py` (new accessor block after the threat-evidence block)
- Modify: `prospector_app/backend/tables.py` (description entry)
- Test: `pipeline/tests/test_review_verdicts_store.py`

**Interfaces:**
- Produces:
  - `schema.review_verdicts` columns: `id` Integer PK autoincrement; `at` String index; `reviewer` String index; `subject_kind` String index; `subject` Integer index; `head_sha` String nullable; `base_sha` String nullable; `prompt_sha` String index; `model` String; `verdict` String index; `data` _JSON not null; `inputs_gz` LargeBinary nullable; `label` _JSON nullable.
  - `storekit.VerdictRecord(id: int, at: str, reviewer: str, subject_kind: str, subject: int, head_sha: str | None, base_sha: str | None, prompt_sha: str, model: str, verdict: str, data: dict, label: dict | None)` (frozen dataclass, no blob).
  - `Store.append_review_verdict(*, at: str, reviewer: str, subject_kind: str, subject: int, head_sha: str | None, base_sha: str | None, prompt_sha: str, model: str, verdict: str, data: dict, inputs_gz: bytes | None) -> int`
  - `Store.review_verdicts(*, reviewer: str | None = None, subject_kind: str | None = None, subject: int | None = None, since: str | None = None) -> list[storekit.VerdictRecord]` newest first.
  - `Store.review_verdict_inputs(verdict_id: int) -> bytes | None`
- Validation (raise `ValidationError`, messages prefixed `review_verdicts.`): `subject_kind in ("pr", "issue", "replay")`; `subject` a non-bool int; `reviewer`, `prompt_sha`, `model`, `verdict`, `at` non-empty str; `data` dict; `inputs_gz` bytes or None. Reviewer/verdict vocabularies are checked in `verdict_log` (Task 2), not here, so the store has no import of reviewer modules.

- [ ] **Step 1: Write failing tests** in `test_review_verdicts_store.py` using `Store(tmp_path)`:
  - `test_append_and_read_round_trip`: append one row, `review_verdicts(reviewer="design")` returns one `VerdictRecord` with the same fields and `label is None`.
  - `test_filters_and_order`: three rows across reviewers/subjects/`at`; filters by `reviewer`, `subject_kind`+`subject`, `since`; newest first.
  - `test_inputs_blob_kept_out_of_records`: `inputs_gz=gzip.compress(b'{"body":"x"}')`; the record has no blob attribute; `review_verdict_inputs(id)` returns the exact bytes; `review_verdict_inputs(999)` is None.
  - `test_validation`: `subject_kind="cluster"`, `subject=True`, `data=[]`, `inputs_gz="s"`, `verdict=""` each raise `ValidationError`.
  - `test_schema_version_is_38`: `schema.STORE_SCHEMA_VERSION == 38`.
- [ ] **Step 2:** `uv run pytest pipeline/tests/test_review_verdicts_store.py -q` → FAIL (no attribute `append_review_verdict`).
- [ ] **Step 3:** Implement, mirroring `append_threat_evidence` / `threat_evidence` / `threat_evidence_blobs` (`storekit.read_retrying` for reads, `.returning(id)` for insert). Schema comment: `# 38 — the review_verdicts table (one append-only row per run of Prospector's own reviewers); an older reviewer runs without recording.` Tables entry: `"review_verdicts": "Append-only record of each run of Prospector's own reviewers: verdict, prompt and model in columns, output in data, filled inputs gzip-compressed (shown as sizes only)."`
- [ ] **Step 4:** Re-run the test file → PASS; `uv run pytest pipeline/tests/test_schema.py pipeline/tests/test_schema_fingerprint.py pipeline/tests/test_store_migrate.py -q` → PASS (fix any test that pins the version or table list).
- [ ] **Step 5: Commit** `Add the review_verdicts table and its store accessors`.

### Task 2: `pipeline/verdict_log.py`

**Files:**
- Create: `pipeline/verdict_log.py`
- Test: `pipeline/tests/test_verdict_log.py`

**Interfaces:**
- Consumes: Task 1 accessors.
- Produces:
  - `REVIEWERS: tuple[str, ...] = ("design", "security", "fix-review", "trust-boundary", "intake", "fixability")`
  - `VERDICTS: dict[str, tuple[str, ...]]` in severity order, each with its failure value last: `design: ("pass", "fail", "failed")`, `security: ("GREEN", "YELLOW", "RED", "INCOMPLETE")`, `fix-review: ("safe", "unsafe", "failed")`, `trust-boundary: ("pass", "hold", "failed")`, `intake: ("clear", "suspicious", "malicious", "failed")`, `fixability: ("go", "ask", "skip", "failed")`.
  - `class Subject(NamedTuple): kind: Literal["pr", "issue", "replay"]; number: int`
  - `prompt_sha(*templates: str, logic: int = 0) -> str`: sha256 over the templates joined with `"\0"` plus `f"\0logic={logic}"`, first 16 hex chars.
  - `record(reviewer: str, subject: Subject | None, *, verdict: str, prompt_sha: str, model: str, data: dict, inputs: dict[str, str], head_sha: str | None = None, base_sha: str | None = None, store: Store | None = None) -> int | None`: returns None without writing when `subject` is None. Raises `ValueError` for an unknown reviewer or a verdict outside `VERDICTS[reviewer]` (a programming error, surfaced in tests). Gzips `json.dumps(inputs, sort_keys=True)`; `at = storekit.now()`; `store` defaults to `Store()`. Any exception from the store write is printed to stderr as `verdict_log: could not record <reviewer> on <kind> #<n>: <error>` and returns None.

- [ ] **Step 1: Write failing tests:**
  - `test_no_subject_records_nothing`: `record(..., subject=None)` returns None and the store has no rows.
  - `test_records_row_with_gzipped_inputs`: `Subject("pr", 7)`; row reads back with that verdict; `json.loads(gzip.decompress(store.review_verdict_inputs(id)))` equals the inputs dict.
  - `test_unknown_reviewer_or_verdict_raises`: `record("nope", …)` and `record("design", …, verdict="GREEN")` raise `ValueError`.
  - `test_store_failure_never_raises`: a store whose `append_review_verdict` raises `RuntimeError("locked")` → returns None; `capsys` stderr contains `could not record design on pr #7`.
  - `test_prompt_sha_stable_and_sensitive`: same args → same 16-char value; changing one template character or `logic` changes it.
- [ ] **Step 2:** run → FAIL (module missing).
- [ ] **Step 3:** Implement `pipeline/verdict_log.py` with a short module docstring naming the spec.
- [ ] **Step 4:** run → PASS.
- [ ] **Step 5: Commit** `Add verdict_log: reviewer vocabularies, prompt_sha, and record`.

### Task 3: Design and security record their runs

**Files:**
- Modify: `pipeline/design_review.py` (`review` gains `subject`; `PROMPT_SHA`)
- Modify: `prospector_app/backend/verify_worker.py:757` (pass `subject=verdict_log.Subject("pr", n)`)
- Modify: `pipeline/security_driver.py` (keep the unfilled template as `REVIEW_TEMPLATE`; `REVIEW_PROMPT = REVIEW_TEMPLATE.replace("__REPO__", settings.repo())`)
- Modify: `pipeline/security_review.py` (`review_pr` gains `subject`; `PROMPT_SHA`; `run` passes `Subject("pr", pr)`)
- Test: `pipeline/tests/test_design_review.py`, `pipeline/tests/test_security_review.py`

**Interfaces:**
- Consumes: `verdict_log.record`, `verdict_log.prompt_sha`, `verdict_log.Subject`.
- Produces:
  - `design_review.PROMPT_SHA = verdict_log.prompt_sha(PROMPT, design_verify.PROMPT, logic=reviewers.DESIGN_RUBRIC * 100 + reviewers.DESIGN_SEVERITY_RUBRIC)`
  - `design_review.review(..., subject: verdict_log.Subject | None = None) -> dict` (otherwise unchanged). Records after the entry is built: verdict `"fail"` when `reviewers._design_bar(entry, head_sha)` is not a pass, else `"pass"`; `"failed"` on the path that raises/returns a failed review. `data = {"summary", "findings"}` from the entry; `inputs = {"title", "coverage", "docs"}` as filled; `head_sha`, `base_sha` from the arguments (the diff itself is read back from the `diffs` table by head SHA, so it is not copied). `model = settings.agent_model()`.
  - `security_review.PROMPT_SHA = verdict_log.prompt_sha(security_driver.REVIEW_TEMPLATE, security_driver.REVIEW_FENCED_TAIL, security_driver.VERIFY_PROMPT, security_driver.VERIFY_FENCED_TAIL, *(lens["prompt"] for lens in security_driver.LENSES))`
  - `security_review.review_pr(pr, title, head, store=None, bot_evidence=None, subject: verdict_log.Subject | None = None)`: records just before returning the item: verdict `item.verdict` (including `INCOMPLETE`); `data = {"flagged": flagged, "confirmed": item.findings, "lenses_ok": lenses_ok}`; `inputs = {"title": title, "bot_evidence": json.dumps(bot_evidence or [])}`; `head_sha=head`; `model = settings.agent_model()`; `store=store`.

- [ ] **Step 1: Write failing tests**, reusing each file's existing canned-agent fixtures:
  - design: `test_review_records_verdict_with_subject` (one `review_verdicts` row, reviewer `design`, verdict matches the bar, `prompt_sha == design_review.PROMPT_SHA`); `test_review_without_subject_records_nothing`.
  - security: `test_review_pr_records_flagged_and_confirmed` (a canned lens finding the verifier refutes: row verdict `GREEN`, `data["flagged"]` holds it, `data["confirmed"] == []`); `test_incomplete_is_recorded`; `test_prompt_sha_ignores_repo` (monkeypatch `settings.repo` and reload is overkill: assert `"__REPO__" in security_driver.REVIEW_TEMPLATE` and `PROMPT_SHA` is computed from it).
- [ ] **Step 2:** run both files → new tests FAIL.
- [ ] **Step 3:** Implement; thread `subject` from `verify_worker.run_design` and `security_review.run`.
- [ ] **Step 4:** run both files plus `pipeline/tests/test_verify_worker*.py` → PASS.
- [ ] **Step 5: Commit** `Record design and security review runs`.

### Task 4: Fix review and trust boundary record their runs

**Files:**
- Modify: `issue_triage/review_issue_fix.py` (`full_review` gains `subject`, `base_sha`, `models` stays from settings; `PROMPT_SHA`)
- Modify: `issue_triage/trust_boundary.py` (`judge` gains `subject`, `base_sha`; `PROMPT_SHA`)
- Modify callers: `issue_triage/cross_lane.py:321,336`, `issue_triage/solo_lane.py:386,406`, `issue_triage/fix_lane.py:533` (pass `Subject("issue", <issue number in scope>)` and `spec.base.sha`)
- Test: `issue_triage/tests/test_review_issue_fix.py`, `issue_triage/tests/test_trust_boundary.py`

**Interfaces:**
- Produces:
  - `review_issue_fix.PROMPT_SHA = verdict_log.prompt_sha(PROMPT, *_QUESTIONS.values(), *_OUTPUT.values())`
  - `full_review(..., subject: verdict_log.Subject | None = None, base_sha: str | None = None) -> list[dict]`: one row per call. Verdict `"unsafe"` if any review's verdict is `unsafe` and not `failed`; `"failed"` if every review failed; else `"safe"`. `data = {"reviews": <the returned list>}`; `inputs = {"report": reproduce_issue.report_block(title, body), "root_cause", "test_paths": "\n".join(test_paths), "evidence", "history": history or "", "patch": patch}` (the unclipped patch: `clip` is applied at fill time and replay re-applies it); `model = ",".join(sorted({r["model"] for r in reviews}))`.
  - `trust_boundary.PROMPT_SHA = verdict_log.prompt_sha(PROMPT)`
  - `trust_boundary.judge(..., subject: verdict_log.Subject | None = None, base_sha: str | None = None) -> dict`: verdict `"failed"` if the result is failed, else `"hold"` if `hold(result, maintainer_filed=False)` else `"pass"`; `data = result`; `inputs = {"title", "body", "patch": fix_patch}`; `model = settings.agent_model()`.

- [ ] **Step 1: Write failing tests:** for each module, `…_records_with_subject` (row present, verdict as above for a canned `unsafe`/crossing case and a clean case) and `…_without_subject_records_nothing`; for fix review, `test_all_failed_reviews_record_failed`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement and thread `subject`/`base_sha` through the five call sites.
- [ ] **Step 4:** run these files plus `issue_triage/tests/test_cross_lane*.py`, `test_solo_lane*.py`, `test_fix_lane*.py` → PASS.
- [ ] **Step 5: Commit** `Record issue fix review and trust boundary runs`.

### Task 5: Intake audit and fixability record their runs

**Files:**
- Modify: `issue_triage/intake_audit.py` (`judge` gains `subject`; `PROMPT_SHA`)
- Modify: `issue_triage/fix_review_runner.py:129` (pass `Subject("issue", n)`)
- Modify: `issue_triage/fixability.py` (`judge` gains `subject`; `PROMPT_SHA`)
- Modify: `prospector_app/backend/issue_fix_worker.py:344` (pass `Subject("issue", n)`)
- Test: `issue_triage/tests/test_intake_audit.py`, `issue_triage/tests/test_fixability.py`, `pipeline/tests/test_issue_redteam.py`

**Interfaces:**
- Produces:
  - `intake_audit.PROMPT_SHA = verdict_log.prompt_sha(PROMPT, NOTES, logic=1)` (`logic` covers the blocklist and hidden-text overrides in `judge`).
  - `intake_audit.judge(title, body, notes, *, blocked, subject: verdict_log.Subject | None = None) -> dict`: verdict `"failed"` if failed else the returned verdict (after overrides); `data = result`; `inputs = {"title", "body", "notes": notes or ""}`; `model = settings.agent_model()`.
  - `fixability.PROMPT_SHA = verdict_log.prompt_sha(PROMPT, REPLIES, logic=1)` (`logic` covers the cited-path downgrade).
  - `fixability.judge(issue, clone, *, base_sha, replies, subject: verdict_log.Subject | None = None) -> dict`: verdict = the record's verdict, `"failed"` when `review` raises (record, then re-raise); `data = record`; `inputs = {"title", "body", "replies": <the filled replies block>}`; `model = MODEL`; `base_sha=base_sha`.

- [ ] **Step 1: Write failing tests:** for each module `…_records_with_subject` and `…_without_subject_records_nothing`; intake `test_blocklist_override_is_the_recorded_verdict` (`blocked=True` → row verdict `malicious`); fixability `test_failed_review_records_failed_and_raises`; in `test_issue_redteam.py`, `test_redteam_run_records_no_verdicts` (a canned `run_case` leaves `review_verdicts` empty).
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement and thread `subject` from the two callers.
- [ ] **Step 4:** run these files plus `prospector_app/backend/tests/test_issue_fix_worker*.py` → PASS.
- [ ] **Step 5: Commit** `Record intake audit and fixability runs`.

### Task 6: Guidance on the `fix:single` ledger row

**Files:**
- Modify: `prospector_app/backend/fix_queue.py:38` (`ledger_entry`)
- Test: `prospector_app/backend/tests/test_fix_queue.py`

**Interfaces:**
- Produces: `ledger_entry` adds `stats["guidance"] = req["guidance"]` when the request carries non-empty guidance; absent otherwise.

- [ ] **Step 1: Write failing tests:** `test_ledger_entry_carries_guidance` (`req={"guidance": "keep the old flag", …}` → `stats["guidance"] == "keep the old flag"`); `test_ledger_entry_omits_empty_guidance`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** run `test_fix_queue.py` and `test_trust_ladder*.py` → PASS.
- [ ] **Step 5: Commit** `Keep a fix request's guidance on its ledger row`.

### Task 7: Gates and live check

- [ ] **Step 1:** `uv run pytest` → all pass.
- [ ] **Step 2:** `uv run ruff check .` → no findings.
- [ ] **Step 3:** `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness` → 0 errors.
- [ ] **Step 4:** Against the configured store (read-only query), count the last 7 days of `design-review:run`, `security:review-one`, `issue-fix:run`, `issue-triage:run` ledger rows to estimate rows/day, and note an estimated compressed input size per row. Put both in the PR description as the growth estimate the spec asks for.
- [ ] **Step 5:** Fresh-subagent architecture review of `git diff origin/main...HEAD` (`.claude/skills/architecture-review`); fix anything it finds.
