#!/usr/bin/env python3
"""Update a running gateway's code without restarting its Slurm job.

    export JAM_GW_USER=<you>          # JAM_GW_TOKEN and JAM_GW_ADMIN, or their files in ~/.config/jam_gateway
    python3 hot_redeploy.py [gateway_file] [panel_file]
    (defaults: jam_gateway.py and jam_panel.html next to this script)

The job's supervisor loop restarts the server from ~/.jam_gateway/app, and the
cluster sign-in (the ControlMasters in ~/fm-cm) outlives the restart, so a
code swap needs no new one-time code and no new job slot. A gateway that does
not run as a Slurm job (a test gateway) has no such loop, so it is refused.

Everything goes through the gateway's hidden `deploy` tool: fixed operations
(stage a file in chunks, verify the complete staged application in a fresh
interpreter, install, clean, restart, status), never a shell. It needs the
owner's gateway token AND the admin secret, which travels in the X-JAM-Admin
header (and in the request's _meta, for a proxy that strips headers). Nothing
is replaced until the new gateway loads with its complete package on the
node; the previous application stays in .hot_redeploy.previous for rollback.
After the restart the script checks that the server answering started after
the swap and runs the uploaded file.

A gateway from before the deploy tool answers "unknown tool": upgrade it once
with hot_redeploy_legacy.py (same job, keeps the sign-in), or relaunch it.
"""
import base64
import hashlib
import importlib.util
import json
import os
import sys
import time
import urllib.request

import gw_auth
from jamai import analysis_catalog, analysis_results

HERE = os.path.dirname(os.path.abspath(__file__))
URL = gw_auth.URL
LOCAL = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "jam_gateway.py")
PANEL = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "jam_panel.html")
CHUNK = 300_000             # bytes per stage_chunk (the gateway takes 400 000 base64 characters)
RELAUNCH = "python3 redeploy_jam.py cancel, then python3 deploy_and_verify.py (and sign in to the cluster again)"
REFUSED = ("the running gateway refuses this token: it is not its owner's. A gateway launched with the current "
           "redeploy_jam.py takes its own token (python3 redeploy_jam.py token; export JAM_GW_TOKEN); an older "
           "one takes the dashboard token of the account that launched it (LQCDMCP_TOKEN).")


def post(body, timeout=90):
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream",
                                          **gw_auth.auth_headers(), **gw_auth.admin_headers()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ping():
    """'ok' when the gateway answers its ping tool for this token, 'refused' when
    it refuses the token, None when no gateway answers."""
    try:
        res = post({"jsonrpc": "2.0", "id": 98, "method": "tools/call",
                    "params": {"name": "ping", "arguments": {}}}, timeout=20).get("result") or {}
    except Exception:
        return None
    if (res.get("structuredContent") or {}).get("status") == "unauthorized":
        return "refused"
    return "ok" if "pong" in " ".join(c.get("text", "") for c in res.get("content") or []) else None


_rid = [100]


def deploy(op, timeout=90, **fields):
    """One deploy operation. Exits on a refusal; returns {"unknown_tool": True}
    for a gateway that predates the tool."""
    _rid[0] += 1
    admin = gw_auth.admin_token()
    params = {"name": "deploy", "arguments": {"op": op, **fields}}
    if admin:
        params["_meta"] = {"jam/admin": admin}
    res = post({"jsonrpc": "2.0", "id": _rid[0], "method": "tools/call", "params": params}, timeout).get("result", {})
    text = " ".join(c.get("text", "") for c in res.get("content", []))
    sc = res.get("structuredContent") or {}
    if "unknown tool" in text and not sc:
        return {"unknown_tool": True}
    if sc.get("status") == "unauthorized":
        sys.exit(REFUSED if sc.get("code") == "AUTH_REQUIRED" else
                 f"{sc.get('message')} (JAM_GW_ADMIN, or ~/.config/jam_gateway/<gateway>.admin: "
                 "python3 redeploy_jam.py token shows where the secrets live)")
    if res.get("isError") or sc.get("ok") is False:
        sys.exit(f"deploy {op} failed: {json.dumps(sc)[:600] if sc else text[:600]}")
    return sc


