"""Run the deep SECURITY review on ONE pull request.

The automated wave (security_driver.eligible + workflows/security.js) reviews
every gated merge candidate lacking a current verdict. This is the per-PR entry
point: review a single PR — e.g. when its head moved and freshness invalidated
the old verdict, or to vet a PR before the wave reaches it.

The Workflow sandbox (security.js) has no filesystem access and is driven by the
operator in Claude, so the cockpit cannot invoke it. This module runs the same
3-lens-then-refute shape via concurrent headless `claude -p` agents and commits
via security_driver.commit_verdicts.

The review ENGINE (review_pr) is gate-free — it reviews whatever PR it's handed,
ignorant of disposition/mergeability. WHICH PRs the automated wave reviews is the
gate's job (security_driver.eligible: clean merge candidates only); this entry
point bypasses it so you can vet a close-fixed / request-changes PR before
deciding to re-route it to merge. A RED verdict only un-routes a PR that was
actually headed to merge (commit_verdicts/_flip_red is disposition-aware).

  uv run python security_review.py --pr N [--store DIR]

Progress is printed to stdout one line per step; the cockpit streams it as SSE.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from pipeline import diff_cache
from pipeline import headless_agent
from pipeline import security_driver
from pipeline.security_driver import (
    LENSES, REVIEW_FENCED_TAIL, REVIEW_PROMPT, VERIFY_CHUNK_SIZE,
    VERIFY_FENCED_TAIL, VERIFY_PROMPT)
from pipeline.settings import REPO_ROOT
from pipeline.store import Store
from pipeline.storekit import now as _now
from pipeline.wire import Finding, VerdictItem

if TYPE_CHECKING:
    from pipeline.model import Pr

# The lenses and review/verify prompts are the canonical copies owned by
# security_driver.py and shipped to the workflow (security.js) via the manifest —
# consumed here, never restated. This headless path only differs in the per-call
# placeholder fills and the fenced-block output tail; the coverage/refute rules below
# re-implement the same gate the workflow applies (two runtimes, one policy).


def _say(msg: str) -> None:
    print(msg, flush=True)


class _LensProgress:
    """Serialize the progress output of N concurrently-running lenses so stdout
    reads lens-by-lens — byte-identical to running them one at a time.

    Exactly one lens is "live" at a time and prints its lines directly; the
    others buffer theirs. When the live lens finishes, the next lens is promoted:
    its header prints, its buffered lines flush in one burst, and it goes live for
    any lines still to come. Promotion cascades past any lens that already
    finished while it was buffered. `emit`/`finish` are called from each agent's
    stdout-reader thread, so all state changes hold `_lock`."""

    def __init__(self, headers: list[str]) -> None:
        self._headers = headers
        self._buffers: list[list[str]] = [[] for _ in headers]
        self._done = [False] * len(headers)
        self._lock = threading.Lock()
        self._live = -1
        self._promote(0)  # lens 0 is live from the start; no thread runs yet

    def _promote(self, i: int) -> None:
        """Make lens `i` live: print its header, flush whatever it buffered while
        waiting. Caller holds `_lock` (or runs before any thread starts)."""
        self._live = i
        _say(self._headers[i])
        for line in self._buffers[i]:
            _say(line)
        self._buffers[i] = []

    def emit(self, i: int, line: str) -> None:
        with self._lock:
            if i == self._live:
                _say(line)
            else:
                self._buffers[i].append(line)

    def finish(self, i: int) -> None:
        with self._lock:
            self._done[i] = True
            # Advance past the live lens and any later lens that already finished.
            while self._live < len(self._headers) and self._done[self._live]:
                if self._live + 1 >= len(self._headers):
                    self._live = len(self._headers)
                    break
                self._promote(self._live + 1)


def _call_agent_json(prompt: str, step: str, on_event: Callable[[tuple], object]) -> dict | None:
    """Run one headless agent and return its parsed JSON, or None if the run or
    the JSON extraction failed (logged under `step`). Shared by the review and
    verify passes — both run a single agent, then parse a JSON object."""
    try:
        text = headless_agent.run_agent(prompt, allow_gh=True, cwd=str(REPO_ROOT),
                                        on_event=on_event)
        return headless_agent.extract_json(text)
    except (RuntimeError, ValueError) as e:
        _say(f"    ! {step} failed: {e}")
        return None


def _review_lens(pr: int, title: str, diff_path: str, lens: str, lens_prompt: str,
                 on_event: Callable[[tuple], object]) -> dict:
    """Run one lens; return {ok, findings}. A failed/garbled run is ok=False so a
    wiped-out lens can't masquerade as a clean GREEN (coverage gate below)."""
    prompt = headless_agent.fill(REVIEW_PROMPT, {
        "__PR__": pr, "__TITLE__": title, "__DIFF_PATH__": diff_path,
        "__LENS__": lens, "__LENS_PROMPT__": lens_prompt}) + REVIEW_FENCED_TAIL
    data = _call_agent_json(prompt, f"{lens} lens", on_event)
    findings = data.get("findings") if data else None
    if not isinstance(findings, list):
        return {"ok": False, "findings": []}
    return {"ok": True, "findings": [f for f in findings
                                     if (f.get("severity") or "").lower() != "green"]}


