"""Stage-2 v3: JAM gateway on the LQCD cluster node — multi-user.

Zero-dependency MCP server reaching the JAM farm cluster through the
two-hop chain:

    node -> scilogin.jlab.org (PIN+OTP, per-user) -> ifarm (password, per-user)

v3 changes vs v2:
  * connect_hpc takes a username; sockets, forward ports, chain and the
    /w/ceph24/JAM/<username>/... paths are all per-user. Nothing is
    hardcoded to one account — every collaborator connects as themselves
    with their own OTP/password, and jobs run under their own name.
  * The connected user is remembered for the session; every HPC tool also
    accepts an explicit username argument.

Hot-redeploy: node_diag can overwrite /tmp/jam_gateway.py and the
supervisor loop restarts the server inside the same job boot, so the
setsid'd ControlMasters survive — no new OTP.
"""
import base64
import json
import os
import secrets
import socket
import stat
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8080
PROXY = "https://lqcdmcp.jlab.org:8000"
CM_DIR = os.path.expanduser("~/fm-cm")
FA_FWD_PORT_BASE = 2222
SIF = "/u/scigroup/scicomp/jupyterhub/theory/theory-notebook.sif"
# Public console entry on port 443 (trycloudflare quick tunnel). Regenerate
# with: cloudflared tunnel --url https://lqcdmcp.jlab.org:8000 --no-autoupdate
CONSOLE_PUBLIC_URL = "https://seniors-loc-directive-memphis.trycloudflare.com/cloud/jam-gateway/mcp"

# Per-client-session state: session_id -> connected username. NO global
# default user — every client must connect (or pass) its own username.
SESSIONS = {}
_tls = threading.local()


def _fwd_port(user):
    return FA_FWD_PORT_BASE + (zlib.crc32(user.encode()) % 400)


def _socks(user):
    return f"{CM_DIR}/gateway-{user}", f"{CM_DIR}/ifarm-{user}"


def _dests(user):
    return f"{user}@scilogin.jlab.org", f"{user}@localhost"


def _paths(user):
    fp = f"/w/ceph24/JAM/{user}/fitpack_legacy"
    rr = f"/w/ceph24/JAM/{user}/jam24repro/smoke4"
    return fp, rr, f"{rr}/logs"


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


def _remote(user, cmd, timeout=120):
    gw, fa = _socks(user)
    dest = f"{user}@localhost"
    port = str(_fwd_port(user))
    return _run(["ssh", "-S", fa, "-p", port, dest, "bash -lc " + _shq(cmd)],
                timeout=timeout)


def _chain_ok(user):
    gw, fa = _socks(user)
    gwd, fad = _dests(user)
    return (_master_alive(gw, gwd)
            and _master_alive(fa, fad, ["-p", str(_fwd_port(user))]))


def _resolve_user(username):
    u = (username or "").strip() or SESSIONS.get(getattr(_tls, "sid", ""), "")
    if not u:
        return "", {"status": "error",
                    "message": "no username known for this session; call connect_hpc "
                               "with username = the user's JLab account (ASK the user — "
                               "never assume a name)"}
    if not u.replace(".", "").replace("_", "").replace("-", "").isalnum() or len(u) > 32:
        return "", {"status": "error", "message": "invalid username; use letters/numbers/._- only"}
    return u, None


