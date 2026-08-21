# First-run onboarding

## Problem

A fresh checkout cannot show anything. `pipeline/settings.py` raises `SystemExit`
at import when `TRIAGE_REPO` or `TRIAGE_BOT_LOGIN` is unset, and `pipeline/cli.py`
imports it at module scope, so `prospector serve` exits before a single route is
registered. The remedy is hand-editing `.env` against `.env.example` and copying
a `profile.json` from a machine that already works.

Onboarding a teammate onto an existing deployment is the same problem wearing a
different hat. The Setup tab's share snippet emits a commented `.env` that names
`profile.json` as something to obtain by hand, so the receiver still edits two
files from instructions before the app will start.

## Goals

- A checkout with no configuration serves a wizard instead of failing to boot.
- A teammate joins an existing deployment by pasting one thing.
- Configuration proceeds one opt-in step at a time, each ending in evidence that
  the step worked and an explicit choice to continue or stop.
- Applying configuration takes effect in the running process — no restart.

## Non-goals

- Creating the bot's GitHub App. That happens on github.com and yields a PEM
  download; the wizard links to it and accepts the result.
- Editing configuration after onboarding. `.env` stays the operator's file; the
  wizard's write surface narrows once the deployment is configured (below).
- Provisioning the worker machine. Step 3 hands off to the existing Setup view,
  which already owns readiness and the lane switches.

## Shape

### 1. `settings` accessors

The deployment-target values become functions reading the current environment,
matching `fix_worker_enabled()` / `push_identity_configured()`, which are already
accessors for exactly this reason: a value written to `.env` cannot take effect
while it is bound at import.

Converted (~87 call sites): `repo()`, `repo_owner()`, `repo_name()`, `repo_url()`,
`display_name()`, `bot_login()`, `store_url()`, `profile_path()`,
`review_provider()`, `review_threshold()`, `feedback_repo()`, `verify_scratch()`.

`REPO_ROOT` stays a constant — it is the checkout's own path, not deployment
config, and never changes for a running process.

Both `SystemExit`s go. In their place:

```python
def configured() -> bool:
    """Whether this checkout has a deployment target. The ONE predicate the
    unconfigured gate and the wizard both read."""
    return "/" in os.environ.get("TRIAGE_REPO", "")
```

`bot_login()` returns `""` when unset — an App-less deployment is legal and
reads fine; §7 covers what that means for writes.

The 33 `from pipeline.settings import REPO`-style imports become qualified
`settings.repo()` calls. Ruff and pyright cover the mechanical risk; the
conversion is the largest diff in this work and lands as its own commit,
reviewed on its own, before anything below.

### 2. Adoption: `reconfigure()`

Accessors are not sufficient on their own. Two things hold values built from the
old configuration:

- `data.py`'s `_store = Store()` — a module-level singleton constructed at import
  with a live engine, plus the in-memory snapshot every board read serves from.
  Left alone, a wizard write appears to succeed while every read still serves the
  local SQLite file an unconfigured boot created.
- `settings.default_branch()`'s `lru_cache` and `profile._load`'s `@cache`
  (keyed by path, so a rewritten `profile.json` at the same path stays stale).

`data.reset()` rebuilds the store and empties the snapshot and its watermarks,
taking `_check_lock` so it cannot race the background freshener.
`profile` gains a public `reset_cache()` rather than other modules reaching into
its `_load`. `onboarding.reconfigure()` is then the ONE adoption path: update
`os.environ` from the validated writes, then `data.reset()`,
`settings.default_branch.cache_clear()`, `profile.reset_cache()`.

`feedback.operator_login()` and `activity.operator()` cache the operator's own
identity, not the deployment target, and are deliberately left alone.

### 3. The unconfigured gate

An unconfigured process that merely fails to raise is worse than one that
crashes: measured against the current code with the `SystemExit`s removed, all
93 routes register and `/api/clusters` answers `{"items":[]}` from a silently
created SQLite file. An unconfigured Prospector renders as a working Prospector
watching an empty repository.

One middleware in `app.py` is the whole remedy. When `settings.configured()` is
false, every `/api/*` request returns `503 {"unconfigured": true}` except:

- `/api/onboarding/*`
- `/api/meta`
- the static and SPA routes

No handler is trusted to check for itself, and the enforcement reads in one
place.

### 4. `/api/onboarding/*`

- `GET /api/onboarding/state` — what is configured and what the next step is:
  `configured`, `writes_ready` (a bot identity and a mintable token),
  `worker_ready` (delegates to `worker_readiness.report()`), and for step 1's
  summary the store's PR/cluster counts.
- `POST /api/onboarding/probe` — validate without committing. Given a candidate
  store URL it opens a connection and counts rows; given a repo it checks the
  operator's `gh` can read it; given a PEM path it tries to mint a token. Returns
  findings, writes nothing. This is what lets each step show evidence before the
  user commits to it.
- `POST /api/onboarding/apply` — the ONE config writer. Validates, writes `.env`
  and `profile.json`, calls `reconfigure()`, returns the new `state`.

