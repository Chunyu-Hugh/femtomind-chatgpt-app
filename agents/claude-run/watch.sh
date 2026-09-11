#!/bin/bash
cd /root/femtomind/femtomind-chatgpt-app/agents/claude-run
prev=""
while true; do
  cur=$(python3 stage.py 2>/dev/null)
  if [ "$cur" != "$prev" ]; then
    echo "$(date -u +%H:%M:%SZ) ladder: $cur"
    prev="$cur"
  fi
  case "$cur" in
    complete*|diverged*|BADRESPONSE*|UNKNOWN*) echo "TERMINAL: $cur"; exit 0;;
  esac
  if ! pgrep -f drive.sh >/dev/null; then
    echo "$(date -u +%H:%M:%SZ) drive.sh exited; last stage: $prev"
    exit 1
  fi
  sleep 60
done