def connect_hpc(username: str = "", pin_otp: str = "", password: str = "") -> dict:
    """Build (or reuse) the per-user two-hop chain node->scilogin->ifarm."""
    u, err = _resolve_user(username)
    if err:
        return err
    sid = getattr(_tls, "sid", "")
    if sid:
        SESSIONS[sid] = u
    os.makedirs(CM_DIR, mode=0o700, exist_ok=True)
    gw, fa = _socks(u)
    gwd, fad = _dests(u)
    port = str(_fwd_port(u))
    # reuse check FIRST: unlink only sockets whose master is dead
    if _chain_ok(u):
        check = _remote(u, "hostname; whoami; date -u", timeout=30)
        return {"status": "reused", "username": u,
                "note": "existing ControlMasters alive — no OTP needed",
                "remote": check["out"][:200], "check": check["rc"]}
    for s in (gw, fa):
        if os.path.exists(s):
            try:
                os.unlink(s)
            except OSError:
                pass
    if not pin_otp or not password:
        return {"status": "error", "username": u,
                "message": f"no live chain for {u}; need pin_otp (PIN+one-time code, no space) and password. Alternatively give the user the web console to sign in interactively (see get_console)"}

    # hop 1: gateway (PIN+OTP)
    r1 = _run(["setsid", "ssh", "-M", "-S", gw, "-o", "ControlPersist=8h",
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "NumberOfPasswordPrompts=2",
               "-o", "PubkeyAuthentication=no",
               "-o", "PreferredAuthentications=keyboard-interactive,password",
               "-fN", gwd], timeout=40, secret=pin_otp)
    if not _master_alive(gw, gwd):
        return {"status": "error", "step": "gateway", "username": u,
                "message": f"scilogin failed: {r1['err'][-400:] or r1['out'][-200:]}"}

    # local forward over the gateway master: <per-user port> -> ifarm:22
    r2 = _run(["ssh", "-S", gw, "-O", "forward",
               "-L", f"127.0.0.1:{port}:ifarm.jlab.org:22", gwd],
              timeout=20)
    if r2["rc"] != 0:
        return {"status": "error", "step": "forward", "username": u,
                "message": r2["err"][-300:]}

    # hop 2: ifarm through the forward (password)
    r3 = _run(["setsid", "ssh", "-M", "-S", fa, "-o", "ControlPersist=8h",
               "-o", "StrictHostKeyChecking=no",
               "-o", "NumberOfPasswordPrompts=2",
               "-o", "PubkeyAuthentication=no",
               "-o", "PreferredAuthentications=keyboard-interactive,password",
               "-fN", "-p", port, fad], timeout=40, secret=password)
    if not _master_alive(fa, fad, ["-p", port]):
        return {"status": "error", "step": "ifarm", "username": u,
                "message": f"ifarm failed: {r3['err'][-400:] or r3['out'][-200:]}"}

    check = _remote(u, "hostname; whoami; date -u", timeout=60)
    return {"status": "ok", "username": u, "chain": "node->scilogin->ifarm",
            "remote": check["out"][:300], "check": check["rc"]}


def _need_user(username):
    u, err = _resolve_user(username)
    if err:
        return u, err
    if not _chain_ok(u):
        return u, {"status": "disconnected", "username": u,
                   "message": f"no live chain for {u}; call connect_hpc first"}
    return u, None


def check_hpc(username: str = "") -> dict:
    u, err = _need_user(username)
    if err:
        return err
    fp, rr, ld = _paths(u)
    out = _remote(u, f"hostname; squeue -u {u} -o '%.10i %.9P %.12j %.2t' | head -8; "
                  f"echo ---; ls /w/ceph24/JAM/{u} 2>&1 | head -10", timeout=90)
    return {"status": "ok", "username": u, **out}


def submit_test_fit(username: str = "") -> dict:
    u, err = _need_user(username)
    if err:
        return err
    script = ("#!/bin/bash\n#SBATCH --partition=priority\n#SBATCH --time=00:05:00\n"
              "hostname; date; sleep 30\n")
    b64 = base64.b64encode(script.encode()).decode()
    out = _remote(u, f"echo {b64} | base64 -d > /tmp/fm_chain_test.sh && "
                  "nohup bash -c 'sbatch /tmp/fm_chain_test.sh' "
                  ">/tmp/fm_submit.log 2>&1 </dev/null & echo FIRED", timeout=15)
    return {"status": "fired", "username": u, **out}


def get_submit_result(username: str = "") -> dict:
    u, err = _need_user(username)
    if err:
        return err
    out = _remote(u, "sleep 3; echo --- submit log ---; cat /tmp/fm_submit.log 2>&1; "
                  f"echo --- queue ---; squeue -u {u} -o '%.10i %.9P %.12j %.2t' | head -6",
                  timeout=20)
    return {"status": "ok", "username": u, **out}


