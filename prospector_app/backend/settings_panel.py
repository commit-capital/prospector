"""Every setting in `settings_registry` as this process runs it, for the Setup
page: its value, where the value came from (the repo-root `.env`, the process
environment, or the default), and for the sandbox sizes the number in effect.
A secret's value is never returned, only whether it is set.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import TypedDict

from pipeline import settings_registry, verify_driver
from prospector_app.backend import env_file


class SettingRow(TypedDict, total=False):
    name: str
    label: str
    group: str
    kind: str
    value: str
    default: str
    help: str
    editable: bool
    choices: list[str]
    minimum: int
    maximum: int | None
    source: str  # .env | environment | default
    effective: str


# The settings whose value in effect is computed rather than read.
_EFFECTIVE: dict[str, Callable[[], object]] = {
    "TRIAGE_SANDBOX_LARGE_SLOTS": verify_driver.large_slots,
    "TRIAGE_SANDBOX_LARGE_CPUS": verify_driver.large_cpus,
}


def _env_file_keys() -> set[str]:
    try:
        text = env_file.ENV_PATH.read_text()
    except OSError:
        return set()
    keys = set()
    for line in text.splitlines():
        bare = line.strip()
        if bare and not bare.startswith("#") and "=" in bare:
            keys.add(bare.split("=", 1)[0].strip().removeprefix("export ").strip())
    return keys


def report() -> dict:
    in_file = _env_file_keys()
    rows: list[SettingRow] = []
    for s in settings_registry.SETTINGS:
        raw = os.environ.get(s.name)
        source = ".env" if s.name in in_file else "environment" if raw is not None else "default"
        row: SettingRow = {
            "name": s.name, "label": s.label, "group": s.group, "kind": s.kind,
            "value": ("set" if raw else "") if s.kind == "secret" else (raw or ""),
            "default": s.default, "help": s.help, "editable": s.editable,
            "choices": list(s.choices), "minimum": s.minimum, "maximum": s.maximum,
            "source": source,
        }
        compute = _EFFECTIVE.get(s.name)
        if compute is not None:
            try:
                row["effective"] = str(compute())
            except (OSError, RuntimeError, ValueError):
                pass
        rows.append(row)
    return {"groups": [{"id": g, "label": label} for g, label in settings_registry.GROUPS.items()],
            "settings": rows}
