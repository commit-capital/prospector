"""Natural-language PR query → a validated filter spec.

A one-shot sandboxed `claude` (same isolation as chat.py, read-only) translates the
operator's sentence into JSON; `coerce` then drops anything not in the schema so
a hallucinated field can never reach the query engine. The engine — not the
model — produces rows, so search results are reproducible and inventable matches
are impossible.
"""
from __future__ import annotations

import asyncio
import json
import re

from pipeline import review_policy
from prospector_app.backend import chat  # reuse CLAUDE_BIN, isolation_flags, REPO_ROOT, system_prompt
from prospector_app.backend import safety_guard
from prospector_app.backend import subproc

# Fields the query vocabulary carries only when Greptile is the configured review
# provider — dropped from both the model prompt and coerce otherwise.
_GREPTILE_SEARCH_FIELDS = {"greptile", "greptile_stale", "greptile_severity"}

_ENUMS = {
    "safety": {"GREEN", "YELLOW", "RED", "not-run"},
    "drift": {"applicable", "already-fixed", "conflicts"},
    "disposition": {"merge", "request-changes", "close-dup", "close-fixed",
                    "close-stale", "needs-human"},
    "ci": {"passing", "failing", "unknown"},
    "threat": {"malicious", "suspicious", "clear"},
    "preset": {"easy", "stale", "merge-ready", "needs-human"},
    "greptile_severity": {"defects", "nits", "clean"},
}
_BOOLS = {"conflicts", "has_tests", "trusted_author", "clean", "artifact_dominated", "greptile_stale"}
_NUMERIC = {"greptile", "score", "age_days"}
_INTS = {"cluster", "max_files", "max_total_lines", "max_score", "risk_tier"}
_OPS = {"<", "<=", "==", ">=", ">"}


def _num_cmp(v) -> dict | None:
    if not isinstance(v, dict) or v.get("op") not in _OPS:
        return None
    val = v.get("value")
    return {"op": v["op"], "value": val} if isinstance(val, (int, float)) and not isinstance(val, bool) else None


def _loc_cmp(v) -> dict | None:
    """Validate a lines-of-code compare {metric, scope, op, value}."""
    if not isinstance(v, dict):
        return None
    metric, scope, op, val = v.get("metric"), v.get("scope"), v.get("op"), v.get("value")
    if metric not in {"additions", "deletions", "both"} or scope not in {"effective", "all"}:
        return None
    if op not in {"<", ">"} or not isinstance(val, (int, float)) or isinstance(val, bool):
        return None
    return {"metric": metric, "scope": scope, "op": op, "value": val}


def coerce(raw) -> dict:
    """Keep only schema-valid keys with valid values. Never raises."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k, v in raw.items():
        if k in _ENUMS:
            if isinstance(v, str) and v in _ENUMS[k]:
                out[k] = v
        elif k in _BOOLS:
            if isinstance(v, bool):
                out[k] = v
        elif k in _NUMERIC:
            c = _num_cmp(v)
            if c:
                out[k] = c
        elif k in _INTS:
            if isinstance(v, int) and not isinstance(v, bool):
                out[k] = v
        elif k in ("q", "author", "paths"):
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
        elif k == "loc":
            c = _loc_cmp(v)
            if c:
                out[k] = c
    if review_policy.active().provider != "greptile":
        for f in _GREPTILE_SEARCH_FIELDS:
            out.pop(f, None)
    return out


def extract_spec(text: str) -> dict:
    """Pull the first JSON object out of model output (fenced or bare)."""
    if not isinstance(text, str):
        return {}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


# Greptile-provider query fields, spliced into the prompt only when Greptile is the
# configured review provider (the braces are doubled to survive str.format).
_GREPTILE_FIELD_DOCS = """- greptile_stale: boolean — true = Greptile reviewed an older commit (score predates author's post-review fixes); false = Greptile reviewed the current head
- greptile_severity: defects|nits|clean — what Greptile's own review text implies about a sub-5 score: "defects" = it flagged a substantive bug/security/logic issue, "nits" = only style-level feedback, "clean" = it left no findings at all
- greptile: {{"op": "<|<=|==|>=|>", "value": <number>}} — the review confidence score
"""


def _review_field_docs() -> str:
    """The provider-specific query fields for the prompt — empty unless Greptile is
    the configured review provider."""
    return _GREPTILE_FIELD_DOCS if review_policy.active().provider == "greptile" else ""


_PROMPT = """Translate the reviewer's PR query into a JSON filter spec.

Output ONLY a single JSON object, no prose. Allowed keys:
- q (string), author (string), cluster (int)
- safety: one of GREEN|YELLOW|RED|not-run   ("no security audit"/"not reviewed" => not-run)
- drift: applicable|already-fixed|conflicts
- disposition: merge|request-changes|close-dup|close-fixed|close-stale|needs-human
- ci: passing|failing|unknown
- threat: malicious|suspicious|clear   (supply-chain threat-scan verdict)
- conflicts/has_tests/trusted_author/clean: booleans
- artifact_dominated: boolean — diff is mostly generated noise (migration snapshots / locale bundles / lockfiles), e.g. "mostly generated", "all snapshots", "bloated by lockfiles"
__REVIEW_FIELDS__- score/age_days: {{"op": "<|<=|==|>=|>", "value": <number>}}
- max_files/max_total_lines/max_score: ints
- paths (string): a file path or fragment the PR must touch, e.g. "PRs touching billing" => "billing", "files under src/auth" => "src/auth"
- risk_tier: 0|1|2|3 — path-based blast-radius tier of the files the PR touches: 0 = orchestration/auth core & supply chain (workflows, package manifests, lockfile), 1 = governed routes/services & db schema/migrations, 2 = shared contracts & other server code, 3 = leaf (ui, docs, tests). e.g. "core-touching PRs" => 0, "leaf-only PRs" => 3
- loc: {{"metric": "additions"|"deletions"|"both", "scope": "effective"|"all", "op": "<"|">", "value": <number>}}
    lines of code changed. scope "effective" = human-written lines (source+test, with generated artifacts like migration snapshots / locale bundles / lockfiles stripped); "all" = the raw diffstat. Default scope to "effective" unless they say "raw"/"total"/"including generated"; default metric to "both".
    e.g. "more than 500 real lines" => {{"metric":"both","scope":"effective","op":">","value":500}}
- preset: easy|stale|merge-ready|needs-human  (use when the query names a lane)

Omit any key you're unsure about. Reviewer query: {query}"""


async def search_to_spec(query: str) -> dict:
    """Run the one-shot agent and return a validated spec (possibly {})."""
    prompt = _PROMPT.replace("__REVIEW_FIELDS__", _review_field_docs()).format(query=query)
    cmd = [chat.CLAUDE_BIN, "-p", prompt,
           *chat.isolation_flags(can_write=False), "--output-format", "json",
           "--append-system-prompt", chat.system_prompt()]
    proc = await subproc.spawn(
        cmd, cwd=chat.REPO_ROOT, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True, env=safety_guard.operator_env())
    out, _ = await proc.communicate()
    try:
        envelope = json.loads(out.decode("utf-8", "replace"))
        text = envelope.get("result", "") if isinstance(envelope, dict) else ""
    except json.JSONDecodeError:
        text = out.decode("utf-8", "replace")
    return coerce(extract_spec(text))
