#!/usr/bin/env python3
"""ONE-TIME upgrade of a gateway from before the deploy tool (it still has node_diag).

Use hot_redeploy.py for every gateway that already has the deploy tool; this
script is deleted one release after the switch. Besides swapping the code it
writes the sha256 of your gateway token and admin secret (never the secrets)
into ~/.jam_gateway/token.sha256 and admin.sha256 on the node, so the restarted
gateway, whose job environment predates JAM_GW_TOKEN_SHA256, accepts the new
token at once and runs in token mode.


    export LQCDMCP_TOKEN='...'   JAM_GW_USER=<you>
    python3 hot_redeploy_legacy.py [gateway_file] [panel_file]
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
After the restart the script checks, with the NEW gateway token and admin
secret (the restarted gateway runs in token mode and has no node_diag), that
the server answering started after the swap and runs the uploaded file.
"""
import base64
import hashlib
import importlib.util
import json
import os
import re
import shlex
import sys
import time
import urllib.request

import gw_auth
from jamai import analysis_catalog, analysis_results

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


# The old gateway takes the dashboard token only. gw_auth would send a stored
# gateway token first (one exists after any earlier upgrade), so the calls to
# the old gateway carry the dashboard token explicitly.
OLD_HEADERS = {}


def post(body, timeout=90, headers=None):
    """headers: the auth headers to send; by default the old gateway's (the dashboard token)."""
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream",
                                          **(OLD_HEADERS if headers is None else headers)})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    # The gateway may answer as an event stream: the last data line is the reply.
    events = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
    return json.loads(events[-1] if events else raw)


def ping(headers=None):
    """'ok' when the gateway answers its ping tool for this token, 'refused' when
    it refuses the token, None when no gateway answers."""
    try:
        res = post({"jsonrpc": "2.0", "id": 98, "method": "tools/call",
                    "params": {"name": "ping", "arguments": {}}}, timeout=20, headers=headers).get("result") or {}
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


def new_status(headers):
    """The restarted gateway's own account of itself (its hidden deploy tool,
    with the admin secret): {'started_at', 'gateway_sha256', ...}, or
    {'unknown_tool': True} when the server answering has no deploy tool (the
    old code), or {'refused': message}."""
    admin = gw_auth.admin_token()
    try:
        res = post({"jsonrpc": "2.0", "id": 97, "method": "tools/call",
                    "params": {"name": "deploy", "arguments": {"op": "status"}, "_meta": {"jam/admin": admin}}},
                   timeout=30, headers=dict(headers, **{"X-JAM-Admin": admin})).get("result") or {}
    except Exception as exc:
        return {"refused": f"no answer ({type(exc).__name__})"}
    sc = res.get("structuredContent") or {}
    text = " ".join(c.get("text", "") for c in res.get("content") or [])
    if "unknown tool" in text and not sc:
        return {"unknown_tool": True}
    if sc.get("status") == "unauthorized" or res.get("isError"):
        return {"refused": sc.get("message") or text[:300]}
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
gateway = importlib.import_module('jam_gateway')
catalog_root = gateway.analysis_catalog.catalog_root(stage)
if catalog_root.resolve() != (stage / 'analyses').resolve():
    raise RuntimeError('catalog escaped staged application')
if not gateway.analysis_catalog.Catalog(catalog_root).deployment_files():
    raise RuntimeError('no validated scientific catalog entries were staged')
results_root = gateway.analysis_results.results_root(stage)
if results_root.resolve() != (stage / 'analyses').resolve():
    raise RuntimeError('results manifests escaped staged application')
if not gateway.analysis_results.Results(results_root).deployment_files():
    raise RuntimeError('no validated reference-results manifests were staged')
for name, module in list(sys.modules.items()):
    if (name == 'jam_gateway' or name == 'jamai' or name.startswith('jamai.')
            or name == 'jam_plugins' or name.startswith('jam_plugin_')):
        path = pathlib.Path(module.__file__).resolve()
        if stage not in path.parents:
            raise RuntimeError('import escaped staged application: ' + name)
errors = getattr(gateway, 'PLUGIN_STATUS', {}).get('errors') or {}
if errors:
    raise RuntimeError('plugin failed to load: ' + '; '.join(n + ': ' + w for n, w in errors.items()))
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
components = ('jamai', 'plugins', 'analyses', 'jam_panel.html', 'jam_gateway.py')
saved, installed = [], []
try:
    for name in components:
        if (app / name).exists():
            shutil.move(str(app / name), str(backup / name))
            saved.append(name)
        if name == 'plugins' and not (stage / name).exists():
            continue                # an application without plugins
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


