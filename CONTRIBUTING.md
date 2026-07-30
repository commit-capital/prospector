# Contributing

Thanks for helping improve Prospector. The project is an early-stage,
local-operator tool, so small, focused pull requests with clear validation are
the easiest to review.

## Set up a development checkout

Prerequisites and configuration are documented in the root `README.md`. From a
fresh clone:

```bash
./setup.sh
cp .env.example .env
```

Use a disposable target repository and the default local SQLite store while
developing. Do not point a development checkout at a production triage store or
give it a live GitHub App key unless the change specifically requires an
end-to-end write test.

Run the application with:

```bash
uv run prospector serve --dev
```

## Validate changes

Run the checks relevant to your change before opening a pull request:

```bash
uv run pytest
uv run ruff check --no-fix .
uv run pyright pipeline issue_triage app/backend review-new-pr/harness
pnpm --dir app/frontend lint
pnpm --dir app/frontend build
bash .github/scripts/release_tree_guard.sh
```

The full Python suite includes SQLite coverage. PostgreSQL-specific tests run in
CI against its local PostgreSQL service.

## Pull requests

- Keep each pull request scoped to one coherent change.
- Explain the behavior change, its operator impact, and how it was tested.
- Add or update tests when behavior changes.
- Update documentation when commands, configuration, storage, or safety
  boundaries change.
- Never commit `.env`, private keys, database files, generated status reports,
  cached diffs, or deployment operating data.

`CLAUDE.md` is the authoritative trust model. Changes to upstream-write paths,
bot identity, command allowlists, merge gates, or activity logging deserve
explicit security-focused tests and review.

For vulnerabilities, follow `SECURITY.md` instead of opening a public issue.
