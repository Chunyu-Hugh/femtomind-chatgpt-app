"""jam_gateway v6 — clean single-file MCP gateway for JAM fits on JLab.

Runs on an LQCD cluster node (qcd24s100) as a Slurm job via the LQCDMCP
platform. Zero dependencies (stdlib only). Speaks plain JSON-RPC MCP.

Architecture (flat, one section per concern):
  constants        host, paths, ports
  local helpers    _run / _master_alive / _shq
  remote helpers   per-user sockets/ports/paths + _remote + _run_ifarm_py
  session          per-client user memory (Mcp-Session-Id)
  chain            connect_hpc (two-hop, per user)
  tools            all 23 tools (one flat function each)
  http             GET (SSE / console / composer) + POST (JSON-RPC)
  boot             self-registration with the LQCDMCP proxy

The web console and the fit composer are embedded as HTML constants at
build time (the editable sources are console.html / composer_page.html).
"""

import base64
import json
import os
import re
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

# ── constants ──────────────────────────────────────────────────────────────

PORT = 8080
PROXY = "https://lqcdmcp.jlab.org:8000"
CM_DIR = os.path.expanduser("~/fm-cm")          # ControlMaster sockets
FA_FWD_PORT_BASE = 2222                          # + crc32(user) % 400
SIF = "/u/scigroup/scicomp/jupyterhub/theory/theory-notebook.sif"
GATEWAY_OWNER = os.environ.get("JAM_GW_OWNER", "chunyu")
HTTPLOG = "/tmp/jam_http.log"
# Public console entry on port 443 (trycloudflare quick tunnel; regenerate:
#   cloudflared tunnel --url https://lqcdmcp.jlab.org:8000 --no-autoupdate)
CONSOLE_PUBLIC_URL = "https://harvest-simply-aud-launch.trycloudflare.com/cloud/jam-gateway/mcp"

# Card dataset-line pattern: conf['datasets']['<reac>']['xlsx'][<id>] = ...
DATASET_LINE_RE = r"conf\['datasets'\]\['(\w+)'\]\['xlsx'\]\[(\w+)\]\s*="

# ── local helpers ───────────────────────────────────────────────────────────

def _run(cmd, timeout=90, secret=None, cap=4000):
    """Run a local command; optionally feed ONE secret via SSH_ASKPASS."""
    env = dict(os.environ)
    helper = None
    if secret is not None:
        helper = tempfile.NamedTemporaryFile("w", suffix="_askpass.sh",
                                             delete=False, dir=os.path.expanduser("~"))
        helper.write('#!/bin/sh\nprintf "%s\\n" "$FEMTO_SECRET"\n')
        helper.close()
        os.chmod(helper.name, stat.S_IRWXU)
        env.update(FEMTO_SECRET=secret, SSH_ASKPASS=helper.name,
                   SSH_ASKPASS_REQUIRE="force", DISPLAY=":0")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
    finally:
        if helper:
            try:
                os.unlink(helper.name)
            except OSError:
                pass
    return {"rc": p.returncode, "out": (p.stdout or "")[-cap:],
            "err": (p.stderr or "")[-800:]}


def _master_alive(sock, dest, opts=None):
    if not os.path.exists(sock):
        return False
    return _run(["ssh", "-S", sock, "-O", "check"] + (opts or []) + [dest],
                timeout=15)["rc"] == 0


def _shq(s):
    """Single-quote a string for safe interpolation into a remote shell."""
    return "'" + s.replace("'", "'\\''") + "'"


# ── per-user remote helpers ─────────────────────────────────────────────────

def _fwd_port(user):
    return FA_FWD_PORT_BASE + (zlib.crc32(user.encode()) % 400)


def _socks(user):
    return f"{CM_DIR}/gateway-{user}", f"{CM_DIR}/ifarm-{user}"


_WS_CACHE = {}


def _workspace(user):
    """The user's workspace = the folder they git-cloned (one folder = everything).
    Recorded in /w/ceph24/JAM/<user>/.jam_workspace at setup time; the default
    is the conventional clone target."""
    if user not in _WS_CACHE:
        r = _remote(user, f"cat /w/ceph24/JAM/{user}/.jam_workspace 2>/dev/null", timeout=30)
        _WS_CACHE[user] = (r.get("out") or "").strip() or f"/w/ceph24/JAM/{user}/fitpack_legacy"
    return _WS_CACHE[user]


def _paths(user):
    """(workspace root, runs root, logs dir) — cards live in ws/inputs/jam24,
    runs live INSIDE the clone at ws/runs."""
    ws = _workspace(user)
    return ws, f"{ws}/runs", f"{ws}/runs/logs"


def _remote(user, cmd, timeout=120, cap=4000):
    """Run a shell command on ifarm through the user's chain."""
    _, fa = _socks(user)
    return _run(["ssh", "-S", fa, "-p", str(_fwd_port(user)), f"{user}@localhost",
                 "bash -lc " + _shq(cmd)], timeout=timeout, cap=cap)


def _run_ifarm_py(user, code, apptainer=False, timeout=300, cap=4000):
    """Ship a python snippet to ifarm as base64 and run it (avoids all
    shell-quoting issues). apptainer=True runs inside the theory image
    (needed for fitpack/numpy imports)."""
    b64 = base64.b64encode(code.encode()).decode()
    if apptainer:
        fp = _paths(user)[0]
        run = (f"echo {b64} | base64 -d | apptainer exec --bind /w/ceph24 "
               f"--env PYTHONPATH={fp} --env FITPACK={fp} {SIF} python3 -")
    else:
        run = f"echo {b64} | base64 -d | python3"
    return _remote(user, run, timeout=timeout, cap=cap)


def _resolve_outdir(user, outdir):
    if outdir.startswith("/"):
        return outdir
    return f"{_paths(user)[1]}/{outdir}"


# ── per-client session state ────────────────────────────────────────────────

SESSIONS = {}                 # mcp-session-id -> username (no global default)
_tls = threading.local()


def _req_sid(hdrs):
    sid = (hdrs.get("Mcp-Session-Id") or hdrs.get("mcp-session-id") or "").strip()
    if not sid:
        sid = "jam-" + secrets.token_hex(8)
        SESSIONS.setdefault(sid, "")
    return sid


def _resolve_user(username):
    u = (username or "").strip() or SESSIONS.get(getattr(_tls, "sid", ""), "")
    if not u:
        return "", {"status": "error",
                    "message": "no username known for this session; call connect_hpc "
                               "with username = the user's JLab account (ASK the user — "
                               "never assume a name)"}
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", u):
        return "", {"status": "error", "message": "invalid username; letters/numbers/._- only"}
    return u, None


def _need_user(username):
    """Resolve the acting user AND require a live chain."""
    u, err = _resolve_user(username)
    if err:
        return u, err
    gw, fa = _socks(u)
    if not (_master_alive(gw, f"{u}@scilogin.jlab.org")
            and _master_alive(fa, f"{u}@localhost", ["-p", str(_fwd_port(u))])):
        return u, {"status": "disconnected", "username": u,
                   "message": f"no live chain for {u}; call connect_hpc first"}
    return u, None


# ── chain ───────────────────────────────────────────────────────────────────

def connect_hpc(username="", pin_otp="", password=""):
    u, err = _resolve_user(username)
    if err:
        return err
    sid = getattr(_tls, "sid", "")
    if sid:
        SESSIONS[sid] = u
    os.makedirs(CM_DIR, mode=0o700, exist_ok=True)
    gw, fa = _socks(u)
    gwd = f"{u}@scilogin.jlab.org"
    port = str(_fwd_port(u))
    # reuse FIRST — never unlink a live socket
    if _master_alive(gw, gwd) and _master_alive(fa, f"{u}@localhost", ["-p", port]):
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
                "message": f"no live chain for {u}; need pin_otp (PIN+one-time code, "
                           "no space) and password. Alternatively give the user the "
                           "web console to sign in interactively (see get_console)"}
    hop_opts = ["-o", "ControlPersist=8h", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "NumberOfPasswordPrompts=2", "-o", "PubkeyAuthentication=no",
                "-o", "PreferredAuthentications=keyboard-interactive,password"]
    r1 = _run(["setsid", "ssh", "-M", "-S", gw, *hop_opts, "-fN", gwd],
              timeout=40, secret=pin_otp)
    if not _master_alive(gw, gwd):
        return {"status": "error", "step": "gateway", "username": u,
                "message": f"scilogin failed: {r1['err'][-400:] or r1['out'][-200:]}"}
    r2 = _run(["ssh", "-S", gw, "-O", "forward",
               "-L", f"127.0.0.1:{port}:ifarm.jlab.org:22", gwd], timeout=20)
    if r2["rc"] != 0:
        return {"status": "error", "step": "forward", "username": u,
                "message": r2["err"][-300:]}
    r3 = _run(["setsid", "ssh", "-M", "-S", fa, *hop_opts, "-fN",
               "-p", port, f"{u}@localhost"], timeout=40, secret=password)
    if not _master_alive(fa, f"{u}@localhost", ["-p", port]):
        return {"status": "error", "step": "ifarm", "username": u,
                "message": f"ifarm failed: {r3['err'][-400:] or r3['out'][-200:]}"}
    check = _remote(u, "hostname; whoami; date -u", timeout=60)
    return {"status": "ok", "username": u, "chain": "node->scilogin->ifarm",
            "remote": check["out"][:300], "check": check["rc"]}