def _verify(pr: int, diff_path: str, flagged: list[Finding]) -> list[Finding]:
    """Chunked refuting verifier. Returns the confirmed findings (upheld, not
    downgraded to not-an-issue), each stamped with the verifier's reasoning."""
    indexed = [{"index": i, **f} for i, f in enumerate(flagged)]
    confirmed: list[Finding] = []
    for start in range(0, len(indexed), VERIFY_CHUNK_SIZE):
        chunk = indexed[start:start + VERIFY_CHUNK_SIZE]
        prompt = headless_agent.fill(VERIFY_PROMPT, {
            "__N__": len(chunk), "__PR__": pr, "__DIFF_PATH__": diff_path,
            "__CHUNK__": json.dumps(chunk)}) + VERIFY_FENCED_TAIL
        data = _call_agent_json(prompt, f"verify chunk {start}", headless_agent.print_progress)
        if data is None:
            continue
        results = data.get("results", [])
        by_index = {r.get("index"): r for r in results if isinstance(r, dict)}
        for f in chunk:
            v = by_index.get(f["index"])
            if not v or not v.get("upheld"):
                continue
            sev = (v.get("adjusted_severity") or "").lower()
            if sev not in ("red", "yellow"):
                continue
            finding = cast(Finding, {k: val for k, val in f.items() if k != "index"})
            finding["severity"] = sev
            finding["detail"] = f"{finding.get('detail', '')}\n\n[verified: {v.get('reasoning', '')}]"
            confirmed.append(finding)
    return confirmed


