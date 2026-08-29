# Configuration

The system reads a single gitignored `/.env` at the repo root. Copy
`/.env.example` to `/.env` and fill in as needed; `pipeline/settings.py`,
`setup.sh`, and Vite all read it. Real shell environment variables override
anything in the file. The app's setup wizard writes the same file for you —
see the [setup guide](setup.md).

## Repository profile

Repository-specific policy vocabulary — the subsystem taxonomy the CLUSTER
phase and issue triage classify against, the path→risk-tier map, the
CODEOWNERS gated paths/owners, trusted/automation authors, dependency
manifests, test/artifact path conventions, review-harness PR-template policy,
and VERIFY full-suite adapter — lives in a JSON **repository profile** selected
by `TRIAGE_PROFILE` (see `profile.example.json` for the shape;
`pipeline/profile.py` validates it strictly and fails loudly on any malformed
or unknown field). Match terms are regular expressions searched against the
lowercased title/body — write them in lowercase. The `test_paths` patterns are
also compiled by the app frontend as JavaScript RegExp — keep them in the
shared regex subset (no `(?P<…>)` named groups, inline flags, or possessive
quantifiers; `\Z` differs between engines). Without a profile the generic
default applies: no subsystem vocabulary, so every PR and issue classifies as
`other` — clustering still works, just without subsystem grouping, risk
ranking knows only the shared supply-chain surface, no path is
CODEOWNERS-gated, no author is trusted, dependency/test/artifact conventions
fall back to cross-ecosystem defaults, the review harness enforces no
PR-template sections, and the baseline/regress leg is skipped until
`verify.suite` is configured. The `dependency_manifests` list also drives the
VERIFY sandbox's dependency-refusal gate — narrowing it below the generic
default weakens that protection. The real profile lives beside `.env` as the
gitignored `profile.json` at the repo root.

## Backing store

With `TRIAGE_STORE_URL` unset, each store component uses a local SQLite file
under its own directory — fine for dev, CI, or solo work. Set
`TRIAGE_STORE_URL` to a shared PostgreSQL database URL to point the whole
system (and every operator machine) at one shared store. SQLite and PostgreSQL
are the supported store dialects.

No example, demo, or seed store ships with the project. A fresh checkout
starts empty, and `prospector ingest` populates it from the repository you
configure.

The SQL store can be exported to a tree of JSON files at any time — a backup /
inspection escape hatch (the JSON is never read back as a store; re-importing
it is the reverse `import` subcommand):

```bash
# PR store
uv run python pipeline/store_migrate.py dump @env <output-dir>

# Issue store
uv run python issue_triage/issue_store_migrate.py dump @env <output-dir>
```

## In-app agent

`TRIAGE_AGENT_PROVIDER` selects the local CLI behind the “Ask the agent”
sidebar: `claude`, `codex`, or `none`. The 🛠️ Setup page exposes the same
choice on both new and configured installs and adopts changes in the running
process. Claude uses the login from `claude auth login`; Codex uses the login
from `codex login`. The choice and credentials stay on the machine and are not
included in deployment-sharing bundles.

## Bot identity (live writes)

Upstream writes execute as a GitHub App, and each deployment registers its own
— the app's private key is its identity, so one app cannot be shared across
deployments. Without one, everything still works read-only: the executor mints
no token and every write runs dry.

1. [Register a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app) on the account or org that owns `TRIAGE_REPO`. Name it what you want writes attributed as; no webhook needed. Grant these repository permissions (the set the write paths need):
   - **Contents**: read & write (squash-merges)
   - **Issues**: read & write (issue close/comment/reopen)
   - **Pull requests**: read & write (PR comment/close/review/merge)
   - **Actions**: read & write (confirmed workflow reruns from the in-app agent)
   - **Metadata**: read (implied)
   - **Code scanning alerts**: read & write (🛡️ Alerts tab — optional; ingest + dismissal)
   - **Dependabot alerts**: read & write (🛡️ Alerts tab — optional)
   - **Secret scanning alerts**: read & write (🛡️ Alerts tab — optional)
   - **Repository security advisories**: read (🛡️ Alerts → Advisories — optional)

   The alert and advisory permissions are optional: without them the 🛡️ Alerts
   tab reports each source unavailable and everything else works unchanged.
   Saving a new required permission prompts the owner of each installation to
   approve it; the old grant remains in effect until that approval is complete.
2. Keep the app install-restricted (**Only on this account**) and install it on the `TRIAGE_REPO` owner, granting the triaged repo. `pipeline/get-bot-token.sh` selects the installation whose account is the owner of `TRIAGE_REPO`, so a stray installation elsewhere is never used — but there's no reason to allow one.
3. Generate a private key in the app settings and save the PEM outside the repo (e.g. `~/.config/<app>/private-key.pem`), then wire `.env`:
   - `TRIAGE_BOT_APP_ID` — the app's numeric id (on the app's settings page)
   - `TRIAGE_BOT_LOGIN` — the app's slug (writes are attributed to `<slug>[bot]`)
   - `TRIAGE_BOT_KEY_FILE` — path to the PEM; the PEM itself stays outside the repo and environment

Only machines that should execute live writes get the key; the app id and
login are not secrets. Verify the wiring with `bash pipeline/get-bot-token.sh`
(needs `node` and `jq`) — it prints a one-hour installation token on success
and a specific error naming the missing piece otherwise.

**Sharing the identity with a teammate.** The 🛠️ Setup tab's "Share this
deployment" bundle always carries the bot login and app id, so a joiner's
writes are attributed the same way. Ticking *Also let the teammate act as the
bot* adds the private key itself: the joiner's app files it owner-only at
`~/.config/prospector/<login>/private-key.pem`, outside the checkout, and sets
`TRIAGE_BOT_KEY_FILE` to that path, so their machine executes approved writes
too. The bundle already carries the store password, so it is a credential
either way — send it through a password manager or a direct message, never a
channel with history.
