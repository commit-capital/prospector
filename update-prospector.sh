#!/usr/bin/env bash
# Update a running ./run-prospector.sh without cutting its worker runs short:
# report what is in flight, optionally let it finish (`prospector drain`),
# stop the dev servers, git pull, and start them again in this terminal.
set -euo pipefail
cd "$(dirname "$0")"

port=$(sed -n 's/^API_PORT=//p' .env 2>/dev/null | tail -1 | tr -d "\"'")
port=${port:-8787}
paused=0

resume() {
  if [ "$paused" = 1 ]; then
    uv run prospector drain resume || true
    paused=0
  fi
}

listening() { lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -1; }

# The launcher is the parent of the backend's process group (pipeline/devserve.py),
# and only this checkout's own `prospector serve` counts.
launcher() {
  local pid pgid ppid
  pid=$(listening) || true
  [ -n "$pid" ] || return 0
  pgid=$(ps -o pgid= -p "$pid" | tr -d ' ')
  ppid=$(ps -o ppid= -p "$pgid" | tr -d ' ')
  case "$(ps -o command= -p "$ppid")" in
    *"$PWD/.venv/bin/prospector serve"*) echo "$ppid" ;;
    *) echo "✗ port $port is held by a process this checkout did not start; stop it by hand" >&2
       exit 1 ;;
  esac
}

pid=$(launcher)
if [ -n "$pid" ]; then
  set +e
  uv run prospector drain status
  busy=$?
  set -e
  case "$busy" in 0|3) ;; *) echo "✗ could not read the runs in flight" >&2; exit 1 ;; esac
  if [ "$busy" = 3 ]; then
    printf '\n<enter> let them finish, then update · <delete> update now · q quit: '
    IFS= read -rsn1 key
    echo
    case "$key" in
      "")
        trap 'echo; resume; echo "drain cancelled; prospector keeps running"; exit 130' INT
        uv run prospector drain pause
        paused=1
        uv run prospector drain wait
        trap - INT
        ;;
      $'\x7f'|$'\x08') echo "updating now; the runs in flight are cut short" ;;
      *) echo "nothing changed"; exit 0 ;;
    esac
  fi

  echo "stopping prospector (pid $pid)"
  kill -TERM "$pid"
  for _ in $(seq 30); do
    [ -n "$(listening)" ] || break
    sleep 0.5
  done
  resume
  if [ -n "$(listening)" ]; then
    echo "✗ port $port is still in use after stopping prospector" >&2
    exit 1
  fi
fi

git pull --ff-only || echo "⚠ git pull failed; restarting on the current checkout"
exec ./run-prospector.sh "$@"
