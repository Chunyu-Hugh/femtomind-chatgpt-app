#!/usr/bin/env python3
"""Update a running gateway's code without restarting its Slurm job.

    export LQCDMCP_TOKEN='...'   JAM_GW_USER=<you>
    python3 hot_redeploy.py [gateway_file] [panel_file]
    (defaults: jam_gateway.py and jam_panel.html next to this script)

The job's supervisor loop restarts the server from ~/.jam_gateway/app, and the
cluster sign-in (the ControlMasters in ~/fm-cm) outlives the restart, so a
code swap needs no new one-time code and no new job slot.

Uploads go through node_diag, which answers only to the gateway's owner; the
gateway recognises its owner's dashboard token (LQCDMCP_TOKEN).
"""
import base64
import json
import os
import sys
import time
import urllib.request

import gw_auth

HERE = os.path.dirname(os.path.abspath(__file__))
URL = gw_auth.URL
LOCAL = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "jam_gateway.py")
PANEL = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "jam_panel.html")
APP = "~/.jam_gateway/app"
# One node_diag call carries one chunk as a single shell argument; Linux caps a
# single argument at 128 KiB, so stay well under it.
CHUNK = 60000


def post(body, timeout=90, with_token=True):
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream",
                                          **(gw_auth.auth_headers() if with_token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def diag(cmd, rid, timeout=90):
    res = post({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                "params": {"name": "node_diag", "arguments": {"cmd": cmd},
                           "_meta": {"jam_gateway_token": gw_auth.token()}}}, timeout).get("result", {})
    text = " ".join(c.get("text", "") for c in res.get("content", []))
    if res.get("isError") and "unknown tool" in text:
        sys.exit("the running gateway refused node_diag: it does not recognise this token as its "
                 "owner's. Use the dashboard token of the account that launched it (LQCDMCP_TOKEN); "
                 "if the login expired, log in to the dashboard again.")
    return res.get("structuredContent") or {"rc": None, "out": text, "err": ""}


def upload(path, dest, rid0):
    """Write path to dest.new on the node, in base64 chunks."""
    data = base64.b64encode(open(path, "rb").read()).decode()
    chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)]
    print(f"uploading {os.path.relpath(path, HERE)}: {len(chunks)} chunk(s)")
    for i, c in enumerate(chunks):
        r = diag(f"mkdir -p $(dirname {dest}) && echo {c} {'>' if i == 0 else '>>'} {dest}.b64", rid0 + i)
        if r.get("rc") != 0:
            sys.exit(f"chunk {i} of {path} FAILED: {str(r)[:300]}")
    r = diag(f"base64 -d {dest}.b64 > {dest}.new && rm -f {dest}.b64 && echo DECODE-OK", rid0 + len(chunks))
    if "DECODE-OK" not in (r.get("out") or ""):
        sys.exit(f"decoding {path} on the node FAILED: {str(r)[:300]}")


def tool_names(with_token):
    res = post({"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}},
               timeout=30, with_token=with_token)
    return [t["name"] for t in res.get("result", {}).get("tools", [])]


def main():
    if not gw_auth.token():
        sys.exit("no token: export LQCDMCP_TOKEN (your dashboard token)")

    # The panel is read from disk on every request: no restart needed.
    upload(PANEL, f"{APP}/jam_panel.html", 100)
    if "OK" not in (diag(f"mv {APP}/jam_panel.html.new {APP}/jam_panel.html && echo OK", 199).get("out") or ""):
        sys.exit("installing the panel FAILED")

    # What new workspaces receive, and the campaign package.
    files = []
    for top in ("workspace_files", "jamai"):
        src = os.path.join(HERE, top)
        files += sorted(f"{top}/" + os.path.relpath(os.path.join(d, n), src)
                        for d, _, ns in os.walk(src) for n in ns if not n.endswith(".pyc"))
    diag(f"rm -rf {APP}/workspace_files {APP}/jamai", 3)
    for i, rel in enumerate(files):
        upload(os.path.join(HERE, rel), f"{APP}/{rel}", 400 + 50 * i)
        diag(f"mv {APP}/{rel}.new {APP}/{rel}", 449 + 50 * i)
    print(f"panel, workspace files and campaign package installed ({len(files)} files)")

    upload(LOCAL, f"{APP}/jam_gateway_new.py", 200)
    r = diag(f"mv {APP}/jam_gateway_new.py.new {APP}/jam_gateway_new.py && "
             f"python3 -m py_compile {APP}/jam_gateway_new.py && echo COMPILE-OK", 299)
    if "COMPILE-OK" not in (r.get("out") or ""):
        sys.exit(f"compile FAILED: {str(r)[:400]}")
    print("compiled on the node; restarting the server…")
    try:
        # [y] keeps pkill from matching this very command line.
        diag(f"cp {APP}/jam_gateway_new.py {APP}/jam_gateway.py; "
             "pkill -u $(id -u) -f 'python3 jam_gatewa[y].py'; echo SWAPPED", 300, timeout=20)
    except Exception as e:
        print("restart call interrupted as expected:", type(e).__name__)
    time.sleep(10)

    owner_view, public_view = tool_names(True), tool_names(False)
    print(f"tools after redeploy: {len(owner_view)} for the owner, {len(public_view)} without a token")
    if "open_panel" not in owner_view:
        print("WARNING: open_panel is missing — the new gateway does not seem to be running")


if __name__ == "__main__":
    main()
