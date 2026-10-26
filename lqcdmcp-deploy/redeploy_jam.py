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
  python3 redeploy_jam.py token [--show]            # your gateway token (what clients send)
  python3 redeploy_jam.py user-token add|revoke|list <name>   # shared gateway: one token per person
  python3 redeploy_jam.py rotate token|admin        # new secret; relaunch for it to take effect

The gateway accepts its own token, not the dashboard token: the job script
carries only the sha256 of the secrets in ~/.config/jam_gateway (mode 600).

Optional: JAM_GW_RESERVATION (default chen_mcp_daily; "" for none),
JAM_GW_SHARED=1 (the gateway then acts as the user named in each call: see
the guide's "Shared gateway"), JAM_GW_PARTITION (default none: the reservation's
partition, mcp), JAM_GW_HOURS (default 6), JAM_GW_CPUS (default 16), JAM_GW_MEM
(e.g. 16G; default: the partition's), JAM_GW_ACCOUNT (the Slurm account, default
hadstruc24s as agreed with Jie Chen; "none" for your default account).
"""
import base64
import http.client
import importlib.util
import json
import os
import re
import socket
import ssl
import secrets
import sys
import time
import urllib.error
import urllib.request
import zlib

import gw_auth
from jamai import analysis_catalog, analysis_results

LOCAL_CMDS = ("script", "token", "user-token", "rotate")     # need no dashboard token
T = gw_auth.dashboard_token()
if not T and (sys.argv[1:2] or [""])[0] not in LOCAL_CMDS:
    raise SystemExit("set LQCDMCP_TOKEN (dashboard Bearer token from the LQCDMCP UI)")
B = "https://lqcdmcp.jlab.org:8000/jlab/"
USER = gw_auth.USER
if not USER:
    raise SystemExit("set JAM_GW_USER to your JLab username")
MCP_NAME = gw_auth.NAME
# qcd24s100 is in partition mcp (since 2026-10-22) and wholly held by the
# reservation chen_mcp_daily: a job without the reservation never starts there.
# Without a partition line the job goes to the reservation's partition, as the
# header Trey Anderson and Chunyu Hu tested on 2026-10-23.
RESERVATION = os.environ.get("JAM_GW_RESERVATION", "chen_mcp_daily").strip()
PARTITION = os.environ.get("JAM_GW_PARTITION", "none").strip()
if PARTITION.lower() == "none":
    PARTITION = ""                  # the cluster's default partition
HOURS = int(os.environ.get("JAM_GW_HOURS", "6"))
ACCOUNT = os.environ.get("JAM_GW_ACCOUNT", "hadstruc24s").strip()
if ACCOUNT.lower() == "none":
    ACCOUNT = ""                    # your default Slurm account
CPUS = int(os.environ.get("JAM_GW_CPUS", "16"))
MEM = os.environ.get("JAM_GW_MEM", "").strip()      # e.g. 16G; empty: the partition's default
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


def plugin_files():
    """{relative path: local path} of the plugins' files (plugins/__init__.py
    says which); a plugin that cannot be shipped is left out with a note."""
    spec = importlib.util.spec_from_file_location("jam_plugins", os.path.join(HERE, "plugins", "__init__.py"))
    plugins = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugins)
    files, skipped = plugins.ship_files()
    for name, why in skipped.items():
        print(f"note: plugin {name} is left out of the gateway: {why}", file=sys.stderr)
    return files


def _payload():
    """{relative path: base64} of everything the gateway needs on the node."""
    files = {"jam_gateway.py": GATEWAY_SRC, "jam_panel.html": PANEL_SRC}
    for dirpath, _, names in os.walk(JAMAI_SRC):
        for n in sorted(names):
            if n.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, n)
            files["jamai/" + os.path.relpath(full, JAMAI_SRC).replace(os.sep, "/")] = full
    files.update(plugin_files())
    files.update(analysis_catalog.Catalog(analysis_catalog.catalog_root(HERE)).deployment_files())
    files.update(analysis_results.Results(analysis_results.results_root(HERE)).deployment_files())
    # zlib first: the job script travels through the dashboard API, and the
    # gateway, panel and package are mostly source text.
    return {k: base64.b64encode(zlib.compress(open(v, "rb").read(), 9)).decode()
            for k, v in files.items()}


def _auth_exports():
    """The gateway's token and admin hashes for the job script. The secrets
    themselves stay in gw_auth.SECRETS_DIR (mode 600) and never travel; the
    gateway compares sha256(bearer) with the hash and never asks the dashboard."""
    return (f"export JAM_GW_TOKEN_SHA256={gw_auth.sha256(gw_auth.ensure_secret('token'))}\n"
            f"export JAM_GW_ADMIN_SHA256={gw_auth.sha256(gw_auth.ensure_secret('admin'))}\n"
            "export JAM_GW_DASHBOARD_AUTH=0\n")


def build_slurm():
    """Job script that boots the CURRENT gateway with its panel and campaign
    package, under the launching user's account. It carries no credential, only
    the sha256 of the gateway's token and admin secret (and, for a shared
    gateway, of each person's token), which the gateway compares bearers with."""
    # No --nodes=1: Jie Chen (2026-10-22) pointed to it as what kept gateways
    # from sharing the mcp node; --ntasks=1 already keeps the job on one node.
    sbatch = [f"#SBATCH --time={HOURS:02d}:00:00",
              "#SBATCH --ntasks=1", f"#SBATCH --cpus-per-task={CPUS}",
              f"#SBATCH --job-name={MCP_NAME}"]
    if PARTITION:
        sbatch.insert(0, f"#SBATCH --partition={PARTITION}")
    if RESERVATION:
        sbatch.insert(2, f"#SBATCH --reservation={RESERVATION}")
    if ACCOUNT:
        sbatch.append(f"#SBATCH --account={ACCOUNT}")
    if MEM:
        sbatch.append(f"#SBATCH --mem={MEM}")
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
            # A shared gateway's per-person token hashes go into site.json "users".
            f"users = json.loads({json.dumps(json.dumps(gw_auth.load_users() if SHARED else {}))})\n"
            "if users:\n"
            "    sp = os.path.join(d, 'site.json')\n"
            "    try:\n"
            "        site = json.load(open(sp))\n"
            "    except Exception:\n"
            "        site = {}\n"
            "    site = site if isinstance(site, dict) else {}\n"
            "    site['users'] = users\n"
            "    fd = os.open(sp + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n"
            "    with os.fdopen(fd, 'w') as fh:\n"
            "        json.dump(site, fh, indent=1)\n"
            "    os.replace(sp + '.tmp', sp)\n"
            "PYDEC\n"
            f"cd {APP_DIR}\n"
            f"export JAM_GW_NAME={MCP_NAME}\n{'export JAM_GW_SHARED=1' + chr(10) if SHARED else ''}"
            + _auth_exports() +
            "# supervisor: keep the JOB alive for the whole window; the gateway\n"
            "# may crash or be hot-redeployed inside it while the setsid'd\n"
            "# ControlMasters (in ~/fm-cm) persist and get reused without OTP\n"
            "while true; do\n"
            "  python3 jam_gateway.py || echo 'jam-gateway exited rc='$?\n"
            "  sleep 3\n"
            "done\n")


