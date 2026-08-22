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
from pipeline import settings
from pipeline import review_policy

_cache: dict | None = None


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
            "bot": settings.bot_login(),
            "bot_configured": bool(settings.bot_login()),
            # Writes go out as the configured app and require a current store
            # schema; per-PR policy remains in the executor.
            "merge_upstream": live and write_ready,
            "store_schema": store_schema,
            "write_block": store_schema["write_block"],
            # Every registry reviewer with whether it gates this repository —
            # drives the frontend's Review/Scans columns, filters, detail
            # blocks, and retrigger controls.
            "reviewers": review_policy.describe(),
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