def confirm_restart(t0, wait=90):
    """After the swap: the server answering started after t0 and runs the uploaded file. Exits with
    what is wrong otherwise."""
    # The restarted gateway runs in token mode: it accepts the gateway token,
    # not the dashboard token, and it has the deploy tool instead of node_diag.
    # So it is checked with the new secrets. (Checking it with the dashboard
    # token and node_diag, as this script did before 2026-10-30, reported a
    # successful upgrade as "the new gateway refuses your token".)
    headers = {"Authorization": "Bearer " + gw_auth.ensure_secret("token")}
    with open(LOCAL, "rb") as fh:
        local_sha = hashlib.sha256(fh.read()).hexdigest()
    deadline, state, status = time.time() + wait, None, {}
    while time.time() < deadline:
        time.sleep(5)
        state = ping(headers)
        if state == "ok":
            status = new_status(headers)
            if status.get("started_at", 0) > t0 and status.get("gateway_sha256") == local_sha:
                break
        # 'refused' or the old server: the old gateway takes only the dashboard
        # token, so a refusal right after the swap can be it, still stopping.
    if state is None:
        sys.exit(f"the gateway does not answer {wait} s after the restart. Check again in a few minutes with "
                 "python3 deploy_and_verify.py --check; if it stays down, the new code fails at start-up: "
                 f"fix it, then {RELAUNCH}. The code that ran before is kept on the node as "
                 f"{APP}/.hot_redeploy.previous (gateway, panel and jamai package).")
    if state == "refused":
        sys.exit("the gateway answering refuses the new gateway token. Either the old gateway is still running "
                 "(it takes only the dashboard token: the restart did not happen) or the new one did not read "
                 "~/.jam_gateway/token.sha256. python3 deploy_and_verify.py --check says which; "
                 f"to be sure the new code runs: {RELAUNCH}.")
    if status.get("unknown_tool"):
        sys.exit("the gateway answering accepts the new token but has no deploy tool, so it is not the uploaded "
                 f"code. To run the new code: {RELAUNCH}.")
    if status.get("refused"):
        sys.exit(f"the new gateway answers but refuses the admin secret ({status['refused'][:200]}); the admin "
                 "hash written on the node may not have been read. python3 deploy_and_verify.py --check shows "
                 "what runs.")
    if not status.get("started_at", 0) > t0:
        sys.exit("the gateway did not restart: the server answering started before the swap. To run the new "
                 f"code: {RELAUNCH}.")
    if status.get("gateway_sha256") != local_sha:
        sys.exit(f"the gateway restarted but runs {str(status.get('gateway_sha256'))[:12]}, not the uploaded "
                 f"{local_sha[:12]}. To be sure the new code runs: {RELAUNCH}.")
    print(f"the new gateway is running (started {round(time.time() - status['started_at'])} s ago, sha256 "
          f"{local_sha[:12]}) in token mode. From now on clients use the gateway token "
          "(python3 redeploy_jam.py token --show) and code updates go through hot_redeploy.py; "
          "python3 deploy_and_verify.py --check checks the rest.")


def main():
    gw_auth.require()
    if not gw_auth.dashboard_token():
        sys.exit("no token: export LQCDMCP_TOKEN (the dashboard token the old gateway accepts)")
    OLD_HEADERS["Authorization"] = "Bearer " + gw_auth.dashboard_token()
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
    # The hashes of the new secrets (never the secrets themselves: a command line
    # is visible to every user of the node). The restarted gateway reads them
    # from these files when its job environment carries no JAM_GW_*_SHA256.
    hashes = {"token.sha256": gw_auth.sha256(gw_auth.ensure_secret("token")),
              "admin.sha256": gw_auth.sha256(gw_auth.ensure_secret("admin"))}
    r = diag("umask 077; " + " && ".join(f"printf '%s\\n' {h} > ~/.jam_gateway/{n}" for n, h in hashes.items())
             + " && echo HASHES-OK", 302)
    if "HASHES-OK" not in (r.get("out") or ""):
        sys.exit(f"could not write the token hashes on the node: {str(r)[:300]}")
    print("token and admin hashes written on the node: the restarted gateway takes the gateway token "
          "(python3 redeploy_jam.py token) and runs in token mode")

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

    confirm_restart(t0)


if __name__ == "__main__":
    main()
