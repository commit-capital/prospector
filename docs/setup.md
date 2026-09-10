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
copies from their 🛠️ Setup tab under "Invite a member to this project" — which carries
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
pick up the same PR. The same command supports macOS and Linux: it starts
Colima on macOS and the host's Docker Engine service on Linux. Linux package
installation supports `apt-get` (Ubuntu/Debian) and `dnf` (Amazon Linux,
Fedora, and related distributions). On another distribution, install Docker
Engine, `gh`, `jq`, Node 24+, and `uv` first and then run the command.
For a verification-only cloud host whose `.env` and `profile.json` are already
in place, the unattended form is:

```bash
./setup-worker-machine.sh --verify-only --yes
```

Run the script as the eventual service user, without `sudo`; it elevates only
package-manager, Docker-service, and group-membership operations.

The worker also needs the Claude CLI and a usable non-interactive login because
VERIFY's blind and post-run judgments are headless Claude processes. Run
`claude auth login` as the account that will own the worker service, or provide
that account an `ANTHROPIC_API_KEY`. GitHub reads likewise use that account's
stored `gh auth login`; exported `GH_TOKEN` values are not the worker's local
read identity.

### Running continuously on Linux

The verification worker lives inside the Prospector backend process. A
dedicated Linux host can keep that process alive with systemd without exposing
the app port. Create `/etc/systemd/system/prospector-worker.service` with the
following unit, replacing `ubuntu`, the two home-directory paths, and the
checkout path for the account and location on the machine:

```ini
[Unit]
Description=Prospector verification worker
Wants=network-online.target
After=network-online.target docker.service
Requires=docker.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/prospector
Environment=HOME=/home/ubuntu
Environment=PATH=/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PROSPECTOR_NO_LAUNCH_SWEEP=1
ExecStart=/home/ubuntu/.local/bin/uv run prospector serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

The service user must be in the `docker` group and must own the checkout,
`.env`, `profile.json`, the `gh` login, and the Claude login. The setup command
adds its invoking Linux user to the group when necessary; a fresh login makes
that membership active. Then enable the service and follow its log:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now prospector-worker
journalctl -u prospector-worker -f
```

No inbound security-group rule is needed for this worker-only process. It binds
the API to loopback and reaches the shared queue through `TRIAGE_STORE_URL`.
Stopping the unit lets an in-flight subprocess finish only until systemd's
default stop timeout, so stop it while idle or re-queue an interrupted run from
the app.

Autofix additionally needs a **contributor-push identity** on the machine: a
GitHub user account — your own, or a dedicated one — whose SSH key pushes
updates, rebases, and fixes to contributors' branches. The Setup tab's
*Contributor-push identity* card sets one up: it generates a key for Prospector
alone, you add the public half to the account, and GitHub confirms which account
the key authenticates before anything is written. A teammate who already has one
can tick *also let the teammate's machine push fixes* on their share card, and
the pasted bundle carries it — into the first-run wizard on a fresh checkout,
or into the same card on a machine that is already set up.

To take a machine back out, the Setup tab's **Unprovision this computer** card
stops the work in one click (every lane off; one click brings it back) and,
expanded, composes the reverse of the setup command for you to run:
`./teardown-worker-machine.sh [--artifacts] [--vm] [--packages]` — lane
switches off always; `--artifacts` removes this machine's base images, sandbox
image, scratch clone, and base pin; `--vm` deletes the Colima VM on macOS and is
a no-op on Linux; `--packages` uninstalls Colima/Docker on macOS or Docker
Engine on Linux (never `gh`, `jq`, or Node). Stop and disable a systemd service
separately with `sudo systemctl disable --now prospector-worker`.

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