# ── tools (flat, one per function) ──────────────────────────────────────────

def ping():
    return "pong from LQCD gateway node"


def get_console():
    base = f"{PROXY}/cloud/jam-gateway/mcp"
    return {
        "console_urls": [
            {"entry": "direct", "url": base},
            {"entry": "port-443 tunnel (works from Claude Desktop / restricted networks)",
             "url": CONSOLE_PUBLIC_URL}],
        "composer_urls": [
            {"entry": "direct", "url": base + "?ui=composer"},
            {"entry": "port-443 tunnel", "url": CONSOLE_PUBLIC_URL + "?ui=composer"}],
        "guidance": "When the user asks to set up / build / pick a fit (choose reactions, "
                    "datasets, object, QCD settings), hand them a composer_url to open in "
                    "their browser — the page is a tabbed input-card UI (Non-perturbative "
                    "object / Reactions / QCD settings / Job config / Review) that submits "
                    "through this gateway. Use a console_url for sign-in and quick "
                    "status/chi2 checks."}


def check_hpc(username=""):
    u, err = _need_user(username)
    if err:
        return err
    out = _remote(u, f"hostname; squeue -u {u} -o '%.10i %.9P %.12j %.2t' | head -8; "
                  f"echo ---; ls /w/ceph24/JAM/{u} 2>&1 | head -10", timeout=90)
    return {"status": "ok", "username": u, **out}


def submit_test_fit(username=""):
    """Trivial fire-and-forget chain smoke test (kept for first-time checks)."""
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


def get_submit_result(username=""):
    u, err = _need_user(username)
    if err:
        return err
    out = _remote(u, "sleep 3; echo --- submit log ---; cat /tmp/fm_submit.log 2>&1; "
                  f"echo --- queue ---; squeue -u {u} -o '%.10i %.9P %.12j %.2t' | head -6",
                  timeout=20)
    return {"status": "ok", "username": u, **out}


def node_diag(cmd="id; hostname"):
    """Shell ON THE GATEWAY NODE (hot-redeploy + diagnostics)."""
    r = _run(["bash", "-lc", cmd], timeout=30)
    return {"rc": r["rc"], "out": r["out"], "err": r["err"]}


def remote_exec(cmd="hostname", timeout=120, username=""):
    u, err = _need_user(username)
    if err:
        return err
    out = _remote(u, cmd, timeout=timeout)
    return {"status": "ok", "username": u, **out}


def list_jam_runs(username=""):
    u, err = _need_user(username)
    if err:
        return err
    fp, rr, _ = _paths(u)
    out = _remote(u,
        f"echo '--- cards ---'; ls {fp}/inputs/jam24 2>&1 | head -20; "
        f"echo '--- run dirs ---'; ls {rr}/001 2>&1 | head -20; "
        f"echo '--- msr files (newest 8) ---'; "
        f"find {rr} -name '*.msr' -printf '%T@ %p\\n' 2>/dev/null | sort -rn | head -8 | cut -d' ' -f2-",
        timeout=60)
    return {"status": "ok", "username": u, **out}


