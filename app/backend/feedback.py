"""Where the app's own bug/feature reports go.

The 🐞 Feedback button opens a popup dialog, uses Claude to generate a polished
title and body from the operator's description, then opens GitHub's prefilled
new-issue page in a new tab. This module supplies the two things the frontend
can't know: which repo to file into, which GitHub login to pre-assign, and the
AI-generated issue content.

Read-only on GitHub — it never files anything itself. The issue is created by the
operator's own GitHub web session, which is also the only path on a private repo
where a pasted screenshot uploads inline.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import subprocess
import urllib.request
from functools import lru_cache
from typing import TypedDict

from pipeline import settings
from app.backend import instance

LABELS = ["app"]

_GENERATE_TIMEOUT = 8
_GENERATE_MODEL = "claude-haiku-4-5-20251001"


class FeedbackTarget(TypedDict):
    repo: str | None
    assignee: str | None
    labels: list[str]
    branch: str | None
    worktree: str | None


class GenerateResult(TypedDict):
    title: str
    body: str


@lru_cache(maxsize=1)
def operator_login() -> str | None:
    """The local gh login (e.g. "octocat"), to pre-assign the new issue, or
    None if gh isn't available. Cached for the process; never raises."""
    try:
        r = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def target() -> FeedbackTarget:
    """The repo, pre-assignee, label, and this-checkout context the Feedback
    button needs to build GitHub's prefilled new-issue URL. repo is None when
    PROSPECTOR_FEEDBACK_REPO is unset, which hides the button."""
    inst = instance.instance()
    return {
        "repo": settings.FEEDBACK_REPO or None,
        "assignee": operator_login(),
        "labels": LABELS,
        "branch": inst.get("branch"),
        "worktree": inst.get("worktree"),
    }


_GENERATE_PROMPT = """\
You are helping file a GitHub issue for a PR-triage app (a web app for reviewing and executing pull-request triage).

Convert this informal description into a concise GitHub issue. Keep it tight — \
one short paragraph max for the body, no extra headers or lists unless they add \
real clarity.

Description:
{description}

Respond with ONLY a JSON object with exactly two keys:
- "title": a specific issue title under 80 characters
- "body": 1–3 sentences describing the problem or request

JSON:"""


def _derive_title(description: str) -> str:
    """Derive a fallback title from the raw description — first line, capped at 80 chars."""
    first_line = description.strip().split("\n")[0].strip()
    if not first_line:
        first_line = description.strip()
    if len(first_line) > 80:
        first_line = first_line[:77] + "..."
    return first_line


_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # Honour custom CA bundle set by the agent proxy (ccr).
    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if ca:
        ctx.load_verify_locations(ca)
    return ctx


def _call_anthropic(prompt: str, api_key: str) -> str:
    """Synchronous Anthropic API call — intended to run in a thread via asyncio.to_thread."""
    payload = json.dumps({
        "model": _GENERATE_MODEL,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        _ANTHROPIC_API_URL,
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_GENERATE_TIMEOUT, context=_ssl_context()) as resp:
        return resp.read().decode("utf-8", "replace")


async def generate_issue(description: str) -> GenerateResult:
    """Use Claude Haiku to turn a raw app-feedback description into a concise
    GitHub issue title and body. Calls the Anthropic API directly via stdlib
    (no subprocess startup overhead). Falls back to derived title + raw description
    on any error so the caller always gets a non-empty result."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"title": _derive_title(description), "body": description}
    prompt = _GENERATE_PROMPT.format(description=description)
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(_call_anthropic, prompt, api_key),
            timeout=_GENERATE_TIMEOUT + 2,
        )
        data = json.loads(raw)
        text: str = ((data.get("content") or [{}])[0]).get("text", "")
        parsed = json.loads(text)
        title = str(parsed.get("title", "")).strip()
        body = str(parsed.get("body", "")).strip()
        return {
            "title": title or _derive_title(description),
            "body": body or description,
        }
    except (json.JSONDecodeError, AttributeError, TypeError, IndexError, KeyError):
        return {"title": _derive_title(description), "body": description}
    except Exception:
        return {"title": _derive_title(description), "body": description}
