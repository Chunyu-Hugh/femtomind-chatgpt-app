"""Stage-2 v2: JAM gateway on the LQCD cluster node.

Zero-dependency MCP server reaching the JAM farm cluster through the
two-hop chain:

    node -> scilogin.jlab.org (gateway, PIN+OTP) -> ifarm (password)

v2 adds the REAL JAM fitpack workflow:
  remote_exec      - arbitrary read-only shell on ifarm through the chain
  submit_jam_fit   - real apptainer maxlike_jam fit (card + prior msr + cores)
  get_jam_result   - poll queue + slurm logs + progress.json + msr listing
  list_jam_runs    - inventory of smoke4 run dirs and msr files
plus the v1 tools (ping / connect_hpc / check_hpc / node_diag).

Hot-redeploy: node_diag can overwrite /tmp/jam_gateway.py and the
supervisor loop restarts the server inside the same job boot, so the
setsid'd ControlMasters survive — no new OTP.
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8080
PROXY = "https://lqcdmcp.jlab.org:8000"
CM_DIR = os.path.expanduser("~/fm-cm")
GW_SOCK = f"{CM_DIR}/gateway"
FA_SOCK = f"{CM_DIR}/ifarm"
FA_FWD_PORT = 2222
SCILOGIN = "chunyu@scilogin.jlab.org"
IFARM = "chunyu@localhost"  # reached through the gateway forward
IFARM_SSH_OPTS = ["-p", str(FA_FWD_PORT)]

# ---- real JAM constants (ifarm side) ----
FITPACK = "/w/ceph24/JAM/chunyu/fitpack_legacy"
RUN_ROOT = "/w/ceph24/JAM/chunyu/jam24repro/smoke4"
LOG_DIR = f"{RUN_ROOT}/logs"
SIF = "/u/scigroup/scicomp/jupyterhub/theory/theory-notebook.sif"


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
    return {"rc": p.returncode, "out": (p.stdout or "")[-4000:],
            "err": (p.stderr or "")[-800:]}


def _master_alive(sock, dest, opts=None):
    if not os.path.exists(sock):
        return False
    cmd = ["ssh", "-S", sock, "-O", "check"] + (opts or []) + [dest]
    r = _run(cmd, timeout=15)
    return r["rc"] == 0


def _shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _remote(cmd, timeout=120):
    return _run(["ssh", "-S", FA_SOCK, *IFARM_SSH_OPTS, IFARM, "bash -lc " + _shq(cmd)],
                timeout=timeout)


def _chain_ok():
    return _master_alive(FA_SOCK, IFARM, IFARM_SSH_OPTS)


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


def check_hpc() -> dict:
    if not _chain_ok():
        return {"status": "disconnected",
                "message": "no ifarm ControlMaster; call connect_hpc first"}
    out = _remote("hostname; squeue -u chunyu -o '%.10i %.9P %.12j %.2t' | head -8; "
                  "echo ---; ls /w/ceph24/JAM/chunyu 2>&1 | head -10", timeout=90)
    return {"status": "ok", **out}


def submit_test_fit() -> dict:
    """Fire-and-forget trivial test job on ifarm (kept from v1)."""
    if not _chain_ok():
        return {"status": "disconnected", "message": "call connect_hpc first"}
    script = ("#!/bin/bash\n#SBATCH --partition=priority\n#SBATCH --time=00:05:00\n"
              "hostname; date; sleep 30\n")
    b64 = base64.b64encode(script.encode()).decode()
    out = _remote(f"echo {b64} | base64 -d > /tmp/fm_chain_test.sh && "
                  "nohup bash -c 'sbatch /tmp/fm_chain_test.sh' "
                  ">/tmp/fm_submit.log 2>&1 </dev/null & echo FIRED", timeout=15)
    return {"status": "fired", **out}


def get_submit_result() -> dict:
    if not _chain_ok():
        return {"status": "disconnected", "message": "call connect_hpc first"}
    out = _remote("sleep 3; echo --- submit log ---; cat /tmp/fm_submit.log 2>&1; "
                  "echo --- queue ---; squeue -u chunyu -o '%.10i %.9P %.12j %.2t' | head -6",
                  timeout=20)
    return {"status": "ok", **out}


def node_diag(cmd: str = "id; hostname") -> dict:
    """Run a shell command ON THE GATEWAY NODE (diagnostics / hot-redeploy)."""
    r = _run(["bash", "-lc", cmd], timeout=30)
    return {"rc": r["rc"], "out": r["out"], "err": r["err"]}


# ================= v2: real JAM fitpack workflow =================

def remote_exec(cmd: str = "hostname", timeout: int = 120) -> dict:
    """Run a shell command on ifarm through the chain."""
    if not _chain_ok():
        return {"status": "disconnected", "message": "call connect_hpc first"}
    out = _remote(cmd, timeout=timeout)
    return {"status": "ok", **out}


def list_jam_runs() -> dict:
    """Inventory: cards available + smoke4 run dirs + msr files."""
    if not _chain_ok():
        return {"status": "disconnected", "message": "call connect_hpc first"}
    out = _remote(
        f"echo '--- cards ---'; ls {FITPACK}/inputs/jam24 2>&1 | head -20; "
        f"echo '--- run dirs ---'; ls {RUN_ROOT}/001 2>&1 | head -20; "
        f"echo '--- msr files (newest 8) ---'; "
        f"find {RUN_ROOT} -name '*.msr' -printf '%T@ %p\\n' 2>/dev/null | sort -rn | head -8 | cut -d' ' -f2-",
        timeout=60)
    return {"status": "ok", **out}


def _resolve_outdir(outdir):
    if outdir.startswith("/"):
        return outdir
    return f"{RUN_ROOT}/{outdir}"


def submit_jam_fit(card: str = "", outdir: str = "", prior_msr: str = "",
                   cores: int = 4, seed: int = 160300, hours: int = 1,
                   partition: str = "jupyter") -> dict:
    """Submit a REAL JAM fit on ifarm: apptainer maxlike_jam with the card.

    card     - card filename in fitpack/inputs/jam24 (e.g. input_jam24_s2_warm.py)
    outdir   - output dir, absolute or relative to smoke4 (e.g. 002/step3)
    prior_msr- path to a prior .msr file ("" = cold start)
    Returns FIRED; poll with get_jam_result.
    """
    if not _chain_ok():
        return {"status": "disconnected", "message": "call connect_hpc first"}
    if not card or not outdir:
        return {"status": "error", "message": "need card and outdir"}
    out_abs = _resolve_outdir(outdir)
    hhmm = f"{int(hours):02d}:00:00"
    prior_flag = f'-p "{prior_msr}"' if prior_msr else ""
    script = f"""#!/bin/bash
