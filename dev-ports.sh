# Per-worktree dev ports — SOURCED by setup.sh, not executed.
#
# Every worktree is its own checkout with its own `.env`, so two of them must
# not name the same API/Vite port or only one app can run at a time. A new
# worktree starts from a copy of another one's `.env` (`.worktreeinclude` lists
# `.env*`, which is how operator config reaches it), so it arrives already
# naming the ports of the checkout it was copied from — which is exactly the
# case this has to correct.
#
# A port pair is left alone unless another worktree's `.env` names one of its
# numbers. A hand-picked port ("edit to override") is nobody else's, so it
# stays; an inherited one collides with the checkout it came from, so it moves.
# The primary checkout never yields: copies flow outward from it, so it is the
# one that was there first.

# Every port named by a worktree other than $2, as a space-padded string.
_dp_claimed_by_others() {
  local root=$1 self=$2 key=$3 out=" " wt sib
  while IFS= read -r wt; do
    sib="$wt/.env"
    [ "$sib" = "$self" ] && continue
    [ -f "$sib" ] || continue
    out+="$(sed -n "s/^$key=//p" "$sib") "
  done < <(git -C "$root" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p')
  printf '%s' "$out"
}

_dp_free() { ! lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

# The checkout copies flow outward from — the first entry git lists.
_dp_primary() {
  git -C "$1" worktree list --porcelain 2>/dev/null \
    | sed -n 's/^worktree //p' | head -1
}

_dp_realpath() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }

# Drop the port block, keeping every other line byte for byte — this file also
# holds the store password. Replaced from a temporary sibling so a failed write
# leaves the previous file intact.
_dp_strip() {
  local env_file=$1 staged
  staged="$(mktemp "$(dirname "$env_file")/.env.XXXXXX")"
  grep -v -e '^API_PORT=' -e '^VITE_PORT=' -e '^# Per-worktree dev ports' \
    "$env_file" >"$staged" || true
  chmod 600 "$staged"
  mv "$staged" "$env_file"
}

# Echo the first offset whose API and Vite ports are unclaimed and unbound.
_dp_pick() {
  local claimed_api=$1 claimed_vite=$2 offset api vite
  for offset in $(seq 0 99); do
    api=$((8787 + offset)) vite=$((5173 + offset))
    case "$claimed_api" in *" $api "*) continue;; esac
    case "$claimed_vite" in *" $vite "*) continue;; esac
    if _dp_free "$api" && _dp_free "$vite"; then break; fi
  done
  printf '%s' "$offset"
}

# claim_dev_ports <repo-root> <env-file>
claim_dev_ports() {
  local root=$1 env_file=$2
  local mine_api mine_vite claimed_api claimed_vite offset api vite verb
  mine_api="$(sed -n 's/^API_PORT=//p' "$env_file" 2>/dev/null | tail -1)"
  mine_vite="$(sed -n 's/^VITE_PORT=//p' "$env_file" 2>/dev/null | tail -1)"
  claimed_api="$(_dp_claimed_by_others "$root" "$env_file" API_PORT)"
  claimed_vite="$(_dp_claimed_by_others "$root" "$env_file" VITE_PORT)"

  verb="appended to"
  if [ -n "$mine_api" ]; then
    # Ports already named. Keep them unless another worktree names one of the
    # same numbers, which is what an inherited .env looks like. The primary
    # keeps its own either way.
    case " $(_dp_realpath "$root") " in
      " $(_dp_realpath "$(_dp_primary "$root")") ") return 0;;
    esac
    case "$claimed_api" in *" $mine_api "*) ;; *)
      case "$claimed_vite" in *" $mine_vite "*) ;; *) return 0;; esac;;
    esac
    verb="moved off $mine_api/$mine_vite in"
    _dp_strip "$env_file"
  fi

  offset="$(_dp_pick "$claimed_api" "$claimed_vite")"
  api=$((8787 + offset)) vite=$((5173 + offset))
  { echo "# Per-worktree dev ports — appended by setup.sh; edit to override."
    echo "API_PORT=$api"
    echo "VITE_PORT=$vite"; } >> "$env_file"
  echo "→ ports    API $api / Vite $vite  ($verb $env_file)"
}