def node_diag(cmd: str = "id; hostname") -> dict:
    """Run a shell command ON THE GATEWAY NODE (diagnostics / hot-redeploy)."""
    r = _run(["bash", "-lc", cmd], timeout=30)
    return {"rc": r["rc"], "out": r["out"], "err": r["err"]}


# ================= real JAM fitpack workflow =================

def remote_exec(cmd: str = "hostname", timeout: int = 120, username: str = "") -> dict:
    u, err = _need_user(username)
    if err:
        return err
    out = _remote(u, cmd, timeout=timeout)
    return {"status": "ok", "username": u, **out}


def list_jam_runs(username: str = "") -> dict:
    u, err = _need_user(username)
    if err:
        return err
    fp, rr, ld = _paths(u)
    out = _remote(
        u,
        f"echo '--- cards ---'; ls {fp}/inputs/jam24 2>&1 | head -20; "
        f"echo '--- run dirs ---'; ls {rr}/001 2>&1 | head -20; "
        f"echo '--- msr files (newest 8) ---'; "
        f"find {rr} -name '*.msr' -printf '%T@ %p\\n' 2>/dev/null | sort -rn | head -8 | cut -d' ' -f2-",
        timeout=60)
    return {"status": "ok", "username": u, **out}


def _resolve_outdir(u, outdir):
    _, rr, _ = _paths(u)
    if outdir.startswith("/"):
        return outdir
    return f"{rr}/{outdir}"


def submit_jam_fit(card: str = "", outdir: str = "", prior_msr: str = "",
                   cores: int = 4, seed: int = 160300, hours: int = 10,
                   partition: str = "jupyter", username: str = "") -> dict:
    u, err = _need_user(username)
    if err:
        return err
    if not card or not outdir:
        return {"status": "error", "message": "need card and outdir"}
    fp, rr, ld = _paths(u)
    out_abs = _resolve_outdir(u, outdir)
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
#SBATCH --output={ld}/jamfit_%j.out
#SBATCH --error={ld}/jamfit_%j.err

export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1
FP={fp}
cd $FP || exit 1
OUT={out_abs}
mkdir -p "$OUT"
echo "===== JAM FIT card={card} out=$OUT cores={cores} seed={seed} prior={prior_msr} ====="
apptainer exec --bind /w/ceph24 \\
  --env PYTHONPATH=$FP:/usr/local/lib/python3.11/site-packages \\
  --env FITPACK=$FP \\
  --env LHAPDF_DATA_PATH=/w/ceph24/JAM/{u}/lhapdf_tables:/w/ceph24/JAM/apps/lhapdf_tables/more/more1 \\
  {SIF} \\
  python3 -m fitlib.maxlike_jam $FP/inputs/jam24/{card} "$OUT" -n {cores} -v 10 -s {seed} {prior_flag}
