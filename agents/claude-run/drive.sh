#!/bin/bash
# Drive orchestrate_jam25 to completion. Stops on complete/diverged/error or 5.5h budget.
cd /root/femtomind/femtomind-chatgpt-app/agents/claude-run
deadline=$(( $(date +%s) + 19800 ))
i=0
while [ $(date +%s) -lt $deadline ]; do
  sleep 300
  i=$((i+1))
  resp=$(./call.sh orchestrate_jam25 '{"seed": 140702, "partition": "priority"}')
  echo "--- iteration $i $(date -u +%H:%M:%SZ) ---"
  echo "$resp"
  if echo "$resp" | grep -q '"complete"'; then echo "DONE: complete"; exit 0; fi
  if echo "$resp" | grep -qi 'diverged'; then echo "DONE: diverged"; exit 2; fi
done
echo "DONE: budget exhausted after $i iterations"; exit 3