def review_pr(pr: int, title: str, head: str) -> tuple[VerdictItem, int] | None:
    """Run the 3-lens-then-refute review on ONE PR. Returns (verdict_item,
    lenses_ok) — the item is a VerdictItem ready for
    security_driver.commit_verdicts, and lenses_ok is how many of the lenses ran
    (diagnostics only). Returns None if the diff can't be cached.

    This is the review ENGINE — deliberately ignorant of disposition/mergeability/
    gates. WHICH PRs get reviewed is a wave-selection concern
    (security_driver.eligible); the engine reviews whatever it's handed."""
    # Ensure the diff for the current head is cached (review + verify Read it).
    _say("① Caching diff…")
    diff_path = diff_cache.DIFFS / f"{head}.diff"
    if not diff_cache.fetch_diff(pr, head):
        _say(f"✗ could not fetch diff for PR #{pr}")
        return None

    # 3 adversarial lenses. They're independent (no cross-lens state; results are
    # consumed as a flat concat), so run them concurrently — wall-clock is max(lens)
    # not sum(lens). _LensProgress serializes their stdout back to lens-by-lens so
    # the byte sequence matches a serial run. GREEN is only trustworthy if every
    # lens ran.
    _say(f"② Reviewing via {len(LENSES)} lenses…")
    prog = _LensProgress([f"  · {lens['key']} lens" for lens in LENSES])
    results: list[dict | None] = [None] * len(LENSES)

    def run_lens(i: int, lens: dict) -> None:
        def on_event(ev: tuple) -> None:
            line = headless_agent.progress_line(ev)
            if line is not None:
                prog.emit(i, line)
        try:
            results[i] = _review_lens(pr, title, str(diff_path),
                                      lens["key"], lens["prompt"], on_event)
        finally:
            prog.finish(i)

    threads = [threading.Thread(target=run_lens, args=(i, lens))
               for i, lens in enumerate(LENSES)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # An unexpected crash (not caught inside _review_lens) leaves a None → treat as
    # a failed lens (ok=False) so it can't masquerade as clean GREEN.
    lens_results = [r if r is not None else {"ok": False, "findings": []}
                    for r in results]
    lenses_ok = sum(1 for r in lens_results if r["ok"])
    coverage_ok = lenses_ok == len(LENSES)
    flagged: list[Finding] = [f for r in lens_results for f in r["findings"]]
    _say(f"  lenses ran: {lenses_ok}/{len(LENSES)}; non-green findings: {len(flagged)}")

    # Refute the flagged findings; only confirmed ones survive.
    if flagged:
        _say(f"③ Verifying {len(flagged)} flagged finding(s)…")
        confirmed = _verify(pr, str(diff_path), flagged)
    else:
        confirmed = []
    reds = [f for f in confirmed if f["severity"] == "red"]
    yellows = [f for f in confirmed if f["severity"] == "yellow"]

    verdict = "RED" if reds else ("YELLOW" if yellows else "GREEN")
    # A clean bill is only trusted with full lens coverage; otherwise HOLD
    # (INCOMPLETE) so the PR stays eligible and re-runs. RED/YELLOW stand.
    if verdict == "GREEN" and not coverage_ok:
        verdict = "INCOMPLETE"

    item = VerdictItem(pr=pr, head_sha=head, verdict=verdict,
                       findings=reds + yellows, tier="adversarial")
    return item, lenses_ok


def run(store: Store, pr: int, *, trigger: str | None = None) -> int:
    """On-demand review of a single PR. Unlike the wave, this is NOT gated on
    mergeability — you can security-review any PR (e.g. to vet a close-fixed /
    request-changes PR before deciding to re-route it to merge). The verdict is
    recorded either way; a RED only un-routes a PR that was actually headed to
    merge (commit_verdicts/_flip_red is disposition-aware).

    trigger stamps the runs-ledger entry with who fired the review (the idle
    auto-hunter passes "autohunt"; an unset trigger records none)."""
    started = _now()
    rec: Pr | None = store.load_pr(pr)
    if rec is None:
        _say(f"✗ PR #{pr} not found in store")
        return 1
    title = rec.title or ""
    head = rec.head_sha or ""
    _say(f"▶ Security review of PR #{pr}: {title}")
    _say(f"  head {head[:7]}")

    reviewed = review_pr(pr, title, head)
    if reviewed is None:
        return 1
    item, lenses_ok = reviewed

    disp_before = rec.disposition
    _, held, errs = security_driver.commit_verdicts(store, [item])
    if errs:
        _say("✗ commit failed:")
        for e in errs:
            _say(f"    ! {e}")
        return 1
    if held:
        _say(f"⚠ verdict HELD (incomplete lens coverage: {lenses_ok}/{len(LENSES)} ran) — "
             f"no GREEN trusted; PR stays eligible and will re-run.")
        return 0
    verdict, findings = item.verdict, item.findings
    reds = sum(1 for f in findings if f["severity"] == "red")
    _say(f"✓ verdict: {verdict} ({reds} red / {len(findings) - reds} yellow confirmed)")
    if verdict == "RED":
        # commit_verdicts un-routes only a PR that was headed to merge; report
        # whichever actually happened rather than re-deriving that policy here.
        after_rec: Pr | None = store.load_pr(pr)
        disp_after = after_rec.disposition if after_rec is not None else None
        if disp_after != disp_before:
            _say(f"  ↳ RED flips PR #{pr} to {disp_after} and reopens its cluster for re-analysis.")
        else:
            _say(f"  ↳ RED recorded; PR #{pr} wasn't routed to merge, so its disposition is unchanged.")
    entry: dict = {"phase": "security:review-one", "pr": pr,
                   "started": started, "finished": _now(),
                   "stats": {"verdict": verdict, "lenses_ok": lenses_ok,
                             "findings": len(findings)}}
    if trigger is not None:
        entry["trigger"] = trigger
    store.append_run(entry)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--store", default=None)
    ap.add_argument("--trigger", default=None,
                    help="provenance stamp for the runs-ledger entry (the idle "
                         "auto-hunter passes 'autohunt')")
    args = ap.parse_args(argv)
    store = Store(args.store) if args.store else Store()
    return run(store, args.pr, trigger=args.trigger)


if __name__ == "__main__":
    sys.exit(main())
