#!/usr/bin/env python3
"""Check that your LQCDMCP dashboard token can open your gateway.

    export LQCDMCP_TOKEN=...        # from the dashboard login
    export JAM_GW_USER=<you>
    python3 check_dashboard_token.py

The gateway accepts a dashboard token when the dashboard confirms the token's
account as the gateway's owner (validate_user) and lists this gateway
(jam-gateway-<you>) with that owner among the token's MCP servers
(get_my_mcp_servers). This script makes the same calls, applies the same rule
and prints the decision. The token is never printed.
"""
import json
import re
import sys
import urllib.error
import urllib.request

import gw_auth

USER = gw_auth.USER
if not USER:
    sys.exit("set JAM_GW_USER to your JLab username")
TOKEN = gw_auth.dashboard_token()
if not TOKEN:
    sys.exit("set LQCDMCP_TOKEN (your dashboard token) first")
URL = "https://lqcdmcp.jlab.org:8000/jlab/"
DASHBOARD_PAGE = "https://lqcdmcp.jlab.org:8000/jlab/lqcd/mcp/dashboard/"


def call(method, params, sid=None):
    h = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if sid:
        h["Mcp-Session-Id"] = sid
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(URL, data=body, headers=h), timeout=30) as r:
            sid = r.headers.get("Mcp-Session-Id") or sid
            raw = r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit(f"the dashboard refused this token (HTTP {e.code}): sign in again at {DASHBOARD_PAGE} "
                     "and export the new LQCDMCP_TOKEN")
        sys.exit(f"the dashboard answered HTTP {e.code}: try again in a few minutes")
    except OSError as e:
        sys.exit(f"cannot reach the dashboard at lqcdmcp.jlab.org:8000 ({getattr(e, 'reason', e)}): "
                 "check your network connection and try again")
    ev = (re.findall(r"^data: (.*)$", raw, re.M) or [raw])[-1]
    return json.loads(ev), sid


def text(resp):
    res = resp.get("result") or {}
    return " ".join(c.get("text", "") for c in res.get("content") or []).replace(TOKEN, "***")


def documents(resp):
    """The JSON a tool result carries: structuredContent and the parsed text parts."""
    res = resp.get("result") or {}
    docs = [] if res.get("structuredContent") is None else [res["structuredContent"]]
    for c in res.get("content") or []:
        try:
            docs.append(json.loads(c.get("text", "")))
        except (AttributeError, ValueError):
            pass
    return docs


def objects(docs):
    """Every JSON object inside docs, outermost first."""
    found, todo = [], list(docs)
    while todo:
        d = todo.pop(0)
        if isinstance(d, dict):
            found.append(d)
            todo += d.values()
        elif isinstance(d, list):
            todo += d
    return found


def refusal(resp, infos):
    """The gateway's rule for validate_user's answer: '' when it lets the token
    through, otherwise why not and what to do. The answer must not be an error,
    MCP must not be refused, and an account it names must be USER (as text)."""
    if (resp.get("result") or {}).get("isError"):
        return ("the dashboard answered with an error (shown above); if it is about the token, sign in "
                f"again at {DASHBOARD_PAGE} and export the new LQCDMCP_TOKEN")
    for info in infos:
        if info.get("allow_mcp") is False:
            return f"MCP servers are not enabled for {USER}: ask Jie Chen to enable them"
        ids = [str(info[k]) for k in ("user_account", "user_id") if k in info]
        if ids and USER not in ids:
            return (f"the dashboard names the account {ids[0]}: check that JAM_GW_USER is your JLab "
                    "username and that the token comes from your own dashboard login")
    return ""


_, sid = call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                             "clientInfo": {"name": "check", "version": "1"}})

v, _ = call("tools/call", {"name": "validate_user", "arguments": {"username": USER}}, sid)
infos = [d for d in objects(documents(v)) if {"allow_mcp", "user_account", "user_id"} & d.keys()]
account = infos[0] if infos else {}
why = refusal(v, infos)
print(f"validate_user({USER}): "
      + (", ".join(f"{k}={account[k]}" for k in ("user_id", "user_account", "allow_mcp") if k in account)
         or text(v)[:300])
      + (" -> NOT confirmed" if why else " -> confirmed" if infos else " -> not refused (it names no account)"))

m, _ = call("tools/call", {"name": "get_my_mcp_servers", "arguments": {"unused": ""}}, sid)
# The gateway takes nothing from an error result, so neither does this check.
listing = [] if (m.get("result") or {}).get("isError") else documents(m)
servers = []
for d in objects(listing):
    if "mcp_name" in d and d not in servers:
        servers.append(d)
print(f"get_my_mcp_servers: {len(servers)} server(s)")
for s in servers:
    print(f"  {s.get('mcp_name')}  owner={s.get('owner', '-')}  job={s.get('slurm_job_id', '-')}"
          f"  state={s.get('slurm_job_state', '-')}")
if not listing:
    print("  listing not recognised:", text(m)[:1500])

name = gw_auth.NAME
mine = [s for s in servers if s.get("mcp_name") == name and ("owner" not in s or s.get("owner") == USER)]
others = sorted({str(s["owner"]) for s in servers if s.get("mcp_name") == name and "owner" in s} - {USER})
if why:
    print(f"\nthe dashboard does not confirm this token as {USER}'s -> the gateway would refuse it: {why}")
elif not mine and others:
    print(f"\n{name} is listed with owner {', '.join(others)}, not {USER} -> the gateway would refuse this token:"
          " check that JAM_GW_USER is your JLab username and that the token comes from your own dashboard login")
elif not mine:
    print(f"\n{name} (owner {USER}) is NOT in the list -> the gateway would refuse this token"
          " (is your gateway running? start it with python3 deploy_and_verify.py)")
else:
    jobs = [str(s["slurm_job_id"]) for s in mine if s.get("slurm_job_id") not in (None, "")]
    print(f"\n{name} (owner {USER}) IS in the list -> the gateway accepts this token"
          + (f" (it also checks that it runs as job {' or '.join(jobs)})" if jobs else ""))
