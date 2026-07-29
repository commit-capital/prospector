#!/usr/bin/env bash
# Fail-closed isolation assertion. Runs INSIDE the sandbox before any PR code.
# Exit 0 only if the network is provably isolated. Any reachable egress path
# aborts the run. This is the enforcement the spec requires live in code, not
# in an instruction an agent can ignore.
set -uo pipefail

reachable_tcp() { timeout 4 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }
resolves()      { timeout 4 getent hosts "$1" >/dev/null 2>&1; }

problems=0
note() { echo "boot-probe: $1"; }

if reachable_tcp 1.1.1.1 443;            then note "REACHED public IP 1.1.1.1:443";        problems=1; fi
if resolves github.com;                  then note "RESOLVED public DNS github.com";       problems=1; fi

# Host services that MUST be unreachable from an isolated sandbox, as
# comma-separated host:port entries. PROBE_DENY comes from the launcher
# (host-authored — TRIAGE_VERIFY_PROBE_DENY names this machine's sensitive
# services). The default covers the Colima/Lima gateway (192.168.5.2), which
# the spike proved bridges host loopback services (a local server holding real
# Claude creds on 3100, ollama on 11434) to non-internal containers; on a
# machine without those services the checks pass vacuously.
DENY="${PROBE_DENY:-192.168.5.2:3100,192.168.5.2:11434,host.docker.internal:3100,host.docker.internal:11434}"
IFS=',' read -ra deny_entries <<< "$DENY"
for entry in "${deny_entries[@]}"; do
  host="${entry%:*}"; port="${entry##*:}"
  [ -n "$host" ] && [ -n "$port" ] || continue
  if reachable_tcp "$host" "$port"; then note "REACHED host $host:$port"; problems=1; fi
done

if [ "$problems" -ne 0 ]; then
  note "ISOLATION CHECK FAILED — refusing to run PR code."
  exit 1
fi
note "isolation OK"
exit 0
