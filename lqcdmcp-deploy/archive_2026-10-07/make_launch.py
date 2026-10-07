#!/usr/bin/env python3
"""Generate (and optionally submit) the LQCDMCP launch script for the
stage-1 fitpack probe.

Usage:
  python3 make_launch.py                        # print the launch script
  python3 make_launch.py --submit <TOKEN>       # submit via dashboard API
  python3 make_launch.py --show-payload <TOKEN> # print the API payload only

The produced Slurm script is fully self-contained: the probe server code is
embedded base64, decoded to /tmp on the node, and run with the system python3
(no venv, no pip, no network needed). It listens on 8080 and self-registers
with the proxy exactly like Jie's test_user_mcp_server.py.
"""
import base64
import json
import pathlib
import sys
import urllib.request

HERE = pathlib.Path(__file__).parent
PROBE = HERE / "probe_server.py"
MCP_NAME = "fitpack-probe"
PORT = 8080
TIME = "06:00:00"

LAUNCH_TMPL = """#!/bin/bash
#SBATCH --partition=24s
#SBATCH --time={time}
#SBATCH --reservation=chen_mcp_daily
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
set -e
cd /tmp
echo "decoding probe server..."
python3 - <<'PYDEC' > /dev/null
import base64
open("probe_server.py", "wb").write(base64.b64decode({b64!r}))
PYDEC
python3 probe_server.py
"""


def build_launch_script() -> str:
    b64 = base64.b64encode(PROBE.read_bytes()).decode()
    return LAUNCH_TMPL.format(time=TIME, b64=b64)


def submit(token: str) -> dict:
    script = build_launch_script()
    payload = {"mcp_name": MCP_NAME, "custom_script_content": script}
    req = urllib.request.Request(
        "https://lqcdmcp.jlab.org:8000/jlab/lqcd/mcp/dashboard/servers/launch",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode()[:400]}


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--submit" in args:
        token = args[args.index("--submit") + 1]
        print(json.dumps(submit(token), indent=1))
    elif "--show-payload" in args:
        token = args[args.index("--show-payload") + 1]
        print(json.dumps({"mcp_name": MCP_NAME,
                          "custom_script_content": build_launch_script()}))
    else:
        print(build_launch_script())
