"""Stage-2 v1: JAM gateway on the LQCD cluster node.

A zero-dependency MCP server that runs on a 24s compute node and reaches the
JAM farm cluster through the official two-hop path:

    node -> scilogin.jlab.org (gateway, PIN+OTP) -> ifarm (password)

ControlMasters live in /tmp/fm-cm/. The gateway hop is reused via a local
port forward (2222 -> ifarm:22), so the second hop never re-prompts the OTP.

Tools (v1, proves the chain end-to-end):
  connect_hpc      - establish the two-hop ControlMasters
  check_hpc        - verify + squeue -u chunyu + list /w/ceph24/JAM/chunyu
  submit_test_fit  - submit a trivial test job on ifarm via the chain
  ping             - liveness

NOTE (security): v1 accepts PIN+OTP and password as tool arguments for the
demo. Do not use this in production; the credential flow must move to a
browser-side panel (FemtoMind /app/panel pattern) before anyone else uses it.
"""
import base64
import json
import os
import socket
import stat
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8080
PROXY = "https://lqcdmcp.jlab.org:8000"
CM_DIR = os.path.expanduser("~/fm-cm")
GW_SOCK = f"{CM_DIR}/gateway"
FA_SOCK = f"{CM_DIR}/ifarm"
FA_FWD_PORT = 2222
SCILOGIN = "chunyu@scilogin.jlab.org"
IFARM = "chunyu@localhost"  # reached through the gateway forward
IFARM_SSH_OPTS = ["-p", str(FA_FWD_PORT)]


def _run(cmd, timeout=90, secret=None):
    """Run a command; optionally feed ONE secret via SSH_ASKPASS."""
    env = dict(os.environ)
    if secret is not None:
        helper = tempfile.NamedTemporaryFile("w", suffix="_askpass.sh", delete=False, dir=os.path.expanduser("~"))
        helper.write('#!/bin/sh\nprintf "%s\\n" "$FEMTO_SECRET"\n')
        helper.close()
        os.chmod(helper.name, stat.S_IRWXU)
        env.update(FEMTO_SECRET=secret, SSH_ASKPASS=helper.name,
                   SSH_ASKPASS_REQUIRE="force", DISPLAY=":0")
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, env=env)
        finally:
            try:
                os.unlink(helper.name)
            except OSError:
                pass
    else:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    return {"rc": p.returncode, "out": (p.stdout or "")[-2000:],
            "err": (p.stderr or "")[-800:]}


def _master_alive(sock, dest, opts=None):
    if not os.path.exists(sock):
        return False
    cmd = ["ssh", "-S", sock, "-O", "check"] + (opts or []) + [dest]
    r = _run(cmd, timeout=15)
    return r["rc"] == 0


def connect_hpc(pin_otp: str = "", password: str = "") -> dict:
    os.makedirs(CM_DIR, mode=0o700, exist_ok=True)
    # reuse check FIRST: unlink only sockets whose master is dead, never live ones
    if _master_alive(GW_SOCK, SCILOGIN) and _master_alive(FA_SOCK, IFARM, IFARM_SSH_OPTS):
        check = _remote("hostname; whoami; date -u", timeout=30)
        return {"status": "reused", "note": "existing ControlMasters alive — no OTP needed",
                "remote": check["out"][:200], "check": check["rc"]}
    for s in (GW_SOCK, FA_SOCK):
        if os.path.exists(s):
            try:
                os.unlink(s)
            except OSError:
                pass
    if not pin_otp or not password:
        return {"status": "error",
                "message": "need pin_otp (PIN+one-time code, no space) and password"}

    # hop 1: gateway (PIN+OTP)
    r1 = _run(["setsid", "ssh", "-M", "-S", GW_SOCK, "-o", "ControlPersist=8h",
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "NumberOfPasswordPrompts=2",
               "-o", "PubkeyAuthentication=no",
               "-o", "PreferredAuthentications=keyboard-interactive,password",
               "-fN", SCILOGIN], timeout=40, secret=pin_otp)
    if not _master_alive(GW_SOCK, SCILOGIN):
        return {"status": "error", "step": "gateway",
                "message": f"scilogin failed: {r1['err'][-400:] or r1['out'][-200:]}"}

    # local forward over the gateway master: 2222 -> ifarm:22
    r2 = _run(["ssh", "-S", GW_SOCK, "-O", "forward",
               "-L", f"127.0.0.1:{FA_FWD_PORT}:ifarm.jlab.org:22", SCILOGIN],
              timeout=20)
    if r2["rc"] != 0:
        return {"status": "error", "step": "forward", "message": r2["err"][-300:]}

    # hop 2: ifarm through the forward (password)
    r3 = _run(["setsid", "ssh", "-M", "-S", FA_SOCK, "-o", "ControlPersist=8h",
               "-o", "StrictHostKeyChecking=no",
               "-o", "NumberOfPasswordPrompts=2",
               "-o", "PubkeyAuthentication=no",
               "-o", "PreferredAuthentications=keyboard-interactive,password",
               "-fN", *IFARM_SSH_OPTS, IFARM], timeout=40, secret=password)
    if not _master_alive(FA_SOCK, IFARM, IFARM_SSH_OPTS):
        return {"status": "error", "step": "ifarm",
                "message": f"ifarm failed: {r3['err'][-400:] or r3['out'][-200:]}"}

    # verify end-to-end
    check = _remote("hostname; whoami; date -u", timeout=60)
    return {"status": "ok", "chain": "node->scilogin->ifarm",
            "remote": check["out"][:300], "check": check["rc"]}


def _remote(cmd, timeout=120):
    return _run(["ssh", "-S", FA_SOCK, *IFARM_SSH_OPTS, IFARM, "bash -lc " + _shq(cmd)],
                timeout=timeout)


