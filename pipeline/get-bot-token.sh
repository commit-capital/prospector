#!/usr/bin/env bash
# get-bot-token.sh — Generate a short-lived GitHub installation token for the
# triage bot.
#
# Output: a GitHub API token valid for 1 hour, scoped to the bot app's
# installation on the TRIAGE_REPO owner's account. `--permissions` prints the
# installation token's repository-permissions object without printing the token.
#
# Usage:
#   export GH_TOKEN=$(bash pipeline/get-bot-token.sh)
#   GH_TOKEN=$(bash pipeline/get-bot-token.sh) gh api repos/<TRIAGE_REPO>/pulls/<N>
#   bash pipeline/get-bot-token.sh --permissions
#
# Config (environment wins over the repo-root .env, matching settings.py):
#   TRIAGE_BOT_APP_ID  — numeric GitHub App id of the bot
#   TRIAGE_REPO        — owner/name of the triaged repo; the owner's
#                        installation is the one the token is minted for
#   TRIAGE_BOT_KEY_FILE — path to the app's private key PEM
# Requires: node (for JWT signing), jq
set -euo pipefail

if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [--permissions]" >&2
  exit 2
fi
OUTPUT_MODE="${1:-token}"
case "$OUTPUT_MODE" in
  token|--permissions) ;;
  *)
    echo "Usage: $0 [--permissions]" >&2
    exit 2;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -f "$ROOT/.env" ]; then
  if [ -z "${TRIAGE_REPO:-}" ]; then
    TRIAGE_REPO="$(sed -n 's/^TRIAGE_REPO=//p' "$ROOT/.env")"
  fi
  if [ -z "${TRIAGE_BOT_APP_ID:-}" ]; then
    TRIAGE_BOT_APP_ID="$(sed -n 's/^TRIAGE_BOT_APP_ID=//p' "$ROOT/.env")"
  fi
  if [ -z "${TRIAGE_BOT_KEY_FILE:-}" ]; then
    TRIAGE_BOT_KEY_FILE="$(sed -n 's/^TRIAGE_BOT_KEY_FILE=//p' "$ROOT/.env")"
  fi
fi

APP_ID="${TRIAGE_BOT_APP_ID:-}"
case "$APP_ID" in
  ''|*[!0-9]*)
    echo "ERROR: TRIAGE_BOT_APP_ID must be the bot's numeric GitHub App id. Set it in .env — see .env.example." >&2
    exit 1;;
esac

case "${TRIAGE_REPO:-}" in
  */*) ;;
  *)
    echo "ERROR: TRIAGE_REPO is required and must be 'owner/name'. Set it in .env — see .env.example." >&2
    exit 1;;
esac
REPO_OWNER="${TRIAGE_REPO%%/*}"

KEY_FILE="${TRIAGE_BOT_KEY_FILE:-}"
case "$KEY_FILE" in
  '~/'*) KEY_FILE="$HOME/${KEY_FILE#\~/}" ;;
esac
if [ -z "$KEY_FILE" ]; then
  echo "ERROR: TRIAGE_BOT_KEY_FILE is required. Set it in .env — see .env.example." >&2
  exit 1
fi
if [ ! -r "$KEY_FILE" ]; then
  echo "ERROR: TRIAGE_BOT_KEY_FILE is not readable: $KEY_FILE" >&2
  exit 1
fi
PRIVATE_KEY="$(<"$KEY_FILE")"

# Generate a JWT signed with the private key (valid 60s — just enough to exchange for install token)
JWT=$(TRIAGE_BOT_PRIVATE_KEY="$PRIVATE_KEY" node -e "
const crypto = require('crypto');

const now = Math.floor(Date.now() / 1000);
const payload = { iat: now - 10, exp: now + 60, iss: '$APP_ID' };
const key = process.env.TRIAGE_BOT_PRIVATE_KEY;

const header = Buffer.from(JSON.stringify({ alg: 'RS256', typ: 'JWT' })).toString('base64url');
const body = Buffer.from(JSON.stringify(payload)).toString('base64url');
const data = header + '.' + body;
const sig = crypto.createSign('RSA-SHA256').update(data).sign(key, 'base64url');
console.log(data + '.' + sig);
")

unset PRIVATE_KEY

if [ -z "$JWT" ]; then
  echo "ERROR: JWT generation failed — check that TRIAGE_BOT_KEY_FILE points to a valid PEM private key" >&2
  exit 1
fi

# Get the app's installation on the TRIAGE_REPO owner's account. The app can be
# installed on other accounts too; only the owner's installation yields a token
# that can touch the triaged repo.
INST_RESP=$(curl -s \
  -H "Authorization: Bearer $JWT" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/app/installations")
INSTALLATION_ID=$(echo "$INST_RESP" | jq -r --arg owner "$REPO_OWNER" \
  '[.[] | select(.account.login == $owner)][0].id // empty')

if [ -z "$INSTALLATION_ID" ]; then
  echo "ERROR: GitHub App $APP_ID has no installation on $REPO_OWNER. Install the app on $TRIAGE_REPO and retry." >&2
  echo "API response: $INST_RESP" >&2
  exit 1
fi

# Exchange for an installation access token (valid 1 hour)
TOKEN_RESP=$(curl -s -X POST \
  -H "Authorization: Bearer $JWT" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/app/installations/$INSTALLATION_ID/access_tokens")
TOKEN=$(echo "$TOKEN_RESP" | jq -r '.token // empty')

if [ -z "$TOKEN" ]; then
  echo "ERROR: Failed to get installation token" >&2
  echo "API response: $TOKEN_RESP" >&2
  exit 1
fi

if [ "$OUTPUT_MODE" = "--permissions" ]; then
  echo "$TOKEN_RESP" | jq -c '.permissions // {}'
else
  echo "$TOKEN"
fi
