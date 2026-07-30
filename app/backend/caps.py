"""Capability detection.

Under the org-admin trust model, the app executes upstream as the
configured GitHub App, NOT as
the local login. So "can we merge / write upstream" is whether this machine can
actually mint a bot token (executor.live_possible) — with none, every
write is forced to dry-run. The local login is reported only for display (reads
run as it).
"""
from __future__ import annotations

from app.backend.safety_guard import run
from pipeline import review_policy
from pipeline.settings import BOT_LOGIN

_cache: dict | None = None


def _review_descriptor() -> dict:
    """The active review provider, for the frontend to drive its Greptile columns,
    filters, detail card, and retrigger control (all hidden when provider=none)."""
    p = review_policy.active()
    return {
        "provider": p.provider,
        "label": p.label,
        "threshold": p.threshold,
        "score_max": p.score_max,
        "retrigger": p.retrigger_mention is not None,
        "stale_tracking": p.provider == "greptile",
    }


def capabilities() -> dict:
    global _cache
    if _cache is None:
        # deferred: executor imports caps, so import it lazily to avoid a cycle
        from app.backend import executor
        lr = run(["gh", "api", "user", "--jq", ".login"], timeout=20)
        login = lr.stdout.strip() if lr.returncode == 0 else None
        _cache = {
            "login": login,
            "bot": BOT_LOGIN,
            # writes (incl. merge) go out as the configured app; gated per-PR
            # in the executor, not by this login's perms
            "merge_upstream": executor.live_possible(),
            "review": _review_descriptor(),
        }
    return _cache


def refresh() -> None:
    global _cache
    _cache = None
    from app.backend import executor
    executor.refresh_live()
