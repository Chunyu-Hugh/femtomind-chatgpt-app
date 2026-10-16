#!/usr/bin/env python3
"""Update a running gateway's code without restarting its Slurm job.

    export LQCDMCP_TOKEN='...'   JAM_GW_USER=<you>
    python3 hot_redeploy.py [gateway_file] [panel_file]
    (defaults: jam_gateway.py and jam_panel.html next to this script)

The job's supervisor loop restarts the server from ~/.jam_gateway/app, and the
cluster sign-in (the ControlMasters in ~/fm-cm) outlives the restart, so a
code swap needs no new one-time code and no new job slot. A gateway that does
not run as a Slurm job (a test gateway) has no such loop, so it is refused.

Uploads go through node_diag, a shell on the gateway node: it is hidden from
AI clients (not in tools/list) but callable by name with the owner's
dashboard token (LQCDMCP_TOKEN). Nothing is replaced until the new gateway
loads with its complete package on the node. The previous gateway, panel and
package stay together in .hot_redeploy.previous for rollback.
After the restart the script checks that the server answering started after
the swap, runs the uploaded file and still accepts the token.
"""
import base64
import hashlib
import json
import os
import re
import shlex
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
REFUSED = ("the running gateway refuses this token: it does not recognise it as its owner's. Use the "
           "dashboard token of the account that launched it (LQCDMCP_TOKEN); if the login expired, "
           "log in to the dashboard again.")
# Starting over runs the code in this folder in a new job, on a new node.
RELAUNCH = "python3 redeploy_jam.py cancel, then python3 deploy_and_verify.py (and sign in to the cluster again)"


def post(body, timeout=90):
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream",
                                          **gw_auth.auth_headers()})
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


def diag(cmd, rid, timeout=90):
    res = post({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                "params": {"name": "node_diag", "arguments": {"cmd": cmd}}}, timeout).get("result", {})
    text = " ".join(c.get("text", "") for c in res.get("content", []))
    refused = ((res.get("structuredContent") or {}).get("status") == "unauthorized"
               or "unknown tool" in text)
    if res.get("isError") and refused:
        sys.exit(REFUSED)
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
    return files


def remote_python(source, arguments, rid):
    """Run a small stdlib-only transaction in the gateway job's Python environment."""
    program = "args = " + repr(arguments) + "\n" + source
    encoded = base64.b64encode(program.encode()).decode()
    command = "import base64; exec(base64.b64decode(" + repr(encoded) + "))"
    return diag("python3 -B -c " + shlex.quote(command), rid)


STAGE_CHECK = r"""
import hashlib, importlib, os, pathlib, sys
stage = pathlib.Path(os.path.expanduser(args['stage'])).resolve()
os.chdir(stage)
sys.path.insert(0, str(stage))
for rel, expected in args['hashes'].items():
    path = stage / rel
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise RuntimeError('staged hash mismatch: ' + rel)
    if path.suffix == '.py':
        compile(path.read_bytes(), str(path), 'exec')
importlib.import_module('jam_gateway')
for name, module in list(sys.modules.items()):
    if name == 'jam_gateway' or name == 'jamai' or name.startswith('jamai.'):
        path = pathlib.Path(module.__file__).resolve()
        if stage not in path.parents:
            raise RuntimeError('import escaped staged application: ' + name)
print('IMPORT-OK')
"""

INSTALL = r"""
import os, pathlib, shutil
app = pathlib.Path(os.path.expanduser(args['app'])).resolve()
stage = pathlib.Path(os.path.expanduser(args['stage'])).resolve()
if stage.parent != app or not stage.name.startswith('.hot_redeploy.stage-'):
    raise RuntimeError('unexpected staging directory')
backup = app / '.hot_redeploy.previous'
if backup.exists():
    shutil.rmtree(backup)
backup.mkdir(mode=0o700)
components = ('jamai', 'jam_panel.html', 'jam_gateway.py')
saved, installed = [], []
try:
    for name in components:
        if (app / name).exists():
            shutil.move(str(app / name), str(backup / name))
            saved.append(name)
        shutil.move(str(stage / name), str(app / name))
        installed.append(name)
except BaseException:
    for name in reversed(installed):
        target = app / name
        shutil.rmtree(target) if target.is_dir() else target.unlink()
    for name in reversed(saved):
        shutil.move(str(backup / name), str(app / name))
    raise
print('SWAPPED')
"""

CLEAN_STAGE = r"""
import os, pathlib, shutil
app = pathlib.Path(os.path.expanduser(args['app'])).resolve()
stage = pathlib.Path(os.path.expanduser(args['stage'])).resolve()
if stage.parent != app or not stage.name.startswith('.hot_redeploy.stage-'):
    raise RuntimeError('unexpected staging directory')
if stage.exists():
    shutil.rmtree(stage)
"""


