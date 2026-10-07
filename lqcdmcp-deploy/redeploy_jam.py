#!/usr/bin/env python3
"""Cancel + relaunch the jam-gateway MCP server on LQCDMCP (single job slot).

Usage:
  python3 redeploy_jam.py cancel
  python3 redeploy_jam.py launch
  python3 redeploy_jam.py status
"""
import base64
import json
import os
import re
import ssl
import sys
import time
import urllib.request

T = os.environ.get("LQCDMCP_TOKEN", "")
if not T:
    raise SystemExit("set LQCDMCP_TOKEN (dashboard Bearer token from the LQCDMCP UI)")
B = "https://lqcdmcp.jlab.org:8000/jlab/"
MCP_NAME = "jam-gateway"
ctx = ssl.create_default_context()
sid = None
rid = 0


def call(method, params=None, timeout=180):
    global sid, rid
    rid += 1
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    hdrs = {"Authorization": "Bearer " + T, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}
    if sid:
        hdrs["Mcp-Session-Id"] = sid
    req = urllib.request.Request(B, data=json.dumps(body).encode(),
                                 headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        sid = r.headers.get("Mcp-Session-Id") or sid
        raw = r.read().decode()
    evs = re.findall(r"data: (\{.*?\})\n\n", raw, re.S) or re.findall(r"data: (\{.*\})", raw, re.S)
    if evs:
        for e in reversed(evs):
            try:
                return json.loads(e)
            except json.JSONDecodeError:
                pass
    return {"raw": raw[:400]}


def tool_call(name, args, timeout=180):
    r = call("tools/call", {"name": name, "arguments": args}, timeout)
    res = r.get("result", {})
    if isinstance(res, dict) and "content" in res:
        return " ".join(c.get("text", "") for c in res["content"]
                        if isinstance(c, dict) and c.get("type") == "text")[:3000]
    return json.dumps(res)[:3000]


def build_slurm():
    server = open("/root/femtomind/lqcdmcp-deploy/jam_gateway_v4.py").read()
    b64 = base64.b64encode(server.encode()).decode()
    return ("#!/bin/bash\n"
            "#SBATCH --partition=24s\n"
            "#SBATCH --time=06:00:00\n"
            "#SBATCH --reservation=chen_mcp_daily\n"
            "#SBATCH --nodes=1\n"
            "#SBATCH --ntasks=1\n"
            "#SBATCH --cpus-per-task=2\n"
            "#SBATCH --job-name=jam-gateway\n"
            "cd /tmp\n"
            "python3 - <<'PYDEC' > /dev/null\nimport base64\n"
            "open('jam_gateway_v4.py','wb').write(base64.b64decode('%s'))\nPYDEC\n"
            "# supervisor: keep the JOB alive for the full 6h window; the gateway\n"
            "# server may crash/redeploy inside it while the setsid'd\n"
            "# ControlMasters (in ~/fm-cm) persist and get reused without OTP\n"
            "while true; do\n"
            "  python3 jam_gateway_v4.py || echo 'jam-gateway exited rc='$?\n"
            "  sleep 3\n"
            "done\n") % b64


def main():
    call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "redeploy-jam", "version": "1"}})
    print("validate:", tool_call("validate_user", {"username": "chunyu"})[:150])
    time.sleep(1)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "cancel":
        print(tool_call("cancel_mcp_server_by_name", {"name": MCP_NAME}))
    elif cmd == "launch":
        print(tool_call("launch_mcp_server_using_script",
                        {"mcp_name": MCP_NAME, "wait": False,
                         "submission_script": build_slurm(), "base64_content": False},
                        timeout=240))
    else:
        print(tool_call("check_mcp_server_status_by_name", {"mcp_name": MCP_NAME}))


if __name__ == "__main__":
    main()
