"""Stage-1 fitpack deployment probe for the LQCD cluster.

Zero-dependency MCP server (stdlib only) whose tools answer every environment
question we have been blind about while debugging remotely:

  * is /w/ceph24 mounted and is fitpack_legacy visible?
  * does sbatch/squeue work from the node (same cluster)?
  * which python is available, and does it have mcp/fastmcp?
  * what do SLURM env vars look like (job id, nodelist)?
  * who am I (uid/gid) and where is HOME?

Runs as a Slurm job on the LQCD cluster (24s), listens on 8080, and
self-registers with the proxy exactly like Jie's test_user_mcp_server.py.
Once the proxy's /cloud route works, calling `probe_cluster` returns the full
report — this decides how stage 2 (the real fitpack tool layer) is packaged.
"""
import json
import os
import platform
import socket
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

PROXY = "https://lqcdmcp.jlab.org:8000"
PORT = 8080


def _run(cmd, timeout=30):
    try:
        p = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return {"rc": p.returncode, "out": (p.stdout or "")[:800],
                "err": (p.stderr or "")[:400]}
    except Exception as e:
        return {"rc": -1, "out": "", "err": f"{type(e).__name__}: {e}"}


def probe_cluster() -> dict:
    return {
        "hostname": socket.gethostname(),
        "fqdn": socket.getfqdn(),
        "ip": socket.gethostbyname(socket.gethostname()),
        "python": platform.python_version(),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "home": os.environ.get("HOME"),
        "cwd": os.getcwd(),
        "slurm_env": {k: v for k, v in os.environ.items()
                      if k.startswith("SLURM") and k in
                      ("SLURM_JOB_ID", "SLURM_JOB_NAME", "SLURM_NODELIST",
                       "SLURM_CLUSTER_NAME", "SLURM_PARTITION")},
        "ceph24": _run("ls -d /w/ceph24/JAM/chunyu/fitpack_legacy 2>&1 | head -3"),
        "ceph24_jam": _run("ls /w/ceph24/JAM/ 2>&1 | head -10"),
        "sbatch": _run("sbatch --version 2>&1 | head -1"),
        "squeue_me": _run("squeue -u chunyu -o '%.10i %.9P %.12j %.2t' 2>&1 | head -8"),
        "mcp_import": _run("python3 -c 'import mcp; print(mcp.__file__)' 2>&1 | head -2"),
        "fastmcp_import": _run("python3 -c 'import fastmcp; print(fastmcp.__file__)' 2>&1 | head -2"),
        "pip_available": _run("python3 -m pip --version 2>&1 | head -1"),
    }


def run_slurm_status() -> dict:
    return {"squeue": _run("squeue -u chunyu 2>&1 | head -15")}



def test_network() -> dict:
    """Test outbound connectivity from this LQCD node to JLab hosts."""
    return {
        "dns_ifarm": _run("getent hosts ifarm.jlab.org | head -2"),
        "tcp_ifarm_22": _run("timeout 5 bash -c 'exec 3<>/dev/tcp/ifarm.jlab.org/22' 2>&1 && echo OPEN || echo CLOSED"),
        "tcp_scilogin_22": _run("timeout 5 bash -c 'exec 3<>/dev/tcp/scilogin.jlab.org/22' 2>&1 && echo OPEN || echo CLOSED"),
        "tcp_proxy_8000": _run("timeout 5 bash -c 'exec 3<>/dev/tcp/lqcdmcp.jlab.org/8000' 2>&1 && echo OPEN || echo CLOSED"),
    }

TOOLS = {
    "probe_cluster": {
        "desc": "Run every environment check needed to package the real fitpack "
                "tool layer for this cluster: ceph24 visibility, sbatch/squeue, "
                "python and mcp availability, slurm env.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: probe_cluster(),
    },
    "test_network": {
        "desc": "Test outbound network from this node to JLab hosts (ifarm/scilogin/proxy).",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: test_network(),
    },
    "run_slurm_status": {
        "desc": "Show chunyu's jobs in the LQCD cluster queue.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: run_slurm_status(),
    },
}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"fitpack-probe MCP server"
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
            result = {
                "protocolVersion": (req.get("params") or {}).get(
                    "protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fitpack-probe", "version": "1.0.0"},
            }
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


def register_with_proxy():
    """Mirror Jie's setup_run: register the backend before serving."""
    import urllib.parse
    import urllib.request
    fqdn = socket.getfqdn() or socket.gethostname()
    ip = socket.gethostbyname(socket.gethostname())
    url = f"http://{fqdn}:{PORT}/mcp"
    job_id = os.environ.get("SLURM_JOB_ID", "0")
    job_name = os.environ.get("SLURM_JOB_NAME", "fitpack-probe")
    owner = "chunyu"
    enc = urllib.parse.quote(url)
    target = (f"{PROXY}/server?mcp_name={job_name}&server_url={enc}"
              f"&owner={owner}&job_id={job_id}&job_name={job_name}")
    for attempt in range(10):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(target, method="POST", data=b""),
                    timeout=15) as r:
                print(f"register ok (attempt {attempt+1}): {r.read().decode()[:120]}")
                return True
        except Exception as e:
            print(f"register retry {attempt+1}: {e}")
            import time
            time.sleep(5)
    return False


if __name__ == "__main__":
    print("fitpack-probe starting on", PORT, "host", socket.getfqdn(), flush=True)
    if os.environ.get("SLURM_JOB_ID"):
        register_with_proxy()
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
