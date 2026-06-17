#!/usr/bin/env bash
# Launch the XEMM live dashboard (macOS / Linux / Git-Bash).
#   ./run.sh
# Stops with Ctrl-C.
set -euo pipefail
cd "$(dirname "$0")"

PORT=$(python -c "import json;print(json.load(open('config.json'))['bind_port'])" 2>/dev/null || echo 8787)
HOST=$(python -c "import json;print(json.load(open('config.json'))['bind_host'])" 2>/dev/null || echo 127.0.0.1)
URL="http://${HOST}:${PORT}"

( sleep 2
  if command -v xdg-open >/dev/null; then xdg-open "$URL"
  elif command -v open >/dev/null; then open "$URL"
  elif command -v start >/dev/null; then start "$URL"; fi ) >/dev/null 2>&1 &

echo "Starting XEMM dashboard at $URL  (Ctrl-C to stop)"
exec python server.py