def _shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def check_hpc() -> dict:
    if not _master_alive(FA_SOCK, IFARM, IFARM_SSH_OPTS):
        return {"status": "disconnected",
                "message": "no ifarm ControlMaster; call connect_hpc first"}
    out = _remote("hostname; squeue -u chunyu -o '%.10i %.9P %.12j %.2t' | head -8; "
                  "echo ---; ls /w/ceph24/JAM/chunyu 2>&1 | head -10", timeout=90)
    return {"status": "ok", **out}


def submit_test_fit() -> dict:
    """Fire-and-forget: writes the script and starts sbatch in the background,
    returns immediately (the proxy's 30s forward timeout cannot kill it)."""
    if not _master_alive(FA_SOCK, IFARM, IFARM_SSH_OPTS):
        return {"status": "disconnected", "message": "call connect_hpc first"}
    script = ("#!/bin/bash\n#SBATCH --partition=priority\n#SBATCH --time=00:05:00\n"
              "hostname; date; sleep 30\n")
    b64 = base64.b64encode(script.encode()).decode()
    out = _remote(f"echo {b64} | base64 -d > /tmp/fm_chain_test.sh && "
                  "nohup bash -c 'sbatch /tmp/fm_chain_test.sh' "
                  ">/tmp/fm_submit.log 2>&1 </dev/null & echo FIRED", timeout=15)
    return {"status": "fired", **out}


def get_submit_result() -> dict:
    """Poll the background sbatch result and the queue through the chain."""
    if not _master_alive(FA_SOCK, IFARM, IFARM_SSH_OPTS):
        return {"status": "disconnected", "message": "call connect_hpc first"}
    out = _remote("sleep 3; echo --- submit log ---; cat /tmp/fm_submit.log 2>&1; "
                  "echo --- queue ---; squeue -u chunyu -o '%.10i %.9P %.12j %.2t' | head -6",
                  timeout=20)
    return {"status": "ok", **out}


def node_diag(cmd: str = "id; hostname") -> dict:
    """Run a read-only shell command ON THE GATEWAY NODE (diagnostics)."""
    r = _run(["bash", "-lc", cmd], timeout=30)
    return {"rc": r["rc"], "out": r["out"], "err": r["err"]}


TOOLS = {
    "ping": {"desc": "Reply pong.", "schema": {"type": "object", "properties": {}},
             "fn": lambda a: "pong from LQCD gateway node"},
    "connect_hpc": {
        "desc": "Establish the two-hop ControlMaster chain node->scilogin->ifarm. "
                "pin_otp = PIN + one-time code concatenated, no space.",
        "schema": {"type": "object",
                   "properties": {"pin_otp": {"type": "string"},
                                  "password": {"type": "string"}},
                   "required": ["pin_otp", "password"]},
        "fn": lambda a: connect_hpc(a.get("pin_otp", ""), a.get("password", ""))},
    "check_hpc": {"desc": "Verify the chain and show chunyu's queue plus /w/ceph24/JAM/chunyu.",
                  "schema": {"type": "object", "properties": {}},
                  "fn": lambda a: check_hpc()},
    "submit_test_fit": {"desc": "Submit a trivial test job on ifarm through the chain (fire-and-forget).",
                        "schema": {"type": "object", "properties": {}},
                        "fn": lambda a: submit_test_fit()},
    "get_submit_result": {"desc": "Poll the background sbatch result and the queue.",
                          "schema": {"type": "object", "properties": {}},
                          "fn": lambda a: get_submit_result()},
    "node_diag": {"desc": "Run a read-only shell command on the gateway node (diagnostics).",
                  "schema": {"type": "object",
                             "properties": {"cmd": {"type": "string"}}},
                  "fn": lambda a: node_diag(a.get("cmd", "id; hostname"))},
}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"jam-gateway MCP server"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            req = json.loads(self.rfile.read(n))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            result = {"protocolVersion": (req.get("params") or {}).get(
                          "protocolVersion", "2025-03-26"),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "jam-gateway", "version": "0.1.0"}}
        elif method == "tools/list":
            result = {"tools": [
                {"name": k, "description": v["desc"], "inputSchema": v["schema"]}
                for k, v in TOOLS.items()]}
        elif method == "tools/call":
            name = (req.get("params") or {}).get("name", "")
            args = (req.get("params") or {}).get("arguments") or {}
            if name not in TOOLS:
                result = {"content": [{"type": "text", "text": "unknown tool"}],
                          "isError": True}
            else:
                try:
                    out = TOOLS[name]["fn"](args)
                    text = json.dumps(out, indent=2) if isinstance(out, (dict, list)) else str(out)
                    result = {"content": [{"type": "text", "text": text}],
                              "isError": False}
                except Exception as e:
                    result = {"content": [{"type": "text", "text": f"error: {e}"}],
                              "isError": True}
        else:
            result = {}
        data = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def register():
    fqdn = socket.getfqdn() or socket.gethostname()
    url = f"http://{fqdn}:{PORT}/mcp"
    job_id = os.environ.get("SLURM_JOB_ID", "0")
    job_name = os.environ.get("SLURM_JOB_NAME", "jam-gateway")
    enc = urllib.parse.quote(url)
    target = (f"{PROXY}/server?mcp_name={job_name}&server_url={enc}"
              f"&owner=chunyu&job_id={job_id}&job_name={job_name}")
    for i in range(10):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(target, method="POST", data=b""),
                    timeout=15) as r:
                print("register ok:", r.read().decode()[:120], flush=True)
                return
        except Exception as e:
            print("register retry:", e, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    print(f"jam-gateway listening on {PORT}, host {socket.getfqdn()}", flush=True)
    if os.environ.get("SLURM_JOB_ID"):
        register()
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
