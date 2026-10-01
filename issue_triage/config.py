"""Shared paths and repository accessors for the issue_triage pipeline."""
from pathlib import Path

from pipeline import settings

ROOT = Path(__file__).resolve().parent


# Read through here from issue_triage modules.
def repo() -> str:
    return settings.repo()


def repo_owner() -> str:
    return settings.repo_owner()


def repo_name() -> str:
    return settings.repo_name()