`GET /api/meta` grows `configured: bool` so the SPA can route at bootstrap; it
stays outside the gate for that reason.

### 5. What `apply` may write

`worker_control.WRITABLE` is deliberately five lane switches and must stay that
way. Onboarding needs a broader surface, so it gets its own allowlist, scoped by
step *and* by whether the deployment is already configured:

| Step | Keys | Allowed when |
|------|------|--------------|
| 1 connect | `TRIAGE_REPO`, `TRIAGE_STORE_URL`, `TRIAGE_PROFILE`, `TRIAGE_DEFAULT_BRANCH`, `TRIAGE_DISPLAY_NAME`, `TRIAGE_REVIEW_PROVIDER`, `TRIAGE_REVIEW_THRESHOLD`, `PROSPECTOR_FEEDBACK_REPO`, plus `profile.json` | only while unconfigured |
| 2 writes | `TRIAGE_BOT_LOGIN`, `TRIAGE_BOT_APP_ID`, `TRIAGE_BOT_KEY_FILE` | always |
| 3 worker | `TRIAGE_PUSH_LOGIN`, `TRIAGE_PUSH_EMAIL`, `TRIAGE_PUSH_SSH_KEY_FILE` | always |

Step 1's keys are refused once configured. That is the security rule that
matters: a configured deployment cannot be retargeted at another repository or
another database through the HTTP surface, while steps 2 and 3 stay open because
the ladder reaches them after step 1 has already configured the app. It is a
property of the write surface, not of Python constant binding — which is why the
accessors in §1 cost nothing here.

A key outside the step's allowlist is a hard error, never a silent skip.

The atomic `.env` rewrite (temp sibling, `chmod 0o600`, `replace`) is extracted
from `worker_control` into `env_file.py` and shared, so there is one merge-and-
replace implementation rather than two. `worker_control.set_flags` keeps its own
allowlist and calls it.

### 6. The bundle

`GET/POST /api/setup/share` returns a JSON envelope instead of a commented
`.env`:

```json
{
  "version": 1,
  "env": {"TRIAGE_REPO": "...", "TRIAGE_STORE_URL": "...", "...": "..."},
  "profile": { }
}
```

It always carries the store URL and the full `profile.json`, because a bundle
that needs a second out-of-band step is the problem this is solving. It is
therefore a credential, and the Setup card says so plainly rather than offering
a checkbox that makes it half-safe: send it through a password manager or a DM,
not a public channel. The `include store credentials` checkbox is removed.

JSON rather than an opaque blob so the receiver can read what they are about to
paste. `version` is checked on parse; an unknown version is refused with the
version it saw.

### 7. App-less deployments

"Easy" at the GitHub App step means skipping it: read Paperclip with your own
`gh` login, write nothing. `bot_login()` is then `""`.

Writes are already inert — no App means no key file, so `executor.live_possible()`
finds no mintable token and forces dry-run. This adds the explicit refusal rather
than relying on that: `safety_guard` refuses any write when `bot_login()` is
empty, alongside its existing empty-token refusal. `safety_guard` currently does
`from pipeline.settings import BOT_LOGIN`, binding the value at import; it moves
to `settings.bot_login()` with the rest of §1.

`caps.py` reports the App-less state so the UI shows why write controls are
absent, rather than showing them and failing.

### 8. Operation-scoped target resolution

With accessors, `repo()` and `bot_login()` are re-read on every call, so a single
upstream write could in principle straddle a configuration change: mint a token
for one target, validate against a second, execute against a third.

The executor and `safety_guard` therefore resolve the target once at the start of
an operation and thread those values through mint → validate → execute. This is
where the invariant belongs — at the operation boundary, where the race actually
is. It is also an improvement on the current code, which re-reads a module global
at each step and is safe only because nothing mutates it today.

### 9. The wizard

`/welcome`, a route like any other, and always reachable — the ladder's steps 2
and 3 run *after* step 1 has configured the app, so becoming configured must not
navigate the user out of the wizard. `configured: false` only makes the redirect
compulsory: every other route sends the user to `/welcome` and the tabs never
mount. Once configured, `/welcome` is simply a page the user can be on, linked
from Setup.

Two branches:

**Connect to an existing deployment.** One textarea. Paste the bundle, `probe`
reports what it found (repo, store reachable, N PRs), `apply` commits it.

**Set up a new deployment.** A guided form where each decision offers Easy and
Full with the cost of each stated:

- *Store* — Easy: local file, nothing to provision, works immediately. Full:
  point at a shared database like Supabase (~5 minutes) so a team shares one
  store.
- *GitHub App* — Easy: skip it, read with your own login, no writes, nothing to
  create. Full: create a GitHub App so a bot can merge, close, and comment
  (~10 minutes on github.com).
- *Profile* — Easy: the generic profile (everything classifies as `other`,
  nothing CODEOWNERS-gated). Full: start from `profile.example.json` and edit.

Then the ladder, each rung opt-in:

1. **See the data.** Ends with evidence — "3,000 PRs loaded. You can *see*
   Paperclip." Next: configure writes. Or stop here.
