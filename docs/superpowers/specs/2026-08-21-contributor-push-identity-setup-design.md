# Contributor-push identity from the app

**Date:** 2026-08-21 · **Status:** approved, implementing

## Problem

Autofix pushes to contributors' branches as the identity in `TRIAGE_PUSH_LOGIN`
/ `TRIAGE_PUSH_EMAIL` / `TRIAGE_PUSH_SSH_KEY_FILE`. Nothing in the app writes
those: the onboarding `worker` step allowlists them but no view reaches it, the
deployment bundle deliberately omits them (the key path is local), and every
piece of copy calls the account a "dedicated machine user". So the only way to
turn on **Prepare fixes** / **Queue fixes on its own** on a freshly joined
machine is to hand-edit `.env`, and the only documented identity is a second
GitHub account — which the code never actually requires. It requires a GitHub
*user* login, a commit email, and a passphrase-less SSH key pinned with
`IdentitiesOnly=yes`; a maintainer's own account satisfies that today.

## Design

### Identity model (unchanged mechanics, wider framing)

The contributor-push user is a GitHub user account that holds push on
`TRIAGE_REPO` — either a dedicated account (outside collaborator with Write) or
the operator's own. Either way it authenticates by a pinned, passphrase-less
SSH key generated for Prospector alone; the operator's daily key is never
reused. The containment story is the same as before: SSH reaches git refs and
nothing else, `assert_push_target` bounds the refs, and the ssh invocation —
`-F /dev/null -i <key> -o IdentitiesOnly=yes -o IdentityAgent=none` — offers
the pinned key and no other (an `IdentityFile` the operator's `ssh_config`
names for github.com is offered even under `IdentitiesOnly`, which is why the
config file is ignored; the probe uses the same form, so GitHub's greeting
names the account *this* key opens). Choosing one's own account trades blast radius (a key on a
personal account reaches every repository that account can push to) and
attribution (fixes land as that person) for having no second account to run —
the UI says so once, plainly.

`pipeline/settings.py` reads the three values on each call (`push_login()`,
`push_email()`, `push_ssh_key_file()`), so a write from the app takes effect in
the running process like every other deployment value. The boot-time "all three
or none" guard stays; the onboarding writer enforces the same invariant on its
side.

### Setup page: a "Contributor-push identity" card

Shown inside the worker section whenever the `push_identity` readiness check
is not ok. Three paths:

1. **Push fixes as me.** The backend reads the operator's `gh` login and id,
   derives `<id>+<login>@users.noreply.github.com`, generates an ed25519 key at
   `~/.config/prospector/<login>/push-key` (0600, owner-only directory), and
   shows the public key with a link to `github.com/settings/ssh/new`. "I added
   it" runs `ssh -T git@github.com -i <key> -o IdentitiesOnly=yes` and requires
   GitHub's `Hi <login>!` before the three values are written through the
   `worker` step. A secondary field accepts an existing passphrase-less key
   path instead of generating one.
2. **Paste a push identity from another machine.** Accepts the deployment
   bundle; on a configured machine only its `push` section is taken.
3. **Use a dedicated account.** The walkthrough (create the user, add it as an
   outside collaborator with Write, generate the key here, add the public key
   to *that* account), then the same probe requiring `Hi <that-login>!`. The
   email is derived from `gh api users/<login>` — public, nothing needed from
   the new account.

### Share card: a second checkbox

"Also let the teammate's machine push fixes — includes the contributor-push
SSH private key." The bundle gains an optional `push: {login, email, ssh_key}`
section, carrying the key *bytes* like `bot_key_pem` does. `join` files the
key under `~/.config/prospector/<login>/push-key` and writes the three values.
`BUNDLE_VERSION` becomes 2: a checkout reading 1 refuses the bundle by name
instead of silently dropping the push section.

### Copy and docs

"Dedicated GitHub user" / "machine user" in the Setup page, readiness remedy,
`settings.py`, `.env.example`, `FixPanel`, `setup-worker-machine.sh`, docs and
`CLAUDE.md` become "the contributor-push user — a dedicated account or the
operator's own, authenticating by a pinned SSH key alone".

## Components

- `pipeline/settings.py` — call-time readers.
- `prospector_app/backend/push_identity.py` (new) — operator account lookup,
  `noreply_email`, `generate_key`, `probe_key` (parses `ssh -T`).
- `prospector_app/backend/onboarding.py` — bundle `push` section, `join` /
  `worker` filing of a pasted key, all-three-or-none validation.
- `app.py` / `models.py` — `/api/onboarding/push-identity/{account,key,probe}`,
  `SetupShare.include_push_key`.
- `Setup.tsx` — `PushIdentitySection`, share checkbox, copy; `Welcome.tsx`
  bundle description; `api.ts` types; `FixPanel.tsx` copy.

## Testing

- settings: readers follow the environment; partial set still refused at boot.
- push_identity: noreply email; `ssh -T` parsing (match, mismatch, denied);
  `generate_key` produces an owner-only ed25519 key and is idempotent.
- onboarding: push travels only on request; round-trips; a pasted push
  identity is filed 0600 outside the checkout and named in `.env`; the `worker`
  step refuses a partial triple; a `worker` apply from a bundle on a configured
  machine takes only the push section; an old-version bundle is refused by
  name.
- worker_readiness: remedy points at the card.
