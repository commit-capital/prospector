# First-Run Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A checkout with no configuration boots into a wizard that gets a user from nothing to a running Prospector — either by pasting a teammate's deployment bundle or by guided setup — one opt-in step at a time.

**Architecture:** `pipeline/settings.py` stops raising at import and exposes the deployment target through accessors plus a `configured()` predicate. One middleware refuses every `/api/*` route on an unconfigured app so it can never answer with a plausible-but-empty store. A step-scoped write surface (`/api/onboarding/apply`) writes `.env` and `profile.json`, then adopts them in-process by resetting the store singleton and two caches. The SPA routes to `/welcome` when `/api/meta` reports `configured: false`.

**Tech Stack:** Python 3.14.6 (uv-locked), FastAPI, SQLAlchemy, pytest, pyright, ruff; React 19 + TypeScript + react-router + Vite, pnpm.

**Spec:** `docs/superpowers/specs/2026-08-20-first-run-onboarding-design.md`

## Global Constraints

- **Run everything through `uv run`** from the repo root. Never activate a venv manually.
- **`uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness` must stay at 0 errors.** CI gate.
- **`uv run ruff check .` must stay at 0 findings.** CI gate; the tree is currently clean.
- **`uv run pytest` from the repo root** runs all four suites and must stay green.
- **Frontend gate, from `prospector_app/frontend/`:** `pnpm run build` (the `tsc -b` step must be 0 errors) and `pnpm exec eslint <changed files>` adds no new errors. Use **pnpm**, never npm.
- **Type every signature as precisely as the value allows.** `str | None`, not bare `str`. Never weaken a precise type to silence the checker.
- **No quoted/string type annotations.** Every module has `from __future__ import annotations`; use `if TYPE_CHECKING:` imports for cycles.
- **Imports are qualified:** `from pipeline import settings`, `from prospector_app.backend import data`. Never `sys.path.insert`, never a bare sibling import.
- **Comments describe the code as it is now.** No "previously", no "instead of X", no counterfactual rationale. A comment that only makes sense by contrast with an older version gets cut.
- **A docstring earns its place only when the body isn't self-evident.** Never restate the signature or the test name in prose.
- **Keep `pyproject.toml` dependency lists alphabetical** if you add a dependency (you should not need to).
- **Never hand-run `gh pr merge/close/comment/review`** against the triaged repository.
- **Commit after every task.** End commit messages with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

## File Structure

**Created:**

| File | Responsibility |
|------|----------------|
| `prospector_app/backend/env_file.py` | The ONE atomic `.env` merge-and-replace. Takes an explicit allowlist per call; knows nothing about which keys matter. |
| `prospector_app/backend/onboarding.py` | The ONE config write path: step allowlists, validation, `apply`, `probe`, `reconfigure`, bundle parse/build. |
| `prospector_app/backend/tests/test_env_file.py` | The atomic writer in isolation. |
| `prospector_app/backend/tests/test_onboarding.py` | Allowlists, validation, apply/rollback, bundle round-trip. |
| `prospector_app/backend/tests/test_unconfigured_gate.py` | The middleware refusal — the regression test for an unconfigured app answering `{"items":[]}`. |
| `prospector_app/frontend/src/views/Welcome.tsx` | The wizard: branch choice, the three-step ladder, per-step Easy/Full forks. |

**Modified:**

| File | Change |
|------|--------|
| `pipeline/settings.py` | Constants → accessors; `configured()`; both `SystemExit`s removed. |
| `pipeline/profile.py:445` | Public `reset_cache()`. |
| `prospector_app/backend/data.py:26` | `_store` singleton becomes resettable via `reset()`. |
| `prospector_app/backend/app.py:94` | The unconfigured gate middleware; `/api/onboarding/*` routes. |
| `prospector_app/backend/repo_meta.py` | `/api/meta` gains `configured`. |
| `prospector_app/backend/worker_control.py` | `_rewrite`/atomic write extracted to `env_file`; `share_snippet` → bundle v1. |
| `prospector_app/backend/safety_guard.py:30,192` | `settings.bot_login()`; explicit empty-identity refusal. |
| `prospector_app/backend/executor.py` | Operation-scoped target resolution. |
| ~33 modules with `from pipeline.settings import REPO` | Qualified accessor calls. |
| `prospector_app/frontend/src/main.tsx` | `/welcome` route. |
| `prospector_app/frontend/src/App.tsx` | Redirect to `/welcome` when unconfigured. |
| `prospector_app/frontend/src/api.ts` | Onboarding client + `RepoMeta.configured`. |
| `prospector_app/frontend/src/views/Setup.tsx` | Share card emits bundle v1; checkbox removed. |
| `CLAUDE.md`, `.env.example`, `README.md` | Trust model + first-run instructions. |

---

## Slice 1 — Accessors

### Task 1: Deployment target becomes accessors

The largest diff and the smallest behaviour change. Nothing calls `apply` yet, so
this slice must be a pure refactor: same values, read later.

**Files:**
- Modify: `pipeline/settings.py:42-49, 53-61, 86-92, 98, 116-154`
- Modify: ~33 modules importing settings names directly (list generated in Step 3)
- Test: `pipeline/tests/test_settings_accessors.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `settings.repo() -> str`, `repo_owner() -> str`, `repo_name() -> str`,
  `repo_url() -> str`, `display_name() -> str`, `bot_login() -> str`,
  `store_url() -> str | None`, `profile_path() -> str`, `review_provider() -> str`,
  `review_threshold() -> int | None`, `feedback_repo() -> str`,
  `verify_scratch() -> Path`, `configured() -> bool`. `settings.REPO_ROOT` stays a
  module constant.

- [ ] **Step 1: Write the failing test**

Create `pipeline/tests/test_settings_accessors.py`:

```python
"""The deployment target is read from the environment on each call, so a value
written to .env during onboarding takes effect without a restart."""
from __future__ import annotations

from pathlib import Path

from pipeline import settings


