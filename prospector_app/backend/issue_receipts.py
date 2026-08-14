"""Structured receipts for meta-repository issue filing."""
from __future__ import annotations

import json
from typing import TypedDict

FILE_ISSUE_COMMAND = "prospector_app/agent/file-issue"


class IssueReceipt(TypedDict):
    ok: bool
    kind: str
    repo: str
    number: int
    url: str


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def parse(command: str, content: object, feedback_repo: str,
          *, is_error: bool = False) -> IssueReceipt | None:
    command = command.lstrip()
    if is_error or not (command == FILE_ISSUE_COMMAND or
                        command.startswith(f"{FILE_ISSUE_COMMAND} ")):
        return None
    try:
        receipt = json.loads(_text(content).strip())
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(receipt, dict) or receipt.get("ok") is not True:
        return None
    number = receipt.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return None
    expected = f"https://github.com/{feedback_repo}/issues/{number}"
    if (receipt.get("kind") != "feedback-issue" or
            receipt.get("repo") != feedback_repo or
            receipt.get("url") != expected):
        return None
    return IssueReceipt(
        ok=True,
        kind="feedback-issue",
        repo=feedback_repo,
        number=number,
        url=expected,
    )


def verified_summary(receipts: list[IssueReceipt]) -> str:
    """Render the authoritative URLs produced by successful file-issue calls."""
    urls = sorted({receipt["url"] for receipt in receipts})
    if not urls:
        return ""
    exact = "\n".join(f"- {url}" for url in urls)
    return f"Prospector-verified filing receipt:\n{exact}"


def attach_verified_summary(text: str, receipts: list[IssueReceipt]) -> str:
    """Append successful filing evidence while preserving the agent's response."""
    summary = verified_summary(receipts)
    if not summary:
        return text
    prefix = f"{text}\n\n---\n\n" if text else ""
    return f"{prefix}{summary}"
