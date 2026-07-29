"""Reformat cluster rationales for display — a presentation-only pass that never
changes the analyst's words.

For each cluster it asks a cheap model to return {summary, body}:
  - body    — the EXACT same rationale text, reformatted as Markdown (paragraph
              breaks, a bolded verdict clause, backticked identifiers). Verified
              verbatim by `gate_body` (strip *,`,whitespace -> must be identical);
              on failure it escalates to a stronger model, and if that also drifts
              the cluster is left raw. So a committed body CANNOT have reworded text.
  - summary — a one-sentence TL;DR written fresh for display. Not identity-gated
              (it's new prose), but `check_summary` rejects invented PR refs.

Idempotent: processes clusters that have a `rationale` but no `rationale_summary`
yet. A re-analysis clears the summary (see Cluster.record_analysis), so reformatted
clusters that get re-analyzed are picked up again on the next run.

  uv run python reformat_rationales.py [--all] [--cluster N] [--limit N]
                                       [--concurrency K] [--store DIR]

Backend: set ANTHROPIC_API_KEY (source it from your secret store) to use the HTTP
Messages API — ~10x faster per call than the CLI, and fine at high --concurrency
(e.g. 24+). Without a key it falls back to headless `claude -p`, which is heavier
(a full harness per call) so keep --concurrency modest there.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from pipeline import headless_agent
from pipeline.store import DEFAULT_ROOT, Store

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5"
PIPELINE_DIR = str(__import__("pathlib").Path(__file__).resolve().parent)

_PROMPT = """You are preparing a PR-triage cluster rationale for on-screen display. Return ONLY a JSON object inside a ```json fenced block with exactly two keys: "summary" and "body".

"body" — the FULL original text below, reformatted as Markdown for readability. STRICT: do NOT add, remove, reword, reorder, paraphrase, or correct ANY words; every word stays exactly as written, in order. You may ONLY: insert paragraph breaks at natural topic shifts; wrap the single key verdict/conclusion clause in **bold**; wrap inline code, identifiers, filenames, commit SHAs, or flags that are ALREADY present in `backticks`. Leave PR references like #1234 as PLAIN text (no backticks, no links). Add no new words.

"summary" — ONE sentence you write yourself (max ~28 words): a crisp TL;DR of the decision a reviewer can read in 3 seconds. It may paraphrase. Do NOT invent PR numbers or an outcome not supported by the text.

TEXT:
{src}"""


def _norm(s: str) -> str:
    """Drop the only characters a faithful reformat may add — markdown emphasis,
    backticks, and whitespace — so two texts compare equal iff their words match."""
    return re.sub(r"[\s*`]+", "", s)


def gate_body(src: str, body: str) -> bool:
    """True iff `body` is `src` with only markdown/whitespace added — verbatim words."""
    return _norm(src) == _norm(body)


def check_summary(src: str, summary: str) -> bool:
    """Reject an empty/overlong summary, or one citing a PR number absent from src."""
    if not summary or not summary.strip():
        return False
    if len(summary.split()) > 35:  # hard cap a few words above the prompt's ~28 target
        return False
    src_nums = set(re.findall(r"\d{2,}", src))
    return all(n in src_nums for n in re.findall(r"#(\d+)", summary))


# Two backends produce the same {summary, body}: the headless `claude -p` CLI, or
# a direct HTTP call to the Anthropic Messages API. The API skips the per-call
# harness startup (~30s -> ~2s) and tolerates much higher concurrency; it's used
# automatically when ANTHROPIC_API_KEY is set (source it from your secret store),
# otherwise we fall back to the CLI, which rides the local Claude subscription.
_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_RETRY_STATUS = {429, 500, 502, 503, 529}


def have_api() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _parse_reply(text: str) -> tuple[str, str] | None:
    """A model reply -> (summary, body), or None if it isn't the JSON we asked for."""
    try:
        obj = headless_agent.extract_json(text)
    except Exception:
        return None
    summary, body = obj.get("summary"), obj.get("body")
    if not isinstance(summary, str) or not isinstance(body, str):
        return None
    return summary, body


def _ask_cli(src: str, model: str) -> tuple[str, str] | None:
    try:
        text = headless_agent.run_agent(_PROMPT.format(src=src), allow_gh=False,
                                        cwd=PIPELINE_DIR, model=model, timeout=240)
    except Exception:
        return None
    return _parse_reply(text)