#SBATCH --job-name=jamfit
#SBATCH --partition={partition}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={cores}
#SBATCH --time={hhmm}
#SBATCH --mem=4G
#SBATCH --output={LOG_DIR}/jamfit_%j.out
#SBATCH --error={LOG_DIR}/jamfit_%j.err

export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1
FP={FITPACK}
cd $FP || exit 1
OUT={out_abs}
mkdir -p "$OUT"
echo "===== JAM FIT card={card} out=$OUT cores={cores} seed={seed} prior={prior_msr} ====="
apptainer exec --bind /w/ceph24 \\
  --env PYTHONPATH=$FP:/usr/local/lib/python3.11/site-packages \\
  --env FITPACK=$FP \\
  --env LHAPDF_DATA_PATH=/w/ceph24/JAM/chunyu/lhapdf_tables:/w/ceph24/JAM/apps/lhapdf_tables/more/more1 \\
  {SIF} \\
  python3 -m fitlib.maxlike_jam $FP/inputs/jam24/{card} "$OUT" -n {cores} -v 10 -s {seed} {prior_flag}
echo "exit=$?"
ls "$OUT"/msr/ 2>/dev/null
"""
    b64 = base64.b64encode(script.encode()).decode()
    out = _remote(f"echo {b64} | base64 -d > /tmp/jam_fit_latest.sh && "
                  "nohup bash -c 'sbatch /tmp/jam_fit_latest.sh' "
                  ">/tmp/jam_fit_latest.submit 2>&1 </dev/null & echo FIRED", timeout=20)
    return {"status": "fired", "card": card, "outdir": out_abs, **out}


def get_jam_result(outdir: str = "", job_name: str = "jamfit") -> dict:
    """Poll the latest JAM fit: queue + submit log + progress.json + msr."""
    if not _chain_ok():
        return {"status": "disconnected", "message": "call connect_hpc first"}
    out_abs = _resolve_outdir(outdir) if outdir else RUN_ROOT
    cmd = (
        f"echo '--- queue ---'; squeue -u chunyu -o '%.10i %.9P %.12j %.2t %.10M' | head -8; "
        f"echo '--- submit log ---'; cat /tmp/jam_fit_latest.submit 2>&1; "
        f"echo '--- progress ---'; ls -t {out_abs}/progress.json 2>/dev/null | head -1 | xargs -r tail -c 1500; "
        f"echo; echo '--- msr ---'; ls {out_abs}/msr 2>&1 | head -5; "
        f"echo '--- newest slurm err tail ---'; ls -t {LOG_DIR}/jamfit_*.err 2>/dev/null | head -1 | xargs -r tail -12"
    )
    out = _remote(cmd, timeout=60)
    return {"status": "ok", **out}


def read_chi2(outdir: str = "") -> dict:
    """Parse progress.json on ifarm and return the chi2/N series + per-dataset
    chi2/N of the latest iteration."""
    if not _chain_ok():
        return {"status": "disconnected", "message": "call connect_hpc first"}
    out_abs = _resolve_outdir(outdir) if outdir else RUN_ROOT
    prog = f"{out_abs}/progress.json"
    py = (
        "import json,sys\n"
        f"d=json.load(open('{prog}'))\n"
        "print('entries:',len(d))\n"
        "tot=[e.get('chi2') for e in d if e.get('chi2') is not None]\n"
        "npt=[sum(v.get('npts',0) for v in e.get('datasets',{}).values()) for e in d]\n"
        "for i,(t,n) in enumerate(zip(tot,npt)):\n"
        "    if i>=len(tot)-12 and n: print(f'iter {i}: chi2={t:.1f} npts={n} chi2/N={t/n:.3f}')\n"
        "last=d[-1]\n"
        "print('--- latest per-dataset chi2/N (top 10) ---')\n"
        "ds=last.get('datasets',{})\n"
        "for k in sorted(ds, key=lambda k: -ds[k]['chi2']/max(ds[k]['npts'],1))[:10]:\n"
        "    v=ds[k]; print(f'{k}: chi2/N={v[\"chi2\"]/v[\"npts\"]:.2f} ({v[\"npts\"]} pts)')\n"
    )
    # ship the python snippet as base64 to avoid shell quoting hell
    import base64 as _b64
    py_b64 = _b64.b64encode(py.encode()).decode()
    out = _remote(f"echo {py_b64} | base64 -d | python3", timeout=120)
    return {"status": "ok", **out}


def evaluate_fit(outdir: str = "", msr_file: str = "") -> dict:
    """Evaluate a finished fit's .msr against the JAM24 paper band
    (chi2/N mean=2.566, band [2.190, 2.819], arXiv:2501.00665 step16a)."""
    if not _chain_ok():
        return {"status": "disconnected", "message": "call connect_hpc first"}
    out_abs = _resolve_outdir(outdir) if outdir else RUN_ROOT
    if msr_file:
        files_expr = f"['{msr_file}']"
    else:
        files_expr = f"sorted(glob.glob('{out_abs}/msr/*.msr'))"
    py = (
        "import sys, glob, statistics\n"
        "sys.path.insert(0, '/w/ceph24/JAM/chunyu/fitpack_legacy')\n"
        "from tools.tools import load\n"
        f"files = {files_expr}\n"
        "def tot(f):\n"
        "    d=load(f)\n"
        "    if 'chi2' not in d: raise KeyError('msr has no chi2 (partial save): keys='+str(sorted(d.keys())))\n"
        "    chi=d['chi2']; tn=tc=0.0\n"
        "    for reac,dd in chi.items():\n"
        "        for idx,info in dd.items(): tn+=info['npts']; tc+=info['chi2']\n"
        "    return tc/tn\n"
        "vals=[tot(f) for f in files]\n"
        "print(f'msr files: {len(files)}')\n"
        "for f,v in zip(files,vals): print(f'  {f.split(chr(47))[-2]+chr(47)+f.split(chr(47))[-1]}: chi2/N={v:.3f}')\n"
        "if vals: print(f'OUR chi2/N: mean={statistics.mean(vals):.3f} min={min(vals):.3f} max={max(vals):.3f}')\n"
        "print('PAPER band (arXiv:2501.00665 step16a): mean=2.566 med=2.561 [2.190, 2.819]')\n"
    )
    import base64 as _b64
    py_b64 = _b64.b64encode(py.encode()).decode()
    run = (f"echo {py_b64} | base64 -d | apptainer exec --bind /w/ceph24 "
           f"--env PYTHONPATH={FITPACK} --env FITPACK={FITPACK} {SIF} python3 -")
    out = _remote(run, timeout=240)
    return {"status": "ok", **out}


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
    "get_submit_result": {"desc": "Poll the background test sbatch result and the queue.",
                          "schema": {"type": "object", "properties": {}},
                          "fn": lambda a: get_submit_result()},
    "node_diag": {"desc": "Run a shell command on the gateway node (diagnostics / hot-redeploy).",
                  "schema": {"type": "object",
                             "properties": {"cmd": {"type": "string"}}},
                  "fn": lambda a: node_diag(a.get("cmd", "id; hostname"))},
    "remote_exec": {"desc": "Run a shell command on ifarm through the chain.",
                    "schema": {"type": "object",
                               "properties": {"cmd": {"type": "string"},
                                              "timeout": {"type": "integer"}}},
                    "fn": lambda a: remote_exec(a.get("cmd", "hostname"),
                                                int(a.get("timeout", 120)))},
    "list_jam_runs": {"desc": "List available JAM24 cards, smoke4 run dirs, and msr files.",
                      "schema": {"type": "object", "properties": {}},
                      "fn": lambda a: list_jam_runs()},
    "submit_jam_fit": {
        "desc": "Submit a REAL JAM fit on ifarm (apptainer maxlike_jam). card = card name in "
                "fitpack/inputs/jam24, outdir = output dir (relative to smoke4 or absolute), "
                "prior_msr = optional prior .msr file, cores/seed/hours/partition tunable.",
        "schema": {"type": "object",
                   "properties": {"card": {"type": "string"},
                                  "outdir": {"type": "string"},
                                  "prior_msr": {"type": "string"},
                                  "cores": {"type": "integer"},
                                  "seed": {"type": "integer"},
                                  "hours": {"type": "integer"},
                                  "partition": {"type": "string"}},
                   "required": ["card", "outdir"]},
        "fn": lambda a: submit_jam_fit(
            a.get("card", ""), a.get("outdir", ""), a.get("prior_msr", ""),
            int(a.get("cores", 4)), int(a.get("seed", 160300)),
            int(a.get("hours", 1)), a.get("partition", "jupyter"))},
    "get_jam_result": {
        "desc": "Poll the latest JAM fit: queue, submit log, progress.json tail, msr listing.",
        "schema": {"type": "object",
                   "properties": {"outdir": {"type": "string"},
                                  "job_name": {"type": "string"}}},
        "fn": lambda a: get_jam_result(a.get("outdir", ""), a.get("job_name", "jamfit"))},
    "read_chi2": {
        "desc": "Parse progress.json on ifarm: chi2/N series + per-dataset chi2/N of latest iteration.",
        "schema": {"type": "object",
                   "properties": {"outdir": {"type": "string"}}},
        "fn": lambda a: read_chi2(a.get("outdir", ""))},
    "evaluate_fit": {
        "desc": "Evaluate finished .msr files against the JAM24 paper band (chi2/N 2.566 [2.190,2.819]).",
        "schema": {"type": "object",
                   "properties": {"outdir": {"type": "string"},
                                  "msr_file": {"type": "string"}}},
        "fn": lambda a: evaluate_fit(a.get("outdir", ""), a.get("msr_file", ""))},
}


SESSION_ID = f"jam-gw-{os.getpid()}"
HTTPLOG = "/tmp/jam_http.log"


def _hlog(msg):
    try:
        with open(HTTPLOG, "a") as fh:
            fh.write(f"{time.time():.0f} {msg}\n")
    except OSError:
        pass


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        _hlog(f"GET {self.path} accept={self.headers.get('Accept')!r} "
              f"ua={self.headers.get('User-Agent','')[:40]!r}")
        # MCP streamable-http: a GET with an SSE Accept must open an SSE
        # stream (strict clients like Claude Desktop refuse to connect when
        # this returns anything else — they treat it as "no server here").
        if "text/event-stream" in (self.headers.get("Accept") or ""):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b"event: message\ndata: {}\n\n")
                self.wfile.flush()
                # hold the stream open with heartbeats; the client disconnects
                # when done (ThreadingHTTPServer keeps other requests moving)
                for _ in range(240):
                    time.sleep(15)
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except Exception:
                pass
            return
        body = b"jam-gateway MCP server"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        _hlog(f"POST {self.path} accept={self.headers.get('Accept')!r} "
              f"ua={self.headers.get('User-Agent','')[:40]!r} "
              f"len={self.headers.get('Content-Length')}")
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
                      "serverInfo": {"name": "jam-gateway", "version": "0.2.0"}}
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
        self.send_header("Mcp-Session-Id", SESSION_ID)
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
    print(f"jam-gateway v2 listening on {PORT}, host {socket.getfqdn()}", flush=True)
    if os.environ.get("SLURM_JOB_ID"):
        register()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