2. **Write to the repository.** The bot App's id and PEM path, verified by
   minting a token. Ends with the bot login it authenticated as. Next: run
   automated tasks. Or stop here.
3. **Run automated tasks on this machine.** Links to the Setup tab's
   provisioning card, which already owns readiness, the one command, and the
   lanes. The wizard does not duplicate it.

A step that is already satisfied renders as done rather than asking again, so a
reload mid-ladder resumes rather than restarting.

## Error handling

`apply` is all-or-nothing per call. Everything is validated before anything is
written: the bundle's version, every key against the step's allowlist,
`TRIAGE_REPO` against `owner/name`, and `profile.json` through
`profile.parse_profile` — a profile the parser would reject at boot is never
written to disk. `.env` is replaced from a temp sibling, so a failed write leaves
the previous file intact.

`profile.json` is written before `.env` and, if the `.env` write then fails, the
previous profile is restored from the pre-image held in memory. A half-applied
step would leave a checkout that boots against a repository whose policy file
belongs to a different deployment.

`probe` never writes and never raises to the client: an unreachable store, an
unreadable repo, and a PEM that will not mint are findings, since diagnosing them
is the wizard's job.

`reconfigure()` failing after a successful write is reported as
`restart_required` rather than swallowed, and the wizard shows the command. The
`.env` on disk is correct at that point; only this process failed to adopt it.

## Security review

- **The gate is the sole enforcement point.** A route added later inherits the
  refusal without its author doing anything.
- **A configured deployment cannot be retargeted over HTTP.** Step 1's keys are
  refused once `configured()` is true, so `TRIAGE_REPO` and `TRIAGE_STORE_URL`
  are not writable by an API caller on a working deployment.
- **The bundle is a credential and is labelled as one.** Removing the checkbox
  removes the state where an operator believes they shared something safe.
- **No new write reaches `TRIAGE_REPO`.** Onboarding writes two local files. The
  executor, `safety_guard`, and the `PreToolUse` hook are untouched, except for
  §7's additional refusal, which only ever refuses more.
- **`bot_login()` empty fails closed** in `safety_guard` rather than comparing
  against an empty string and matching something unintended.
- **`probe` acts on caller-supplied values**, which is inherent to diagnosing
  them: it opens a connection to a given store URL and attempts to mint from a
  given PEM path. On a localhost operator tool that is the same trust level as
  the existing `/api/setup/flags`, but it is a request-driven outbound connection
  and a file-existence oracle, so `probe` is rate-limited per process, never
  echoes file contents or the URL's password back to the caller, and reports
  failures as categories rather than raw exception text.

## Sequencing

Four slices, each shippable and reviewable on its own. Every slice leaves the
tree green; none depends on a later one to be correct.

1. **Accessors** (§1) — the mechanical conversion plus `configured()`, with the
   `SystemExit`s removed. Nothing behaves differently yet, because nothing calls
   `apply`. Largest diff, smallest behaviour change; reviewed alone for that
   reason.
2. **Adoption and the gate** (§2, §3) — `data.reset()`, `profile.reset_cache()`,
   `reconfigure()`, and the middleware. After this an unconfigured app boots and
   refuses to pretend, which is already an improvement on `SystemExit`.
3. **The write surface** (§4, §5, §6, §7, §8) — `env_file.py`, the onboarding
   endpoints, the bundle, the App-less refusal, operation-scoped resolution. The
   backend is complete and testable here with no UI.
4. **The wizard** (§9) — the frontend, which is the only slice a screenshot can
   verify.

## Testing

- `settings`: each accessor reads the current environment; `configured()` on a
  well-formed repo, a malformed one, and unset.
- The gate: an unconfigured app 503s a sample of `/api/*` routes, and serves
  `/api/meta`, `/api/onboarding/state`, and the SPA. A configured app serves all.
  This is the regression test for `{"items":[]}` from an accidental SQLite file.
- `apply`: writes `.env` and `profile.json`; refuses a key outside the step's
  allowlist; refuses step-1 keys once configured; refuses a profile
  `parse_profile` rejects, leaving both files untouched; restores the previous
  profile when the `.env` write fails; preserves unrelated `.env` lines
  byte-for-byte, including credential-bearing ones.
- `reconfigure`: a store URL change is visible to `data.store()` afterwards, and
  `default_branch` / `profile` return the new values rather than cached ones.
- The bundle: round-trips share → parse → apply; refuses an unknown version.
- `safety_guard`: refuses every write with an empty `bot_login()`.
- Operation-scoped resolution: a write whose target changes mid-operation still
  mints, validates, and executes against one target.
- `worker_control.set_flags` keeps its existing tests unchanged through the
  `env_file` extraction — the proof the refactor is behaviour-preserving.

## Documentation

`CLAUDE.md`'s trust model gains the onboarding write surface: a second `.env`
writer exists, what each step may write, and why step 1 closes once configured.
`.env.example` and `README.md` stop being the first-run instructions and point at
the wizard.
