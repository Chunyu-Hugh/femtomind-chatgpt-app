#!/usr/bin/env python3
"""Launch your gateway on LQCDMCP and check it end to end, in one go.

    export LQCDMCP_TOKEN='...'      # dashboard token (Globus login); never printed
    export JAM_GW_USER=<you>
    python3 deploy_and_verify.py           # launch (if not running or queued), wait, verify
    python3 deploy_and_verify.py --check   # only verify a gateway that is already running

Prints a step-by-step report you can share: nothing in it is a credential.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gw_auth  # noqa: E402

TOKEN = gw_auth.dashboard_token()
if not TOKEN or not gw_auth.USER:
    sys.exit("set LQCDMCP_TOKEN (dashboard token) and JAM_GW_USER (your JLab username) first")
# Slurm states of a gateway job that is queued or starting: launching again
# would only queue a second job.
ACTIVE = ("PENDING", "CONFIGURING", "RUNNING")
TOKEN_HELP = ("If they show HTTP 401 or 403, the dashboard did not accept your token: sign in to the dashboard "
              "again, copy the new token, export LQCDMCP_TOKEN and run this again. If they show URLError or "
              "'timed out', or say the dashboard could not be reached or did not answer, check the network "
              "connection and try again.")


def say(*a):
    print(*a, flush=True)


def scrub(text):
    text = str(text)
    for tok in {TOKEN, gw_auth.token()}:
        text = text.replace(tok, "***")
    return text


def gateway(method, params=None, with_token=True, timeout=60):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if with_token:
        h.update(gw_auth.auth_headers())
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


def run(args, limit=700):
    """Run one of the other scripts: (exit code, its output without the token).
    The limit is above redeploy_jam.py's worst case (180 + 180 + 1 + 240 s)."""
    try:
        p = subprocess.run([sys.executable] + args, cwd=HERE, capture_output=True, text=True, timeout=limit)
    except subprocess.TimeoutExpired:
        return 1, f"{args[0]} did not finish in {limit} s: the dashboard did not answer"
    return p.returncode, scrub((p.stdout + p.stderr).strip())


def job_state():
    """Your gateway job's Slurm state as the dashboard reports it ('' when it
    names none), and the report itself."""
    rc, status = run(["redeploy_jam.py", "status"])
    if rc:
        say("   " + status.replace("\n", "\n   ")[-800:])
        sys.exit("stop: asking the dashboard about your gateway job failed (the lines above). " + TOKEN_HELP)
    m = re.search(r"""slurm_job_state["']?\s*[:=]\s*["']?(\w*)""", status)
    return (m.group(1) if m else next((s for s in ACTIVE if s in status), "")), status


def queued():
    """Stop on a gateway job that waits in the queue: it may wait for hours, so
    waiting here would not help."""
    say("   your gateway job is waiting in the queue: gateways run one at a time and only in the daily"
        " window (about 10:00–16:00 US Eastern), so it waits for a colleague's gateway to stop or for"
        " the window to open, then starts by itself.")
    sys.exit("the gateway is not up yet: run python3 deploy_and_verify.py --check again later")


say(f"gateway: {gw_auth.NAME}\nurl:     {gw_auth.URL}\n")
launched = False
up = running()
if "--check" not in sys.argv and not up:
    state, _ = job_state()
    if state in ACTIVE:
        say(f"1. launch: skipped (your gateway job is already {state})")
        if state == "PENDING":
            queued()
    else:
        say("1. launch")
        rc, out = run(["redeploy_jam.py", "launch"])
        say("   " + out.replace("\n", "\n   ")[-1500:])
        if rc and "the platform did not start your gateway" not in out:
            # The request itself failed (expired token, network). After a timeout or a
            # dropped connection the platform may still have queued the job.
            sys.exit("stop: the launch request failed (the lines above). If it timed out or the connection "
                     "dropped, a job may still have been queued: run python3 deploy_and_verify.py --check "
                     "before launching again. " + TOKEN_HELP)
        if re.search(r"^validate: \{\}\s*$", out, re.M):
            say(f"   note: the dashboard has no MCP record for {gw_auth.USER} (validate_user answered {{}})")
        # redeploy_jam.py's line when the platform refused the launch, with the
        # job settings it asked for and what to do.
        if "the platform did not start your gateway" in out:
            # Nothing was queued, so waiting for the gateway would only waste ten minutes.
            sys.exit("stop: the LQCDMCP platform could not start your gateway, so nothing was queued. "
                     "The lines just above say why and what to do.")
        launched = True
    say("   waiting for the gateway to register with the proxy (up to 10 min)…")
    t0 = asked = time.time()
    while time.time() - t0 < 600 and not running():
        time.sleep(15)
        if launched and time.time() - asked >= 60:
            # About once a minute, ask about the job just launched: one that ended, or
            # still waits in the queue after two minutes, will not register soon.
            asked = time.time()
            state, _ = job_state()
            if state == "PENDING" and asked - t0 >= 120:
                queued()
            if state not in ACTIVE:
                break   # step 2 reports it
