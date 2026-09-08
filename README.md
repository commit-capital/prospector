# Prospector

**Take back control of an overflowing GitHub repository.**

In the age of AI, code is cheap. The cost has moved to the repository. Prospector
works your open pull requests and issues down to repository inbox zero: it
triages, tests, fixes, and merges contributions in repositories where the pace
of contribution has outrun human review. Every upstream action still waits for
a human click.

It was built working down a real ~3,000-PR backlog. That deployment is
written up in [docs/deployments/paperclip.md](docs/deployments/paperclip.md).

## Quick start

```bash
npx github:commit-capital/prospector
```

One command: it installs [`uv`](https://docs.astral.sh/uv/) if missing, clones
the repository, sets up the pinned Python environment and the frontend, and
starts the app in your browser. You need macOS or Linux with `git` and Node
≥ 24. Everything else is fetched for you.

Three more things, none needed to get the app up:

- Sign in the [`gh` CLI](https://cli.github.com/) before your first ingest,
  since every GitHub read runs as your login.
- Triage judgment (clustering, analysis, security review, verification) runs
  through [Claude Code](https://code.claude.com/docs) on your
  own login; the in-app agent can use Claude Code or Codex. Ingest, the threat
  scan, and the app itself need neither.
- Docker is needed only on a machine that runs the sandboxed verification and
  autofix queues; the [setup guide](docs/setup.md) covers provisioning one.

Already have a clone?

```bash
./setup.sh                      # needs uv; installs the locked Python env + frontend deps (idempotent)
uv run prospector serve --dev   # then open the printed frontend URL
```

First launch opens a **setup wizard**: paste the share-bundle a teammate
copies from their 🛠️ Setup tab, or answer a few questions to point Prospector
at a repository of your own. Every step after that is opt-in: see the
repository, then let a bot write to it, then optionally run automated tasks on
this machine. The [setup guide](docs/setup.md) covers the rest, including
worker machines and running without the dev servers.

## How it works

One SQL store is the single source of truth: one validated row per PR and per
cluster, every fact stamped with the PR head it was computed against, so it
goes stale automatically when an author pushes. Seven idempotent phases plus a
deterministic threat scan feed it:

```
INGEST ─► THREAT SCAN ─► CLUSTER ─► ANALYZE ─► GATE ─► SECURITY ─► VERIFY ─► RESOLVE
```

- **INGEST:** fetch open PRs (drafts included) and their linked issues.
- **THREAT SCAN:** apply deterministic attack signatures and the actor blocklist.
- **CLUSTER / ANALYZE:** summarize diffs, group related PRs, and propose dispositions.
- **GATE / SECURITY / VERIFY:** apply quality gates, adversarial review, and
  secretless red→green verification.
- **RESOLVE:** a human approves; the executor performs controlled upstream
  actions as a GitHub App.

Deterministic drivers own selection, validation, and store writes; agentic
judgment runs through schema-validated workflows and never writes the store
directly.

## Learn more

- [Setup guide](docs/setup.md) - prerequisites in detail, worker machines,
  single-process serve, repository layout.
- [Configuration](docs/configuration.md) - `.env`, the repository profile,
  the backing store, and registering the GitHub App bot identity.
- [Operations](docs/operations.md) - which commands you run, which the agent
  runs, the disposition vocabulary, and the merge bars.
- [ARCHITECTURE.md](ARCHITECTURE.md) - where state lives and how it flows.
- [CLAUDE.md](CLAUDE.md) - the trust model and operating rules (authoritative).
- [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). MIT licensed.

## Safety

Prospector is a local, single-operator tool: the backend binds localhost and
has no authentication. Reads run as your local `gh` login; writes run only as
the GitHub App you register, only on a machine holding its private key, and
every executor action is gated and logged. Merges additionally pass a per-PR
eligibility gate. The full model is in [CLAUDE.md](CLAUDE.md) and
[docs/operations.md](docs/operations.md).
