#!/usr/bin/env python3
"""Launch your gateway on LQCDMCP and check it end to end, in one go.

    export LQCDMCP_TOKEN='...'      # dashboard token (Globus login); never printed
    export JAM_GW_USER=<you>
    python3 deploy_and_verify.py           # launch (if not running), wait, verify
    python3 deploy_and_verify.py --check   # only verify a gateway that is already running

Prints a step-by-step report you can share: nothing in it is a credential.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gw_auth  # noqa: E402

TOKEN = os.environ.get("LQCDMCP_TOKEN", "").strip()
if not TOKEN or not gw_auth.USER:
    sys.exit("set LQCDMCP_TOKEN (dashboard token) and JAM_GW_USER (your JLab username) first")


def say(*a):
    print(*a, flush=True)


def scrub(text):
    return str(text).replace(TOKEN, "***")


def gateway(method, params=None, with_token=True, timeout=60):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if with_token:
        h["Authorization"] = "Bearer " + TOKEN
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    with urllib.request.urlopen(urllib.request.Request(gw_auth.URL, data=json.dumps(body).encode(),
                                                       headers=h), timeout=timeout) as r:
        return json.loads(r.read().decode())


def tool(name, args=None):
    res = gateway("tools/call", {"name": name, "arguments": args or {}}).get("result", {})
    return res.get("structuredContent") or {"text": " ".join(c.get("text", "") for c in res.get("content", []))}


def running():
    try:
        return gateway("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                      "clientInfo": {"name": "verify", "version": "1"}},
                       with_token=False, timeout=20).get("result", {}).get("serverInfo", {})
    except Exception:
        return None


def run(args):
    p = subprocess.run([sys.executable] + args, cwd=HERE, capture_output=True, text=True, timeout=600)
    return scrub((p.stdout + p.stderr).strip())


say(f"gateway: {gw_auth.NAME}\nurl:     {gw_auth.URL}\n")
if "--check" not in sys.argv and not running():
    say("1. launch")
    say("   " + run(["redeploy_jam.py", "launch"]).replace("\n", "\n   ")[-1500:])
    say("   waiting for the gateway to register with the proxy (up to 10 min)…")
    t0 = time.time()
    while time.time() - t0 < 600 and not running():
        time.sleep(15)
else:
    say("1. launch: skipped (already running)" if running() else "1. launch: skipped (--check)")

info = running()
say(f"\n2. reachable through the proxy: {'yes ' + json.dumps(info) if info else 'NO'}")
if not info:
    say("   " + run(["redeploy_jam.py", "status"])[-800:])
    sys.exit("the gateway is not up yet: run again with --check in a few minutes")

say("\n3. the dashboard knows this token's gateways")
say("   " + run(["check_dashboard_token.py"]).replace("\n", "\n   ")[-1500:])

say("\n4. access control")
anon = gateway("tools/call", {"name": "ping", "arguments": {}}, with_token=False)["result"]
say(f"   no token     -> {'refused (good)' if anon.get('isError') else 'ALLOWED (bad)'}")
ok = gateway("tools/call", {"name": "ping", "arguments": {}})["result"]
say(f"   your token   -> {'accepted (good)' if not ok.get('isError') else 'REFUSED: ' + scrub(json.dumps(ok))[:300]}")
if ok.get("isError"):
    sys.exit("stop: the gateway does not accept this token; send this report")

say("\n5. tools on the deployed gateway")
names = [t["name"] for t in gateway("tools/list")["result"]["tools"]]
say(f"   {len(names)} tools; campaign tools present: "
    f"{all(n in names for n in ('campaign_options', 'validate_campaign', 'submit_campaign', 'campaign_pdf_plots', 'draft_slurm_job'))}")
c = tool("check_hpc_connection")
say(f"   cluster chain: {'open' if c.get('connected') else 'not open yet — sign in on the panel Connect tab'}"
    f"  (site {c.get('site', {}).get('label')}, account {c.get('account', {}).get('username')})")
o = tool("campaign_options")
p = (o.get("parameters") or {}).get("pdf") or {}
say(f"   composer: {len(p.get('rows', []))} PDF parameters ({p.get('free')} free / {p.get('fixed')} fixed), "
    f"reactions available: {[r['key'] for r in o.get('reactions', []) if r.get('enabled')]}")
if c.get("connected"):
    pr = tool("check_project")
    say(f"   workspace: {pr.get('workspace')}  ok={pr.get('ok')}")
    lc = tool("list_campaigns")
    say(f"   campaigns: {lc.get('count')}")
say(f"\n6. panel: {gw_auth.URL}?ui=panel  (paste the dashboard token there once)")
say("\nDONE")
