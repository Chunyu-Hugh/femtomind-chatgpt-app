#!/usr/bin/env python3
"""Poll the running JAM fit through the gateway until it finishes or 3h pass.

Usage: python3 fit_poller.py <outdir> [interval_sec]
Writes one compact line per poll to fit_poll.log.
"""
import json
import sys
import time
import urllib.request

URL = "https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp"
OUTDIR = sys.argv[1] if len(sys.argv) > 1 else "001/step7_agent"
IVAL = int(sys.argv[2]) if len(sys.argv) > 2 else 300
MAX_POLLS = 40  # ~3.3h at 5min

LOG = open("/root/femtomind/lqcdmcp-deploy/fit_poll.log", "a", buffering=1)


def call(name, args):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}).encode()
    req = urllib.request.Request(URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode())
    return json.loads(d["result"]["content"][0]["text"])


last = ""
for i in range(MAX_POLLS):
    try:
        r = call("get_jam_result", {"outdir": OUTDIR})
        q = r.get("out", "")
        running = " R " in q or "jamfit" in q
        line = f"[{time.strftime('%H:%M:%S')}] poll {i}: running={running} | {q[-600:].replace(chr(10),' / ')}"
        LOG.write(line + "\n")
        print(line)
        if not running and i >= 2:
            LOG.write("JOB NO LONGER IN QUEUE — finishing\n")
            break
    except Exception as e:
        LOG.write(f"[{time.strftime('%H:%M:%S')}] poll {i} ERROR {e}\n")
        print("poll error:", e)
    time.sleep(IVAL)
LOG.close()
print("poller done")
