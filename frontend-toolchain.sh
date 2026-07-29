# Shared frontend toolchain resolver — SOURCED by setup.sh and pipeline/devserve.py's
# frontend child, not executed. The Vite frontend's package.json pins Node >=24 and pnpm >=11, so
# a shell whose default Node/pnpm is older fails the engine check. Put an adequate
# Node on PATH and choose a pnpm >=11, so the launcher works regardless of the
# caller's shell defaults. The sourcing script must define FRONTEND first.

# Major version of the `node` currently on PATH, or empty when none resolves.
_fe_node_major() { local v; v="$(command -v node >/dev/null 2>&1 && node -v 2>/dev/null)"; v="${v#v}"; printf '%s' "${v%%.*}"; }

if [ -z "$(_fe_node_major)" ] || [ "$(_fe_node_major)" -lt 24 ]; then
  # Prefer a Homebrew node@25/node, then the newest nvm install >=24.
  for _c in /opt/homebrew/opt/node@25/bin /opt/homebrew/opt/node/bin \
            "$HOME"/.nvm/versions/node/v2[4-9]*/bin "$HOME"/.nvm/versions/node/v[3-9][0-9]*/bin; do
    [ -x "$_c/node" ] || continue
    _m="$("$_c/node" -v 2>/dev/null)"; _m="${_m#v}"; _m="${_m%%.*}"
    if [ -n "$_m" ] && [ "$_m" -ge 24 ]; then PATH="$_c:$PATH"; export PATH; break; fi
  done
fi
if [ -z "$(_fe_node_major)" ] || [ "$(_fe_node_major)" -lt 24 ]; then
  echo "✗ Node >=24 is required for the Vite frontend, but none was found." >&2
  echo "  Install Node 24+ by any means and re-run." >&2
  exit 1
fi

# pnpm >=11, pinned to the frontend's packageManager field. Use an on-PATH pnpm
# when it's new enough; otherwise run the pinned version through npx (no global
# install). PNPM is an array so callers invoke it as "${PNPM[@]}".
_fe_pnpm_ver="$(sed -n 's/.*"packageManager":[[:space:]]*"pnpm@\([0-9.]*\)".*/\1/p' "$FRONTEND/package.json" 2>/dev/null | head -1)"
_fe_pnpm_ver="${_fe_pnpm_ver:-11.5.3}"
if command -v pnpm >/dev/null 2>&1 && [ "$(pnpm -v 2>/dev/null | cut -d. -f1)" -ge 11 ] 2>/dev/null; then
  PNPM=(pnpm)
else
  PNPM=(npx -y "pnpm@$_fe_pnpm_ver")
fi