def main():
    gw_auth.require()
    if not gw_auth.token():
        sys.exit("no token: export LQCDMCP_TOKEN (your dashboard token)")
    state = ping()
    if state is None:
        sys.exit(f"your gateway does not answer at {URL}: start it with python3 deploy_and_verify.py")
    if state == "refused":
        sys.exit(REFUSED)
    # Only the job's supervisor loop starts the server again after the swap; a
    # gateway run any other way would stay down.
    if not re.search(r"SLURM_JOB_ID=\d", diag('echo "SLURM_JOB_ID=$SLURM_JOB_ID"', 2).get("out") or ""):
        sys.exit("this gateway does not run as a Slurm job (a test gateway?), so nothing would start it again "
                 "after the swap. Restart a test gateway yourself with the new code; this script is for the "
                 "gateway job that deploy_and_verify.py launches.")

    stage = f"{APP}/.hot_redeploy.stage-{time.time_ns()}"
    files = deployment_files()
    try:
        for i, (rel, path) in enumerate(files.items()):
            upload(path, f"{stage}/{rel}", 400 + 50 * i)
            r = diag(f"mv {stage}/{rel}.new {stage}/{rel}", 449 + 50 * i)
            if r.get("rc") != 0:
                sys.exit(f"staging {rel} FAILED; the running deployment was not replaced")
        expected = {rel: hashlib.sha256(open(path, "rb").read()).hexdigest()
                    for rel, path in files.items()}
        r = remote_python(STAGE_CHECK, {"stage": stage, "hashes": expected}, 299)
        if r.get("rc") != 0 or "IMPORT-OK" not in (r.get("out") or ""):
            last = ((r.get("err") or r.get("out") or "").strip().splitlines() or ["no output"])[-1]
            sys.exit(f"the staged application does not load on the node ({last[:300]}), "
                     "so nothing was replaced")
        print(f"the complete staged application loads on the node ({len(files)} files)")
        r = remote_python(INSTALL, {"app": APP, "stage": stage}, 300)
        if r.get("rc") != 0 or "SWAPPED" not in (r.get("out") or ""):
            sys.exit(f"installing the staged application FAILED: {str(r)[:300]}")
    finally:
        remote_python(CLEAN_STAGE, {"app": APP, "stage": stage}, 398)
    print("gateway, panel and campaign package installed; restarting the server…")

    t0 = time.time()
    try:
        # node_diag's shell is a direct child of this gateway. Verify its cwd
        # before signaling that exact server; other gateways are untouched.
        r = diag(f'test "$(readlink -f /proc/$PPID/cwd)" = "$(readlink -f {APP})" && '
                 'echo SWAPPED && kill -TERM "$PPID"', 399, timeout=20)
    except Exception as e:
        print("restart call interrupted as expected:", type(e).__name__)
    else:
        # An answer means the server that ran the command is still alive.
        if "SWAPPED" not in (r.get("out") or ""):
            sys.exit(f"replacing the gateway file on the node FAILED, so nothing was restarted: {str(r)[:300]}")
        print("the server answered the restart call, so it may not have stopped; checking…")

    deadline = time.time() + 60
    state = None
    while state is None and time.time() < deadline:
        time.sleep(5)
        state = ping()
    if state == "refused":
        time.sleep(20)          # a failed dashboard check is retried after 15 s; a refusal stands
        state = ping() or state
    if state is None:
        sys.exit("the gateway does not answer 60 s after the restart. Check again in a few minutes with "
                 "python3 deploy_and_verify.py --check; if it stays down, the new code fails at start-up: "
                 f"fix it, then {RELAUNCH}. The code that ran before is kept on the node as "
                 f"{APP}/.hot_redeploy.previous (gateway, panel and jamai package).")
    if state == "refused":
        sys.exit("the new gateway runs but refuses your token, so this script cannot reach it any more. "
                 "python3 check_dashboard_token.py shows why; once it says the gateway accepts your token, "
                 f"start over: {RELAUNCH}.")
    # $PPID is the server that runs this node_diag call.
    out = diag("cd /proc/$PPID/cwd; sha256sum jam_gateway.py; ps -o etimes= -p $PPID", 301).get("out") or ""
    age = re.findall(r"^\s*(\d+)\s*$", out, re.M)
    if age and int(age[-1]) > time.time() - t0:
        sys.exit("the gateway did not restart: the server answering started before the swap and still runs "
                 f"the old code. To run the new code: {RELAUNCH}.")
    if not age or hashlib.sha256(open(LOCAL, "rb").read()).hexdigest() not in out:
        sys.exit(f"could not confirm that the server runs the uploaded file (the node said: "
                 f"{out.strip()[:300]!r}). To be sure the new code runs: {RELAUNCH}.")
    print(f"the new gateway is running (started {age[-1]} s ago); python3 deploy_and_verify.py --check "
          "checks the rest")


if __name__ == "__main__":
    main()
