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
JAM_GW_SHARED=1 (the gateway then acts as the user named in each call: see
the guide's "Shared gateway"), JAM_GW_PARTITION (default 24s), JAM_GW_HOURS
(default 6), JAM_GW_ACCOUNT (a
Slurm account such as jlablqcd24s, when your default account cannot use the
partition; default: none, i.e. Slurm's default account for you).
"""
import base64
import http.client
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
import zlib

import gw_auth

T = gw_auth.dashboard_token()
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
ACCOUNT = os.environ.get("JAM_GW_ACCOUNT", "").strip()
SHARED = os.environ.get("JAM_GW_SHARED", "").strip() == "1"
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
    # A failed request ends the script with one line that says what to do, not a
    # traceback; deploy_and_verify.py shows that line in its report.
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            sid = r.headers.get("Mcp-Session-Id") or sid
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        answer = f"HTTP {e.code} {e.reason or ''}".strip()
        if e.code in (401, 403):
            raise SystemExit(f"the dashboard refused this token ({answer}): sign in to the dashboard again, "
                             "copy the new token, export LQCDMCP_TOKEN and run this again")
        raise SystemExit(f"the dashboard answered {answer}: try again in a few minutes")
    except urllib.error.URLError as e:
        raise SystemExit(f"could not reach the dashboard: {e.reason}. Check the network connection and "
                         "try again")
    except socket.timeout:
        raise SystemExit(f"the dashboard did not answer within {timeout} s (timed out). The dashboard may "
                         "still carry out the request: run python3 redeploy_jam.py status before you repeat it")
    except (OSError, http.client.HTTPException) as e:
        raise SystemExit(f"the connection to the dashboard dropped before it answered ({type(e).__name__}: "
                         f"{e}). The dashboard may still carry out the request: run python3 redeploy_jam.py "
                         "status before you repeat it")
    # The dashboard answers as an event stream with CRLF line ends, often a log
    # notification first: the answer is the last event that carries a result or
    # an error.
    for e in reversed(re.findall(r"^data: (.*?)\r?$", raw, re.M) or [raw]):
        try:
            d = json.loads(e)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and ("result" in d or "error" in d):
            return d
    return {"raw": raw[:400]}


def tool_call(name, args, timeout=180):
    """(the text of the tool's answer, whether the answer is an error: the
    tool's isError, or a JSON-RPC error from the dashboard)."""
    r = call("tools/call", {"name": name, "arguments": args}, timeout)
    if r.get("error") is not None:
        return f"the dashboard answered with an error: {json.dumps(r['error'])}"[:3000], True
    res = r.get("result", {})
    error = isinstance(res, dict) and bool(res.get("isError"))
    if isinstance(res, dict) and "content" in res:
        return " ".join(c.get("text", "") for c in res["content"]
                        if isinstance(c, dict) and c.get("type") == "text")[:3000], error
    return json.dumps(res)[:3000], error


HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY_SRC = os.path.join(HERE, "jam_gateway.py")
PANEL_SRC = os.path.join(HERE, "jam_panel.html")
JAMAI_SRC = os.path.join(HERE, "jamai")          # the campaign materializer and replica runner
# Where the job unpacks the gateway, in the launching user's own home, so
# several people's gateways on one node never share files. It holds only what
# the job ships, so each launch rebuilds it and nothing from an older version
# stays behind.
APP_DIR = "~/.jam_gateway/app"


def _payload():
    """{relative path: base64} of everything the gateway needs on the node."""
    files = {"jam_gateway.py": GATEWAY_SRC, "jam_panel.html": PANEL_SRC}
    for dirpath, _, names in os.walk(JAMAI_SRC):
        for n in sorted(names):
            if n.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, n)
            files["jamai/" + os.path.relpath(full, JAMAI_SRC).replace(os.sep, "/")] = full
    # zlib first: the job script travels through the dashboard API, and the
    # gateway, panel and package are mostly source text.
    return {k: base64.b64encode(zlib.compress(open(v, "rb").read(), 9)).decode()
            for k, v in files.items()}


def build_slurm():
    """Job script that boots the CURRENT gateway with its panel and campaign
    package, under the launching user's account. It carries no credential: the
    gateway checks callers' dashboard tokens with the dashboard."""
    sbatch = [f"#SBATCH --partition={PARTITION}",
              f"#SBATCH --time={HOURS:02d}:00:00",
              "#SBATCH --nodes=1", "#SBATCH --ntasks=1", "#SBATCH --cpus-per-task=2",
              f"#SBATCH --job-name={MCP_NAME}"]
    if RESERVATION:
        sbatch.insert(2, f"#SBATCH --reservation={RESERVATION}")
    if ACCOUNT:
        sbatch.append(f"#SBATCH --account={ACCOUNT}")
    return ("#!/bin/bash\n" + "\n".join(sbatch) + "\n"
            "python3 - <<'PYDEC' > /dev/null\n"
            "import base64, json, os, shutil, zlib\n"
            "d = os.path.expanduser('~/.jam_gateway')\n"
            "os.makedirs(d, mode=0o700, exist_ok=True); os.chmod(d, 0o700)\n"
            f"app = os.path.expanduser({APP_DIR!r})\n"
            "shutil.rmtree(app, ignore_errors=True)\n"
            f"for rel, b in json.loads({json.dumps(json.dumps(_payload()))}).items():\n"
            "    os.makedirs(os.path.dirname(os.path.join(app, rel)), exist_ok=True)\n"
            "    open(os.path.join(app, rel), 'wb').write(zlib.decompress(base64.b64decode(b)))\n"
            "PYDEC\n"
            f"cd {APP_DIR}\n"
            f"export JAM_GW_NAME={MCP_NAME}\n{'export JAM_GW_SHARED=1' + chr(10) if SHARED else ''}"
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
    print("validate:", tool_call("validate_user", {"username": USER})[0][:150])
    time.sleep(1)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "cancel":
        print(tool_call("cancel_mcp_server_by_name", {"name": MCP_NAME})[0])
    elif cmd == "launch":
        out, refused = tool_call("launch_mcp_server_using_script",
                                 {"mcp_name": MCP_NAME, "wait": False,
                                  "submission_script": build_slurm(), "base64_content": False},
                                 timeout=240)
        if refused or re.search(r"Error calling tool|Slurm command failed", out):
            # An error answer: nothing was queued. deploy_and_verify.py stops on this message.
            asked = ((f"reservation {RESERVATION}" if RESERVATION else "no reservation")
                     + f", partition {PARTITION}, {HOURS} h"
                     + (f", account {ACCOUNT}" if ACCOUNT else ", your default Slurm account"))
            raise SystemExit(f"the platform did not start your gateway ({asked}): {out[:600]}\n"
                             f"Most often Jie Chen has not yet enabled your JLab account ({USER}) for MCP "
                             f"servers and the gateway reservation: ask him, and send him this output. The "
                             f"reservation is required: the proxy reaches a gateway only on its reserved node, "
                             f"so a job without it starts on a node nobody can connect to. Also ask for "
                             f"hadstruc24s as your default Slurm account (the 24s partition does not allow "
                             f"jlablqcd24s), or export JAM_GW_ACCOUNT=hadstruc24s. If you set "
                             f"JAM_GW_RESERVATION, JAM_GW_PARTITION or JAM_GW_HOURS yourself, unset them and "
                             f"try again first.")
        print(out)
        print(f"\nyour gateway: {gw_auth.URL}\npanel:        {gw_auth.URL} (in a browser)\n"
              "The job usually starts within minutes; outside the daily window (about 10:00–16:00 US "
              "Eastern) or while a colleague's gateway holds the reservation, it waits in the queue and "
              "starts by itself. python3 deploy_and_verify.py --check shows when your gateway is up.")
    else:
        print(tool_call("check_mcp_server_status_by_name", {"mcp_name": MCP_NAME})[0])


if __name__ == "__main__":
    main()
