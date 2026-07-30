"""Agentic 'deep search': judge a candidate set of PRs against a free-text query.

The fast search (pr_search) maps a query to deterministic filters. Deep search
handles the residual *semantic* case the schema can't express — e.g. "PRs that
look like they work around the rate limiter" — by having the sandboxed headless
agent read each PR's compact facts and decide.

Batched + run in parallel for throughput. Output is coerced so only a real
candidate PR can appear in the result (a hallucinated number is dropped — same
stance as pr_search.coerce). Per-PR verdicts are cached by (query, pr, head_sha)
so a re-run over an overlapping set is stable and nearly free, and a moved head
invalidates just that PR's verdict.

Judged on COMPACT FACTS only (title, description, changed file paths, the
pipeline's own summary, signals) — no diffs — so it is cheap and fast; queries
that hinge on in-code detail not reflected in those facts are out of scope.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from app.backend import chat  # CLAUDE_BIN, isolation_flags, REPO_ROOT, APP_ROOT, system_prompt
from app.backend import data
from app.backend import service
from app.backend import subproc
from app.backend import testpaths

if TYPE_CHECKING:
    from pipeline.model import Pr

CACHE_DIR = chat.APP_ROOT / "cache" / "deep_search"
BATCH_SIZE = 25          # PRs of compact facts per agent call
MAX_CONCURRENCY = 5      # parallel headless `claude` processes
MAX_CANDIDATES = 500     # hard ceiling; the UI narrows with fast filters first
_BODY_BUDGET = 400       # chars of PR description fed to the agent
_REASON_BUDGET = 200     # chars of the per-PR reason kept


def _norm_query(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip().lower())


def _cache_path(query: str) -> Path:
    h = hashlib.sha256(_norm_query(query).encode()).hexdigest()[:16]
    return CACHE_DIR / f"{h}.json"


def _load_cache(query: str) -> dict:
    p = _cache_path(query)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_cache(query: str, cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(query).write_text(json.dumps(cache))


def _head_sha(rec: Pr) -> str:
    return rec.head_sha or ""


def compact_record(rec: Pr, body: str | None = None) -> dict:
    """The cheap, diff-free fact sheet the agent judges one PR on. `body` is the
    PR description, hydrated by the caller — the cached record carries none, since
    the bulk load omits it."""
    sig = rec.signals or {}
    ds = sig.get("diffstat") or {}
    summ = rec.section("summary") or {}
    paths = testpaths.changed_paths(service.diff_text(rec) or "")
    return {
        "pr": rec.number,
        "title": rec.title,
        "description": (body or "")[:_BODY_BUDGET],
        "files": paths[:25],
        "summary": summ.get("one_liner"),
        "mechanism": summ.get("mechanism"),
        "greptile": rec.greptile,
        "ci": rec.ci,
        "additions": ds.get("additions"),
        "deletions": ds.get("deletions"),
        "changed_files": ds.get("changed_files"),
        "disposition": rec.disposition,
    }


def _extract_array(text: str) -> list:
    """Pull the first JSON array out of model output (fenced or bare)."""
    m = re.search(r"\[.*\]", text or "", re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
        return arr if isinstance(arr, list) else []
    except json.JSONDecodeError:
        return []


def coerce_judgments(text: str, valid_prs: set[int]) -> dict[int, dict]:
    """Model output → {pr: {match: bool, reason: str}} for real candidate PRs
    only. A hallucinated or out-of-batch PR number is dropped; bad shapes are
    skipped. Never raises."""
    out: dict[int, dict] = {}
    for item in _extract_array(text):
        if not isinstance(item, dict):
            continue
        pr = item.get("pr")
        if not isinstance(pr, int) or isinstance(pr, bool) or pr not in valid_prs:
            continue
        out[pr] = {"match": bool(item.get("match")),
                   "reason": str(item.get("reason") or "").strip()[:_REASON_BUDGET]}
    return out


_JUDGE_PROMPT = """You are filtering pull requests for a reviewer. For EACH PR below, decide whether it satisfies the reviewer's criterion, judging ONLY on the facts given — never invent or assume anything beyond them.