def fs_list(path="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    p = path.strip() or f"/w/ceph24/JAM/{u}"
    if not p.startswith("/w/ceph24"):
        return {"status": "error", "message": "path must be under /w/ceph24"}
    out = _remote(u, f"ls -la {_shq(p)} 2>&1 | head -60", timeout=60)
    return {"status": "ok", "username": u, "path": p, **out}


def fs_read(path="", max_lines=200, username=""):
    u, err = _need_user(username)
    if err:
        return err
    p = path.strip()
    if not p.startswith("/w/ceph24"):
        return {"status": "error", "message": "path must be under /w/ceph24"}
    ml = max(1, min(int(max_lines), 500))
    out = _remote(u, f"head -n {ml} {_shq(p)} 2>&1", timeout=60)
    return {"status": "ok", "username": u, "path": p, **out}


def fs_write(path="", content="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    p = os.path.normpath(path.strip())
    root = f"/w/ceph24/JAM/{u}"
    if not p.startswith(root + "/"):
        return {"status": "error", "username": u,
                "message": f"write restricted to your own area under {root}"}
    b64 = base64.b64encode((content or "").encode()).decode()
    out = _remote(u, f"mkdir -p {_shq(os.path.dirname(p))} && "
                  f"echo {b64} | base64 -d > {_shq(p)} && wc -c {_shq(p)}", timeout=60)
    return {"status": "ok", "username": u, "path": p, **out}


def setup_workspace(source="chunyu", repo_url="", github_token="",
                     target_dir="", username=""):
    """git clone (or copy) the fitpack workspace to target_dir (default
    /w/ceph24/JAM/<user>/fitpack_legacy), record it as THE workspace, and
    create <workspace>/runs — one folder holds cards AND runs."""
    u, err = _need_user(username)
    if err:
        return err
    target = (target_dir or "").strip() or f"/w/ceph24/JAM/{u}/fitpack_legacy"
    if not target.startswith("/w/ceph24/JAM"):
        return {"status": "error", "message": "target_dir must be under /w/ceph24/JAM"}
    steps = []
    exists = _remote(u, f"test -d {_shq(target)} && echo YES || echo NO", timeout=30)
    if "YES" in (exists.get("out") or ""):
        steps.append("workspace already present — skipped clone")
    elif repo_url.strip():
        url = repo_url.strip()
        tok = (github_token or "").strip()
        if tok and url.startswith("https://"):
            url = url.replace("https://", f"https://{tok}@", 1)
        out = _remote(u, f"mkdir -p {_shq(os.path.dirname(target))} && "
                      f"git clone {_shq(url)} {_shq(target)} 2>&1 | tail -3",
                      timeout=900)
        steps.append(f"git clone: rc={out['rc']} {out['out'][-200:]}")
        if out["rc"] != 0:
            return {"status": "error", "username": u, "steps": steps,
                    "detail": out["err"][-300:]}
    else:
        srcu = (source or "chunyu").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", srcu):
            return {"status": "error", "message": "invalid source username"}
        out = _remote(u, f"mkdir -p {_shq(os.path.dirname(target))} && "
                      f"cp -r {_shq(_paths(srcu)[0])} {_shq(target)} 2>&1 | tail -3",
                      timeout=900)
        steps.append(f"cp from {srcu}: rc={out['rc']} {out['err'][-200:]}")
        if out["rc"] != 0:
            return {"status": "error", "username": u, "steps": steps,
                    "detail": out["err"][-300:]}
    out = _remote(u, f"mkdir -p {_shq(target)}/runs/logs && "
                  f"printf '%s\n' {_shq(target)} > /w/ceph24/JAM/{u}/.jam_workspace && "
                  f"if [ ! -e /w/ceph24/JAM/{u}/lhapdf_tables ]; then "
                  f"ln -s /w/ceph24/JAM/chunyu/lhapdf_tables /w/ceph24/JAM/{u}/lhapdf_tables; fi; "
                  f"echo DONE", timeout=60)
    steps.append("runs tree + workspace marker: " + (out.get("out") or "").strip()[-60:])
    _WS_CACHE.pop(u, None)
    return {"status": "ok", "username": u, "workspace": target, "steps": steps}



def get_fit_parameters(card="input_jam24_s7_warm.py", username=""):
    """Read the actual fit parameters (block/name/value/min/max/fixed) from a
    card, so UIs and agents always show the card's true values."""
    u, err = _need_user(username)
    if err:
        return err
    code = f"""
import re, json, ast
txt = open({_shq(f'{_paths(u)[0]}/inputs/jam24/{card}')}).read()
rows = []
for m in re.finditer(r"conf\\['params'\\]\\['(\\w+)'\\]\\['([^']+)'\\]\\s*=\\s*(\\{{[^}}]*\\}})", txt):
    block, name, body = m.group(1), m.group(2), m.group(3)
    try:
        d = ast.literal_eval(body)
        rows.append({{'block': block, 'name': name,
                      'value': d.get('value'), 'min': d.get('min'),
                      'max': d.get('max'), 'fixed': bool(d.get('fixed'))}})
    except Exception:
        pass
print(json.dumps(rows))
"""
    r = _run_ifarm_py(u, code, timeout=60, cap=200000)
    try:
        rows = json.loads(r["out"].strip().splitlines()[-1])
        return {"status": "ok", "username": u, "card": card,
                "parameters": rows, "count": len(rows)}
    except Exception:
        return {"status": "error", "username": u, "detail": r}


def _card_dataset_lines(user, card):
    """Return the remote python snippet data for a card's dataset lines."""
    return _run_ifarm_py(user, f"""
import re
txt = open({_shq(f'{_paths(user)[0]}/inputs/jam24/{card}')}).read()
for m in re.finditer(r"{DATASET_LINE_RE}", txt):
    print(m.group(1), m.group(2))
""", timeout=60)


def search_datasets(card="input_jam24_s7_warm.py", query="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    q = (query or "").strip().lower()
    code = f"""
import re
from collections import Counter
txt = open({_shq(f'{_paths(u)[0]}/inputs/jam24/{card}')}).read()
rows = [(m.group(1), m.group(2)) for m in re.finditer(r"{DATASET_LINE_RE}", txt)
        if {q!r} and {q!r} in (m.group(1) + m.group(2)).lower() or not {q!r}]
c = Counter(reac for reac, _ in rows)
print('datasets matching:', len(rows))
print('by reaction:', dict(c))
for reac, idx in rows[:40]:
    print(f'  {{reac}} {{idx}}')
"""
    out = _run_ifarm_py(u, code, timeout=60)
    return {"status": "ok", "username": u, "card": card, **out}


def get_dataset_catalog(card="input_jam24_s7_warm.py", username=""):
    u, err = _need_user(username)
    if err:
        return err
    code = f"""
import re, json
txt = open({_shq(f'{_paths(u)[0]}/inputs/jam24/{card}')}).read()
c = {{}}
for m in re.finditer(r"{DATASET_LINE_RE}", txt):
    c.setdefault(m.group(1), {{}})[m.group(2)] = True
out = {{r: [{{'id': i, 'label': str(i), 'experiment': '', 'default_on': True,
            'enabled': True}} for i in sorted(ids)] for r, ids in c.items()}}
print(json.dumps(out))
"""
    r = _run_ifarm_py(u, code, timeout=60, cap=60000)
    try:
        catalog = json.loads(r["out"].strip().splitlines()[-1])
        return {"status": "ok", "username": u, "card": card, "catalog": catalog}
    except Exception:
        return {"status": "error", "username": u, "detail": r}


def build_card(base_card="", exclude_ids="", out_card="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    if not base_card or not out_card:
        return {"status": "error", "message": "need base_card and out_card"}
    excl = [x.strip() for x in (exclude_ids or "").split(",") if x.strip()]
    code = f"""
import re
txt = open({_shq(f'{_paths(u)[0]}/inputs/jam24/{base_card}')}).read()
excl = {excl!r}
n = 0
def rep(m):
    global n
    if m.group(2) in excl:
        n += 1
        return '# ' + m.group(0)
    return m.group(0)
txt2 = re.sub(r"{DATASET_LINE_RE}", rep, txt)
open({_shq(f'{_paths(u)[0]}/inputs/jam24/{out_card}')}, 'w').write(txt2)
print('commented out', n, 'dataset lines')
"""
    out = _run_ifarm_py(u, code, timeout=60)
    return {"status": "ok", "username": u, "base_card": base_card,
            "out_card": out_card, "excluded": excl, **out}


def submit_jam_fit(card="", outdir="", prior_msr="", cores=4, seed=160300,
                   hours=10, partition="jupyter", username=""):
    u, err = _need_user(username)
    if err:
        return err
    if not card or not outdir:
        return {"status": "error", "message": "need card and outdir"}
    fp, _, ld = _paths(u)
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


def submit_fit_from_selection(base_card="input_jam24_s7_warm.py", selected_ids="",
                              out_card="agent_selection.py", outdir="", prior_msr="",
                              cores=4, hours=0, partition="jupyter", username="",
                              qcd=None, params=None):
    u, err = _need_user(username)
    if err:
        return err
    if not selected_ids.strip():
        return {"status": "error", "message": "need selected_ids (comma-separated)"}
    if int(hours) <= 0:
        hours = 2 if prior_msr else 10   # resource rules: warm 2h, cold 10h
    keep = set(x.strip() for x in selected_ids.split(",") if x.strip())

    # QCD overrides patch (Q20 / tmc / ht lines in the derived card)
    qcd = qcd or {}
    qpatches = []
    if qcd.get("Q20"):
        qpatches.append("txt2 = re.sub(r\"conf\\['Q20'\\]\\s*=\\s*[^\\n]+\", \"conf['Q20']=%s\", txt2)" % repr(qcd["Q20"]))
    for key in ("tmc", "ht"):
        if key in qcd:
            qpatches.append("txt2 = re.sub(r\"conf\\['%s'\\]\\s*=\\s*\\w+\", \"conf['%s']=%s\", txt2)" % (key, key, repr(qcd[key])))
    qpatch_code = "\n".join(qpatches)

    # Parameter overrides patch: replace the param dict line, or append if absent
    plist = [p for p in (params or []) if p.get("block") and p.get("name")]
    import json as _pj
    param_json = _pj.dumps(plist)
    param_lines = [
        "overrides = json.loads(%r)" % param_json,
        "for o in overrides:",
        "    b, nm = o['block'], o['name']",
        "    v = repr(o.get('value')); mn = 'None' if o.get('min') is None else repr(o['min'])",
        "    mx = 'None' if o.get('max') is None else repr(o['max'])",
        "    fx = 'True' if o.get('fixed') else 'False'",
        "    newline = \"conf['params']['%s']['%s']={'value':%s,'min':%s,'max':%s,'fixed':%s}\" % (b, nm, v, mn, mx, fx)",
        "    pat = re.compile(r\"conf\\['params'\\]\\['\" + re.escape(b) + r\"'\\]\\['\" + re.escape(nm) + r\"'\\]\\s*=\\s*\\{[^}]*\\}\")",
        "    txt2, cnt = pat.subn(lambda m: newline, txt2)",
        "    if cnt == 0:",
        "        txt2 += \"\\nconf['params'].setdefault('%s',{})['%s']={'value':%s,'min':%s,'max':%s,'fixed':%s}\\n\" % (b, nm, v, mn, mx, fx)",
        "print('param overrides applied:', len(overrides))",
    ]
    param_code = "\n".join(param_lines)

    code = f"""import re, json
txt = open({_shq(f'{_paths(u)[0]}/inputs/jam24/{base_card}')}).read()
keep = {sorted(keep)!r}
n = 0
def rep(m):
    global n
    if m.group(2) not in keep:
        n += 1
        return '# ' + m.group(0)
    return m.group(0)
txt2 = re.sub(r"{DATASET_LINE_RE}", rep, txt)
{qpatch_code}
{param_code}
open({_shq(f'{_paths(u)[0]}/inputs/jam24/{out_card}')}, 'w').write(txt2)
print('commented out', n, 'of the non-selected datasets')
"""
    gen = _run_ifarm_py(u, code, timeout=60)
    if gen.get("rc") != 0:
        return {"status": "error", "username": u, "card_gen": gen}
    sub = submit_jam_fit(card=out_card, outdir=outdir, prior_msr=prior_msr,
                         cores=cores, hours=hours, partition=partition, username=u)
    return {"status": sub.get("status"), "username": u, "out_card": out_card,
            "kept": sorted(keep), "card_gen": gen.get("out", "").strip(),
            "submit": sub}


def get_jam_result(outdir="", job_name="jamfit", username=""):
    u, err = _need_user(username)
    if err:
        return err
    _, rr, ld = _paths(u)
    out_abs = _resolve_outdir(u, outdir) if outdir else rr
    out = _remote(u,
        f"echo '--- queue ---'; squeue -u {u} -o '%.10i %.9P %.12j %.2t %.10M' | head -8; "
        f"echo '--- submit log ---'; cat /tmp/jam_fit_latest.submit 2>&1; "
        f"echo '--- progress ---'; ls -t {out_abs}/progress.json 2>/dev/null | head -1 | xargs -r tail -c 1500; "
        f"echo; echo '--- msr ---'; ls {out_abs}/msr 2>&1 | head -5; "
        f"echo '--- newest slurm err tail ---'; ls -t {ld}/jamfit_*.err 2>/dev/null | head -1 | xargs -r tail -12",
        timeout=60)
    return {"status": "ok", "username": u, **out}


def read_chi2(outdir="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    out_abs = _resolve_outdir(u, outdir) if outdir else _paths(u)[1]
    code = f"""
import json
d = json.load(open({_shq(out_abs + '/progress.json')}))
print('entries:', len(d))
tot = [e.get('chi2') for e in d if e.get('chi2') is not None]
npt = [sum(v.get('npts', 0) for v in e.get('datasets', {{}}).values()) for e in d]
for i, (t, n) in enumerate(zip(tot, npt)):
    if i >= len(tot) - 12 and n:
        print(f'iter {{i}}: chi2={{t:.1f}} npts={{n}} chi2/N={{t/n:.3f}}')
ds = d[-1].get('datasets', {{}})
print('--- latest per-dataset chi2/N (top 10) ---')
for k in sorted(ds, key=lambda k: -ds[k]['chi2'] / max(ds[k]['npts'], 1))[:10]:
    v = ds[k]
    print(f'{{k}}: chi2/N={{v["chi2"]/v["npts"]:.2f}} ({{v["npts"]}} pts)')
"""
    out = _run_ifarm_py(u, code, timeout=120)
    return {"status": "ok", "username": u, **out}


def evaluate_fit(outdir="", msr_file="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    fp = _paths(u)[0]
    out_abs = _resolve_outdir(u, outdir) if outdir else _paths(u)[1]
    files_expr = f"['{msr_file}']" if msr_file else f"sorted(glob.glob({_shq(out_abs + '/msr/*.msr')}))"
    code = f"""
import sys, glob, statistics
sys.path.insert(0, {_shq(fp)})
from tools.tools import load
files = {files_expr}
def tot(f):
    d = load(f)
    if 'chi2' not in d:
        raise KeyError('msr has no chi2 (partial save): keys=' + str(sorted(d.keys())))
    tn = tc = 0.0
    for reac, dd in d['chi2'].items():
        for info in dd.values():
            tn += info['npts']; tc += info['chi2']
    return tc / tn
vals = [tot(f) for f in files]
print(f'msr files: {{len(files)}}')
for f, v in zip(files, vals):
    print(f'  {{f.split(chr(47))[-2]}}/{{f.split(chr(47))[-1]}}: chi2/N={{v:.3f}}')
if vals:
    print(f'OUR chi2/N: mean={{statistics.mean(vals):.3f}} min={{min(vals):.3f}} max={{max(vals):.3f}}')
print('PAPER band (arXiv:2501.00665 step16a): mean=2.566 med=2.561 [2.190, 2.819]')
"""
    out = _run_ifarm_py(u, code, apptainer=True, timeout=300)
    return {"status": "ok", "username": u, **out}


def cluster_status(username=""):
    u, err = _need_user(username)
    if err:
        return err
    out = _remote(u,
        "echo '--- partitions ---'; sinfo -o '%P %a %D %C' 2>/dev/null | head -8; "
        f"echo '--- my queue ---'; squeue -u {u} -o '%.10i %.9P %.12j %.2t %.10M %.6D' | head -10; "
        "echo '--- run-time hints ---'; echo 'warm start: ~40 min @ 4 cores (jupyter)'; "
        "echo 'cold start: budget 10h @ 4 cores (jupyter, reference ladder)'; "
        "echo 'reference: step7 warm converges to chi2/N ~ 1.452'", timeout=90)
    return {"status": "ok", "username": u, **out}


def chain_state(outdir="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    fp = _paths(u)[0]
    rr = _paths(u)[1]
    target = _resolve_outdir(u, outdir.strip()) if outdir.strip() else ""
    code = f"""
import sys, glob, os
sys.path.insert(0, {_shq(fp)})
from tools.tools import load
root = {_shq(rr)}
target = {target!r}
if target and os.path.isdir(target + '/msr'):
    files = sorted(glob.glob(target + '/msr/*.msr'))
else:
    files = sorted(glob.glob(root + '/**/msr/*.msr', recursive=True), key=os.path.getmtime)
def tot(d):
    chi = d.get('chi2', {{}}); tn = tc = 0.0
    for dd in chi.values():
        for info in dd.values():
            tn += info['npts']; tc += info['chi2']
    return tc / tn, chi
if not files:
    print('no msr found')
else:
    f = files[-1]; d = load(f); t, chi = tot(d)
    print('msr:', f)
    print(f'total chi2/N={{t:.3f}}')
    rows = []
    for reac, dd in chi.items():
        for idx, info in dd.items():
            rows.append((reac, idx, info['chi2'] / max(info['npts'], 1), info['npts']))
    rows.sort(key=lambda r: -r[2])
    print('worst 8 datasets (chi2/N):')
    for reac, idx, v, n in rows[:8]:
        print(f'  {{reac}} {{idx}}: {{v:.2f}} ({{n}} pts)')
    params = d.get('params', {{}})
    steps = sorted(k for k in params if isinstance(k, int))
    if steps:
        p = params[steps[-1]]
        print(f'params (step {{steps[-1]}}, {{len(p)}} total), first 12:')
        order = d.get('order', {{}}).get(steps[-1], [])
        for i, v in enumerate(p[:12]):
            name = order[i][2] if i < len(order) and len(order[i]) > 2 else str(i)
            print(f'  {{name}}: {{v}}')
print('--- ladder context (per-step chi2/N) ---')
for m in sorted(glob.glob(root + '/**/msr/*.msr', recursive=True), key=os.path.getmtime):
    d2 = load(m); t2, _ = tot(d2)
    print(f'  {{m.split(chr(47))[-3]}}: {{t2:.3f}}')
"""
    out = _run_ifarm_py(u, code, apptainer=True, timeout=300)
    return {"status": "ok", "username": u, **out}


# ── tool registry ───────────────────────────────────────────────────────────

TOOLS = {
    "ping": {"desc": "Reply pong.",
             "schema": {"type": "object", "properties": {}},
             "fn": lambda a: ping()},
    "get_console": {
        "desc": "Return the web UI URLs. When the user asks to SET UP or BUILD a fit "
                "(pick reactions/datasets/object/QCD settings) give them a composer_url "
                "to open in a browser — it is a tabbed input-card UI that submits through "
                "this gateway. Give a console_url for sign-in and quick status/chi2 checks.",
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
                  "schema": {"type": "object", "properties": {"username": {"type": "string"}}},
                  "fn": lambda a: check_hpc(a.get("username", ""))},
    "submit_test_fit": {"desc": "Submit a trivial test job on ifarm through the chain (fire-and-forget).",
                        "schema": {"type": "object", "properties": {"username": {"type": "string"}}},
                        "fn": lambda a: submit_test_fit(a.get("username", ""))},
    "get_submit_result": {"desc": "Poll the background test sbatch result and the queue.",
                          "schema": {"type": "object", "properties": {"username": {"type": "string"}}},
                          "fn": lambda a: get_submit_result(a.get("username", ""))},
    "node_diag": {"desc": "Run a shell command on the gateway node (diagnostics / hot-redeploy).",
                  "schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
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
                      "schema": {"type": "object", "properties": {"username": {"type": "string"}}},
                      "fn": lambda a: list_jam_runs(a.get("username", ""))},
    "fs_list": {"desc": "List a directory on ifarm (defaults to the user's JAM root).",
                "schema": {"type": "object",
                           "properties": {"path": {"type": "string"},
                                          "username": {"type": "string"}}},
                "fn": lambda a: fs_list(a.get("path", ""), a.get("username", ""))},
    "fs_read": {"desc": "Read a text file on ifarm (fit cards, slurm logs, progress.json). "
                       "Path must be under /w/ceph24.",
                "schema": {"type": "object",
                           "properties": {"path": {"type": "string"},
                                          "max_lines": {"type": "integer"},
                                          "username": {"type": "string"}}},
                "fn": lambda a: fs_read(a.get("path", ""), int(a.get("max_lines", 200)),
                                        a.get("username", ""))},
    "fs_write": {"desc": "Write a text file on ifarm, restricted to the user's own "
                        "/w/ceph24/JAM/<username>/ area.",
                 "schema": {"type": "object",
                            "properties": {"path": {"type": "string"},
                                           "content": {"type": "string"},
                                           "username": {"type": "string"}},
                            "required": ["path", "content"]},
                 "fn": lambda a: fs_write(a.get("path", ""), a.get("content", ""),
                                          a.get("username", ""))},
    "setup_workspace": {
        "desc": "Prepare /w/ceph24/JAM/<user>: copy fitpack_legacy from another "
                "user's copy on the shared FS (default source=chunyu) or git-clone "
                "repo_url, create the smoke4 run tree, and symlink lhapdf_tables. "
                "Run this once for each new user before their first fit.",
        "schema": {"type": "object",
                   "properties": {"source": {"type": "string"},
                                  "repo_url": {"type": "string"},
                                  "target_dir": {"type": "string"},
                                  "username": {"type": "string"},
                                  "github_token": {"type": "string"}}},
        "fn": lambda a: setup_workspace(a.get("source", "chunyu"),
                                        a.get("repo_url", ""), a.get("github_token", ""),
                                        a.get("target_dir", ""), a.get("username", ""))},
    "search_datasets": {
        "desc": "List datasets (reaction + id) inside a JAM card, optionally "
                "filtered by query. Use it to help the user pick which reactions "
                "to include before building a custom card.",
        "schema": {"type": "object",
                   "properties": {"card": {"type": "string"},
                                  "query": {"type": "string"},
                                  "username": {"type": "string"}}},
        "fn": lambda a: search_datasets(a.get("card", "input_jam24_s7_warm.py"),
                                        a.get("query", ""), a.get("username", ""))},
    "get_dataset_catalog": {
        "desc": "Return the dataset catalog for a card in the shape the fit "
                "composer needs: {reaction: [{id, label, default_on...}]}.",
        "schema": {"type": "object",
                   "properties": {"card": {"type": "string"},
                                  "username": {"type": "string"}}},
        "fn": lambda a: get_dataset_catalog(a.get("card", "input_jam24_s7_warm.py"),
                                            a.get("username", ""))},
    "get_fit_parameters": {
        "desc": "Read the ACTUAL fit parameters (block/name/value/min/max/fixed) from "
                "a card. UIs and agents must use these values, never hardcoded ones.",
        "schema": {"type": "object",
                   "properties": {"card": {"type": "string"},
                                  "username": {"type": "string"}}},
        "fn": lambda a: get_fit_parameters(a.get("card", "input_jam24_s7_warm.py"),
                                           a.get("username", ""))},
    "build_card": {
        "desc": "Create a modified fit card: copy base_card and comment out the "
                "dataset lines whose ids are in exclude_ids (comma-separated). "
                "base_card and out_card are filenames under fitpack/inputs/jam24.",
        "schema": {"type": "object",
                   "properties": {"base_card": {"type": "string"},
                                  "exclude_ids": {"type": "string"},
                                  "out_card": {"type": "string"},
                                  "username": {"type": "string"}},
                   "required": ["base_card", "out_card"]},
        "fn": lambda a: build_card(a.get("base_card", ""), a.get("exclude_ids", ""),
                                   a.get("out_card", ""), a.get("username", ""))},
    "submit_fit_from_selection": {
        "desc": "Composer-style submission: keep only selected_ids (comma list) "
                "in a server-generated derived card and submit it. Powers the "
                "web fit composer's Submit button. hours=0 (default) auto-sizes: "
                "2h for warm starts, 10h for cold starts; 4 cores, jupyter. "
                "Optional qcd overrides (Q20/tmc/ht) and params overrides "
                "(block/name/value/min/max/fixed) are written into the card.",
        "schema": {"type": "object",
                   "properties": {"base_card": {"type": "string"},
                                  "selected_ids": {"type": "string"},
                                  "out_card": {"type": "string"},
                                  "outdir": {"type": "string"},
                                  "prior_msr": {"type": "string"},
                                  "cores": {"type": "integer"},
                                  "hours": {"type": "integer"},
                                  "partition": {"type": "string"},
                                  "username": {"type": "string"},
                                  "qcd": {"type": "object",
                                          "properties": {"Q20": {"type": "number"},
                                                         "tmc": {"type": "boolean"},
                                                         "ht": {"type": "boolean"}}},
                                  "params": {"type": "array",
                                             "items": {"type": "object",
                                                       "properties": {"block": {"type": "string"},
                                                                      "name": {"type": "string"},
                                                                      "value": {},
                                                                      "min": {},
                                                                      "max": {},
                                                                      "fixed": {"type": "boolean"}},
                                                       "required": ["block", "name"]}}},
                   "required": ["selected_ids"]},
        "fn": lambda a: submit_fit_from_selection(
            a.get("base_card", "input_jam24_s7_warm.py"),
            a.get("selected_ids", ""), a.get("out_card", "agent_selection.py"),
            a.get("outdir", ""), a.get("prior_msr", ""),
            int(a.get("cores", 4)), int(a.get("hours", 0)),
            a.get("partition", "jupyter"), a.get("username", ""),
            a.get("qcd") or None, a.get("params") or None)},
    "submit_jam_fit": {
        "desc": "Submit a REAL JAM fit on ifarm as the connected user (apptainer "
                "maxlike_jam). card = card name in fitpack/inputs/jam24; outdir = "
                "output dir (relative to smoke4 or absolute); prior_msr = optional "
                "prior .msr file (empty = cold start). RESOURCE RULES: warm start "
                "(prior given) 1-2h; cold start (no prior) hours>=10; 4 cores is "
                "the ladder reference; partition jupyter is what the ladder used. "
                "Check cluster_status first when the queue looks busy.",
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
        "desc": "Evaluate finished .msr files against the JAM24 paper band "
                "(chi2/N 2.566 [2.190,2.819]). Runs inside apptainer (needs numpy).",
        "schema": {"type": "object",
                   "properties": {"outdir": {"type": "string"},
                                  "msr_file": {"type": "string"},
                                  "username": {"type": "string"}}},
        "fn": lambda a: evaluate_fit(a.get("outdir", ""), a.get("msr_file", ""),
                                     a.get("username", ""))},
    "cluster_status": {
        "desc": "Cluster perception: partitions, the user's queue, and run-time "
                "hints. Call BEFORE submit_jam_fit to size cores/hours/partition "
                "sensibly for the current load.",
        "schema": {"type": "object", "properties": {"username": {"type": "string"}}},
        "fn": lambda a: cluster_status(a.get("username", ""))},
    "chain_state": {
        "desc": "Freeze/continue/reset judgment data: newest msr total chi2/N, "
                "worst per-dataset chi2/N, fitted parameter values, and the "
                "per-step ladder chi2/N. Use it to decide whether to freeze the "
                "fit and add datasets, adjust the dataset set, or reset cold.",
        "schema": {"type": "object",
                   "properties": {"outdir": {"type": "string"},
                                  "username": {"type": "string"}}},
        "fn": lambda a: chain_state(a.get("outdir", ""), a.get("username", ""))},
}

CONSOLE_HTML = '<!doctype html><html><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>JAM Gateway Console</title>\n<style>\nbody{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;\nmargin:0;background:#f6f7f9;color:#15202b}\n.wrap{max-width:860px;margin:0 auto;padding:24px}\nh1{font-size:20px;margin:0 0 4px}.sub{color:#5c6774;font-size:13px;margin-bottom:18px}\n.card{background:#fff;border:1px solid #e0e4ea;border-radius:12px;padding:18px;margin-bottom:14px}\n.card h2{font-size:15px;margin:0 0 10px}\nlabel{display:block;font-size:12.5px;font-weight:600;margin:8px 0 3px}\ninput,select{width:100%;font:inherit;padding:8px 10px;border:1px solid #ccd3dc;\nborder-radius:8px;box-sizing:border-box}\nbutton{font:inherit;font-weight:600;padding:8px 16px;border:0;border-radius:8px;\nbackground:#1d5fd0;color:#fff;cursor:pointer;margin-top:10px;margin-right:8px}\nbutton.ghost{background:#eef1f5;color:#15202b}\npre{background:#0f1319;color:#e8ecf2;padding:12px;border-radius:8px;font-size:12px;\nmax-height:300px;overflow:auto;white-space:pre-wrap}\n.row{display:flex;gap:10px}.row>div{flex:1}\n#st{font-size:13px;color:#1d5fd0;margin-top:8px}\n.err{color:#c0392b}\n</style></head><body><div class="wrap">\n<h1>JAM Gateway Console</h1>\n<div class="sub">qcd24s100 &rarr; scilogin &rarr; ifarm &middot; 每步都在这页点选,不用聊天</div>\n<div class="card">\n<h2>1 &middot; 连接(先只填用户名;链活着就不需要凭证)</h2>\n<label>JLab 用户名</label><input id="user" placeholder="chunyu">\n<label id="otplab" style="display:none">PIN+OTP(仅当提示需要时)</label>\n<input id="otp" style="display:none">\n<label id="pwlab" style="display:none">ifarm 密码(仅当提示需要时)</label>\n<input id="pw" type="password" style="display:none">\n<button onclick="doConnect()">连接</button>\n<button class="ghost" onclick="call(\'check_hpc\',{})">检查 HPC</button>\n<button class="ghost" onclick="call(\'list_jam_runs\',{})">清点 JAM 工作区</button>\n<div id="st"></div>\n</div>\n<div class="card">\n<h2>2 &middot; 提交拟合(向导式点选)</h2>\n<div class="hint">选反应 → 选数据集 → 定预算 → 复审提交,全程勾选,无需手填参数。</div>\n<button onclick="location.search=\'?ui=composer\'" style="font-size:15px;padding:12px 22px">打开拟合向导(选项式 UI)</button>\n<div style="margin-top:14px">\n<label>输出目录(用于下面的快捷查看)</label>\n<input id="outdir" placeholder="001/step7_claude">\n<button class="ghost" onclick="call(\'get_jam_result\',{outdir:val(\'outdir\')})">查状态</button>\n<button class="ghost" onclick="call(\'read_chi2\',{outdir:val(\'outdir\')})">读 &chi;²</button>\n<button class="ghost" onclick="call(\'evaluate_fit\',{outdir:val(\'outdir\')})">对照论文 band</button>\n<button class="ghost" onclick="call(\'chain_state\',{outdir:val(\'outdir\')})">阶梯状态(冻结/继续/重制)</button>\n</div>\n</div>\n<div class="card"><h2>3 &middot; 输出</h2><pre id="out">(点上面任意按钮,结果显示在这里)</pre></div>\n</div>\n<script>\nvar sid="";\nfunction val(id){return document.getElementById(id).value.trim();}\nfunction show(id,s){document.getElementById(id).style.display=s?"":"none";}\nfunction setSt(t,c){var e=document.getElementById("st");e.textContent=t;e.className=c||"";}\nasync function call(tool,args){\n  setSt("调用 "+tool+" &hellip;");\n  try{\n    var hdrs={"Content-Type":"application/json","Accept":"application/json, text/event-stream"};\n    if(sid)hdrs["Mcp-Session-Id"]=sid;\n    var r=await fetch(location.pathname,{method:"POST",headers:hdrs,\n      body:JSON.stringify({jsonrpc:"2.0",id:Date.now()%100000,method:"tools/call",\n      params:{name:tool,arguments:args}})});\n    var h=r.headers.get("Mcp-Session-Id"); if(h)sid=h;\n    var d=await r.json();\n    var t=(d.result&&d.result.content)?d.result.content.map(function(c){return c.text||"";}).join("\\n"):JSON.stringify(d,null,1);\n    document.getElementById("out").textContent=t;\n    setSt("完成 "+tool,"");\n    return t;\n  }catch(e){setSt("失败: "+e,"err");document.getElementById("out").textContent=String(e);}\n}\nasync function doConnect(){\n  var u=val("user"); if(!u){setSt("先填用户名","err");return;}\n  var t=await call("connect_hpc",{username:u});\n  if(t&&t.indexOf("need pin_otp")>=0){\n    show("otplab",true);show("otp",true);show("pwlab",true);show("pw",true);\n    setSt("链不活 &mdash; 需要凭证,填 PIN+OTP 和密码后再点一次连接","err");\n  } else if(t&&t.indexOf("reused")>=0){setSt("链已复用,无需凭证");}\n  else if(val("otp")&&val("pw")){\n    await call("connect_hpc",{username:u,pin_otp:val("otp"),password:val("pw")});\n  }\n}\nasync function submitFit(){\n  await call("submit_jam_fit",{card:val("card"),outdir:val("outdir"),prior_msr:val("prior"),\n    cores:parseInt(val("cores")||"4"),hours:parseInt(val("hours")||"10"),partition:val("part")});\n}\n</script></body></html>\n'

COMPOSER_HTML = '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8" />\n<meta name="viewport" content="width=device-width, initial-scale=1" />\n<title>JAM Input Card</title>\n<style>\n  :root {\n    --bg: #ffffff; --fg: #1a1a1a; --muted: #6b7280; --line: #e5e7eb;\n    --card: #f9fafb; --accent: #0c7a40; --accent-fg: #ffffff;\n    --chip: #eef2f7; --chip-on: #d8efde; --warn: #b45309; --disabled: #9ca3af;\n  }\n  @media (prefers-color-scheme: dark) {\n    :root {\n      --bg: #1a1a1a; --fg: #ececec; --muted: #9b9b9b; --line: #333;\n      --card: #232323; --accent: #12a24a; --accent-fg: #fff;\n      --chip: #2a2f36; --chip-on: #16351f; --warn: #d08a1e; --disabled: #5b5b5b;\n    }\n  }\n  * { box-sizing: border-box; }\n  body { margin: 0; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;\n         background: var(--bg); color: var(--fg); }\n  .wrap { max-width: 820px; margin: 0 auto; padding: 14px 16px 18px; }\n  h1 { font-size: 17px; margin: 0 0 2px; }\n  .sub { color: var(--muted); font-size: 12px; margin: 0 0 12px; }\n  .tabs { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }\n  .tab { font-size: 12px; padding: 6px 11px; border-radius: 999px; background: var(--chip);\n         color: var(--muted); cursor: pointer; border: 1px solid transparent; white-space: nowrap; }\n  .tab.active { background: var(--accent); color: var(--accent-fg); }\n  .tab .num { opacity: .7; margin-right: 4px; }\n  .panel { display: none; }\n  .panel.active { display: block; }\n  .panelTitle { font-weight: 600; font-size: 14px; margin: 2px 0 8px; }\n  .guide { background: var(--chip); border-left: 3px solid var(--accent); border-radius: 6px;\n           padding: 8px 10px; font-size: 12px; color: var(--muted); margin-bottom: 12px; }\n  .guide b { color: var(--fg); }\n  .field { margin-bottom: 12px; }\n  .label { font-weight: 600; font-size: 12px; margin-bottom: 4px; }\n  .hint { color: var(--muted); font-size: 11px; margin-top: 3px; }\n  select, input[type=text], input[type=number], input[type=password] {\n    width: 100%; padding: 7px 9px; border: 1px solid var(--line); border-radius: 8px;\n    background: var(--bg); color: var(--fg); font-size: 13px; }\n  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }\n  .grid3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }\n  .opt { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border: 1px solid var(--line);\n         border-radius: 8px; margin-bottom: 6px; background: var(--card); cursor: pointer; }\n  .opt:hover { border-color: var(--accent); }\n  .opt.dis { opacity: .55; cursor: not-allowed; }\n  .opt .why { display: block; font-size: 11px; color: var(--muted); margin-top: 1px; }\n  .opt .cnt { margin-left: auto; font-size: 11px; color: var(--muted); white-space: nowrap; }\n  .rgroup { margin-bottom: 12px; }\n  .rgroup h3 { font-size: 12px; margin: 0 0 6px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }\n  table.tbl { width: 100%; border-collapse: collapse; font-size: 12.5px; }\n  .tbl th { text-align: left; color: var(--muted); font-weight: 600; font-size: 11px;\n            padding: 4px 6px; border-bottom: 1px solid var(--line); }\n  .tbl td { padding: 4px 6px; border-bottom: 1px dashed var(--line); }\n  .tbl td.id { color: var(--muted); font-size: 11px; }\n  .rev { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; margin-bottom: 12px; }\n  .rev .row { display: flex; justify-content: space-between; padding: 3px 0; border-bottom: 1px dashed var(--line); font-size: 13px; }\n  .rev .row:last-child { border-bottom: none; }\n  .rev .k { color: var(--muted); }\n  .rev .v { font-weight: 600; text-align: right; }\n  .nav { display: flex; gap: 8px; margin-top: 14px; }\n  button { font: inherit; border: none; border-radius: 8px; padding: 9px 16px; cursor: pointer; }\n  .ghost { background: var(--chip); color: var(--fg); }\n  .primary { background: var(--accent); color: var(--accent-fg); font-weight: 600; margin-left: auto; }\n  button:disabled { opacity: .5; cursor: not-allowed; }\n  .err { color: var(--warn); font-size: 12px; margin-top: 8px; }\n  .ok { color: var(--accent); font-size: 12px; margin-top: 8px; }\n  .chk { width: 15px; height: 15px; accent-color: var(--accent); }\n  .connbar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }\n  .connbar .who { flex: 1; min-width: 140px; }\n  .connbar span.note { font-size: 11.5px; color: var(--muted); }\n  pre.out { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 10px;\n            font-size: 11.5px; max-height: 220px; overflow: auto; white-space: pre-wrap; }\n</style>\n</head>\n<body>\n<div class="wrap">\n  <h1>JAM Input Card</h1>\n  <p class="sub">Build the input card, then submit it to the HPC cluster. You choose every value.</p>\n\n  <div class="connbar">\n    <span class="who"><input id="user" type="text" placeholder="JLab 用户名" /></span>\n    <button class="ghost" onclick="doConnect()">连接</button>\n    <button class="ghost" onclick="showClone()">git clone</button>\n    <button class="ghost" onclick="call(\'check_hpc\',{})">检查 HPC</button>\n    <button class="ghost" onclick="call(\'cluster_status\',{})">集群负载</button>\n    <span class="note" id="connst"></span>\n  </div>\n\n  <div id="cloneRow" style="display:none" class="guide">\n    <div class="grid2">\n      <div class="field"><div class="label">仓库 URL</div>\n        <input id="repoUrl" type="text" placeholder="https://github.com/org/fitpack_legacy.git" /></div>\n      <div class="field"><div class="label">GitHub token(私有库必填)</div>\n        <input id="ghToken" type="password" placeholder="ghp_…" /></div>\n    </div>\n    <div class="field"><div class="label">clone 到哪个文件夹(路径选择;留空 = 默认 /w/ceph24/JAM/&lt;用户名&gt;/fitpack_legacy)</div>\n      <input id="targetDir" type="text" placeholder="(默认)" /></div>\n    <div class="hint">跑 JAM 就在这个文件夹里:卡在 &lt;clone&gt;/inputs/jam24,结果在 &lt;clone&gt;/runs。</div>\n    <button class="ghost" onclick="doClone()">执行 git clone</button>\n    <button class="ghost" onclick="document.getElementById(\'cloneRow\').style.display=\'none\'">收起</button>\n  </div>\n  <div class="tabs" id="tabs"></div>\n\n  <div class="panel" data-step="object">\n    <div class="panelTitle">1 · Non-perturbative object</div>\n    <div class="guide">选择要拟合的非微扰对象。参数表<b>可直接编辑</b>,提交时会写进生成的卡。</div>\n    <div id="objectList"></div>\n    <div class="rgroup" style="margin-top:14px">\n      <h3>PDF parameters(可编辑,写进卡)</h3>\n      <table class="tbl" id="paramTable"></table>\n    </div>\n  </div>\n\n  <div class="panel" data-step="reactions">\n    <div class="panelTitle">2 · Reactions</div>\n    <div class="guide">勾选要纳入拟合的反应。每个反应约束 PDF 的不同部分 — <b>选好族后,该族数据集可展开细选</b>。</div>\n    <div id="reactionList"></div>\n    <div id="datasetList" style="margin-top:10px"></div>\n  </div>\n\n  <div class="panel" data-step="qcd">\n    <div class="panelTitle">3 · QCD settings</div>\n    <div class="guide">这些值会<b>写入生成的卡</b>(Q0²、TMC、higher-twist)。</div>\n    <div class="grid3">\n      <div class="field"><div class="label">Q0²</div>\n        <input id="q20" type="number" step="0.0001" value="1.6384" /></div>\n      <div class="field"><div class="label">Target-mass corrections (TMC)</div>\n        <select id="tmc"><option value="false" selected>Off</option><option value="true">On</option></select></div>\n      <div class="field"><div class="label">Higher-twist (HT)</div>\n        <select id="ht"><option value="false" selected>Off</option><option value="true">On</option></select></div>\n    </div>\n  </div>\n\n  <div class="panel" data-step="job">\n    <div class="panelTitle">4 · Job config</div>\n    <div class="guide">campaign 名决定结果目录(001/&lt;campaign&gt;)。质量阈值供 agent 后续判定收敛。</div>\n    <div class="grid3">\n      <div class="field"><div class="label">Campaign name</div>\n        <input id="campaign" type="text" value="agent_run" /></div>\n      <div class="field"><div class="label">Replicas</div>\n        <input type="number" value="1" disabled /><div class="hint">阶梯复本暂为 1</div></div>\n      <div class="field"><div class="label">Quality: χ²/N ok / warn / good</div>\n        <input id="qthresh" type="text" placeholder="2.5 / 3.0 / 0.05" /></div>\n    </div>\n  </div>\n\n  <div class="panel" data-step="review">\n    <div class="panelTitle">5 · Review</div>\n    <div class="rev" id="reviewBox"></div>\n    <div id="st"></div>\n    <pre class="out" id="out">(提交后这里显示结果)</pre>\n  </div>\n\n  <div class="nav">\n    <button class="ghost" id="backBtn">上一步</button>\n    <button class="primary" id="nextBtn">下一步</button>\n  </div>\n</div>\n\n<script>\nvar sid="", catalog={}, selected=new Set(), families=new Set(), stepIdx=0, submitted=false;\nvar STEPS=["object","reactions","qcd","job","review"];\nvar LABELS=["Non-perturbative object","Reactions","QCD settings","Job config","Review"];\nvar FAMILY_NAMES = { idis:"DIS", dy:"Drell-Yan", "dy-kaon":"DY kaon ratio", lqcd_mom:"Lattice moments",\n  jet:"Inclusive jets", wasym:"W asymmetry", wzrv:"WZ rapidity", zrap:"Z rapidity",\n  wc:"Weak charge", sidis:"SIDIS", sia:"SIA" };\nvar FAMILY_WHY = { idis:"constrains the quark and gluon PDFs over a wide x range",\n  dy:"constrains the d̄/ū flavor ratio", wasym:"constrains flavor separation via W± asymmetries",\n  wzrv:"constrains flavor separation via W± asymmetries", zrap:"constrains light-quark flavors at intermediate x",\n  jet:"constrains the gluon PDF at large x", wc:"constrains the strange quark PDF",\n  sidis:"constrains PDFs and fragmentation functions together", sia:"constrains the fragmentation functions" };\nvar OBJECTS = [\n  { value:"pdf", label:"Unpolarized PDF", card:"input_jam24_s7_warm.py" },\n  { value:"polarized_pdf", label:"Polarized PDF", card:"input_pdf_fit.py" },\n  { value:"pion_ff", label:"Pion FF", card:"input_pdf_fit.py" },\n  { value:"kaon_ff", label:"Kaon FF", card:"input_pdf_fit.py" },\n  { value:"hadron_ff", label:"Hadron FF", card:"input_pdf_fit.py" }\n];\nfunction objCard(){return (document.querySelector(\'input[name=obj]:checked\')||{}).value&&\n  OBJECTS.find(function(o){return o.value===(document.querySelector(\'input[name=obj]:checked\')||{}).value;}).card;}\nvar PARAM_ROWS = [];\n\nfunction val(id){return document.getElementById(id).value.trim();}\nfunction setSt(t,c){var e=document.getElementById("st");e.textContent=t;e.className=c||"";}\nfunction sync(){\n  document.querySelectorAll(".tab").forEach(function(el,i){\n    el.className="tab"+(i===stepIdx?" active":"");});\n  document.querySelectorAll(".panel").forEach(function(p){\n    p.classList.toggle("active",p.dataset.step===STEPS[stepIdx]);});\n  document.getElementById("backBtn").style.visibility=(stepIdx===0||submitted)?"hidden":"visible";\n  var last=stepIdx===STEPS.length-2;\n  document.getElementById("nextBtn").textContent=last?"Submit to HPC":"下一步";\n  document.getElementById("nextBtn").disabled=submitted;\n  if(stepIdx===STEPS.length-1)renderReview();\n}\nasync function call(tool,args){\n  setSt("调用 "+tool+" …");\n  var hdrs={"Content-Type":"application/json","Accept":"application/json, text/event-stream"};\n  if(sid)hdrs["Mcp-Session-Id"]=sid;\n  var r=await fetch(location.pathname,{method:"POST",headers:hdrs,\n    body:JSON.stringify({jsonrpc:"2.0",id:Date.now()%100000,method:"tools/call",\n    params:{name:tool,arguments:args}})});\n  var h=r.headers.get("Mcp-Session-Id"); if(h)sid=h;\n  var d=await r.json();\n  var txt=(d.result&&d.result.content)?d.result.content.map(function(c){return c.text||"";}).join("\\n"):JSON.stringify(d,null,1);\n  document.getElementById("out").textContent=txt;\n  setSt("完成 "+tool,"");\n  try{return JSON.parse(txt);}catch(e){return {raw:txt};}\n}\nasync function doConnect(){\n  var u=val("user"); if(!u){setSt("先填用户名","err");return;}\n  var t=await call("connect_hpc",{username:u});\n  var s=JSON.stringify(t||{});\n  document.getElementById("connst").textContent =\n    s.indexOf("reused")>=0?"链已复用 · 无需凭证":\n    s.indexOf(\'"ok"\')>=0?"已连接":\n    s.indexOf("need pin_otp")>=0?"需要凭证(控制台页输入)":s.slice(0,80);\n  if(s.indexOf(\'"ok"\')>=0||s.indexOf("reused")>=0){\n    var r=await call("get_dataset_catalog",{card:objCard()});\n    catalog=(r&&r.catalog)||{};\n    renderReactions();\n  }\n}\nfunction showClone(){\n  var el=document.getElementById("cloneRow");\n  el.style.display=el.style.display==="none"?"block":"none";\n}\nasync function doClone(){\n  var u=val("user"); if(!u){setSt("先填用户名","err");return;}\n  if(!val("repoUrl")){setSt("填仓库 URL","err");return;}\n  await call("setup_workspace",{source:"",repo_url:val("repoUrl"),github_token:val("ghToken"),target_dir:val("targetDir")});\n}\nfunction renderObjects(){\n  var c=document.getElementById("objectList"); c.innerHTML="";\n  OBJECTS.forEach(function(o){\n    var el=document.createElement("div"); el.className="opt";\n    el.innerHTML=\'<input type="radio" name="obj" class="chk" value="\'+o.value+\'"\'+\n      (o.value==="pdf"?" checked":"")+\'><span>\'+o.label+\'</span>\';\n    el.querySelector("input").onchange=function(){loadCatalog();};\n    c.appendChild(el);\n  });\n}\nfunction renderParams(){\n  var t=document.getElementById("paramTable");\n  var h="<tr><th>block</th><th>param</th><th>fixed</th><th>value</th><th>min</th><th>max</th></tr>";\n  PARAM_ROWS.forEach(function(r,i){\n    var mn=r.min===null||r.min===undefined?"":r.min, mx=r.max===null||r.max===undefined?"":r.max;\n    h+="<tr><td class=\\\'id\\\'>"+r.block+"</td><td>"+r.name+"</td>"+\n       "<td><input type=\\\'checkbox\\\' class=\\\'pfx\\\' data-i=\\\'"+i+"\\\'"+(r.fixed?" checked":"")+"></td>"+\n       "<td><input type=\\\'text\\\' class=\\\'pv\\\' data-i=\\\'"+i+"\\\' value=\\\'"+r.value+"\\\'></td>"+\n       "<td><input type=\\\'text\\\' class=\\\'pmn\\\' data-i=\\\'"+i+"\\\' value=\\\'"+mn+"\\\'></td>"+\n       "<td><input type=\\\'text\\\' class=\\\'pmx\\\' data-i=\\\'"+i+"\\\' value=\\\'"+mx+"\\\'></td></tr>";\n  });\n  t.innerHTML=h;\n}\nfunction collectParams(){\n  var out=[];\n  document.querySelectorAll(".pv").forEach(function(el){\n    var i=+el.dataset.i, r=PARAM_ROWS[i];\n    var mn=document.querySelector(".pmn[data-i=\\\'"+i+"\\\']").value;\n    var mx=document.querySelector(".pmx[data-i=\\\'"+i+"\\\']").value;\n    out.push({block:r.block,name:r.name,\n      value:parseFloat(el.value),min:mn===""?null:parseFloat(mn),max:mx===""?null:parseFloat(mx),\n      fixed:document.querySelector(".pfx[data-i=\\\'"+i+"\\\']").checked});\n  });\n  return out;\n}\nfunction renderReactions(){\n  var c=document.getElementById("reactionList"); c.innerHTML="";\n  var keys=Object.keys(catalog);\n  if(!keys.length){c.innerHTML=\'<div class="hint">没有目录 — 先连接(顶部),或网关窗口未开。</div>\';return;}\n  keys.forEach(function(rk){\n    var n=(catalog[rk]||[]).length;\n    var el=document.createElement("div"); el.className="opt";\n    el.innerHTML=\'<input type="checkbox" class="chk" value="\'+rk+\'"><span><span>\'+\n      (FAMILY_NAMES[rk]||rk.toUpperCase())+\'</span><span class="why">\'+(FAMILY_WHY[rk]||"")+\'</span></span>\'+\n      \'<span class="cnt">\'+n+\' datasets</span>\';\n    var cb=el.querySelector("input");\n    cb.onchange=function(){\n      if(cb.checked){families.add(rk);(catalog[rk]||[]).forEach(function(d){selected.add(rk+":"+d.id);});}\n      else{families.delete(rk);(catalog[rk]||[]).forEach(function(d){selected.delete(rk+":"+d.id);});}\n      renderDatasets();\n    };\n    c.appendChild(el);\n  });\n}\nfunction renderDatasets(){\n  var c=document.getElementById("datasetList"); c.innerHTML="";\n  if(!families.size){c.innerHTML=\'<div class="hint">勾选反应后,这里显示数据集明细(默认全选,取消即剔除)。</div>\';return;}\n  families.forEach(function(rk){\n    var g=document.createElement("div"); g.className="rgroup";\n    g.innerHTML=\'<h3>\'+(FAMILY_NAMES[rk]||rk)+\'</h3>\';\n    (catalog[rk]||[]).forEach(function(d){\n      var el=document.createElement("div"); el.className="opt";\n      el.innerHTML=\'<input type="checkbox" class="chk" value="\'+rk+":"+d.id+\'"><span>\'+d.id+\'</span>\';\n      var cb=el.querySelector("input"); cb.checked=true;\n      cb.onchange=function(){if(cb.checked)selected.add(rk+":"+d.id);else selected.delete(rk+":"+d.id);};\n      g.appendChild(el);\n    });\n    c.appendChild(g);\n  });\n}\nfunction loadCatalog(){\n  call("get_dataset_catalog",{card:objCard()}).then(function(r){\n    catalog=(r&&r.catalog)||{};\n    selected=new Set(); families=new Set();\n    renderReactions();\n  });\n  call("get_fit_parameters",{card:objCard()}).then(function(r){\n    PARAM_ROWS=(r&&r.parameters)||[];\n    renderParams();\n  });\n}\nfunction campaignOutdir(){return "001/"+ (val("campaign")||"agent_run");}\nfunction renderReview(){\n  var box=document.getElementById("reviewBox");\n  var ids=Array.from(selected).map(function(s){return s.split(":")[1];});\n  var obj=(document.querySelector(\'input[name=obj]:checked\')||{}).value||"pdf";\n  var rows=[["Non-perturbative object",obj],\n    ["Reactions",families.size+" 族 · "+ids.length+" 个数据集 · "+ids.slice(0,12).join(", ")+(ids.length>12?"…":"")],\n    ["QCD settings","Q0²="+val("q20")+" · TMC="+val("tmc")+" · HT="+val("ht")],\n    ["Campaign / 输出目录",campaignOutdir()],\n    ["Quality thresholds",val("qthresh")||"(未设,agent 用默认)"],\n    ["资源","自动:冷启动 10h / warm 2h · 4 核 · jupyter"]];\n  box.innerHTML=rows.map(function(r){return \'<div class="row"><span class="k">\'+r[0]+\'</span><span class="v">\'+r[1]+\'</span></div>\';}).join("");\n}\nasync function submitFit(){\n  var ids=Array.from(selected).map(function(s){return s.split(":")[1];}).join(",");\n  if(!ids){setSt("至少选一个数据集","err");return;}\n  setSt("提交中 …");\n  var r=await call("submit_fit_from_selection",{\n    base_card:objCard(),selected_ids:ids,out_card:"agent_selection_"+Date.now()%100000+".py",\n    outdir:campaignOutdir(),prior_msr:"",\n    cores:4,hours:0,partition:"jupyter",\n    qcd:{Q20:parseFloat(val("q20")||"1.6384"),tmc:val("tmc")==="true",ht:val("ht")==="true"},\n    params:collectParams()});\n  if(r&&r.status==="fired"){submitted=true;setSt("已提交 ✓","ok");}\n  else{setSt(r&&r.status?("提交状态: "+r.status):"提交失败","err");return;}\n  sync();\n}\nfunction boot(){\n  renderObjects();\n  renderParams();\n  var tabsEl=document.getElementById("tabs");\n  LABELS.forEach(function(l,i){\n    var el=document.createElement("div"); el.className="tab"+(i===0?" active":"");\n    el.innerHTML=\'<span class="num">\'+(i+1)+\'</span>\'+l;\n    el.onclick=function(){if(!submitted){stepIdx=i;sync();}};\n    tabsEl.appendChild(el);\n  });\n  sync();\n}\ndocument.getElementById("backBtn").onclick=function(){if(stepIdx>0){stepIdx--;sync();}};\ndocument.getElementById("nextBtn").onclick=function(){\n  if(stepIdx===STEPS.length-2){submitFit();return;}\n  if(stepIdx===1)renderDatasets();\n  stepIdx++;sync();\n};\nboot();\n</script>\n</body>\n</html>\n'

# ── http layer ──────────────────────────────────────────────────────────────

def _hlog(msg):
    try:
        with open(HTTPLOG, "a") as fh:
            fh.write(f"{time.time():.0f} {msg}\n")
    except OSError:
        pass


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        _hlog(f"GET {self.path} accept={self.headers.get('Accept')!r} "
              f"ua={self.headers.get('User-Agent', '')[:40]!r}")
        accept = self.headers.get("Accept") or ""
        if "text/html" in accept:
            ui = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("ui", [""])[0]
            body = (COMPOSER_HTML if ui == "composer" else CONSOLE_HTML).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if "text/event-stream" in accept:
            # streamable-http SSE channel; strict clients refuse anything else
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
              f"ua={self.headers.get('User-Agent', '')[:40]!r} "
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
                      "serverInfo": {"name": "jam-gateway", "version": "0.7.0"}}
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


# ── boot ────────────────────────────────────────────────────────────────────

def register():
    fqdn = socket.getfqdn() or socket.gethostname()
    url = f"http://{fqdn}:{PORT}/mcp"
    job_id = os.environ.get("SLURM_JOB_ID", "0")
    job_name = os.environ.get("SLURM_JOB_NAME", "jam-gateway")
    enc = urllib.parse.quote(url)
    target = (f"{PROXY}/server?mcp_name={job_name}&server_url={enc}"
              f"&owner={GATEWAY_OWNER}&job_id={job_id}&job_name={job_name}")
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
    print(f"jam-gateway v6 listening on {PORT}, host {socket.getfqdn()}", flush=True)
    if os.environ.get("SLURM_JOB_ID"):
        register()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
