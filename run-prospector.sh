#!/usr/bin/env bash
# Convenience alias: launches the cockpit dev servers via the CLI.
# The launcher itself lives in pipeline/devserve.py (`prospector serve --dev`).
set -euo pipefail
cd "$(dirname "$0")"
exec uv run prospector serve --dev "$@"