echo "exit=$?"
ls "$OUT"/msr/ 2>/dev/null
"""
    b64 = base64.b64encode(script.encode()).decode()
    out = _remote(u, f"echo {b64} | base64 -d > /tmp/jam_fit_latest.sh && "
                  "nohup bash -c 'sbatch /tmp/jam_fit_latest.sh' "
                  ">/tmp/jam_fit_latest.submit 2>&1 </dev/null & echo FIRED", timeout=20)
    return {"status": "fired", "username": u, "card": card, "outdir": out_abs, **out}


def get_jam_result(outdir: str = "", job_name: str = "jamfit", username: str = "") -> dict:
    u, err = _need_user(username)
    if err:
        return err
    fp, rr, ld = _paths(u)
    out_abs = _resolve_outdir(u, outdir) if outdir else rr
    cmd = (
        f"echo '--- queue ---'; squeue -u {u} -o '%.10i %.9P %.12j %.2t %.10M' | head -8; "
        f"echo '--- submit log ---'; cat /tmp/jam_fit_latest.submit 2>&1; "
        f"echo '--- progress ---'; ls -t {out_abs}/progress.json 2>/dev/null | head -1 | xargs -r tail -c 1500; "
        f"echo; echo '--- msr ---'; ls {out_abs}/msr 2>&1 | head -5; "
        f"echo '--- newest slurm err tail ---'; ls -t {ld}/jamfit_*.err 2>/dev/null | head -1 | xargs -r tail -12"
    )
    out = _remote(u, cmd, timeout=60)
    return {"status": "ok", "username": u, **out}


def read_chi2(outdir: str = "", username: str = "") -> dict:
    u, err = _need_user(username)
    if err:
        return err
    out_abs = _resolve_outdir(u, outdir) if outdir else _paths(u)[1]
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
    import base64 as _b64
    py_b64 = _b64.b64encode(py.encode()).decode()
    out = _remote(u, f"echo {py_b64} | base64 -d | python3", timeout=120)
    return {"status": "ok", "username": u, **out}


def evaluate_fit(outdir: str = "", msr_file: str = "", username: str = "") -> dict:
    u, err = _need_user(username)
    if err:
        return err
    fp, rr, ld = _paths(u)
    out_abs = _resolve_outdir(u, outdir) if outdir else rr
    if msr_file:
        files_expr = f"['{msr_file}']"
    else:
        files_expr = f"sorted(glob.glob('{out_abs}/msr/*.msr'))"
    py = (
        "import sys, glob, statistics\n"
        f"sys.path.insert(0, '{fp}')\n"
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
           f"--env PYTHONPATH={fp} --env FITPACK={fp} {SIF} python3 -")
    out = _remote(u, run, timeout=240)
    return {"status": "ok", "username": u, **out}


def get_console() -> dict:
    """Return the web console URL(s) the user can open in a browser to sign
    in and pick cards/priors interactively."""
    return {"console_urls": [
        {"entry": "direct", "url": "https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp"},
        {"entry": "port-443 tunnel (works from Claude Desktop and restricted networks)",
         "url": CONSOLE_PUBLIC_URL},
    ]}


def _u(username, fn):
    return fn(username)


TOOLS = {
    "ping": {"desc": "Reply pong.", "schema": {"type": "object", "properties": {}},
             "fn": lambda a: "pong from LQCD gateway node"},
    "get_console": {
        "desc": "Return the web console URL. When the user wants to sign in, pick a card, "
                "choose a prior, or submit/check a fit interactively, give them this URL "
                "to open in a browser instead of asking them to type answers in chat.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: get_console()},
    "connect_hpc": {
        "desc": "Establish (or reuse) the per-user two-hop chain node->scilogin->ifarm. "
                "IMPORTANT: first call with username ONLY, no pin_otp, no password. "
                "If the reply is status=reused, the chain is already live and NO "
                "credentials are needed — do not ask the user for any. Only when the "
                "reply says it needs pin_otp should you collect from the user: their "
                "ifarm password plus PIN+one-time-code concatenated with no space. "
                "Never assume the username — always ask the user.",
        "schema": {"type": "object",
                   "properties": {"username": {"type": "string"},
                                  "pin_otp": {"type": "string"},
                                  "password": {"type": "string"}},
                   "required": ["username"]},
        "fn": lambda a: connect_hpc(a.get("username", ""), a.get("pin_otp", ""),
                                    a.get("password", ""))},
    "check_hpc": {"desc": "Verify the chain and show the connected user's queue plus their JAM directory.",
                  "schema": {"type": "object",
                             "properties": {"username": {"type": "string"}}},
                  "fn": lambda a: check_hpc(a.get("username", ""))},
    "submit_test_fit": {"desc": "Submit a trivial test job on ifarm through the chain (fire-and-forget).",
                        "schema": {"type": "object",
                                   "properties": {"username": {"type": "string"}}},
                        "fn": lambda a: submit_test_fit(a.get("username", ""))},
    "get_submit_result": {"desc": "Poll the background test sbatch result and the queue.",
                          "schema": {"type": "object",
                                     "properties": {"username": {"type": "string"}}},
                          "fn": lambda a: get_submit_result(a.get("username", ""))},
    "node_diag": {"desc": "Run a shell command on the gateway node (diagnostics / hot-redeploy).",
                  "schema": {"type": "object",
                             "properties": {"cmd": {"type": "string"}}},
                  "fn": lambda a: node_diag(a.get("cmd", "id; hostname"))},
    "remote_exec": {"desc": "Run a shell command on ifarm through the connected user's chain.",
                    "schema": {"type": "object",
                               "properties": {"cmd": {"type": "string"},
                                              "timeout": {"type": "integer"},
                                              "username": {"type": "string"}}},
                    "fn": lambda a: remote_exec(a.get("cmd", "hostname"),
                                                int(a.get("timeout", 120)),
                                                a.get("username", ""))},
    "list_jam_runs": {"desc": "List the connected user's JAM24 cards, run dirs, and msr files.",
                      "schema": {"type": "object",
                                 "properties": {"username": {"type": "string"}}},
                      "fn": lambda a: list_jam_runs(a.get("username", ""))},
    "submit_jam_fit": {
        "desc": "Submit a REAL JAM fit on ifarm as the connected user (apptainer maxlike_jam). "
                "card = card name in fitpack/inputs/jam24, outdir = output dir (relative to "
                "smoke4 or absolute), prior_msr = optional prior .msr file. "
                "NOTE: a cold start (no prior) needs a LONG budget — the reference "
                "ladder ran 10 hours per step on 4 cores (jupyter partition), so pass "
                "hours>=10 for cold starts; warm starts can use 1-2.",
        "schema": {"type": "object",
                   "properties": {"card": {"type": "string"},
                                  "outdir": {"type": "string"},
                                  "prior_msr": {"type": "string"},
                                  "cores": {"type": "integer"},
                                  "seed": {"type": "integer"},
                                  "hours": {"type": "integer"},
                                  "partition": {"type": "string"},
                                  "username": {"type": "string"}},
                   "required": ["card", "outdir"]},
        "fn": lambda a: submit_jam_fit(
            a.get("card", ""), a.get("outdir", ""), a.get("prior_msr", ""),
            int(a.get("cores", 4)), int(a.get("seed", 160300)),
            int(a.get("hours", 10)), a.get("partition", "jupyter"),
            a.get("username", ""))},
    "get_jam_result": {
        "desc": "Poll the latest JAM fit: queue, submit log, progress.json tail, msr listing.",
        "schema": {"type": "object",
                   "properties": {"outdir": {"type": "string"},
                                  "job_name": {"type": "string"},
                                  "username": {"type": "string"}}},
        "fn": lambda a: get_jam_result(a.get("outdir", ""), a.get("job_name", "jamfit"),
                                       a.get("username", ""))},
    "read_chi2": {
        "desc": "Parse progress.json on ifarm: chi2/N series + per-dataset chi2/N.",
        "schema": {"type": "object",
                   "properties": {"outdir": {"type": "string"},
                                  "username": {"type": "string"}}},
        "fn": lambda a: read_chi2(a.get("outdir", ""), a.get("username", ""))},
    "evaluate_fit": {
        "desc": "Evaluate finished .msr files against the JAM24 paper band (chi2/N 2.566 [2.190,2.819]).",
        "schema": {"type": "object",
                   "properties": {"outdir": {"type": "string"},
                                  "msr_file": {"type": "string"},
                                  "username": {"type": "string"}}},
        "fn": lambda a: evaluate_fit(a.get("outdir", ""), a.get("msr_file", ""),
                                     a.get("username", ""))},
}


HTTPLOG = "/tmp/jam_http.log"


def _hlog(msg):
    try:
        with open(HTTPLOG, "a") as fh:
            fh.write(f"{time.time():.0f} {msg}\n")
    except OSError:
        pass


def _req_sid(hdrs):
    sid = (hdrs.get("Mcp-Session-Id") or hdrs.get("mcp-session-id") or "").strip()
    if not sid:
        sid = "jam-" + secrets.token_hex(8)
        SESSIONS.setdefault(sid, "")
    return sid


CONSOLE_HTML = '<!doctype html><html><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>JAM Gateway Console</title>\n<style>\nbody{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;\nmargin:0;background:#f6f7f9;color:#15202b}\n.wrap{max-width:860px;margin:0 auto;padding:24px}\nh1{font-size:20px;margin:0 0 4px}.sub{color:#5c6774;font-size:13px;margin-bottom:18px}\n.card{background:#fff;border:1px solid #e0e4ea;border-radius:12px;padding:18px;margin-bottom:14px}\n.card h2{font-size:15px;margin:0 0 10px}\nlabel{display:block;font-size:12.5px;font-weight:600;margin:8px 0 3px}\ninput,select{width:100%;font:inherit;padding:8px 10px;border:1px solid #ccd3dc;\nborder-radius:8px;box-sizing:border-box}\nbutton{font:inherit;font-weight:600;padding:8px 16px;border:0;border-radius:8px;\nbackground:#1d5fd0;color:#fff;cursor:pointer;margin-top:10px;margin-right:8px}\nbutton.ghost{background:#eef1f5;color:#15202b}\npre{background:#0f1319;color:#e8ecf2;padding:12px;border-radius:8px;font-size:12px;\nmax-height:300px;overflow:auto;white-space:pre-wrap}\n.row{display:flex;gap:10px}.row>div{flex:1}\n#st{font-size:13px;color:#1d5fd0;margin-top:8px}\n.err{color:#c0392b}\n</style></head><body><div class="wrap">\n<h1>JAM Gateway Console</h1>\n<div class="sub">qcd24s100 &rarr; scilogin &rarr; ifarm &middot; 每步都在这页点选,不用聊天</div>\n<div class="card">\n<h2>1 &middot; 连接(先只填用户名;链活着就不需要凭证)</h2>\n<label>JLab 用户名</label><input id="user" placeholder="chunyu">\n<label id="otplab" style="display:none">PIN+OTP(仅当提示需要时)</label>\n<input id="otp" style="display:none">\n<label id="pwlab" style="display:none">ifarm 密码(仅当提示需要时)</label>\n<input id="pw" type="password" style="display:none">\n<button onclick="doConnect()">连接</button>\n<button class="ghost" onclick="call(\'check_hpc\',{})">检查 HPC</button>\n<button class="ghost" onclick="call(\'list_jam_runs\',{})">清点 JAM 工作区</button>\n<div id="st"></div>\n</div>\n<div class="card">\n<h2>2 &middot; 提交拟合(点选)</h2>\n<div class="row"><div>\n<label>卡 card</label>\n<select id="card">\n<option>input_jam24_s1.py</option><option>input_jam24_s1_warm.py</option>\n<option>input_jam24_s2.py</option><option>input_jam24_s2_seed.py</option><option>input_jam24_s2_warm.py</option>\n<option>input_jam24_s3.py</option><option>input_jam24_s3_seed.py</option><option>input_jam24_s3_warm.py</option>\n<option>input_jam24_s4.py</option><option>input_jam24_s4_seed.py</option><option>input_jam24_s4_warm.py</option>\n<option>input_jam24_s5.py</option><option>input_jam24_s5_seed.py</option><option>input_jam24_s5_warm.py</option>\n<option>input_jam24_s6.py</option><option>input_jam24_s6_seed.py</option><option>input_jam24_s6_warm.py</option>\n<option>input_jam24_s7.py</option><option>input_jam24_s7_seed.py</option><option selected>input_jam24_s7_warm.py</option>\n</select>\n</div><div>\n<label>输出目录 outdir(相对 smoke4)</label><input id="outdir" placeholder="001/step7_claude">\n</div></div>\n<div class="row"><div>\n<label>prior .msr(留空 = 冷启动;冷启动请把 hours 设为 10+)</label>\n<input id="prior" placeholder="/w/ceph24/JAM/chunyu/jam24repro/smoke4/001/step6_v2/msr/8XW0AVCIGLJ8.msr">\n</div><div>\n<label>预算 hours</label><input id="hours" value="2" type="number">\n</div></div>\n<div class="row"><div>\n<label>cores</label><input id="cores" value="4" type="number">\n</div><div>\n<label>分区</label>\n<select id="part"><option selected>jupyter</option><option>production</option><option>priority</option><option>ifarm</option></select>\n</div></div>\n<button onclick="submitFit()">提交拟合</button>\n<button class="ghost" onclick="call(\'get_jam_result\',{outdir:val(\'outdir\')})">查状态</button>\n<button class="ghost" onclick="call(\'read_chi2\',{outdir:val(\'outdir\')})">读 &chi;²</button>\n<button class="ghost" onclick="call(\'evaluate_fit\',{outdir:val(\'outdir\')})">对照论文 band</button>\n</div>\n<div class="card"><h2>3 &middot; 输出</h2><pre id="out">(点上面任意按钮,结果显示在这里)</pre></div>\n</div>\n<script>\nvar sid="";\nfunction val(id){return document.getElementById(id).value.trim();}\nfunction show(id,s){document.getElementById(id).style.display=s?"":"none";}\nfunction setSt(t,c){var e=document.getElementById("st");e.textContent=t;e.className=c||"";}\nasync function call(tool,args){\n  setSt("调用 "+tool+" &hellip;");\n  try{\n    var hdrs={"Content-Type":"application/json","Accept":"application/json, text/event-stream"};\n    if(sid)hdrs["Mcp-Session-Id"]=sid;\n    var r=await fetch(location.pathname,{method:"POST",headers:hdrs,\n      body:JSON.stringify({jsonrpc:"2.0",id:Date.now()%100000,method:"tools/call",\n      params:{name:tool,arguments:args}})});\n    var h=r.headers.get("Mcp-Session-Id"); if(h)sid=h;\n    var d=await r.json();\n    var t=(d.result&&d.result.content)?d.result.content.map(function(c){return c.text||"";}).join("\\n"):JSON.stringify(d,null,1);\n    document.getElementById("out").textContent=t;\n    setSt("完成 "+tool,"");\n    return t;\n  }catch(e){setSt("失败: "+e,"err");document.getElementById("out").textContent=String(e);}\n}\nasync function doConnect(){\n  var u=val("user"); if(!u){setSt("先填用户名","err");return;}\n  var t=await call("connect_hpc",{username:u});\n  if(t&&t.indexOf("need pin_otp")>=0){\n    show("otplab",true);show("otp",true);show("pwlab",true);show("pw",true);\n    setSt("链不活 &mdash; 需要凭证,填 PIN+OTP 和密码后再点一次连接","err");\n  } else if(t&&t.indexOf("reused")>=0){setSt("链已复用,无需凭证");}\n  else if(val("otp")&&val("pw")){\n    await call("connect_hpc",{username:u,pin_otp:val("otp"),password:val("pw")});\n  }\n}\nasync function submitFit(){\n  await call("submit_jam_fit",{card:val("card"),outdir:val("outdir"),prior_msr:val("prior"),\n    cores:parseInt(val("cores")||"4"),hours:parseInt(val("hours")||"10"),partition:val("part")});\n}\n</script></body></html>\n'


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        _hlog(f"GET {self.path} accept={self.headers.get('Accept')!r} "
              f"ua={self.headers.get('User-Agent','')[:40]!r}")
        if "text/html" in (self.headers.get("Accept") or ""):
            body = CONSOLE_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if "text/event-stream" in (self.headers.get("Accept") or ""):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b"event: message\ndata: {}\n\n")
                self.wfile.flush()
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
        sid = _req_sid(self.headers)
        _tls.sid = sid
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
                      "serverInfo": {"name": "jam-gateway", "version": "0.3.0"}}
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
        self.send_header("Mcp-Session-Id", sid)
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
    print(f"jam-gateway v3 listening on {PORT}, host {socket.getfqdn()}", flush=True)
    if os.environ.get("SLURM_JOB_ID"):
        register()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
