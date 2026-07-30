# Design: rename the project surface to Prospector

Date: 2026-07-30
Status: implemented

## Problem

The project is called Prospector. Three older names survive in the tree:

- `review_cockpit` — the Python package holding the web app (125 files reference it).
- `pr-triager` — the distribution name, the console script, and the `pr_triager.sh`
  wrapper (16 files).
- "cockpit" — the bare word, used as the domain noun for the web surface in docs,
  comments, and `CLAUDE.md` (112 files), and as the `COCKPIT_*` prefix on six
  environment variables.

The genericization pass that produced Prospector made the *product* deployment-neutral —
repository, bot identity, and profile are all configuration, and the UI title derives
from `meta.display_name` — but left the package and distribution names untouched.

The deployment names remaining under `docs/deployments/` are not drift: that case study
and its README link are deliberate, and allowlisted by
`.github/scripts/release_tree_guard.sh`. Note that the guard scans for those identifiers
tree-wide, so documents about the deployment must refer to it by path, not by name.

## Target names

| current | target | kind |
| --- | --- | --- |
| `review_cockpit/` | `app/` | directory and Python package |
| `review_cockpit.backend` | `app.backend` | package; uvicorn target becomes `app.backend.app:app` |
| `pr-triager` (`[project] name`) | `prospector` | distribution |
| `pr-triager` (`[project.scripts]`) | `prospector` | console script |
| `pr_triager.sh` | `run-prospector.sh` | convenience wrapper |
| `COCKPIT_*` (6 vars) | `PROSPECTOR_*` | environment variables |
| "cockpit" (bare word) | "the app" or "Prospector" | prose |

Component directories stay descriptive rather than carrying the product name:
`app/` sits beside `pipeline/`, `issue_triage/`, and `sandbox/`. "Prospector" names
the project; each directory says what it is.

`app` is a legal Python package name and is currently unclaimed in the environment —
nothing in site-packages provides a top-level `app` module. A future dependency could
claim it; that risk is accepted.

## Environment variables

Renamed with no compatibility fallback:

| current | target |
| --- | --- |
| `COCKPIT_FEEDBACK_REPO` | `PROSPECTOR_FEEDBACK_REPO` |
| `COCKPIT_LIVE_TTL_MIN` | `PROSPECTOR_LIVE_TTL_MIN` |
| `COCKPIT_OPERATOR` | `PROSPECTOR_OPERATOR` |
| `COCKPIT_NO_LAUNCH_SWEEP` | `PROSPECTOR_NO_LAUNCH_SWEEP` |
| `COCKPIT_REVIEW_REFRESH_POLL_SECONDS` | `PROSPECTOR_REVIEW_REFRESH_POLL_SECONDS` |
| `COCKPIT_REVIEW_REFRESH_ATTEMPTS` | `PROSPECTOR_REVIEW_REFRESH_ATTEMPTS` |

Every deployment's `.env` must be edited before its next restart. An unedited `.env`
starts with the feedback repository unset and the live TTL and review-refresh settings
back at their defaults. This is a coordination step, not a code change.

The `TRIAGE_*` prefix is correct and unchanged.

## Prose rule

Product references in subject position become "Prospector" ("Prospector executes
approved actions"). Component references become "the app" ("the app's activity log").
The trust-model section of `CLAUDE.md` carries the heaviest rewrite.

## Sequencing

PR #9 (`fix-dev-reload-exclude-fresh-clone`) lands first. Its `reload_exclude()` holds
the literal `review_cockpit / "cache"`, which this work rewrites; landing it first keeps
both diffs clean.

The rename is one pull request of four commits. It cannot be split across pull requests —
a partially renamed tree does not import, so every intermediate state would need a
compatibility shim that is deleted a day later.

1. **Package move** — `git mv review_cockpit app`, then `review_cockpit` → `app` across
   source, CI workflows, `pyproject.toml` packages, `pyrightconfig.json`, `setup.sh`,
   and `frontend-toolchain.sh`.
2. **Distribution and CLI** — `pyproject.toml` name and script, `pr_triager.sh` →
   `run-prospector.sh`, `uv.lock` refresh, and `.conductor/settings.toml`,
   `.superset/config.json`, `.claude/launch.json` moved in lockstep as the `CLAUDE.md`
   sync rule requires.
3. **Environment variables** — the six renames above.
4. **Prose sweep** — the bare word across docs, comments, and `CLAUDE.md`.

Commits 1–3 are mechanical and fully covered by the gates. Commit 4 is the one a
reviewer has to read; keeping it separate is the purpose of the split.

The identifier replacements run scripted (`git grep -l` plus a targeted replace per
identifier). Hand-editing 400+ sites reintroduces exactly the inconsistency this work
removes.

## Out of scope

- `review-new-pr/`, `pipeline/`, `issue_triage/`, `sandbox/` — descriptive component
  names. The hyphen in `review-new-pr/` is legal because it is not an importable package.
- `TRIAGE_*` environment variables.
- The case study under `docs/deployments/` and its README link.
- The UI title, which already derives from deployment configuration.

## Verification

A rename either passes every gate or is broken, so all of them run:

- `uv run pytest` — all three suites.
- `uv run ruff check .` — zero findings. `N999` confirms `app/` is a legal package directory.
- `uv run pyright pipeline issue_triage app/backend review-new-pr/harness` — zero errors.
- `pnpm run build` and `pnpm run lint` in `app/frontend/`.
- `bash .github/scripts/release_tree_guard.sh`.
- `uv sync && uv run prospector --help` on a clean editable install.
- `./run-prospector.sh`, then confirm the app renders with no console errors.

Three things the gates do not cover, each needing a manual pass:

- `.claude/skills/*` and `.claude/launch.json` are outside CI.
- `app/agent/*` bootstraps its own package root under bare `python3`, outside the
  editable install, so its paths are not checked by pyright or pytest.
- `review-new-pr/harness/` reads the profile standalone via `sys.path` manipulation.

## Risks

- **Deployments break on restart** until their `.env` is edited. Accepted; requires a
  heads-up to other operators.
- **Stale editable install** — the old `pr_triager.egg-info/` and the `pr-triager`
  console script linger in `.venv` until a resync.
- **In-flight branches conflict** — 125 files move. The work should be timed with other
  contributors.