else:
    say("1. launch: skipped (already running)" if up else "1. launch: skipped (--check)")

info = running()
say(f"\n2. reachable through the proxy: {'yes ' + json.dumps(info) if info else 'NO'}")
if not info:
    state, status = job_state()
    say("   " + status[-800:])
    if state == "PENDING":
        queued()
    if state in ACTIVE:
        sys.exit("the gateway is not up yet: its job runs but the gateway does not answer. Run again with"
                 " --check in a few minutes; if it still does not answer, python3 redeploy_jam.py cancel,"
                 " then python3 deploy_and_verify.py.")
    if launched:
        sys.exit(f"the gateway is not up yet, and the job launched above is no longer queued or running"
                 f" (state {state or 'unknown'}). Gateway jobs end when the daily window closes (about 16:00"
                 f" US Eastern); before that, send this report.")
    sys.exit("the gateway is not up yet: you have no gateway job queued or running. Start one with"
             " python3 deploy_and_verify.py (without --check).")

say("\n3. the dashboard knows this token's gateways")
say("   " + run(["check_dashboard_token.py"])[1].replace("\n", "\n   ")[-1500:])

say("\n4. access control")
anon = gateway("tools/call", {"name": "ping", "arguments": {}}, with_token=False)["result"]
say(f"   no token     -> {'refused (good)' if anon.get('isError') else 'ALLOWED (bad)'}")
if not anon.get("isError"):
    sys.exit("stop: the gateway answers without a token, so anyone could use your cluster session: "
             "stop it now with python3 redeploy_jam.py cancel, and send this report.")
ok = gateway("tools/call", {"name": "ping", "arguments": {}})["result"]
say(f"   your token   -> {'accepted (good)' if not ok.get('isError') else 'REFUSED: ' + scrub(json.dumps(ok))[:300]}")
if ok.get("isError"):
    sys.exit("stop: the gateway does not accept this token. Most likely it expired: sign in to the dashboard "
             "again (https://lqcdmcp.jlab.org:8000/jlab/lqcd/mcp/dashboard/), export the new LQCDMCP_TOKEN "
             "and run this again. Step 3 above shows what the dashboard says about the token.")

say("\n5. tools on the deployed gateway")
names = [t["name"] for t in gateway("tools/list")["result"]["tools"]]
missing = [n for n in ("campaign_options", "validate_campaign", "submit_campaign", "list_campaigns",
                       "cancel_campaign", "delete_campaign", "campaign_pdf_plots", "campaign_logs", "draft_slurm_job")
           if n not in names]
say(f"   {len(names)} tools; campaign tools present: {'all' if not missing else 'MISSING ' + ', '.join(missing)}")
say(f"   node_diag hidden from AI clients: {'yes (good)' if 'node_diag' not in names else 'NO (bad)'}")
if missing or "node_diag" in names:
    sys.exit("stop: the running gateway is an older version than the code in this folder. Update it with "
             "python3 hot_redeploy.py (keeps the cluster sign-in), then run python3 deploy_and_verify.py "
             "--check again; or start over with python3 redeploy_jam.py cancel, then python3 deploy_and_verify.py.")
c = tool("check_hpc_connection")
if c.get("shared"):
    say(f"   shared gateway: yes (acts as the user named in each call; owner {c.get('owner')})")
say(f"   cluster chain: {'open' if c.get('connected') else 'not open yet — sign in on the panel Connect tab'}"
    f"  (site {c.get('site', {}).get('label')}, account {c.get('account', {}).get('username')})")
if c.get("connected"):
    pr = tool("check_project")
    bad = [i.get("name") for i in pr.get("items") or [] if not i.get("ok")]
    say(f"   workspace: {pr.get('workspace') or pr.get('note')}  ok={pr.get('ok')}"
        + (f"  missing: {', '.join(bad)}" if bad else ""))
    o = tool("campaign_options")
    p = (o.get("parameters") or {}).get("pdf") or {}
    if o.get("status") == "ok":
        src = f" from {p.get('source')}" if p.get("source") else ""
        say(f"   composer: {len(p.get('rows', []))} PDF parameters ({p.get('free')} free / {p.get('fixed')} fixed)"
            f"{src}, reactions available: {[r['key'] for r in o.get('reactions', []) if r.get('enabled')]}")
    else:
        say("   composer: " + scrub(o.get("message") or o.get("text") or json.dumps(o))[:300])
    lc = tool("list_campaigns")
    say(f"   campaigns: {lc.get('count')}"
        + (f"; your jobs in the Slurm queue: {len(lc['queue'])}" if isinstance(lc.get("queue"), list) else ""))
say(f"\n6. panel: {gw_auth.URL}  (open it in a browser; it asks for the dashboard token)")
say("\nDONE")