def main():
    cmd0 = (sys.argv[1:2] or [""])[0]
    if cmd0 == "token":
        tok, path = gw_auth.ensure_secret("token"), gw_auth.secret_path("token")
        print(f"gateway token of {MCP_NAME}: {path} (mode 600; sha256 {gw_auth.sha256(tok)[:12]}...)\n"
              f"  export JAM_GW_TOKEN=\"$(cat {path})\"\n"
              + (f"  {tok}" if "--show" in sys.argv else "(--show prints the token itself)"))
        return
    if cmd0 == "user-token":
        sub, name = (sys.argv[2:3] or [""])[0], (sys.argv[3:4] or [""])[0].strip().lower()
        users = gw_auth.load_users()
        if sub == "list":
            print("\n".join(sorted(users)) or "(no user tokens)")
        elif sub == "add" and re.fullmatch(r"[a-z0-9._-]{1,32}", name):
            tok = secrets.token_urlsafe(32)
            users[name] = gw_auth.sha256(tok)
            gw_auth.save_users(users)
            print(f"token for {name} (hand it over privately; only its sha256 is stored):\n  {tok}\n"
                  "It takes effect at the next launch with JAM_GW_SHARED=1.")
        elif sub == "revoke" and name in users:
            del users[name]
            gw_auth.save_users(users)
            print(f"revoked {name}; it takes effect at the next launch.")
        else:
            raise SystemExit("usage: redeploy_jam.py user-token add|revoke|list <name>")
        return
    if cmd0 == "rotate":
        kind = (sys.argv[2:3] or [""])[0]
        if kind not in ("token", "admin"):
            raise SystemExit("usage: redeploy_jam.py rotate token|admin")
        gw_auth.rotate_secret(kind)
        print(f"new {kind} secret in {gw_auth.secret_path(kind)}. Relaunch the gateway for it to take effect "
              "(python3 redeploy_jam.py cancel, then python3 deploy_and_verify.py).")
        return
    if cmd0 == "script":
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
                     + (f", partition {PARTITION}" if PARTITION else ", the default partition") + f", {HOURS} h"
                     + (f", account {ACCOUNT}" if ACCOUNT else ", your default Slurm account"))
            raise SystemExit(f"the platform did not start your gateway ({asked}): {out[:600]}\n"
                             f"Check with Jie Chen that your JLab account ({USER}) is enabled for MCP "
                             f"servers, is on the user list of the reservation chen_mcp_daily and may use "
                             f"the hadstruc24s Slurm account in partition mcp. The job asks for hadstruc24s "
                             f"unless JAM_GW_ACCOUNT says otherwise. "
                             f"If you set JAM_GW_RESERVATION, JAM_GW_PARTITION, JAM_GW_HOURS, JAM_GW_CPUS or "
                             f"JAM_GW_ACCOUNT yourself, unset them and "
                             f"try again first.")
        print(out)
        print(f"\nyour gateway: {gw_auth.URL}\npanel:        {gw_auth.URL} (in a browser)\n"
              "It runs for JAM_GW_HOURS (6 h) at most. Starting it ends every other gateway on the node "
              "(see the README, \"One node, and its prolog\"). "
              "python3 deploy_and_verify.py --check shows when your gateway is up.")
    else:
        print(tool_call("check_mcp_server_status_by_name", {"mcp_name": MCP_NAME})[0])


if __name__ == "__main__":
    main()