REVIEWER CRITERION: {query}

PRs (JSON array of compact facts):
{records}

Output ONLY a JSON array, one object per PR, no prose:
[{{"pr": <number>, "match": true|false, "reason": "<=15 words: why it does or doesn't match"}}]"""


async def _judge_batch(query: str, records: list[dict], sem: asyncio.Semaphore) -> dict[int, dict]:
    """Run one sandboxed headless `claude` over a batch; return coerced verdicts.
    A subprocess/parse failure degrades to {} (those PRs count as non-matches)."""
    prompt = _JUDGE_PROMPT.format(query=query, records=json.dumps(records, ensure_ascii=False))
    # Read-only judge: no token, no write tools — the strictly read-only sandbox.
    cmd = [chat.CLAUDE_BIN, "-p", prompt, *chat.isolation_flags(can_write=False),
           "--output-format", "json", "--append-system-prompt", chat.system_prompt()]
    valid = {r["pr"] for r in records}
    async with sem:
        proc = await subproc.spawn(
            cmd, cwd=chat.REPO_ROOT, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        out, _ = await proc.communicate()
    try:
        env = json.loads(out.decode("utf-8", "replace"))
        text = env.get("result", "") if isinstance(env, dict) else ""
    except json.JSONDecodeError:
        text = out.decode("utf-8", "replace")
    return coerce_judgments(text, valid)


async def stream(query: str, prs: list[int]):
    """Judge `prs` against `query`, yielding progress then a final result.

    Yields {"type": "progress", "done", "total"} as batches land, then
    {"type": "result", "matches": [{pr, reason}], "total", "capped",
     "judged", "from_cache"}. Cached verdicts are reused; only the rest hit the
    agent. An empty query or zero candidates short-circuits to an empty result.
    """
    query = (query or "").strip()
    candidates = list(dict.fromkeys(int(p) for p in (prs or [])))
    capped = len(candidates) > MAX_CANDIDATES
    candidates = candidates[:MAX_CANDIDATES]
    store = data.prs()
    recs = [(n, store[n]) for n in candidates if n in store]
    total = len(recs)

    if not query or total == 0:
        yield {"type": "result", "matches": [], "total": total, "capped": capped,
               "judged": 0, "from_cache": 0}
        return

    cache = _load_cache(query)
    verdicts: dict[int, dict] = {}
    to_judge: list[tuple[int, Pr]] = []
    for n, rec in recs:
        v = cache.get(f"{n}:{_head_sha(rec)}")
        if v is not None:
            verdicts[n] = v
        else:
            to_judge.append((n, rec))

    done = len(verdicts)
    yield {"type": "progress", "done": done, "total": total}

    # The cached records carry no body (the bulk load omits it); hydrate the
    # descriptions for just the PRs we're about to judge in one store read.
    bodies = data.pr_bodies([n for n, _ in to_judge])
    batches = [to_judge[i:i + BATCH_SIZE] for i in range(0, len(to_judge), BATCH_SIZE)]
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def do_batch(batch):
        records = [compact_record(rec, bodies.get(n)) for n, rec in batch]
        return batch, await _judge_batch(query, records, sem)

    for coro in asyncio.as_completed([do_batch(b) for b in batches]):
        batch, judged = await coro
        for n, rec in batch:
            v = judged.get(n, {"match": False, "reason": ""})
            verdicts[n] = v
            cache[f"{n}:{_head_sha(rec)}"] = v
        done += len(batch)
        yield {"type": "progress", "done": min(done, total), "total": total}

    _save_cache(query, cache)
    matches = [{"pr": n, "reason": verdicts[n].get("reason", "")}
               for n in candidates if verdicts.get(n, {}).get("match")]
    yield {"type": "result", "matches": matches, "total": total, "capped": capped,
           "judged": len(to_judge), "from_cache": total - len(to_judge)}
