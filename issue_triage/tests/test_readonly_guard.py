"""No module in issue_triage may reference a GitHub write verb."""
import re
from pathlib import Path

FORBIDDEN = [
    r"gh\s+pr\s+(comment|close|edit|merge|review)",
    r"gh\s+issue\s+(create|edit|close|comment)",
    r"gh\s+api\s+-X\s+(POST|PATCH|DELETE|PUT)",
    r"curl\s+-X\s+(POST|PATCH|DELETE|PUT)",
    r"git\s+push\s+.*test-owner/test-repo",
]


def test_no_write_verbs():
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for py in root.rglob("*.py"):
        if "tests" in py.parts:
            continue
        text = py.read_text()
        for pat in FORBIDDEN:
            if re.search(pat, text, re.IGNORECASE):
                offenders.append((py.name, pat))
    assert offenders == [], f"write verbs found: {offenders}"
