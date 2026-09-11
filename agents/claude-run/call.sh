#!/bin/bash
# call.sh <tool> <json> -> logs raw response to raw/, appends to WORKLOG.md, prints response
tool="$1"; payload="$2"
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
fname="raw/$(date -u +%Y%m%dT%H%M%SZ)_${tool}.json"
resp=$(curl -s -m 900 -X POST "http://127.0.0.1:8000/$tool" -H 'Content-Type: application/json' -d "$payload")
rc=$?
echo "$resp" > "$fname"
{
  echo ""
  echo "### $ts — \`$tool\` $payload (curl rc=$rc)"
  echo '```json'
  echo "$resp" | python3 -c 'import sys,json;d=sys.stdin.read()
try: print(json.dumps(json.loads(d),indent=2)[:4000])
except Exception: print(d[:4000])'
  echo '```'
} >> WORKLOG.md
echo "$resp" | python3 -c 'import sys,json;d=sys.stdin.read()
try: print(json.dumps(json.loads(d),indent=2))
except Exception: print(d)'