def deployment_files():
    """Discover the whole application package, never a hand-maintained module list."""
    files = {"jam_gateway.py": LOCAL, "jam_panel.html": PANEL}
    src = os.path.join(HERE, "jamai")
    if not os.path.isfile(os.path.join(src, "__init__.py")):
        sys.exit("the source jamai package is missing __init__.py")
    for directory, directories, names in os.walk(src):
        directories[:] = sorted(d for d in directories if d not in ("__pycache__", ".ipynb_checkpoints"))
        for name in sorted(names):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = os.path.join(directory, name)
            if os.path.islink(path):
                sys.exit(f"refusing a symlink in the application package: {path}")
            rel = "jamai/" + os.path.relpath(path, src).replace(os.sep, "/")
            files[rel] = path
    # The plugins' files, as plugins/__init__.py lists them (the same as the job script).
    spec = importlib.util.spec_from_file_location("jam_plugins", os.path.join(HERE, "plugins", "__init__.py"))
    plugins = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugins)
    shipped, skipped = plugins.ship_files()
    for name, why in skipped.items():
        print(f"note: plugin {name} is left out of the gateway: {why}")
    files.update(shipped)
    files.update(analysis_catalog.Catalog(analysis_catalog.catalog_root(HERE)).deployment_files())
    files.update(analysis_results.Results(analysis_results.results_root(HERE)).deployment_files())
    return files


def upload(stage, rel, path):
    """Stage one file in chunks and seal it with its sha256."""
    data = open(path, "rb").read()
    chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
    for i, c in enumerate(chunks):
        deploy("stage_chunk", stage=stage, rel=rel, index=i, data_b64=base64.b64encode(c).decode())
    deploy("stage_finish", stage=stage, rel=rel, sha256=hashlib.sha256(data).hexdigest())
    return hashlib.sha256(data).hexdigest()


def main():
    gw_auth.require()
    if not gw_auth.token():
        sys.exit("no token: export JAM_GW_TOKEN (python3 redeploy_jam.py token shows it)")
    state = ping()
    if state is None:
        sys.exit(f"your gateway does not answer at {URL}: start it with python3 deploy_and_verify.py")
    if state == "refused":
        sys.exit(REFUSED)
    status = deploy("status")
    if status.get("unknown_tool"):
        sys.exit("this gateway predates the deploy tool. Upgrade it once with python3 hot_redeploy_legacy.py "
                 f"(same job, keeps the sign-in), or relaunch it: {RELAUNCH}.")
    # Only the job's supervisor loop starts the server again after the swap; a
    # gateway run any other way would stay down.
    if not status.get("slurm_job_id"):
        sys.exit("this gateway does not run as a Slurm job (a test gateway?), so nothing would start it again "
                 "after the swap. Restart a test gateway yourself with the new code; this script is for the "
                 "gateway job that deploy_and_verify.py launches.")
    print(f"gateway {status.get('gateway_sha256', '')[:12]} up {status.get('uptime_s')} s in job "
          f"{status['slurm_job_id']} (admin via {status.get('admin_via')})")

    stage = str(time.time_ns())
    files = deployment_files()
    hashes = {}
    try:
        for rel, path in files.items():
            hashes[rel] = upload(stage, rel, path)
        print(f"staged {len(files)} files")
        r = deploy("verify", stage=stage, hashes=hashes, timeout=180)
        print(f"the complete staged application loads on the node ({r.get('files')} files)")
        r = deploy("install", stage=stage, timeout=120)
        print(f"installed {', '.join(r.get('installed') or [])}; previous application kept in {r.get('previous')}")
    finally:
        try:
            deploy("clean", stage=stage)
        except SystemExit as e:
            print(f"note: could not clean the stage: {e}")
    print("restarting the server…")
    t0 = time.time()
    try:
        deploy("restart", timeout=20)
    except Exception as e:
        print("restart call interrupted as expected:", type(e).__name__)

    local_sha = hashlib.sha256(open(LOCAL, "rb").read()).hexdigest()
    deadline = time.time() + 90
    state = None
    while time.time() < deadline:
        time.sleep(5)
        state = ping()
        if state == "ok":
            status = deploy("status")
            if status.get("started_at", 0) > t0 and status.get("gateway_sha256") == local_sha:
                print(f"the new gateway is running (started {status.get('uptime_s')} s ago, sha256 {local_sha[:12]}); "
                      "python3 deploy_and_verify.py --check checks the rest")
                return
            if status.get("started_at", 0) > t0:
                sys.exit(f"the gateway restarted but runs {status.get('gateway_sha256', '')[:12]}, not the uploaded "
                         f"file {local_sha[:12]}. To be sure the new code runs: {RELAUNCH}.")
    if state == "refused":
        sys.exit("the new gateway runs but refuses your token, so this script cannot reach it any more. "
                 f"Check the token (python3 redeploy_jam.py token); if it stands, start over: {RELAUNCH}.")
    sys.exit("the gateway does not answer 90 s after the restart. Check again in a few minutes with "
             "python3 deploy_and_verify.py --check; if it stays down, the new code fails at start-up: "
             f"fix it, then {RELAUNCH}. The code that ran before is kept on the node as "
             "~/.jam_gateway/app/.hot_redeploy.previous.")


if __name__ == "__main__":
    main()