class TestConfigured:
    def test_true_for_a_well_formed_repo(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "octocat/hello-world")
        assert settings.configured() is True

    def test_false_when_unset(self, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        assert settings.configured() is False

    def test_false_without_an_owner(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "hello-world")
        assert settings.configured() is False


class TestAccessorsReadTheCurrentEnvironment:
    def test_repo_and_its_parts_follow_a_change(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        assert settings.repo() == "acme/widgets"
        assert settings.repo_owner() == "acme"
        assert settings.repo_name() == "widgets"
        assert settings.repo_url() == "https://github.com/acme/widgets"
        monkeypatch.setenv("TRIAGE_REPO", "other/thing")
        assert settings.repo() == "other/thing"
        assert settings.repo_owner() == "other"

    def test_unset_repo_reads_empty_rather_than_raising(self, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        assert settings.repo() == ""
        assert settings.repo_owner() == ""
        assert settings.repo_name() == ""

    def test_bot_login_empty_is_legal(self, monkeypatch):
        monkeypatch.delenv("TRIAGE_BOT_LOGIN", raising=False)
        assert settings.bot_login() == ""

    def test_display_name_falls_back_to_the_repo_short_name(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.delenv("TRIAGE_DISPLAY_NAME", raising=False)
        assert settings.display_name() == "widgets"

    def test_verify_scratch_is_per_repo(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.delenv("TRIAGE_VERIFY_SCRATCH", raising=False)
        assert settings.verify_scratch() == Path.home() / ".pr-triage-verify" / "acme-widgets"
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest pipeline/tests/test_settings_accessors.py -q
```

Expected: `AttributeError: module 'pipeline.settings' has no attribute 'configured'`.

- [ ] **Step 3: Inventory every call site before touching anything**

```bash
grep -rn "^from pipeline.settings import" --include=*.py . | grep -v "\.venv\|/tests/" > /tmp/direct-imports.txt
grep -rn "settings\.\(REPO\|REPO_OWNER\|REPO_NAME\|REPO_URL\|DISPLAY_NAME\|BOT_LOGIN\|STORE_URL\|PROFILE_PATH\|REVIEW_PROVIDER\|REVIEW_THRESHOLD\|FEEDBACK_REPO\|VERIFY_SCRATCH\)\b" --include=*.py . | grep -v "\.venv" > /tmp/qualified.txt
wc -l /tmp/direct-imports.txt /tmp/qualified.txt
```

Keep both lists open. `REPO_ROOT` appears in neither — it stays a constant, and a
conversion that touches it is a mistake.

- [ ] **Step 4: Convert `pipeline/settings.py`**

Replace the two `SystemExit` blocks and the derived constants:

```python
def configured() -> bool:
    """Whether this checkout has a deployment target. The ONE predicate the
    unconfigured gate and the onboarding wizard both read."""
    return "/" in os.environ.get("TRIAGE_REPO", "")


def repo() -> str:
    """"owner/name" of the upstream repository being triaged. Empty until the
    deployment is configured; every upstream read and write targets it."""
    return os.environ.get("TRIAGE_REPO", "")


def repo_owner() -> str:
    return repo().split("/", 1)[0] if "/" in repo() else ""


def repo_name() -> str:
    return repo().split("/", 1)[1] if "/" in repo() else ""


def repo_url() -> str:
    return f"https://github.com/{repo()}"


def display_name() -> str:
    """Human-facing product name for the app, defaulting to the repo short name."""
    return os.environ.get("TRIAGE_DISPLAY_NAME") or repo_name()


def bot_login() -> str:
    """Login of the GitHub App upstream writes post as. Empty when no App is
    configured, which makes every upstream write refuse."""
    return os.environ.get("TRIAGE_BOT_LOGIN", "")


def store_url() -> str | None:
    """SQLAlchemy URL for the backing store; None falls back to local SQLite."""
    return os.environ.get("TRIAGE_STORE_URL") or None


def feedback_repo() -> str:
    return os.environ.get("PROSPECTOR_FEEDBACK_REPO", "")


def profile_path() -> str:
    return os.environ.get("TRIAGE_PROFILE", "")


def review_provider() -> str:
    return parse_review_provider(os.environ.get("TRIAGE_REVIEW_PROVIDER"))


def review_threshold() -> int | None:
    raw = os.environ.get("TRIAGE_REVIEW_THRESHOLD", "")
    return int(raw) if raw else None


def verify_scratch() -> Path:
    """Host scratch root for the VERIFY sandbox. Per-repo so two triaged
    repositories on one machine never share base trees. Must live under $HOME on
    macOS+Colima: virtiofs shares only $HOME."""
    configured_path = os.environ.get("TRIAGE_VERIFY_SCRATCH", "")
    if configured_path:
        return Path(configured_path).expanduser()
    return Path.home() / ".pr-triage-verify" / f"{repo_owner()}-{repo_name()}"
```

Keep `REPO_ROOT`, `load_env_file`, `default_branch`, `STORE_ALLOW_STALE`,
`STORE_ALLOW_FOREIGN_REPO`, and the existing lane accessors as they are. Delete
the module-level `_repo`, `REPO`, `REPO_OWNER`, `REPO_NAME`, `REPO_URL`,
`DISPLAY_NAME`, `FEEDBACK_REPO`, `_bot_login`, `BOT_LOGIN`, `STORE_URL`,
`PROFILE_PATH`, `REVIEW_PROVIDER`, `REVIEW_THRESHOLD`, `VERIFY_SCRATCH`.

- [ ] **Step 5: Run the accessor test — it should pass now**

```bash
uv run pytest pipeline/tests/test_settings_accessors.py -q
```

Expected: PASS. Everything else is still broken; that's next.

- [ ] **Step 6: Convert the direct-import sites**

For each line in `/tmp/direct-imports.txt`, drop the `from pipeline.settings import X`
line, add `from pipeline import settings` if absent, and change each use to the
accessor. `alert_triage/config.py:16-18` re-exports `REPO`/`REPO_NAME`/`REPO_OWNER`
with `as` — convert those to thin accessor wrappers so its own consumers keep working:

```python
def repo() -> str:
    return settings.repo()


def repo_owner() -> str:
    return settings.repo_owner()


def repo_name() -> str:
    return settings.repo_name()
```

`prospector_app/backend/safety_guard.py:30` is the security-relevant one: its
`BOT_LOGIN` is used only in an error message at line 192, so it becomes
`settings.bot_login()` there. Task 9 adds the refusal.

- [ ] **Step 7: Convert the qualified sites**

For each line in `/tmp/qualified.txt`, `settings.REPO` → `settings.repo()`, and so
on. These are mechanical; the checker catches misses.

- [ ] **Step 8: Update `conftest.py`'s comment**

`conftest.py:4-7` says settings "requires TRIAGE_REPO and TRIAGE_BOT_LOGIN (no
defaults)". That is no longer true of the code as it stands. Rewrite:

```python
"""Repo-root pytest config: make every test session hermetic against a developer's
real .env. Set before any project module imports settings (which loads .env).

The identity values are deliberately fake and are NOT the real deployment's
repo/bot — a test that silently depends on the real identity fails here, which is
the point. setdefault so a real shell export still wins."""
```

The `setdefault` lines stay: the suite runs configured, and Task 4's tests opt
out per-test with `monkeypatch.delenv`.

- [ ] **Step 9: Run every gate**

```bash
uv run ruff check . && uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness && uv run pytest -q
```

Expected: ruff 0 findings, pyright 0 errors, full suite green. Any pyright error
naming a settings attribute is an unconverted site — fix it rather than adding a
constant back.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Read the deployment target through accessors

The repo, bot identity, store URL, profile path, review policy, and verify
scratch root are read from the environment on each call rather than bound at
import, matching the worker-lane accessors already here. settings.configured()
is the one predicate for whether this checkout has a deployment target, and
neither a missing repo nor a missing bot login exits at import.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Slice 2 — Adoption and the gate

### Task 2: Resettable store and profile cache

**Files:**
- Modify: `pipeline/profile.py:445-470`
- Modify: `prospector_app/backend/data.py:26, 46, 291-305`
- Test: `prospector_app/backend/tests/test_data_reset.py` (create)

**Interfaces:**
- Consumes: `settings.store_url()` from Task 1.
- Produces: `data.reset() -> None`, `profile.reset_cache() -> None`.

- [ ] **Step 1: Write the failing test**

Create `prospector_app/backend/tests/test_data_reset.py`:

```python
"""Adopting a new store in the running process. The snapshot and the engine are
both built at import, so a configuration write that does not reset them leaves
every read serving the store the process started with."""
from __future__ import annotations

from pipeline import profile
from prospector_app.backend import data


def test_reset_rebuilds_the_store_against_the_current_url(tmp_path, monkeypatch):
    before = data.store()
    monkeypatch.setenv("TRIAGE_STORE_URL", f"sqlite:///{tmp_path}/other.db")
    data.reset()
    after = data.store()
    assert after is not before
    assert str(tmp_path) in str(after.engine.url)


def test_reset_empties_the_snapshot(monkeypatch):
    data.reset()
    assert data.prs() == {}
    assert data.clusters() == {}


def test_profile_reset_cache_rereads_the_file(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    path.write_text('{"version": 1, "subsystems": []}')
    monkeypatch.setenv("TRIAGE_PROFILE", str(path))
    first = profile.active()
    path.write_text('{"version": 1, "subsystems": [{"name": "core", "match": ["core"]}]}')
    profile.reset_cache()
    second = profile.active()
    assert first is not second
    assert [s.name for s in second.subsystems] == ["core"]
```

Check `data`'s actual snapshot accessor names before writing the assertions —
if `data.prs()` / `data.clusters()` are named differently, use the real names.

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest prospector_app/backend/tests/test_data_reset.py -q
```

Expected: `AttributeError: module ... has no attribute 'reset'`.

- [ ] **Step 3: Add `profile.reset_cache()`**

In `pipeline/profile.py`, below `active()`:

```python
def reset_cache() -> None:
    """Drop the parsed-profile cache so a rewritten file at the same path is
    read again. `_load` is keyed by path, so a same-path rewrite is invisible
    without this."""
    _load.cache_clear()
```

- [ ] **Step 4: Make `data`'s store resettable**

`data.py:26` is `_store = Store()`. Add below the snapshot globals:

```python
def reset() -> None:
    """Rebuild the store against the current configuration and empty the
    snapshot. Takes `_check_lock` so it cannot race the background freshener."""
    global _store
    with _check_lock:
        _store = Store()
        _prs.clear()
        _clusters.clear()
        _pr_to_clusters_idx.clear()
```

Also clear whatever watermark globals the snapshot freshener keeps — read
`data.py:291-305` (`refresh`) and reset the same state it does, so a reset
snapshot refetches from scratch rather than resuming at a watermark belonging to
the previous store.

- [ ] **Step 5: Run the tests**

```bash
uv run pytest prospector_app/backend/tests/test_data_reset.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Let the store and profile cache be rebuilt in place

data.reset() rebuilds the engine against the current store URL and empties the
snapshot under the freshener's lock; profile.reset_cache() drops the per-path
parse cache so a rewritten profile.json is read again.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

### Task 3: The unconfigured gate

**Files:**
- Modify: `prospector_app/backend/app.py:94-103`
- Modify: `prospector_app/backend/repo_meta.py`
- Test: `prospector_app/backend/tests/test_unconfigured_gate.py` (create)

**Interfaces:**
- Consumes: `settings.configured()` from Task 1.
- Produces: the `/api/*` 409 refusal; `/api/meta` gains `configured: bool`.

- [ ] **Step 1: Write the failing test**

Create `prospector_app/backend/tests/test_unconfigured_gate.py`:

```python
"""An unconfigured app must refuse to look like a working one.

With no deployment target, data.store() falls back to a local SQLite file and
every list route answers {"items": []} — a Prospector watching an empty
repository is indistinguishable from a Prospector that was never configured.
The gate is the one place that refusal lives.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prospector_app.backend import app as app_mod


@pytest.fixture
def unconfigured(monkeypatch):
    monkeypatch.delenv("TRIAGE_REPO", raising=False)
    return TestClient(app_mod.app, raise_server_exceptions=False)


@pytest.fixture
def configured():
    return TestClient(app_mod.app, raise_server_exceptions=False)


GATED = ["/api/clusters", "/api/activity", "/api/setup/readiness"]


class TestUnconfigured:
    @pytest.mark.parametrize("path", GATED)
    def test_api_routes_refuse(self, unconfigured, path):
        r = unconfigured.get(path)
        assert r.status_code == 409
        assert r.json()["unconfigured"] is True

    def test_meta_is_served_so_the_spa_can_route(self, unconfigured):
        r = unconfigured.get("/api/meta")
        assert r.status_code == 200
        assert r.json()["configured"] is False

    def test_onboarding_state_is_served(self, unconfigured):
        assert unconfigured.get("/api/onboarding/state").status_code == 200


class TestConfigured:
    @pytest.mark.parametrize("path", GATED)
    def test_api_routes_are_served(self, configured, path):
        assert configured.get(path).status_code == 200

    def test_meta_reports_configured(self, configured):
        assert configured.get("/api/meta").json()["configured"] is True
```

`test_onboarding_state_is_served` fails until Task 7 registers the route. Mark it
`@pytest.mark.xfail(reason="route lands in Task 7", strict=True)` now and remove
the marker in Task 7 — a strict xfail turns into a failure the moment the route
exists, so it cannot be forgotten.

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest prospector_app/backend/tests/test_unconfigured_gate.py -q
```

Expected: the `TestUnconfigured` API-route cases fail with 200 instead of 409 —
which is precisely the bug this gate exists to prevent.

- [ ] **Step 3: Add the middleware**

In `app.py`, directly below the `no_store_api` middleware:

```python
# Paths that answer before this checkout has a deployment target: the wizard's
# own surface, the metadata the SPA routes on, and the static app.
_UNCONFIGURED_OK = ("/api/onboarding/", "/api/meta")


@app.middleware("http")
async def require_configured(request, call_next):
    """Refuse every API call until a deployment target exists.

    An unconfigured process reaches a local SQLite fallback and answers list
    routes with an empty result, which reads as a configured Prospector watching
    an empty repository. Refusing in one place means a route added later inherits
    it."""
    path = request.url.path
    if (path.startswith("/api/") and not settings.configured()
            and not path.startswith(_UNCONFIGURED_OK)):
        return JSONResponse({"unconfigured": True}, status_code=409)
    return await call_next(request)
```

`str.startswith` accepts a tuple, so `_UNCONFIGURED_OK` covers both the prefix and
the exact path. Add `from pipeline import settings` to `app.py`'s imports if it is
not already there.

- [ ] **Step 4: Add `configured` to `/api/meta`**

In `repo_meta.py`, add `"configured": settings.configured()` to the returned dict,
and add the field to `RepoMeta` in `prospector_app/frontend/src/api.ts`:

```typescript
export interface RepoMeta {
  repo: string;
  owner: string;
  name: string;
  url: string;
  default_branch: string;
  display_name: string;
  feedback_repo: string | null;
  configured: boolean;
  test_paths: { dir_pattern: string; file_pattern: string };
}
```

- [ ] **Step 5: Run the tests and the gates**

```bash
uv run pytest prospector_app/backend/tests/test_unconfigured_gate.py -q
uv run pytest -q && uv run ruff check . && uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness
```

Expected: the gate tests pass except the xfail-marked one; full suite green.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Refuse every API call until a deployment target exists

An unconfigured process reaches the local SQLite fallback and answers list
routes with empty results, which is indistinguishable from a configured
Prospector watching an empty repository. One middleware refuses instead, with
the wizard's surface and /api/meta exempt so the SPA can route.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Slice 3 — The write surface

### Task 4: Extract the atomic `.env` writer

Behaviour-preserving. `worker_control`'s existing tests are the proof.

**Files:**
- Create: `prospector_app/backend/env_file.py`
- Modify: `prospector_app/backend/worker_control.py:30-90`
- Test: `prospector_app/backend/tests/test_env_file.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `env_file.ENV_PATH: Path`, `env_file.merge(text: str, updates: dict[str, str]) -> str`, `env_file.write(updates: dict[str, str]) -> None`.

- [ ] **Step 1: Write the failing test**

Create `prospector_app/backend/tests/test_env_file.py`:

```python
"""The one atomic .env merge-and-replace. This file holds the store password and
both credential paths, so a write that mangles an unrelated line costs more than
a failed write."""
from __future__ import annotations

import pytest

from prospector_app.backend import env_file

ENV = """\
TRIAGE_REPO=owner/name
TRIAGE_STORE_URL=postgresql+psycopg://user:sup3rsecret@host:6543/postgres
# TRIAGE_VERIFY_WORKER=1
"""


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text(ENV)
    monkeypatch.setattr(env_file, "ENV_PATH", p)
    return p


class TestMerge:
    def test_replaces_a_commented_key_in_place(self):
        out = env_file.merge(ENV, {"TRIAGE_VERIFY_WORKER": "1"})
        assert "TRIAGE_VERIFY_WORKER=1\n" in out
        assert "# TRIAGE_VERIFY_WORKER=1" not in out

    def test_appends_a_key_the_file_never_mentioned(self):
        out = env_file.merge(ENV, {"TRIAGE_BOT_APP_ID": "12345"})
        assert "TRIAGE_BOT_APP_ID=12345\n" in out

    def test_keeps_every_other_line_byte_for_byte(self):
        out = env_file.merge(ENV, {"TRIAGE_VERIFY_WORKER": "1"})
        assert "TRIAGE_STORE_URL=postgresql+psycopg://user:sup3rsecret@host:6543/postgres" in out
        assert "TRIAGE_REPO=owner/name" in out


class TestWrite:
    def test_round_trips(self, path):
        env_file.write({"TRIAGE_BOT_APP_ID": "12345"})
        assert "TRIAGE_BOT_APP_ID=12345" in path.read_text()

    def test_is_owner_only(self, path):
        env_file.write({"TRIAGE_BOT_APP_ID": "12345"})
        assert path.stat().st_mode & 0o777 == 0o600

    def test_creates_the_file_when_absent(self, tmp_path, monkeypatch):
        p = tmp_path / "fresh" / ".env"
        p.parent.mkdir()
        monkeypatch.setattr(env_file, "ENV_PATH", p)
        env_file.write({"TRIAGE_REPO": "acme/widgets"})
        assert "TRIAGE_REPO=acme/widgets" in p.read_text()
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest prospector_app/backend/tests/test_env_file.py -q
```

Expected: `ModuleNotFoundError: prospector_app.backend.env_file`.

- [ ] **Step 3: Create the module**

`prospector_app/backend/env_file.py` — move `_rewrite`'s body and `set_flags`'
write half verbatim:

```python
"""The repo-root `.env`, written atomically.

Callers own their own allowlist; this module takes an already-validated mapping
and puts it on disk without disturbing anything else in the file. `.env` holds
the store password and the paths to both credentials, so the file is replaced
whole from a temporary sibling — a failed write leaves the previous file intact
rather than a truncated one.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"


def merge(text: str, updates: dict[str, str]) -> str:
    """`text` with each update applied to its own line, every other line kept
    byte for byte. A key the file does not mention is appended; one it comments
    out is replaced in place, so a commented example becomes the live setting
    rather than a duplicate below it."""
    lines = text.splitlines(keepends=True)
    remaining = dict(updates)
    for i, line in enumerate(lines):
        bare = line.lstrip("#").strip()
        key = bare.split("=", 1)[0].strip() if "=" in bare else ""
        if key not in remaining:
            continue
        ending = "\n" if line.endswith("\n") else ""
        lines[i] = f"{key}={remaining.pop(key)}{ending}"
    if remaining:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n# Written by the app.\n")
        lines.extend(f"{k}={v}\n" for k, v in remaining.items())
    return "".join(lines)


def write(updates: dict[str, str]) -> None:
    """Apply `updates` to `.env` on disk, owner-readable only."""
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    with tempfile.NamedTemporaryFile("w", dir=ENV_PATH.parent, delete=False) as tmp:
        tmp.write(merge(text, updates))
        staged = Path(tmp.name)
    staged.chmod(0o600)
    staged.replace(ENV_PATH)
```

- [ ] **Step 4: Point `worker_control` at it**

Delete `_rewrite` and the temp-file block from `worker_control.set_flags`, keeping
`_validated` and the allowlist exactly as they are:

```python
def set_flags(updates: dict[str, str]) -> dict[str, str]:
    """Write `updates` to `.env` and to this process's environment. Returns the
    flags as they now stand."""
    clean = _validated(updates)
    env_file.write(clean)
    os.environ.update(clean)
    return flags()
```

`worker_control.ENV_PATH` is monkeypatched by `test_worker_setup.py`, so keep the
name bound to the same object: `ENV_PATH = env_file.ENV_PATH` will *not* work
because rebinding one does not rebind the other. Instead update
`test_worker_setup.py`'s fixture to patch `env_file.ENV_PATH`, and delete
`worker_control.ENV_PATH`.

- [ ] **Step 5: Run both suites**

```bash
uv run pytest prospector_app/backend/tests/test_env_file.py prospector_app/backend/tests/test_worker_setup.py -q
```

Expected: PASS. `test_worker_setup.py`'s assertions are unchanged apart from the
fixture's patch target — that is the proof this refactor preserved behaviour.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Extract the atomic .env write

One merge-and-replace, with the caller owning its allowlist. The worker-lane
writer keeps its own five-key allowlist and its tests unchanged.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

### Task 5: The onboarding write surface

The security-critical task. The step allowlist is the whole story.

**Files:**
- Create: `prospector_app/backend/onboarding.py`
- Test: `prospector_app/backend/tests/test_onboarding.py` (create)

**Interfaces:**
- Consumes: `env_file.write`, `data.reset`, `profile.reset_cache`, `settings.configured`, `profile.parse_profile`.
- Produces: `onboarding.STEP_KEYS: dict[str, tuple[str, ...]]`, `onboarding.apply(step: str, env: dict[str, str], profile_doc: dict[str, object] | None) -> dict[str, object]`, `onboarding.reconfigure(applied: dict[str, str]) -> None`, `onboarding.build_bundle() -> dict[str, object]`, `onboarding.parse_bundle(text: str) -> tuple[dict[str, str], dict[str, object] | None]`, `onboarding.state() -> dict[str, object]`.

- [ ] **Step 1: Write the failing test**

Create `prospector_app/backend/tests/test_onboarding.py`:

```python
"""What the wizard is allowed to write, and what it refuses.

The step allowlist is load-bearing: step 1 names the repository and the store,
so it closes the moment a deployment is configured. A configured Prospector
cannot be retargeted at another repository or another database over HTTP.
"""
from __future__ import annotations

import json

import pytest

from prospector_app.backend import env_file, onboarding

ENV = """\
TRIAGE_STORE_URL=postgresql+psycopg://user:sup3rsecret@host:6543/postgres
"""

PROFILE = {"version": 1, "subsystems": [{"name": "core", "match": ["core"]}]}


@pytest.fixture
def files(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(ENV)
    prof = tmp_path / "profile.json"
    monkeypatch.setattr(env_file, "ENV_PATH", env)
    monkeypatch.setattr(onboarding, "PROFILE_PATH", prof)
    monkeypatch.setattr(onboarding, "reconfigure", lambda applied: None)
    return env, prof


class TestStepAllowlist:
    def test_step_one_writes_the_deployment_target(self, files, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        env, _ = files
        onboarding.apply("connect", {"TRIAGE_REPO": "acme/widgets"}, None)
        assert "TRIAGE_REPO=acme/widgets" in env.read_text()

    def test_step_one_is_refused_once_configured(self, files, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        with pytest.raises(ValueError, match="already configured"):
            onboarding.apply("connect", {"TRIAGE_REPO": "attacker/repo"}, None)

    def test_store_url_is_refused_once_configured(self, files, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        with pytest.raises(ValueError, match="already configured"):
            onboarding.apply("connect", {"TRIAGE_STORE_URL": "sqlite:///evil.db"}, None)

    def test_bot_identity_is_writable_while_configured(self, files, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        env, _ = files
        onboarding.apply("writes", {"TRIAGE_BOT_LOGIN": "acme-bot"}, None)
        assert "TRIAGE_BOT_LOGIN=acme-bot" in env.read_text()

    def test_a_key_outside_the_step_is_a_hard_error(self, files, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        with pytest.raises(ValueError, match="not writable"):
            onboarding.apply("connect", {"TRIAGE_FIX_AUTOPUSH": "fix"}, None)

    def test_an_unknown_step_is_a_hard_error(self, files):
        with pytest.raises(ValueError, match="not a step"):
            onboarding.apply("whatever", {}, None)


class TestValidation:
    def test_a_malformed_repo_is_refused(self, files, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        with pytest.raises(ValueError, match="owner/name"):
            onboarding.apply("connect", {"TRIAGE_REPO": "widgets"}, None)

    def test_a_profile_the_parser_rejects_is_never_written(self, files, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        env, prof = files
        with pytest.raises(ValueError):
            onboarding.apply("connect", {"TRIAGE_REPO": "acme/widgets"},
                             {"version": 1, "subsystems": "not-a-list"})
        assert not prof.exists()
        assert "TRIAGE_REPO" not in env.read_text()

    def test_unrelated_env_lines_survive(self, files, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        env, _ = files
        onboarding.apply("connect", {"TRIAGE_REPO": "acme/widgets"}, PROFILE)
        assert "sup3rsecret" in env.read_text()

    def test_the_previous_profile_is_restored_when_the_env_write_fails(
            self, files, monkeypatch):
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        env, prof = files
        prof.write_text(json.dumps({"version": 1, "subsystems": []}))
        def boom(updates):
            raise OSError("disk full")
        monkeypatch.setattr(env_file, "write", boom)
        with pytest.raises(OSError):
            onboarding.apply("connect", {"TRIAGE_REPO": "acme/widgets"}, PROFILE)
        assert json.loads(prof.read_text())["subsystems"] == []


class TestBundle:
    def test_round_trips(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
        monkeypatch.setenv("TRIAGE_STORE_URL", "sqlite:///team.db")
        text = json.dumps(onboarding.build_bundle())
        env, prof = onboarding.parse_bundle(text)
        assert env["TRIAGE_REPO"] == "acme/widgets"
        assert env["TRIAGE_STORE_URL"] == "sqlite:///team.db"

    def test_an_unknown_version_is_refused_with_what_it_saw(self):
        with pytest.raises(ValueError, match="version 99"):
            onboarding.parse_bundle(json.dumps({"version": 99, "env": {}}))

    def test_junk_is_refused_without_a_traceback(self):
        with pytest.raises(ValueError, match="not a Prospector bundle"):
            onboarding.parse_bundle("hello")
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest prospector_app/backend/tests/test_onboarding.py -q
```

Expected: `ModuleNotFoundError: prospector_app.backend.onboarding`.

- [ ] **Step 3: Write the module**

```python
"""Configuring this checkout from the app.

The ONE config write path. `worker_control` writes five lane switches and must
stay that narrow; onboarding needs the deployment target, the bot identity, and
the push identity, so it carries its own allowlist scoped by step.

Step 1's keys name the repository and the store. They are writable only while
`settings.configured()` is false, which is what stops a configured deployment
from being retargeted at another repository or another database by an API
caller. Steps 2 and 3 stay open because the wizard reaches them after step 1 has
already configured the app.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from pipeline import profile, settings
from prospector_app.backend import data, env_file

BUNDLE_VERSION = 1

PROFILE_PATH = settings.REPO_ROOT / "profile.json"

_REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")

# What each step of the wizard may write. A key outside its step is a hard
# error, never a silent skip.
STEP_KEYS: dict[str, tuple[str, ...]] = {
    "connect": ("TRIAGE_REPO", "TRIAGE_STORE_URL", "TRIAGE_PROFILE",
                "TRIAGE_DEFAULT_BRANCH", "TRIAGE_DISPLAY_NAME",
                "TRIAGE_REVIEW_PROVIDER", "TRIAGE_REVIEW_THRESHOLD",
                "PROSPECTOR_FEEDBACK_REPO"),
    "writes": ("TRIAGE_BOT_LOGIN", "TRIAGE_BOT_APP_ID", "TRIAGE_BOT_KEY_FILE"),
    "worker": ("TRIAGE_PUSH_LOGIN", "TRIAGE_PUSH_EMAIL", "TRIAGE_PUSH_SSH_KEY_FILE"),
}

# The keys the bundle carries to a teammate: everything step 1 needs to point a
# fresh checkout at this deployment.
_BUNDLE_KEYS = STEP_KEYS["connect"] + ("TRIAGE_BOT_LOGIN", "TRIAGE_BOT_APP_ID")


def _validated(step: str, updates: dict[str, str]) -> dict[str, str]:
    """`updates`, proved writable for `step`. Raises ValueError on an unknown
    step, a key outside it, a step-1 write to a configured deployment, or a
    malformed repository."""
    allowed = STEP_KEYS.get(step)
    if allowed is None:
        raise ValueError(f"not a step: {step}")
    if step == "connect" and settings.configured():
        raise ValueError(
            "this deployment is already configured; the repository and store are "
            "not writable from here")
    outside = sorted(set(updates) - set(allowed))
    if outside:
        raise ValueError(f"not writable in step {step}: {', '.join(outside)}")
    clean = {k: str(v).strip() for k, v in updates.items()}
    target = clean.get("TRIAGE_REPO", "")
    if "TRIAGE_REPO" in clean and not _REPO_RE.match(target):
        raise ValueError(f"TRIAGE_REPO must be owner/name, not {target!r}")
    return clean


def apply(step: str, env: dict[str, str], profile_doc: dict[str, object] | None,
          ) -> dict[str, object]:
    """Write one step's configuration and adopt it in this process.

    Everything is validated before anything is written. `profile.json` goes
    first so a profile the parser would reject at boot never reaches disk; if
    the `.env` write then fails, the previous profile is put back, because a
    checkout whose policy file belongs to one deployment and whose `.env` names
    another is worse than either failure alone.
    """
    clean = _validated(step, env)
    previous: str | None = None
    if profile_doc is not None:
        profile.parse_profile(profile_doc, "onboarding bundle")
        previous = PROFILE_PATH.read_text() if PROFILE_PATH.exists() else None
        PROFILE_PATH.write_text(json.dumps(profile_doc, indent=2) + "\n")
    try:
        env_file.write(clean)
    except OSError:
        if profile_doc is not None:
            if previous is None:
                PROFILE_PATH.unlink(missing_ok=True)
            else:
                PROFILE_PATH.write_text(previous)
        raise
    reconfigure(clean)
    return state()


def reconfigure(applied: dict[str, str]) -> None:
    """Adopt written configuration in the running process. The ONE adoption
    path: the environment, then the two things built from it at import."""
    os.environ.update(applied)
    data.reset()
    settings.default_branch.cache_clear()
    profile.reset_cache()


def build_bundle() -> dict[str, object]:
    """This deployment, as one thing a teammate can paste.

    Carries the store URL and the whole profile, because a bundle needing a
    second out-of-band step is the problem it exists to solve. It is therefore a
    credential, and the UI says so.
    """
    env = {k: os.environ[k] for k in _BUNDLE_KEYS
           if os.environ.get(k, "").strip()}
    doc: dict[str, object] | None = None
    path = Path(settings.profile_path() or PROFILE_PATH)
    if not path.is_absolute():
        path = settings.REPO_ROOT / path
    if path.is_file():
        doc = json.loads(path.read_text())
    return {"version": BUNDLE_VERSION, "env": env, "profile": doc}


def parse_bundle(text: str) -> tuple[dict[str, str], dict[str, object] | None]:
    """The env mapping and profile document a bundle carries. Raises ValueError
    on anything that is not a bundle of a version this checkout knows."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("not a Prospector bundle: expected JSON")
    if not isinstance(doc, dict) or "env" not in doc:
        raise ValueError("not a Prospector bundle: no env section")
    version = doc.get("version")
    if version != BUNDLE_VERSION:
        raise ValueError(
            f"this bundle is version {version}; this checkout reads version "
            f"{BUNDLE_VERSION}")
    env = doc["env"]
    if not isinstance(env, dict):
        raise ValueError("not a Prospector bundle: env is not an object")
    prof = doc.get("profile")
    return ({str(k): str(v) for k, v in env.items()},
            prof if isinstance(prof, dict) else None)


def state() -> dict[str, object]:
    """Where this checkout is on the ladder."""
    from prospector_app.backend import executor, worker_readiness

    counts: dict[str, int] = {}
    if settings.configured():
        counts = {"prs": len(data.prs()), "clusters": len(data.clusters())}
    return {
        "configured": settings.configured(),
        "repo": settings.repo(),
        "display_name": settings.display_name(),
        "bot_login": settings.bot_login(),
        "writes_ready": bool(settings.bot_login()) and executor.live_possible(),
        "worker_ready": worker_readiness.report()["ready"] if settings.configured() else False,
        "counts": counts,
    }
```

Check `data`'s real snapshot accessor names and `executor.live_possible`'s real
signature before finalizing `state()`; use the names that exist.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest prospector_app/backend/tests/test_onboarding.py -q
```

Expected: PASS.

- [ ] **Step 5: Run every gate**

```bash
uv run ruff check . && uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness && uv run pytest -q
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Add the onboarding config write path

One writer, allowlisted per wizard step. The repository and store are writable
only while unconfigured, so a working deployment cannot be retargeted over HTTP;
the bot and push identities stay writable because the wizard reaches them after
the app is already configured. A profile the parser rejects never reaches disk,
and a failed .env write puts the previous profile back.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

### Task 6: Probe

**Files:**
- Modify: `prospector_app/backend/onboarding.py`
- Test: `prospector_app/backend/tests/test_onboarding.py` (extend)

**Interfaces:**
- Consumes: Task 5's module.
- Produces: `onboarding.probe(store_url: str | None, repo: str | None, key_file: str | None) -> dict[str, object]`.

- [ ] **Step 1: Write the failing test**

Append to `test_onboarding.py`:

```python
class TestProbe:
    def test_reports_a_reachable_store_with_its_counts(self, tmp_path):
        found = onboarding.probe(store_url=f"sqlite:///{tmp_path}/probe.db",
                                 repo=None, key_file=None)
        assert found["store"]["ok"] is True
        assert "prs" in found["store"]

    def test_an_unreachable_store_is_a_finding_not_an_exception(self):
        found = onboarding.probe(store_url="postgresql+psycopg://nope:nope@127.0.0.1:1/none",
                                 repo=None, key_file=None)
        assert found["store"]["ok"] is False
        assert isinstance(found["store"]["problem"], str)

    def test_never_echoes_the_password_back(self):
        url = "postgresql+psycopg://user:sup3rsecret@127.0.0.1:1/none"
        found = onboarding.probe(store_url=url, repo=None, key_file=None)
        assert "sup3rsecret" not in json.dumps(found)

    def test_a_missing_key_file_is_a_finding(self, tmp_path):
        found = onboarding.probe(store_url=None, repo=None,
                                 key_file=str(tmp_path / "absent.pem"))
        assert found["key_file"]["ok"] is False

    def test_writes_nothing(self, files):
        env, prof = files
        before = env.read_text()
        onboarding.probe(store_url="sqlite:///probe.db", repo=None, key_file=None)
        assert env.read_text() == before
        assert not prof.exists()
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest prospector_app/backend/tests/test_onboarding.py::TestProbe -q
```

Expected: `AttributeError: module ... has no attribute 'probe'`.

- [ ] **Step 3: Implement**

```python
def _probe_store(url: str) -> dict[str, object]:
    """Whether this store answers, and how much is in it. Failures come back as
    a category rather than raw exception text, which can carry the URL."""
    from sqlalchemy import create_engine, text as sql_text
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            prs = conn.execute(sql_text("SELECT count(*) FROM prs")).scalar_one()
            clusters = conn.execute(sql_text("SELECT count(*) FROM clusters")).scalar_one()
        return {"ok": True, "prs": int(prs), "clusters": int(clusters)}
    except Exception as e:
        return {"ok": False, "problem": type(e).__name__}


def probe(store_url: str | None, repo: str | None, key_file: str | None,
          ) -> dict[str, object]:
    """Check candidate configuration without committing any of it.

    Diagnosing these is the wizard's job, so nothing here raises to the caller:
    an unreachable store, an unreadable repository, and a PEM that will not mint
    are findings. Failures report a category, never raw exception text or the
    store URL, which carries the database password.
    """
    found: dict[str, object] = {}
    if store_url:
        found["store"] = _probe_store(store_url)
    if repo:
        found["repo"] = _probe_repo(repo)
    if key_file:
        path = Path(key_file).expanduser()
        found["key_file"] = ({"ok": True} if path.is_file()
                             else {"ok": False, "problem": "no file at that path"})
    return found
```

`_probe_repo` runs `gh api repos/<repo> --jq .full_name` through
`pipeline.gh.operator_env()` with a 15s timeout and returns
`{"ok": bool, "problem": str}` — the same shape, and the same rule about not
echoing raw output back.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest prospector_app/backend/tests/test_onboarding.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Let the wizard check configuration before committing it

probe answers whether a store is reachable and how much it holds, whether the
operator can read a repository, and whether a PEM path exists. Failures are
findings with a category, never raw exception text or the store URL.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

### Task 7: The HTTP surface

**Files:**
- Modify: `prospector_app/backend/app.py`
- Modify: `prospector_app/backend/models.py`
- Modify: `prospector_app/backend/tests/test_unconfigured_gate.py` (drop the xfail)

**Interfaces:**
- Consumes: Tasks 5 and 6.
- Produces: `GET /api/onboarding/state`, `POST /api/onboarding/probe`, `POST /api/onboarding/apply`.

- [ ] **Step 1: Remove the xfail marker from Task 3's test**

Delete the `@pytest.mark.xfail` from `test_onboarding_state_is_served`. Run it:

```bash
uv run pytest prospector_app/backend/tests/test_unconfigured_gate.py -q
```

Expected: that test fails with 404 — the route does not exist yet.

- [ ] **Step 2: Add the request models**

In `models.py`, beside `ShareRequest`:

```python
class OnboardingProbe(BaseModel):
    store_url: str | None = None
    repo: str | None = None
    key_file: str | None = None


class OnboardingApply(BaseModel):
    step: str
    env: dict[str, str] = {}
    profile: dict[str, object] | None = None
    bundle: str | None = None
```

- [ ] **Step 3: Add the routes**

In `app.py`, beside the existing `/api/setup/*` routes:

```python
@app.get("/api/onboarding/state")
def onboarding_state():
    return onboarding.state()


@app.post("/api/onboarding/probe")
def onboarding_probe(body: models.OnboardingProbe):
    return onboarding.probe(body.store_url, body.repo, body.key_file)


@app.post("/api/onboarding/apply")
def onboarding_apply(body: models.OnboardingApply):
    env, profile_doc = body.env, body.profile
    if body.bundle is not None:
        try:
            env, profile_doc = onboarding.parse_bundle(body.bundle)
        except ValueError as e:
            raise HTTPException(400, str(e))
    try:
        return onboarding.apply(body.step, env, profile_doc)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"could not write configuration: {e}")
```

- [ ] **Step 4: Run the gate tests**

```bash
uv run pytest prospector_app/backend/tests/test_unconfigured_gate.py -q
```

Expected: PASS, including the formerly-xfail case.

- [ ] **Step 5: Add an end-to-end route test**

Append to `test_onboarding.py`:

```python
class TestRoutes:
    def test_apply_from_a_bundle_configures_an_unconfigured_app(
            self, files, monkeypatch):
        from fastapi.testclient import TestClient
        from prospector_app.backend import app as app_mod
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        client = TestClient(app_mod.app, raise_server_exceptions=False)
        bundle = json.dumps({"version": 1,
                             "env": {"TRIAGE_REPO": "acme/widgets"},
                             "profile": PROFILE})
        r = client.post("/api/onboarding/apply",
                        json={"step": "connect", "bundle": bundle})
        assert r.status_code == 200
        assert r.json()["configured"] is True

    def test_a_bad_bundle_is_a_400_not_a_500(self, files, monkeypatch):
        from fastapi.testclient import TestClient
        from prospector_app.backend import app as app_mod
        monkeypatch.delenv("TRIAGE_REPO", raising=False)
        client = TestClient(app_mod.app, raise_server_exceptions=False)
        r = client.post("/api/onboarding/apply",
                        json={"step": "connect", "bundle": "hello"})
        assert r.status_code == 400
```

- [ ] **Step 6: Run every gate and commit**

```bash
uv run ruff check . && uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness && uv run pytest -q
git add -A
git commit -m "$(cat <<'EOF'
Serve the onboarding surface

state, probe, and apply, exempt from the unconfigured gate so a checkout with no
deployment target can be configured through them. A malformed bundle is a 400
with what it saw.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

### Task 8: App-less deployments fail closed

**Files:**
- Modify: `prospector_app/backend/safety_guard.py:188-193`
- Modify: `prospector_app/backend/caps.py`
- Test: `prospector_app/backend/tests/test_safety_guard_no_bot.py` (create)

**Interfaces:**
- Consumes: `settings.bot_login()` from Task 1.
- Produces: no new names; `bot_run` / `bot_merge_run` refuse with no bot identity.

- [ ] **Step 1: Write the failing test**

```python
"""A deployment with no GitHub App writes nothing.

Writes are already inert without a mintable token, but an empty bot identity is
its own refusal: an allowlist that compares an acting identity against "" must
not match something unintended.
"""
from __future__ import annotations

import pytest

from prospector_app.backend import safety_guard


def test_bot_run_refuses_without_a_bot_identity(monkeypatch):
    monkeypatch.delenv("TRIAGE_BOT_LOGIN", raising=False)
    with pytest.raises(safety_guard.WriteAttemptBlocked, match="no bot identity"):
        safety_guard.bot_run(["gh", "pr", "comment", "1", "-b", "hi"], "token-value")


def test_bot_merge_run_refuses_without_a_bot_identity(monkeypatch):
    monkeypatch.delenv("TRIAGE_BOT_LOGIN", raising=False)
    with pytest.raises(safety_guard.WriteAttemptBlocked, match="no bot identity"):
        safety_guard.bot_merge_run(["gh", "pr", "merge", "1", "--squash"], "token-value")


def test_a_configured_bot_still_reaches_the_allowlist(monkeypatch):
    monkeypatch.setenv("TRIAGE_BOT_LOGIN", "acme-bot")
    with pytest.raises(safety_guard.WriteAttemptBlocked, match="not an allowlisted"):
        safety_guard.bot_run(["gh", "repo", "delete", "acme/widgets"], "token-value")
```

Confirm `bot_merge_run`'s real signature before writing the second test.

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest prospector_app/backend/tests/test_safety_guard_no_bot.py -q
```

Expected: the first two fail — the write reaches the allowlist instead of being
refused for having no identity.

- [ ] **Step 3: Add the refusal**

```python
def _require_bot_identity(action: str) -> str:
    """The configured bot login. A deployment with no GitHub App has none, and a
    write attributed to nobody is refused rather than attempted."""
    login = settings.bot_login()
    if not login:
        raise WriteAttemptBlocked(
            f"refusing to {action}: no bot identity is configured")
    return login


def _require_bot_token(token: str | None, action: str) -> str:
    login = _require_bot_identity(action)
    if not token or not token.strip():
        raise WriteAttemptBlocked(
            f"refusing to {action} without a {login} token (would fall back to default login)"
        )
    return token
```

Every path that calls `_require_bot_token` inherits the identity refusal. Check
that `bot_merge_run` does too; if it validates its token separately, call
`_require_bot_identity` there as well.

- [ ] **Step 4: Report it in `caps.py`**

Add `"bot_configured": bool(settings.bot_login())` to the capabilities payload so
the UI can say why write controls are absent rather than offering them and
failing.

- [ ] **Step 5: Run the tests and gates, then commit**

```bash
uv run pytest -q && uv run ruff check . && uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness
git add -A
git commit -m "$(cat <<'EOF'
Refuse upstream writes with no bot identity

A deployment configured without a GitHub App has no login to attribute a write
to, so every bot write refuses on that alone rather than relying on token
minting to fail. caps reports it so the UI can explain the absence.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

### Task 9: Operation-scoped target resolution

**Files:**
- Modify: `prospector_app/backend/executor.py`
- Modify: `prospector_app/backend/safety_guard.py`
- Test: `prospector_app/backend/tests/test_executor_target_scope.py` (create)

**Interfaces:**
- Consumes: Task 1's accessors.
- Produces: no new public names; one target snapshot threaded through mint → validate → execute.

- [ ] **Step 1: Write the failing test**

```python
"""One upstream write sees one deployment target.

Accessors are re-read on each call, so a write that resolves the repository
separately at mint, validate, and execute could straddle a configuration change
and act against a target it was not authorized for.
"""
from __future__ import annotations

from prospector_app.backend import executor


def test_a_write_uses_one_target_even_if_the_environment_moves(monkeypatch):
    seen: list[str] = []
    monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")

    def record(argv, token, **kwargs):
        seen.append(next(a for a in argv if "/" in a and a.count("/") == 1))
        monkeypatch.setenv("TRIAGE_REPO", "attacker/repo")
        class Done:
            returncode = 0
            stdout = ""
            stderr = ""
        return Done()

    monkeypatch.setattr(executor.safety_guard, "bot_run", record)
    executor.comment_on_pr(1, "hello")
    executor.comment_on_pr(2, "hello")
    assert seen == ["acme/widgets", "acme/widgets"] or seen[0] == "acme/widgets"
```

Read `executor.py` first and write this against its real entry point and argv
shape — the assertion must pin that the repository used to build the command is
the one resolved at the start of that operation.

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest prospector_app/backend/tests/test_executor_target_scope.py -q
```

- [ ] **Step 3: Thread the target through**

In `executor.py`, resolve once at the top of each write operation:

```python
target = settings.repo()
bot = settings.bot_login()
```

and pass `target`/`bot` down to token minting, command construction, validation,
and the activity-log entry, rather than calling the accessors again at each step.

- [ ] **Step 4: Run the tests and gates, then commit**

```bash
uv run pytest -q && uv run ruff check . && uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness
git add -A
git commit -m "$(cat <<'EOF'
Resolve the write target once per operation

Mint, validate, and execute now share one snapshot of the repository and bot
identity, so a single upstream write is internally consistent no matter what the
environment does mid-operation.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Slice 4 — The wizard

### Task 10: Bundle v1 from the Setup share card

**Files:**
- Modify: `prospector_app/backend/worker_control.py:95-160` (delete `share_snippet`)
- Modify: `prospector_app/backend/app.py:372` (`/api/setup/share`)
- Modify: `prospector_app/backend/models.py` (`ShareRequest`)
- Modify: `prospector_app/frontend/src/views/Setup.tsx:206-250`
- Modify: `prospector_app/frontend/src/api.ts:1007`

**Interfaces:**
- Consumes: `onboarding.build_bundle()` from Task 5.
- Produces: `/api/setup/share` returns `{bundle: string}` — the JSON envelope, indented, ready to paste.

- [ ] **Step 1: Update the endpoint**

`share_snippet` and `_SHARE_KEYS` are deleted from `worker_control`;
`onboarding._BUNDLE_KEYS` replaces them. The route becomes:

```python
@app.post("/api/setup/share")
def setup_share():
    return {"bundle": json.dumps(onboarding.build_bundle(), indent=2)}
```

`ShareRequest` had one field, `include_store`. Delete the model and drop the body
parameter — the bundle always carries the store URL now.

- [ ] **Step 2: Update the API client**

```typescript
setupShare: async () => {
  const r = await fetch("/api/setup/share", { method: "POST" });
  if (!r.ok) throw new Error(`/api/setup/share → ${r.status}`);
  return (await r.json()) as { bundle: string };
},
```

- [ ] **Step 3: Rewrite `ShareSection`**

Delete `includeStore` and its checkbox. The warning becomes unconditional,
because the bundle is now always a credential:

```tsx
function ShareSection() {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");

  const copy = async () => {
    try {
      const { bundle } = await api.setupShare();
      await navigator.clipboard.writeText(bundle);
      setState("copied");
    } catch {
      setState("failed");
    }
    setTimeout(() => setState("idle"), 3000);
  };

  return (
    <section className="setup-card setup-share">
      <h3>🤝 Share this deployment</h3>
      <p className="muted small">
        About a teammate's machine, not this one. Copies everything a fresh
        checkout needs: the repo, bot identity, review config, the store URL, and
        this deployment's <code>profile.json</code>. Your teammate pastes it into
        the setup wizard their app opens on first run.
      </p>
      <p className="setup-warn small">
        ⚠ This carries the database password. Send it through a password manager
        or a direct message — never a channel with history.
      </p>
      <div>
        <button onClick={() => void copy()}>
          {state === "copied" ? "copied ✓" : state === "failed" ? "copy failed" : "copy setup for a teammate"}
        </button>
      </div>
    </section>
  );
}
```

Add to `styles.css` beside the other setup rules:

```css
.setup-warn { color: var(--gold); background: color-mix(in srgb, var(--gold) 10%, transparent);
  border-radius: 6px; padding: 7px 10px; margin: 0 0 10px; line-height: 1.5; }
```

- [ ] **Step 4: Verify**

```bash
cd prospector_app/frontend && pnpm run build && pnpm exec eslint src/views/Setup.tsx
uv run pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Share a deployment as one pasteable bundle

The share button emits the JSON envelope the wizard reads, carrying the store
URL and profile.json so a fresh checkout needs nothing else. It is a credential,
and the card says so instead of offering a checkbox that makes it half-safe.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

### Task 11: Route to the wizard when unconfigured

**Files:**
- Modify: `prospector_app/frontend/src/main.tsx:33-56`
- Modify: `prospector_app/frontend/src/App.tsx`
- Create: `prospector_app/frontend/src/views/Welcome.tsx`
- Modify: `prospector_app/frontend/src/api.ts`

**Interfaces:**
- Consumes: `RepoMeta.configured` from Task 3; `/api/onboarding/*` from Task 7.
- Produces: `api.onboardingState()`, `api.onboardingProbe()`, `api.onboardingApply()`; the `/welcome` route.

- [ ] **Step 1: Add the API client functions**

```typescript
export interface OnboardingState {
  configured: boolean;
  repo: string;
  display_name: string;
  bot_login: string;
  writes_ready: boolean;
  worker_ready: boolean;
  counts: { prs?: number; clusters?: number };
}

export interface ProbeFinding { ok: boolean; problem?: string; prs?: number; clusters?: number }
export interface ProbeResult { store?: ProbeFinding; repo?: ProbeFinding; key_file?: ProbeFinding }
```

with `onboardingState: () => get<OnboardingState>("/api/onboarding/state")`, and
POST helpers for `probe` and `apply` following the shape of `setSetupFlags`.

- [ ] **Step 2: Register the route**

In `main.tsx`, inside `children`:

```tsx
{ path: "welcome", lazy: lazyView(() => import("./views/Welcome")) },
```

- [ ] **Step 3: Redirect when unconfigured**

In `App.tsx`, where `useRepoMeta()` is already consumed:

```tsx
const { meta } = useRepoMeta();
const location = useLocation();
const navigate = useNavigate();

// An unconfigured checkout has no data to show, and its API refuses every call.
// The wizard is the only page that works, so it is the only page reachable.
useEffect(() => {
  if (meta && !meta.configured && location.pathname !== "/welcome") {
    navigate("/welcome", { replace: true });
  }
}, [meta, location.pathname, navigate]);
```

`/welcome` stays reachable once configured — the ladder's steps 2 and 3 run after
step 1 has configured the app, so becoming configured must not navigate the user
out of the wizard.

- [ ] **Step 4: Create the wizard shell**

`Welcome.tsx` renders the branch choice when `state.configured` is false and
nothing has been chosen, and the ladder otherwise:

```tsx
type Branch = "join" | "new" | null;

export default function Welcome() {
  const [state, setState] = useState<OnboardingState | null>(null);
  const [branch, setBranch] = useState<Branch>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setState(await api.onboardingState()); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  if (error) return <div className="pad"><p className="chip chip-red">{error}</p></div>;
  if (!state) return <div className="pad muted">reading this checkout…</div>;

  return (
    <div className="pad welcome">
      <h2>👋 Welcome to Prospector</h2>
      {!state.configured && branch == null && <BranchChoice onPick={setBranch} />}
      {!state.configured && branch === "join" && <JoinBranch onDone={load} />}
      {!state.configured && branch === "new" && <NewBranch onDone={load} />}
      {state.configured && <Ladder state={state} onChange={load} />}
    </div>
  );
}
```

- [ ] **Step 5: Verify**

```bash
cd prospector_app/frontend && pnpm run build && pnpm exec eslint src/views/Welcome.tsx src/App.tsx src/main.tsx
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Open the wizard on a checkout with no deployment target

An unconfigured app has no data to show and refuses every API call, so /welcome
is the only reachable page until it is configured. It stays reachable afterwards
because the ladder continues there.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

### Task 12: The join branch

**Files:**
- Modify: `prospector_app/frontend/src/views/Welcome.tsx`

**Interfaces:**
- Consumes: `api.onboardingApply`.
- Produces: `JoinBranch`, `BranchChoice`.

- [ ] **Step 1: Write `BranchChoice`**

```tsx
function BranchChoice({ onPick }: { onPick: (b: Branch) => void }) {
  return (
    <section className="welcome-choice">
      <button className="welcome-card" onClick={() => onPick("join")}>
        <h3>🤝 Join a deployment</h3>
        <p className="muted small">
          A teammate sent you a setup bundle. Paste it and you are looking at the
          same triage data they are.
        </p>
      </button>
      <button className="welcome-card" onClick={() => onPick("new")}>
        <h3>🌱 Triage a new repository</h3>
        <p className="muted small">
          Point Prospector at a repository of your own. A few questions, each
          with an instant option and a thorough one.
        </p>
      </button>
    </section>
  );
}
```

- [ ] **Step 2: Write `JoinBranch`**

One textarea, one button. `apply` with `step: "connect"` and the pasted text as
`bundle`; the backend parses and validates it.

```tsx
function JoinBranch({ onDone }: { onDone: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setProblem(null);
    try {
      await api.onboardingApply({ step: "connect", bundle: text });
      onDone();
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="setup-card">
      <h3>Paste the setup bundle</h3>
      <p className="muted small">
        Your teammate copies it from their Setup tab, under “Share this
        deployment”. It carries a database password, so it should have reached
        you privately.
      </p>
      <textarea className="welcome-paste" value={text} rows={10}
        placeholder='{ "version": 1, "env": { … } }'
        onChange={(e) => setText(e.target.value)} />
      {problem && <p className="chip chip-red sm">{problem}</p>}
      <button disabled={busy || text.trim() === ""} onClick={() => void submit()}>
        {busy ? "connecting…" : "connect"}
      </button>
    </section>
  );
}
```

- [ ] **Step 3: Verify and commit**

```bash
cd prospector_app/frontend && pnpm run build && pnpm exec eslint src/views/Welcome.tsx
git add -A
git commit -m "$(cat <<'EOF'
Join a deployment by pasting its bundle

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

### Task 13: The new-deployment branch

**Files:**
- Modify: `prospector_app/frontend/src/views/Welcome.tsx`

**Interfaces:**
- Consumes: `api.onboardingProbe`, `api.onboardingApply`.
- Produces: `NewBranch`, `EasyOrFull`.

- [ ] **Step 1: Write the fork control**

Each decision states what the instant option gets you and what the thorough one
costs, and neither is preselected:

```tsx
function EasyOrFull(
  { title, easy, full, pick, onPick }: {
    title: string;
    easy: { label: string; detail: string };
    full: { label: string; detail: string };
    pick: "easy" | "full" | null;
    onPick: (p: "easy" | "full") => void;
  },
) {
  return (
    <div className="welcome-fork">
      <h4>{title}</h4>
      {(["easy", "full"] as const).map((k) => {
        const o = k === "easy" ? easy : full;
        return (
          <label key={k} className={pick === k ? "fork-opt on" : "fork-opt"}>
            <input type="radio" checked={pick === k} onChange={() => onPick(k)} />
            {" "}<strong>{o.label}</strong>
            {" "}<span className="muted small">{o.detail}</span>
          </label>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 2: Compose the three forks**

Copy, verbatim from the spec:

- **Store** — Easy: *"Store triage data on this computer — nothing to set up, works immediately."* Full: *"Use a cloud database so a team shares one store — needs a database like Supabase, about 5 minutes."*
- **GitHub App** — Easy: *"See and analyze the repository with your own GitHub login — no writes, nothing to create."* Full: *"Create a GitHub App so a bot can merge, close, and comment for you — about 10 minutes on github.com."*
- **Profile** — Easy: *"Use the generic policy — every area classifies the same, nothing is owner-gated."* Full: *"Start from the example profile and describe this repository's areas and risk tiers."*

The repository field is required in both paths; on blur, call
`api.onboardingProbe({repo})` and show whether the operator's `gh` can read it.
Full store shows a store-URL field with the same probe treatment. Full App shows
`TRIAGE_BOT_LOGIN`, `TRIAGE_BOT_APP_ID`, `TRIAGE_BOT_KEY_FILE` fields, a link to
GitHub's App-creation docs, and probes the PEM path.

Submitting calls `apply` with `step: "connect"` carrying `TRIAGE_REPO` plus
whichever of `TRIAGE_STORE_URL` / `TRIAGE_PROFILE` the choices produced, and — if
the Full App path was taken — a second `apply` with `step: "writes"`.

- [ ] **Step 3: Verify and commit**

```bash
cd prospector_app/frontend && pnpm run build && pnpm exec eslint src/views/Welcome.tsx
git add -A
git commit -m "$(cat <<'EOF'
Guide a new deployment, with an instant option at each decision

Store, GitHub App, and profile each offer a choice that works immediately and
one that costs time, with what each gets you stated rather than implied.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

### Task 14: The ladder

**Files:**
- Modify: `prospector_app/frontend/src/views/Welcome.tsx`
- Modify: `prospector_app/frontend/src/views/Setup.tsx` (link back from the provisioning card)

**Interfaces:**
- Consumes: `OnboardingState` from Task 11.
- Produces: `Ladder`.

- [ ] **Step 1: Write the ladder**

Three rungs. A satisfied rung renders as done rather than asking again, so a
reload mid-ladder resumes:

```tsx
function Ladder({ state, onChange }: { state: OnboardingState; onChange: () => void }) {
  const name = state.display_name || state.repo;
  return (
    <>
      <section className="setup-card setup-done">
        <h3>✅ You can see {name}</h3>
        <p className="muted small">
          {state.counts.prs ?? 0} pull requests and {state.counts.clusters ?? 0}{" "}
          clusters loaded from the store. <Link to="/">Go look around</Link> — or
          keep going below.
        </p>
      </section>

      {state.writes_ready
        ? <section className="setup-card setup-done">
            <h3>✅ You can write to {name}</h3>
            <p className="muted small">
              Approved actions post as <code>{state.bot_login}</code>.
            </p>
          </section>
        : <WritesStep onDone={onChange} />}

      {state.worker_ready
        ? <section className="setup-card setup-done">
            <h3>✅ This computer runs automated tasks</h3>
          </section>
        : <section className="setup-card">
            <h3>Optional last step: run automated tasks here</h3>
            <p className="muted small">
              Analyze, test, and fix pull requests in a sandbox on this machine.
              Heavier, and meant for a computer you can leave running.
            </p>
            <Link to="/setup">Set this computer up →</Link>
          </section>}
    </>
  );
}
```

`WritesStep` collects `TRIAGE_BOT_LOGIN`, `TRIAGE_BOT_APP_ID`,
`TRIAGE_BOT_KEY_FILE`, probes the PEM, and calls `apply` with `step: "writes"`.
The third rung links to Setup rather than duplicating readiness — that card
already owns the checks, the one command, and the lanes.

- [ ] **Step 2: Verify and commit**

```bash
cd prospector_app/frontend && pnpm run build && pnpm exec eslint src/views/Welcome.tsx
git add -A
git commit -m "$(cat <<'EOF'
Climb the setup ladder one opt-in step at a time

Each rung ends with evidence it worked and an explicit choice to continue or
stop. A rung already satisfied renders as done, so a reload resumes.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

### Task 15: Verify the wizard in the running app

Not a code task. The wizard is the only slice a screenshot can prove.

- [ ] **Step 1: Serve an unconfigured checkout**

Build the frontend, then start the backend with the deployment target removed:

```bash
cd prospector_app/frontend && pnpm run build
```

Use the preview tooling (never a bare `uvicorn` in Bash) against a `.env` whose
`TRIAGE_REPO` is commented out. Keep a copy of the real `.env` and restore it
afterwards.

- [ ] **Step 2: Confirm each state**

- Unconfigured: the app lands on `/welcome` from any URL; the two branch cards
  render; `/api/clusters` returns 409 in the network log.
- Join: pasting a bundle copied from a configured instance's Setup tab configures
  the app and shows the "You can see …" rung with real counts.
- Junk paste: a 400 with a readable message, not a stack trace.
- Ladder: the writes rung renders when no App is configured; the worker rung
  links to Setup.

- [ ] **Step 3: Screenshot both branch states and the ladder**

- [ ] **Step 4: Restore `.env`**

### Task 16: Documentation

**Files:**
- Modify: `CLAUDE.md` (trust model)
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Add the onboarding write surface to the trust model**

In `CLAUDE.md`'s "Trust model" section, after the `worker_control` description,
state: a second `.env` writer exists; what each step may write; that step 1's
keys close once `settings.configured()` is true, so a configured deployment
cannot be retargeted over HTTP; and that a deployment with no `TRIAGE_BOT_LOGIN`
is legal and refuses every upstream write.

- [ ] **Step 2: Point first-run at the wizard**

`.env.example`'s header and `README.md`'s setup section currently read as the
first-run instructions. Both should say that `prospector serve` on an
unconfigured checkout opens a setup wizard, and that `.env.example` is the
reference for every option rather than the starting point.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Document the onboarding write surface and first run

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage.** §1 accessors → Task 1. §2 adoption → Tasks 2 and 5
(`reconfigure`). §3 gate → Task 3. §4 endpoints → Task 7. §5 write surface →
Tasks 4 and 5. §6 bundle → Tasks 5 and 10. §7 App-less → Task 8. §8
operation-scoped resolution → Task 9. §9 wizard → Tasks 11–14. Error handling →
Task 5's rollback tests and Task 7's 400/500 mapping. Security review → Tasks 3,
5, 6, 8. Testing → each task's own test step. Documentation → Task 16. No gaps.

**Naming consistency.** `settings.configured()`, `data.reset()`,
`profile.reset_cache()`, `env_file.merge`/`write`, `onboarding.STEP_KEYS`/`apply`/
`probe`/`reconfigure`/`build_bundle`/`parse_bundle`/`state` are used with the same
names and signatures in every task that references them. `RepoMeta.configured` and
`OnboardingState` match between Tasks 3, 11, and 14.

**Known soft spots**, flagged rather than papered over — each task's Step 1 says
to confirm against the real code before writing assertions:

- `data`'s snapshot accessor and watermark names (Tasks 2, 5).
- `executor`'s write entry points and argv shape (Task 9).
- `bot_merge_run`'s signature (Task 8).
- `caps.py`'s payload shape (Task 8).