def _ask_api(src: str, model: str) -> tuple[str, str] | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    headers = {"x-api-key": key, "anthropic-version": _API_VERSION, "content-type": "application/json"}
    body = {"model": model, "max_tokens": 4096, "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": _PROMPT.format(src=src)}]}
    for attempt in range(3):
        try:
            r = httpx.post(_API_URL, headers=headers, json=body, timeout=120.0)
        except httpx.HTTPError:
            return None
        if r.status_code in _RETRY_STATUS:  # transient — brief backoff, then retry
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 200:
            return None
        block = "".join(b.get("text", "") for b in r.json().get("content", [])
                        if b.get("type") == "text")
        return _parse_reply(block)
    return None


def _ask(src: str, model: str) -> tuple[str, str] | None:
    """One model call -> (summary, body), or None on a malformed / unreachable reply.
    Routes to the HTTP API when a key is present, else the headless CLI."""
    return _ask_api(src, model) if have_api() else _ask_cli(src, model)


def reformat_one(src: str, haiku_attempts: int = 2) -> dict:
    """Escalation ladder: retry the cheap model first, then escalate to a stronger
    one, then keep raw. Haiku's rare gate failure is stochastic (it occasionally
    rewords a clause), so a second Haiku pass usually clears it — only a rationale
    that *systematically* trips Haiku is worth Sonnet. A committed body is always
    gate-verified verbatim, whichever tier produced it. Returns a result dict:
    {tier: 'haiku'|'sonnet'|'raw', summary, body}. Tier 'raw' keeps src as the body
    (no safe reformat) but still carries a salvaged summary when one was sound."""
    ladder = [("haiku", HAIKU)] * haiku_attempts + [("sonnet", SONNET)]
    salvaged_summary: str | None = None
    for tier, model in ladder:
        got = _ask(src, model)
        if got is None:
            continue
        summary, body = got
        if check_summary(src, summary):
            if gate_body(src, body):
                return {"tier": tier, "summary": summary, "body": body}
            # body drifted, but the summary is sound — hold it as a fallback so a
            # cluster we can't safely reformat still gets a TL;DR over its raw text.
            salvaged_summary = salvaged_summary or summary
    return {"tier": "raw", "summary": salvaged_summary, "body": src}


def _select(store: Store, args: argparse.Namespace) -> list[int]:
    clusters = store.all_clusters()
    if args.cluster is not None:
        return [args.cluster] if args.cluster in clusters else []
    ids = [cid for cid, c in sorted(clusters.items())
           if c.rationale and (args.all or not c.rationale_summary)]
    return ids[: args.limit] if args.limit else ids


def main() -> int:
    ap = argparse.ArgumentParser(description="Reformat cluster rationales for display.")
    ap.add_argument("--cluster", type=int, help="reformat just this cluster (forces redo)")
    ap.add_argument("--all", action="store_true", help="redo clusters that already have a summary")
    ap.add_argument("--limit", type=int, help="cap how many clusters to process")
    ap.add_argument("--concurrency", type=int, default=6, help="parallel model calls (default 6)")
    ap.add_argument("--store", default=str(DEFAULT_ROOT))
    args = ap.parse_args()

    store = Store(args.store)
    ids = _select(store, args)
    if not ids:
        print("nothing to reformat.")
        return 0
    backend = "http-api" if have_api() else "cli"
    print(f"reformatting {len(ids)} cluster(s) · concurrency {args.concurrency} · backend {backend}",
          flush=True)

    tally = {"haiku": 0, "sonnet": 0, "raw": 0}
    done = 0

    def work(cid: int) -> tuple[int, dict]:
        c = store.load_cluster(cid)
        return cid, reformat_one((c.rationale if c else "") or "")

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(work, cid) for cid in ids]
        for fut in as_completed(futures):
            cid, res = fut.result()
            # each cluster is its own JSON file — concurrent saves never collide.
            store.edit_cluster(cid).record_reformat(res["body"], res["summary"])
            tally[res["tier"]] += 1
            done += 1
            print(f"[{done}/{len(ids)}] cluster {cid}: {res['tier']}", flush=True)

    print(f"\ndone. haiku={tally['haiku']} sonnet-escalated={tally['sonnet']} kept-raw={tally['raw']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
