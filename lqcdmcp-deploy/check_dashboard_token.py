#!/usr/bin/env python3
"""Check that your LQCDMCP dashboard token can open your gateway.

    export LQCDMCP_TOKEN=...        # from the dashboard login
    export JAM_GW_USER=<you>
    python3 check_dashboard_token.py

The gateway accepts a dashboard token when the dashboard lists this gateway
(jam-gateway-<you>) among the token owner's MCP servers. This script makes the
same calls and prints what the dashboard answers. The token is never printed.
"""
import json
import os
import re
import sys
import urllib.request

import gw_auth

TOKEN = os.environ.get("LQCDMCP_TOKEN", "").strip()
if not TOKEN:
    sys.exit("set LQCDMCP_TOKEN first")
URL = "https://lqcdmcp.jlab.org:8000/jlab/"


def call(method, params, sid=None):
    h = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if sid:
        h["Mcp-Session-Id"] = sid
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    with urllib.request.urlopen(urllib.request.Request(URL, data=body, headers=h), timeout=30) as r:
        sid = r.headers.get("Mcp-Session-Id") or sid
        raw = r.read().decode(errors="replace")
    ev = (re.findall(r"^data: (.*)$", raw, re.M) or [raw])[-1]
    return json.loads(ev), sid


def text(resp):
    res = resp.get("result") or {}
    return " ".join(c.get("text", "") for c in res.get("content") or []).replace(TOKEN, "***")


_, sid = call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                             "clientInfo": {"name": "check", "version": "1"}})
user = gw_auth.USER or "?"
v, _ = call("tools/call", {"name": "validate_user", "arguments": {"username": user}}, sid)
print("validate_user(%s): %s" % (user, text(v)[:300]))
m, _ = call("tools/call", {"name": "get_my_mcp_servers", "arguments": {"unused": ""}}, sid)
print("get_my_mcp_servers:", text(m)[:1500])
name = gw_auth.NAME
found = re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text(m)) is not None
print(f"\n{name} {'IS' if found else 'is NOT'} in the list -> the gateway "
      f"{'accepts' if found else 'would refuse'} this token"
      + ("" if found else " (is the gateway running? launch it with redeploy_jam.py)"))
