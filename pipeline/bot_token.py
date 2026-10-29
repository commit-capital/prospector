"""The configured GitHub App's installation token, minted by
pipeline/get-bot-token.sh and reused until shortly before it expires.

The helper reads TRIAGE_BOT_KEY_FILE from the process environment or the
repo-root .env, and reads the PEM only inside its own subprocess."""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

GET_TOKEN = Path(__file__).resolve().parent / "get-bot-token.sh"
LIFETIME_SECONDS = 60 * 60
# The least life a handed-out token has, so the write that takes it finishes
# before it expires.
MARGIN_SECONDS = 5 * 60

logger = logging.getLogger(__name__)

# The lock keeps a mint that straddles forget() from caching a token for an
# App the deployment no longer names.
_lock = threading.Lock()
_token: str | None = None
_expires_at = 0.0
_last_error: str | None = None


def get() -> str | None:
    """A token with at least MARGIN_SECONDS of life, or None when none can be
    minted (`last_error` says why). Never raises."""
    global _token, _expires_at, _last_error
    with _lock:
        if _token and time.monotonic() < _expires_at - MARGIN_SECONDS:
            return _token
        _token = None
        if not GET_TOKEN.exists():
            _last_error = f"{GET_TOKEN} not found"
            return None
        minted_at = time.monotonic()
        try:
            r = subprocess.run(["bash", str(GET_TOKEN)], capture_output=True, text=True, timeout=30)
        except Exception:
            logger.exception("token helper failed unexpectedly")
            _last_error = "token helper failed unexpectedly; see server logs"
            return None
        tok = (r.stdout or "").strip()
        if not tok:
            _last_error = (r.stderr or "").strip() or f"get-bot-token.sh exited {r.returncode} with no output"
            return None
        _token, _expires_at, _last_error = tok, minted_at + LIFETIME_SECONDS, None
        return tok


def available() -> bool:
    return get() is not None


def last_error() -> str | None:
    """Why the last `get` returned None: the key or helper missing, or whatever
    get-bot-token.sh printed to stderr. None after a success."""
    return _last_error


def forget() -> None:
    """Drop the cached token, so the next `get` mints against the current
    configuration."""
    global _token
    with _lock:
        _token = None
