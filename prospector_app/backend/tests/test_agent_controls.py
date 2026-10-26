"""The chat agent answers "what does this button do" from agent/controls.md, so
the map must name real routes, functions and files, and cover every per-item
write the app exposes."""
from __future__ import annotations

import re
from pathlib import Path

from prospector_app.backend import chat

ROOT = Path(__file__).resolve().parents[3]
APP = (ROOT / "prospector_app" / "backend" / "app.py").read_text()
MAP = chat.AGENT_CONTROLS.read_text()
ROUTES = set(re.findall(r'@app\.(?:post|put|delete|get)\("([^"]+)"', APP))
WRITES = set(re.findall(r'@app\.(?:post|put|delete)\("([^"]+)"', APP))
MODULE_DIRS = ("prospector_app/backend", "pipeline", "issue_triage", "alert_triage")


def _named_routes() -> set[str]:
    return set(re.findall(r"/api(?:/(?:[\w-]+|\{[^}]+\}))+", MAP))


def test_every_route_in_the_map_exists():
    assert _named_routes() - ROUTES == set()


def test_every_per_item_write_is_in_the_map():
    per_item = {r for r in WRITES if "{" in r or r.startswith("/api/execute/")}
    assert per_item - _named_routes() == set()


def test_every_function_in_the_map_exists():
    missing = []
    for mod, fn in re.findall(r"`(\w+)\.(?!tsx`)(\w+)`", MAP):
        files = [ROOT / d / f"{mod}.py" for d in MODULE_DIRS if (ROOT / d / f"{mod}.py").exists()]
        if not any(re.search(rf"^def {fn}\b", f.read_text(), re.M) for f in files):
            missing.append(f"{mod}.{fn}")
    assert missing == []


def test_every_file_in_the_map_exists():
    src = ROOT / "prospector_app" / "frontend" / "src"
    missing = [f for f in re.findall(r"`(\w+\.tsx)`", MAP)
               if not list(src.rglob(f))]
    assert missing == []
