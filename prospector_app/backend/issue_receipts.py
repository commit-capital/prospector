"""Receipt validation for meta-repository issue filing."""
from __future__ import annotations

import json
import re

FILE_ISSUE_COMMAND = "prospector_app/agent/file-issue"
FILED_CLAIM_RE = re.compile(
    r"(?ix)(?:^\s*filed\b|\b(?:"
    r"(?:i|we)\s+(?:have\s+)?(?:now\s+)?(?:filed|created|opened)"
    r"|(?:filed|created|opened)\s+it"
    r")\b)"
)


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
          *, is_error: bool = False) -> dict | None:
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
    return receipt


def validate(text: str, receipts: list[dict], feedback_repo: str) -> str:
    pattern = rf"https://github\.com/{re.escape(feedback_repo)}/issues/\d+"
    urls = re.findall(pattern, text or "") if feedback_repo else []
    receipt_urls = {str(receipt["url"]) for receipt in receipts}
    mismatch = any(url not in receipt_urls for url in urls)
    if mismatch or (FILED_CLAIM_RE.search(text or "") and not receipt_urls):
        if receipt_urls:
            exact = "\n".join(f"- {url}" for url in sorted(receipt_urls))
            return ("The issue filing succeeded, but the reported URL did not match the "
                    f"filing receipt.\n\nVerified issue:\n{exact}")
        return ("I did not file an issue. No successful `file-issue` receipt was recorded "
                "for this turn. Any issue text produced in the turn is only a draft.")
    missing = [url for url in sorted(receipt_urls) if url not in urls]
    if missing:
        exact = "\n".join(f"- {url}" for url in missing)
        suffix = f"Verified filing receipt:\n{exact}"
        return f"{text.rstrip()}\n\n{suffix}" if text.strip() else suffix
    return text
