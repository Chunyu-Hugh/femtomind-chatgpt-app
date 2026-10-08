#!/usr/bin/env python3
"""Launch, cancel or check YOUR jam-gateway on the LQCDMCP platform.

Each person runs their own gateway under their own account. It registers as
jam-gateway-<JAM_GW_USER> (JAM_GW_NAME overrides), so its URL is
https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<user>/mcp.

Usage:
  export LQCDMCP_TOKEN=...      # dashboard Bearer token from the LQCDMCP UI
  export JAM_GW_USER=<your JLab username>
  python3 redeploy_jam.py launch | cancel | status
  python3 redeploy_jam.py script     # write the job script, to paste into the dashboard page

Optional: JAM_GW_RESERVATION (default chen_mcp_daily, "" for none),
JAM_GW_PARTITION (default 24s), JAM_GW_HOURS (default 6).
"""
import base64
import json
import os
import re
import ssl
import sys
import time
import urllib.request
import zlib

import gw_auth

T = os.environ.get("LQCDMCP_TOKEN", "")
if not T and (sys.argv[1:2] or [""])[0] != "script":
    raise SystemExit("set LQCDMCP_TOKEN (dashboard Bearer token from the LQCDMCP UI)")
B = "https://lqcdmcp.jlab.org:8000/jlab/"
USER = gw_auth.USER
if not USER:
    raise SystemExit("set JAM_GW_USER to your JLab username")
MCP_NAME = gw_auth.NAME
RESERVATION = os.environ.get("JAM_GW_RESERVATION", "chen_mcp_daily").strip()
PARTITION = os.environ.get("JAM_GW_PARTITION", "24s").strip()
HOURS = int(os.environ.get("JAM_GW_HOURS", "6"))
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


HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY_SRC = os.path.join(HERE, "jam_gateway.py")
PANEL_SRC = os.path.join(HERE, "jam_panel.html")
FILES_SRC = os.path.join(HERE, "workspace_files")
JAMAI_SRC = os.path.join(HERE, "jamai")          # the campaign materializer and replica runner
# Where the job unpacks the gateway, in the launching user's own home, so
# several people's gateways on one node never share files.
APP_DIR = "~/.jam_gateway/app"


def _payload():
    """{relative path: base64} of everything the gateway needs on the node."""
    files = {"jam_gateway.py": GATEWAY_SRC, "jam_panel.html": PANEL_SRC}
    for src, dest in ((FILES_SRC, "workspace_files"), (JAMAI_SRC, "jamai")):
        for dirpath, _, names in os.walk(src):
            for n in sorted(names):
                if n.endswith(".pyc"):
                    continue
                full = os.path.join(dirpath, n)
                files[dest + "/" + os.path.relpath(full, src).replace(os.sep, "/")] = full
    # zlib first: the job script travels through the dashboard API, and the
    # gateway, panel and packages are mostly source text.
    return {k: base64.b64encode(zlib.compress(open(v, "rb").read(), 9)).decode()
            for k, v in files.items()}


def build_slurm():
    """Job script that boots the CURRENT gateway with its panel, workspace files
    and campaign package, under the launching user's account. It carries no
    credential: the gateway checks callers' dashboard tokens with the dashboard."""
    sbatch = [f"#SBATCH --partition={PARTITION}",
              f"#SBATCH --time={HOURS:02d}:00:00",
              "#SBATCH --nodes=1", "#SBATCH --ntasks=1", "#SBATCH --cpus-per-task=2",
              f"#SBATCH --job-name={MCP_NAME}"]
    if RESERVATION:
        sbatch.insert(2, f"#SBATCH --reservation={RESERVATION}")
    return ("#!/bin/bash\n" + "\n".join(sbatch) + "\n"
            "python3 - <<'PYDEC' > /dev/null\n"
            "import base64, json, os, shutil, zlib\n"
            "d = os.path.expanduser('~/.jam_gateway')\n"
            "os.makedirs(d, mode=0o700, exist_ok=True); os.chmod(d, 0o700)\n"
            f"app = os.path.expanduser({APP_DIR!r})\n"
            "for sub in ('workspace_files', 'jamai'): shutil.rmtree(os.path.join(app, sub), ignore_errors=True)\n"
            f"for rel, b in json.loads({json.dumps(json.dumps(_payload()))}).items():\n"
            "    os.makedirs(os.path.dirname(os.path.join(app, rel)), exist_ok=True)\n"
            "    open(os.path.join(app, rel), 'wb').write(zlib.decompress(base64.b64decode(b)))\n"
            "PYDEC\n"
            f"cd {APP_DIR}\n"
            f"export JAM_GW_NAME={MCP_NAME}\n"
            "# supervisor: keep the JOB alive for the whole window; the gateway\n"
            "# may crash or be hot-redeployed inside it while the setsid'd\n"
            "# ControlMasters (in ~/fm-cm) persist and get reused without OTP\n"
            "while true; do\n"
            "  python3 jam_gateway.py || echo 'jam-gateway exited rc='$?\n"
            "  sleep 3\n"
            "done\n")


def main():
    if (sys.argv[1:2] or [""])[0] == "script":
        # For the dashboard page: paste this file's content as the submission
        # script and use the same MCP name.
        path = f"{MCP_NAME}.slurm"
        with open(path, "w") as fh:
            fh.write(build_slurm())
        print(f"wrote {path} ({os.path.getsize(path)} bytes). In the dashboard, launch an MCP "
              f"server named {MCP_NAME} with this file as its submission script.\n"
              f"Your gateway will be at {gw_auth.URL}")
        return
    call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "redeploy-jam", "version": "1"}})
    print("validate:", tool_call("validate_user", {"username": USER})[:150])
    time.sleep(1)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "cancel":
        print(tool_call("cancel_mcp_server_by_name", {"name": MCP_NAME}))
    elif cmd == "launch":
        print(tool_call("launch_mcp_server_using_script",
                        {"mcp_name": MCP_NAME, "wait": False,
                         "submission_script": build_slurm(), "base64_content": False},
                        timeout=240))
        print(f"\nyour gateway: {gw_auth.URL}\npanel:        {gw_auth.URL}?ui=panel")
    else:
        print(tool_call("check_mcp_server_status_by_name", {"mcp_name": MCP_NAME}))


if __name__ == "__main__":
    main()
