#!/usr/bin/env python3
"""Hot-redeploy jam_gateway to the running job WITHOUT killing the Slurm job.

The job's supervisor loop restarts the server from /tmp/jam_gateway.py, and
the setsid'd ControlMasters live in ~/fm-cm, so an in-place code swap keeps
the two-hop chain alive — no new OTP, no new job slot.

Usage: python3 hot_redeploy.py [local_file]
"""
import base64
import json
import sys
import time
import urllib.request

URL = "https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp"
LOCAL = sys.argv[1] if len(sys.argv) > 1 else "/root/femtomind/lqcdmcp-deploy/jam_gateway_v2.py"


def diag(cmd, rid, timeout=90):
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                       "params": {"name": "node_diag", "arguments": {"cmd": cmd}}}).encode()
    req = urllib.request.Request(URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def main():
    src = open(LOCAL).read()
    b64 = base64.b64encode(src.encode()).decode()
    n = (len(b64) + 3) // 4
    chunks = [b64[i:i + n] for i in range(0, len(b64), n)]
    print(f"uploading {len(src)} bytes as {len(chunks)} chunks...")
    for i, c in enumerate(chunks):
        resp = diag(f"echo {c} {'>' if i == 0 else '>>'} /tmp/jam_gateway_v2.b64", i)
        if '\\"rc\\": 0' not in resp:
            print(f"chunk {i} FAILED:", resp[:200])
            sys.exit(1)
    resp = diag("base64 -d /tmp/jam_gateway_v2.b64 > /tmp/jam_gateway_new.py && "
                "python3 -m py_compile /tmp/jam_gateway_new.py && echo COMPILE-OK", 90)
    if "COMPILE-OK" not in resp:
        print("compile FAILED:", resp[:400])
        sys.exit(1)
    print("compiled on node; copying over all script-name variants + restarting...")
    try:
        diag("for f in /tmp/jam_gateway.py /tmp/jam_gateway_v2.py /tmp/jam_gateway_v3.py "
             "/tmp/jam_gateway_v4.py /tmp/jam_gateway_v5.py; do "
             "cp /tmp/jam_gateway_new.py $f; done; "
             "pkill -f 'python3 jam_gateway'; echo SWAPPED", 91, timeout=20)
    except Exception as e:
        print("restart call interrupted as expected:", type(e).__name__)
    time.sleep(10)
    body = json.dumps({"jsonrpc": "2.0", "id": 99, "method": "tools/list",
                       "params": {}}).encode()
    req = urllib.request.Request(URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode())
    tools = [t["name"] for t in resp.get("result", {}).get("tools", [])]
    print("tools after redeploy:", tools)


if __name__ == "__main__":
    main()
