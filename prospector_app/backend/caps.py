"""Capability detection.

Under the org-admin trust model, the app executes upstream as the
configured GitHub App, NOT as
the local login. Upstream writes require both a mintable bot token
(`executor.live_possible`) and a server checkout that is not behind the shared
store's schema. The local login is reported only for display (reads run as it).
"""
from __future__ import annotations

from prospector_app.backend.safety_guard import run
from prospector_app.backend.safety_guard import store_schema_status
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
        from prospector_app.backend import executor
        lr = run(["gh", "api", "user", "--jq", ".login"], timeout=20)
        login = lr.stdout.strip() if lr.returncode == 0 else None
        live = executor.live_possible()
        store_schema = store_schema_status()
        write_ready = store_schema["write_block"] is None
        _cache = {
            "login": login,
            "bot": BOT_LOGIN,
            # Writes go out as the configured app and require a current store
            # schema; per-PR policy remains in the executor.
            "merge_upstream": live and write_ready,
            "store_schema": store_schema,
            "write_block": store_schema["write_block"],
            "review": _review_descriptor(),
            # alert reads/writes run as the same App identity; per-source
            # availability is probed separately by /api/alerts/caps
            "alerts": {"available": live and write_ready},
        }
    return _cache


def refresh() -> None:
    global _cache
    _cache = None
    from prospector_app.backend import executor
    executor.refresh_live()
