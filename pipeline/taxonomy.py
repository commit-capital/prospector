"""The ONE subsystem-taxonomy accessor — shared by the PR pipeline and issue_triage.

Heuristic only: the CLUSTER phase uses it to pre-group PRs before semantic
clustering; agents may override per PR. The vocabulary itself is repository
policy and lives in the active profile (pipeline/profile.py); the generic
default has no subsystems, so everything classifies as "other".
"""
from __future__ import annotations

import re

from pipeline import profile


def subsystem_names() -> list[str]:
    """Accepted subsystem values from the active profile, ending with the
    catch-all "other"."""
    return profile.active().subsystem_names()


def classify(title: str, body: str = "") -> str:
    text = (title + " " + (body or "")[:600]).lower()
    for sub in profile.active().subsystems:
        if any(re.search(p, text) for p in sub.match_terms):
            return sub.name
    return "other"
