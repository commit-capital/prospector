# Setup guide

Everything about installing and running Prospector. The short version is on
the [README](../README.md); this is the detail.

## Prerequisites

- **macOS or Linux.**
- **`git`.**
- **[`uv`](https://docs.astral.sh/uv/)** — it fetches the pinned Python
  itself; you never install Python by hand.
- **The [`gh` CLI](https://cli.github.com/)**, authenticated to an account
  that can read the target repository (reads run as your local `gh` login).
- **Node ≥ 24** for the web UI (pnpm is resolved automatically).
- **Docker**, only if you run the VERIFY phase (sandboxed red→green fix
  verification).

The `npx github:commit-capital/prospector` bootstrap checks git and Node,
installs uv if it is missing, clones the repository, runs the same `setup.sh`
documented below, and opens the app in your browser once it is serving
(`--no-open` to skip that, `--no-serve` to stop after setup).

## Install and run

```bash
# 1. clone this repo, then from its root:
./setup.sh                      # uv-locked Python env + frontend deps (idempotent)

# 2. run the app and open the printed frontend URL
uv run prospector serve --dev   # or: ./run-prospector.sh
```

On a checkout with no deployment configured, the app opens a setup wizard
instead of the triage tabs. It takes either route: paste the bundle a teammate
copies from their 🛠️ Setup tab under "Share this deployment" — which carries
the repository, the store, and the repository profile, so one paste is enough —
or answer a few questions to point Prospector at a repository of your own,
where each decision offers an option that works immediately and one that costs
a few minutes. Setup then proceeds one opt-in step at a time: see the
repository, then let a bot write to it, then optionally run automated tasks on
this machine. `/.env.example` documents every option for editing the file
directly; you do not need it to get started.

For a single-process setup without the dev servers, build the frontend once
(`pnpm --dir prospector_app/frontend build`, or
`npx -y pnpm@11 --dir prospector_app/frontend build` without a pnpm install)
and run `uv run prospector serve`.

`uv run prospector --help` lists every subcommand. The Clusters board in the
web UI is the front door; `CLAUDE.md` (trust model and operating rules) and
`ARCHITECTURE.md` (the data layer) are the two documents to read before going
deeper. `STATUS.md` is a generated text snapshot of the store — regenerate it
with `uv run prospector status`.

## Worker machines

To make a machine process work rather than just serve the UI — running
verification sandboxes and autofix — run `./setup-worker-machine.sh` on it and
watch the app's 🛠️ Setup tab go green. Any number of machines can; each holds
its own sandbox base, and the queue claim is a compare-and-swap so two never
pick up the same PR.

## Repository layout

| Folder | Purpose |
|--------|---------|
| `pipeline/` | The store (`store/`), the phase drivers, `gates.py` / `freshness.py` / `taxonomy.py` / `profile.py`, the Workflow scripts, the `prospector` CLI (`cli.py`), and `views.py` (generates `STATUS.md`). |
| `prospector_app/` | The web app: `backend/` (FastAPI), `frontend/` (React/Vite), and `agent/` (Ask-pane helpers and operating context). |
| `issue_triage/` | The **issue** pipeline, on the same substrate as `pipeline/`: its own validated store (`store/`), `issue_freshness.py` / `issue_gates.py` / `issue_model.py` over the shared `pipeline/storekit.py`, and phase drivers (INGEST → CLUSTER → ANALYZE). Imports `pipeline/taxonomy.py`; the app Issues tab projects its store. |
| `alert_triage/` | The **security-alert** pipeline, on the same substrate: GitHub code-scanning / Dependabot / secret-scanning alerts for `TRIAGE_REPO`, read and actioned as the bot App. `alert_store.py` / `alert_model.py` / `alert_freshness.py` / `alert_gates.py` over the shared `pipeline/storekit.py`, plus `alert_ingest.py` (fetch + deterministic PR linking) and `alert_fixed_driver.py` / `find_fixed.py` (the tiered already-fixed pass). Plus `advisory_store.py` / `advisory_model.py` / `advisory_ingest.py` / `advisory_find_fixed.py` for repository security advisories (read-only; no upstream write path) and `security_sweep.py`, the one Control-tab `security-sweep` job over both. The app 🛡️ Alerts tab projects both stores, opening on its Advisories sub-view; secret values are never stored. |

## Platform contract & versioning

Python is pinned to `==3.14.*` (`.python-version` + `uv.lock`); that exact pin
is the tested contract, and `uv` downloads it on any supported platform — you
never install Python by hand. The frontend needs Node ≥ 24 and pnpm ≥ 11
(`setup.sh` resolves pnpm via `npx` if it isn't installed). Supported
platforms: macOS and Linux.

Versioning is `0.x`, bumped manually in `pyproject.toml` at meaningful
milestones; there is no release cadence.
