#!/bin/bash
# FemtoMind ChatGPT app — start the MCP server (and optionally a public tunnel).
#   ./run.sh          start server on :8010
#   ./run.sh tunnel   also open a cloudflared quick tunnel (public HTTPS URL)
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"   # package root, one level up from scripts/
PY="${FEMTOMIND_PYTHON:-python3}"          # override for a venv: FEMTOMIND_PYTHON=/path/to/venv/bin/python

pkill -f 'femtomind_app.py' 2>/dev/null || true
sleep 1
nohup "$PY" "$DIR/femtomind_app.py" > /tmp/femtomind-chatgpt-app.log 2>&1 &
echo $! > /tmp/femtomind-chatgpt-app.pid
echo "MCP server: http://localhost:8010/mcp  (pid $(cat /tmp/femtomind-chatgpt-app.pid))"

if [ "$1" = "tunnel" ]; then
  command -v cloudflared >/dev/null || { echo "cloudflared not installed"; exit 1; }
  echo "Opening quick tunnel (URL changes on every restart — fine for testing)…"
  cloudflared tunnel --url http://localhost:8010 2>&1 | grep -m1 -oE 'https://[a-z0-9-]+\.trycloudflare\.com' &
fi
