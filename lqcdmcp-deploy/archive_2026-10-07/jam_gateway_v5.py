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


def _run(cmd, timeout=90, secret=None, cap=4000):
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
    return {"rc": p.returncode, "out": (p.stdout or "")[-cap:],
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
    return _remote_cap(user, cmd, timeout=timeout, cap=4000)


def _remote_cap(user, cmd, timeout=120, cap=4000):
    gw, fa = _socks(user)
    dest = f"{user}@localhost"
    port = str(_fwd_port(user))
    return _run(["ssh", "-S", fa, "-p", port, dest, "bash -lc " + _shq(cmd)],
                timeout=timeout, cap=cap)


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



# ================= v4: full experiment control =================

def _user_root(u):
    return f"/w/ceph24/JAM/{u}"


def fs_list(path: str = "", username: str = "") -> dict:
    """List a directory on ifarm (defaults to the user's JAM root)."""
    u, err = _need_user(username)
    if err:
        return err
    p = path.strip() or _user_root(u)
    if not p.startswith("/w/ceph24"):
        return {"status": "error", "message": "path must be under /w/ceph24"}
    out = _remote(u, f"ls -la {_shq(p)} 2>&1 | head -60", timeout=60)
    return {"status": "ok", "username": u, "path": p, **out}


def fs_read(path: str = "", max_lines: int = 200, username: str = "") -> dict:
    """Read a text file on ifarm (cards, logs, progress.json...)."""
    u, err = _need_user(username)
    if err:
        return err
    p = path.strip()
    if not p.startswith("/w/ceph24"):
        return {"status": "error", "message": "path must be under /w/ceph24"}
    ml = max(1, min(int(max_lines), 500))
    out = _remote(u, f"head -n {ml} {_shq(p)} 2>&1", timeout=60)
    return {"status": "ok", "username": u, "path": p, **out}


def fs_write(path: str = "", content: str = "", username: str = "") -> dict:
    """Write a text file on ifarm, restricted to the user's own JAM area."""
    u, err = _need_user(username)
    if err:
        return err
    p = os.path.normpath(path.strip())
    root = _user_root(u)
    if not p.startswith(root + "/"):
        return {"status": "error", "username": u,
                "message": f"write restricted to your own area under {root}"}
    import base64 as _b64
    b64 = _b64.b64encode((content or "").encode()).decode()
    out = _remote(u, f"mkdir -p {_shq(os.path.dirname(p))} && "
                  f"echo {b64} | base64 -d > {_shq(p)} && wc -c {_shq(p)}", timeout=60)
    return {"status": "ok", "username": u, "path": p, **out}


def setup_workspace(source: str = "chunyu", repo_url: str = "", username: str = "") -> dict:
    """Prepare /w/ceph24/JAM/<user>: fitpack_legacy (copied from <source>
    user's copy on the shared FS, or git-cloned from repo_url), the smoke4
    run tree, and an lhapdf_tables symlink."""
    u, err = _need_user(username)
    if err:
        return err
    fp = f"{_user_root(u)}/fitpack_legacy"
    steps = []
    exists = _remote(u, f"test -d {fp} && echo YES || echo NO", timeout=30)
    if "YES" in (exists.get("out") or ""):
        steps.append("fitpack_legacy already present")
    else:
        if repo_url.strip():
            out = _remote(u, f"git clone {_shq(repo_url.strip())} {_shq(fp)} 2>&1 | tail -3",
                          timeout=600)
            steps.append(f"git clone: rc={out['rc']} {out['out'][-200:]}")
        else:
            srcu = (source or "chunyu").strip()
            if not srcu.replace(".", "").replace("_", "").replace("-", "").isalnum():
                return {"status": "error", "message": "invalid source username"}
            out = _remote(u, f"cp -r {_shq(f'{_user_root(srcu)}/fitpack_legacy')} {_shq(fp)} 2>&1 | tail -3",
                          timeout=900)
            steps.append(f"cp from {srcu}: rc={out['rc']} {out['err'][-200:]}")
    rr = f"{_user_root(u)}/jam24repro/smoke4"
    out = _remote(u, f"mkdir -p {rr}/001 {rr}/logs && "
                  f"if [ ! -e {_user_root(u)}/lhapdf_tables ]; then "
                  f"ln -s /w/ceph24/JAM/chunyu/lhapdf_tables {_user_root(u)}/lhapdf_tables; fi; "
                  f"echo DONE", timeout=60)
    steps.append("run tree + lhapdf symlink: " + (out.get("out") or "").strip()[-80:])
    return {"status": "ok", "username": u, "steps": steps}


def search_datasets(card: str = "input_jam24_s7_warm.py", query: str = "", username: str = "") -> dict:
    """List the datasets (reaction + id) inside a JAM card, optionally
    filtered by a query (reaction name or id substring)."""
    u, err = _need_user(username)
    if err:
        return err
    fp = f"{_user_root(u)}/fitpack_legacy"
    card_path = f"{fp}/inputs/jam24/{card}"
    q = (query or "").strip().lower()
    py = (
        "import re\n"
        f"txt=open('{card_path}').read()\n"
        "rows=[]\n"
        "for m in re.finditer(r\"conf\\['datasets'\\]\\['(\\w+)'\\]\\['xlsx'\\]\\[(\\w+)\\]\\s*=\",txt):\n"
        "    reac,idx=m.group(1),m.group(2)\n"
        f"    if {q!r} and {q!r} not in (reac+idx).lower(): continue\n"
        "    rows.append((reac,idx))\n"
        "from collections import Counter\n"
        "c=Counter(reac for reac,idx in rows)\n"
        "print('datasets matching:', len(rows))\n"
        "print('by reaction:', dict(c))\n"
        "for reac,idx in rows[:40]: print(f'  {reac} {idx}')\n"
    )
    import base64 as _b64
    py_b64 = _b64.b64encode(py.encode()).decode()
    out = _remote(u, f"echo {py_b64} | base64 -d | python3", timeout=60)
    return {"status": "ok", "username": u, "card": card, **out}


def build_card(base_card: str = "", exclude_ids: str = "", out_card: str = "",
               username: str = "") -> dict:
    """Create a modified card: copy base_card and comment out the xlsx
    dataset lines whose ids appear in exclude_ids (comma-separated)."""
    u, err = _need_user(username)
    if err:
        return err
    if not base_card or not out_card:
        return {"status": "error", "message": "need base_card and out_card"}
    fp = f"{_user_root(u)}/fitpack_legacy"
    base_path = f"{fp}/inputs/jam24/{base_card}"
    out_path = f"{fp}/inputs/jam24/{out_card}"
    excl = [x.strip() for x in (exclude_ids or "").split(",") if x.strip()]
    py = (
        "import re\n"
        f"txt=open('{base_path}').read()\n"
        f"excl={excl!r}\n"
        "n=0\n"
        "def rep(m):\n"
        "    global n\n"
        "    if m.group(3) in excl:\n"
        "        n+=1\n"
        "        return '# '+m.group(0)\n"
        "    return m.group(0)\n"
        "txt2=re.sub(r\"(conf\\['datasets'\\]\\['(\\w+)'\\]\\['xlsx'\\]\\[(\\w+)\\])\",rep,txt)\n"
        f"open('{out_path}','w').write(txt2)\n"
        "print('commented out',n,'dataset lines')\n"
    )
    import base64 as _b64
    py_b64 = _b64.b64encode(py.encode()).decode()
    out = _remote(u, f"echo {py_b64} | base64 -d | python3", timeout=60)
    return {"status": "ok", "username": u, "base_card": base_card,
            "out_card": out_card, "excluded": excl, **out}


# ================= v5: cluster perception + chain state + composer =================

def cluster_status(username: str = "") -> dict:
    """Cluster perception: partitions, queue, and run-time hints so the LLM
    can size Slurm submissions sensibly (cores/hours/partition)."""
    u, err = _need_user(username)
    if err:
        return err
    cmd = ("echo '--- partitions ---'; sinfo -o '%P %a %D %C' 2>/dev/null | head -8; "
           f"echo '--- my queue ---'; squeue -u {u} -o '%.10i %.9P %.12j %.2t %.10M %.6D' | head -10; "
           "echo '--- run-time hints ---'; echo 'warm start: ~40 min @ 4 cores (jupyter)'; "
           "echo 'cold start: budget 10h @ 4 cores (jupyter, reference ladder)'; "
           "echo 'reference: step7 warm converges to chi2/N ~ 1.452'")
    out = _remote(u, cmd, timeout=90)
    return {"status": "ok", "username": u, **out}


def chain_state(outdir: str = "", username: str = "") -> dict:
    """Freeze/continue/reset judgment data: the newest msr's total chi2/N,
    per-dataset chi2/N (top offenders first), fitted parameter values, plus
    the neighbouring step dirs for ladder context."""
    u, err = _need_user(username)
    if err:
        return err
    fp, rr, ld = _paths(u)
    target = _resolve_outdir(u, outdir.strip()) if outdir.strip() else ""
    py = (
        "import sys, glob, statistics, os\n"
        f"sys.path.insert(0, '{fp}')\n"
        "from tools.tools import load\n"
        f"root='{rr}'\n"
        f"target={target!r}\n"
        "if target and os.path.isdir(target+'/msr'):\n"
        "    files=sorted(glob.glob(target+'/msr/*.msr'))\n"
        "else:\n"
        "    files=sorted(glob.glob(root+'/**/msr/*.msr', recursive=True), key=os.path.getmtime)\n"
        "def tot(d):\n"
        "    chi=d.get('chi2',{}); tn=tc=0.0\n"
        "    for reac,dd in chi.items():\n"
        "        for idx,info in dd.items(): tn+=info['npts']; tc+=info['chi2']\n"
        "    return tc/tn, chi\n"
        "if not files:\n"
        "    print('no msr found')\n"
        "else:\n"
        "    f=files[-1]\n"
        "    d=load(f)\n"
        "    t,chi=tot(d)\n"
        "    print('msr:', f)\n"
        "    print(f'total chi2/N={t:.3f}')\n"
        "    rows=[]\n"
        "    for reac,dd in chi.items():\n"
        "        for idx,info in dd.items(): rows.append((reac,idx,info['chi2']/max(info['npts'],1),info['npts']))\n"
        "    rows.sort(key=lambda r:-r[2])\n"
        "    print('worst 8 datasets (chi2/N):')\n"
        "    for reac,idx,v,n in rows[:8]: print(f'  {reac} {idx}: {v:.2f} ({n} pts)')\n"
        "    params=d.get('params',{})\n"
        "    steps=sorted(k for k in params if isinstance(k,int))\n"
        "    if steps:\n"
        "        p=params[steps[-1]]\n"
        "        print(f'params (step {steps[-1]}, {len(p)} total), first 12:')\n"
        "        order=d.get('order',{}).get(steps[-1],[])\n"
        "        for i,v in enumerate(p[:12]):\n"
        "            name=order[i][2] if i<len(order) and len(order[i])>2 else str(i)\n"
        "            print(f'  {name}: {v}')\n"
        "print('--- ladder context (per-step chi2/N) ---')\n"
        "for m in sorted(glob.glob(root+'/**/msr/*.msr', recursive=True), key=os.path.getmtime):\n"
        "    d2=load(m)\n"
        "    t2,_=tot(d2)\n"
        "    print(f'  {m.split(chr(47))[-3]}: {t2:.3f}')\n"
    )
    import base64 as _b64
    py_b64 = _b64.b64encode(py.encode()).decode()
    run = (f"echo {py_b64} | base64 -d | apptainer exec --bind /w/ceph24 "
           f"--env PYTHONPATH={fp} --env FITPACK={fp} {SIF} python3 -")
    out = _remote(u, run, timeout=300)
    return {"status": "ok", "username": u, **out}


def get_dataset_catalog(card: str = "input_jam24_s7_warm.py", username: str = "") -> dict:
    """Return the dataset catalog shape the fit composer expects:
    {reaction: [{id, label, experiment, default_on, enabled}]} for a card."""
    u, err = _need_user(username)
    if err:
        return err
    fp = f"{_user_root(u)}/fitpack_legacy"
    card_path = f"{fp}/inputs/jam24/{card}"
    py = (
        "import re, json\n"
        f"txt=open('{card_path}').read()\n"
        "c={}\n"
        "for m in re.finditer(r\"conf\\['datasets'\\]\\['(\\w+)'\\]\\['xlsx'\\]\\[(\\w+)\\]\\s*=\",txt):\n"
        "    reac,idx=m.group(1),m.group(2)\n"
        "    c.setdefault(reac,{})[idx]=True\n"
        "out={r:[{'id':i,'label':str(i),'experiment':'','default_on':True,'enabled':True} for i in sorted(ids)] for r,ids in c.items()}\n"
        "print(json.dumps(out))\n"
    )
    import base64 as _b64
    py_b64 = _b64.b64encode(py.encode()).decode()
    r = _remote_cap(u, f"echo {py_b64} | base64 -d | python3", timeout=60, cap=60000)
    try:
        import json as _json
        catalog = _json.loads(r["out"].strip().splitlines()[-1])
        return {"status": "ok", "username": u, "card": card, "catalog": catalog}
    except Exception:
        return {"status": "error", "username": u, "detail": r}


def submit_fit_from_selection(base_card: str = "input_jam24_s7_warm.py",
                              selected_ids: str = "", out_card: str = "agent_selection.py",
                              outdir: str = "", prior_msr: str = "", cores: int = 4,
                              hours: int = 2, partition: str = "jupyter",
                              username: str = "") -> dict:
    """Composer submission: keep only selected_ids (comma list like
    '10010,20001') in a derived card and submit it. Generates the card
    server-side via the same comment-out logic as build_card."""
    u, err = _need_user(username)
    if err:
        return err
    if not selected_ids.strip():
        return {"status": "error", "message": "need selected_ids (comma-separated)"}
    keep = set(x.strip() for x in selected_ids.split(",") if x.strip())
    fp = f"{_user_root(u)}/fitpack_legacy"
    base_path = f"{fp}/inputs/jam24/{base_card}"
    out_path = f"{fp}/inputs/jam24/{out_card}"
    py = (
        "import re\n"
        f"txt=open('{base_path}').read()\n"
        f"keep={sorted(keep)!r}\n"
        "n=0\n"
        "def rep(m):\n"
        "    global n\n"
        "    if m.group(3) not in keep:\n"
        "        n+=1\n"
        "        return '# '+m.group(0)\n"
        "    return m.group(0)\n"
        "txt2=re.sub(r\"(conf\\['datasets'\\]\\['(\\w+)'\\]\\['xlsx'\\]\\[(\\w+)\\])\",rep,txt)\n"
        f"open('{out_path}','w').write(txt2)\n"
        "print('commented out',n,'of the non-selected datasets')\n"
    )
    import base64 as _b64
    py_b64 = _b64.b64encode(py.encode()).decode()
    gen = _remote(u, f"echo {py_b64} | base64 -d | python3", timeout=60)
    if gen.get("rc") != 0:
        return {"status": "error", "username": u, "card_gen": gen}
    sub = submit_jam_fit(card=out_card, outdir=outdir, prior_msr=prior_msr,
                         cores=cores, seed=160300, hours=hours,
                         partition=partition, username=u)
    return {"status": sub.get("status"), "username": u, "out_card": out_card,
            "kept": sorted(keep), "card_gen": gen.get("out", "").strip(),
            "submit": sub}

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
    "fs_list": {
        "desc": "List a directory on ifarm (defaults to the user's JAM root). "
                "Use it to browse the workspace before reading or editing files.",
        "schema": {"type": "object",
                   "properties": {"path": {"type": "string"},
                                  "username": {"type": "string"}}},
        "fn": lambda a: fs_list(a.get("path", ""), a.get("username", ""))},
    "fs_read": {
        "desc": "Read a text file on ifarm (fit cards, slurm logs, progress.json). "
                "Path must be under /w/ceph24.",
        "schema": {"type": "object",
                   "properties": {"path": {"type": "string"},
                                  "max_lines": {"type": "integer"},
                                  "username": {"type": "string"}}},
        "fn": lambda a: fs_read(a.get("path", ""), int(a.get("max_lines", 200)),
                                a.get("username", ""))},
    "fs_write": {
        "desc": "Write a text file on ifarm, restricted to the user's own "
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
                                  "username": {"type": "string"}}},
        "fn": lambda a: setup_workspace(a.get("source", "chunyu"),
                                        a.get("repo_url", ""), a.get("username", ""))},
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
    "cluster_status": {
        "desc": "Cluster perception: partitions, the user's queue, and run-time "
                "hints. Call BEFORE submit_jam_fit to size cores/hours/partition "
                "sensibly for the current load.",
        "schema": {"type": "object",
                   "properties": {"username": {"type": "string"}}},
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
    "get_dataset_catalog": {
        "desc": "Return the dataset catalog for a card in the shape the fit "
                "composer needs: {reaction: [{id, label, default_on...}]}.",
        "schema": {"type": "object",
                   "properties": {"card": {"type": "string"},
                                  "username": {"type": "string"}}},
        "fn": lambda a: get_dataset_catalog(a.get("card", "input_jam24_s7_warm.py"),
                                            a.get("username", ""))},
    "submit_fit_from_selection": {
        "desc": "Composer-style submission: keep only selected_ids (comma list) "
                "in a server-generated derived card and submit it. Powers the "
                "web fit composer's Submit button.",
        "schema": {"type": "object",
                   "properties": {"base_card": {"type": "string"},
                                  "selected_ids": {"type": "string"},
                                  "out_card": {"type": "string"},
                                  "outdir": {"type": "string"},
                                  "prior_msr": {"type": "string"},
                                  "cores": {"type": "integer"},
                                  "hours": {"type": "integer"},
                                  "partition": {"type": "string"},
                                  "username": {"type": "string"}},
                   "required": ["selected_ids"]},
        "fn": lambda a: submit_fit_from_selection(
            a.get("base_card", "input_jam24_s7_warm.py"),
            a.get("selected_ids", ""), a.get("out_card", "agent_selection.py"),
            a.get("outdir", ""), a.get("prior_msr", ""),
            int(a.get("cores", 4)), int(a.get("hours", 2)),
            a.get("partition", "jupyter"), a.get("username", ""))},
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


COMPOSER_HTML = '<!doctype html><html><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>JAM Fit Composer</title>\n<style>\nbody{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;\nmargin:0;background:#f6f7f9;color:#15202b}\n.wrap{max-width:900px;margin:0 auto;padding:24px}\nh1{font-size:20px;margin:0 0 4px}.sub{color:#5c6774;font-size:13px;margin-bottom:16px}\n.steps{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap}\n.step{font-size:12px;padding:5px 10px;border-radius:999px;background:#eef1f5;cursor:pointer}\n.step.active{background:#1d5fd0;color:#fff}\n.card{background:#fff;border:1px solid #e0e4ea;border-radius:12px;padding:18px;margin-bottom:14px}\n.card h2{font-size:15px;margin:0 0 10px}\n.opt{display:flex;gap:10px;align-items:center;padding:7px 8px;border-radius:8px;cursor:pointer}\n.opt:hover{background:#f2f5f9}.opt input{width:auto}\n.cnt{margin-left:auto;color:#8a94a1;font-size:12px;white-space:nowrap}\nlabel{display:block;font-size:12.5px;font-weight:600;margin:8px 0 3px}\ninput,select{width:100%;font:inherit;padding:8px 10px;border:1px solid #ccd3dc;border-radius:8px;box-sizing:border-box}\nbutton{font:inherit;font-weight:600;padding:9px 18px;border:0;border-radius:8px;\nbackground:#1d5fd0;color:#fff;cursor:pointer;margin-top:12px;margin-right:8px}\nbutton.ghost{background:#eef1f5;color:#15202b}\npre{background:#0f1319;color:#e8ecf2;padding:12px;border-radius:8px;font-size:12px;\nmax-height:260px;overflow:auto;white-space:pre-wrap}\n.row{display:flex;gap:10px}.row>div{flex:1}\n#st{font-size:13px;color:#1d5fd0;margin:10px 0}\n.err{color:#c0392b}.hint{color:#5c6774;font-size:13px}\n.panel{display:none}.panel.active{display:block}\n</style></head><body><div class="wrap">\n<div class="card" style="display:flex;gap:10px;align-items:flex-end">\n<div style="flex:1"><label>JLab 用户名</label><input id="user" placeholder="chunyu"></div>\n<button onclick="doConnect()">连接</button>\n<button class="ghost" onclick="call(\'check_hpc\',{})">检查 HPC</button>\n<span id="connst" style="font-size:12.5px;color:#1d5fd0"></span>\n</div>\n<h1>JAM Fit Composer</h1>\n<div class="sub">选反应 → 选数据集 → 定预算 → 提交到 ifarm · 网关代写卡和 slurm</div>\n<div class="steps" id="steps"></div>\n\n<div class="panel" data-step="reactions">\n <div class="card"><h2>1 · 选反应族</h2>\n <div class="hint">勾选要包含的反应;每族数据集在下一步细选。</div>\n <div id="reactionList"></div></div>\n</div>\n\n<div class="panel" data-step="datasets">\n <div class="card"><h2>2 · 选数据集</h2>\n <div class="hint">默认全选;取消勾选即从拟合中剔除。</div>\n <div id="datasetList"></div></div>\n</div>\n\n<div class="panel" data-step="job">\n <div class="card"><h2>3 · 预算与提交</h2>\n <div class="row"><div>\n <label>基础卡(数据集全集来源)</label>\n <select id="basecard">\n  <option selected>input_jam24_s7_warm.py</option><option>input_jam24_s6_warm.py</option>\n  <option>input_jam24_s5_warm.py</option><option>input_jam24_s4_warm.py</option>\n  <option>input_jam24_s3_warm.py</option><option>input_jam24_s2_warm.py</option>\n  <option>input_jam24_s1_warm.py</option>\n </select></div><div>\n <label>输出目录</label><input id="outdir" placeholder="001/composer_run">\n </div></div>\n <div class="row"><div>\n <label>prior .msr(留空 = 冷启动)</label><input id="prior" placeholder="(可选)">\n </div><div>\n <label>hours(冷启动建议 10)</label><input id="hours" value="2" type="number">\n </div></div>\n <div class="row"><div>\n <label>cores</label><input id="cores" value="4" type="number">\n </div><div>\n <label>分区</label><select id="part"><option selected>jupyter</option><option>production</option><option>priority</option><option>ifarm</option></select>\n </div></div>\n <button onclick="submitFit()">提交拟合</button>\n <button class="ghost" onclick="call(\'cluster_status\',{})">看集群负载</button>\n <button class="ghost" onclick="call(\'chain_state\',{})">看阶梯状态</button>\n</div></div>\n\n<div class="panel" data-step="done">\n <div class="card"><h2>4 · 结果</h2>\n <div id="st"></div><pre id="out">(提交后这里显示结果;之后用控制台页的"读 χ²/对照 band"按钮跟踪)</pre></div>\n</div>\n\n<div id="nav" style="display:none">\n <button class="ghost" id="backBtn">上一步</button>\n <button id="nextBtn">下一步</button>\n</div>\n</div>\n<script>\nvar sid="", catalog={}, selected=new Set(), families=new Set(), stepIdx=0, submitted=false;\nvar STEPS=["reactions","datasets","job","done"];\nfunction val(id){return document.getElementById(id).value.trim();}\nfunction setSt(t,c){var e=document.getElementById("st");e.textContent=t;e.className=c||"";}\nfunction sync(){\n  document.querySelectorAll(".steps .step").forEach(function(el,i){\n    el.className="step"+(i===stepIdx?" active":"");});\n  document.querySelectorAll(".panel").forEach(function(p){\n    p.classList.toggle("active",p.dataset.step===STEPS[stepIdx]);});\n  document.getElementById("backBtn").style.visibility=(stepIdx===0||submitted)?"hidden":"visible";\n  document.getElementById("nextBtn").textContent=(stepIdx===STEPS.length-2)?"提交到 HPC":"下一步";\n  document.getElementById("nextBtn").style.display=(stepIdx===STEPS.length-1)?"none":"";\n}\nasync function call(tool,args){\n  setSt("调用 "+tool+" …");\n  var hdrs={"Content-Type":"application/json","Accept":"application/json, text/event-stream"};\n  if(sid)hdrs["Mcp-Session-Id"]=sid;\n  var r=await fetch(location.pathname,{method:"POST",headers:hdrs,\n    body:JSON.stringify({jsonrpc:"2.0",id:Date.now()%100000,method:"tools/call",\n    params:{name:tool,arguments:args}})});\n  var h=r.headers.get("Mcp-Session-Id"); if(h)sid=h;\n  var d=await r.json();\n  var txt=(d.result&&d.result.content)?d.result.content.map(function(c){return c.text||"";}).join("\\n"):JSON.stringify(d,null,1);\n  document.getElementById("out").textContent=txt;\n  setSt("完成 "+tool,"");\n  try{return JSON.parse(txt);}catch(e){return {raw:txt};}\n}\nfunction renderReactions(){\n  var c=document.getElementById("reactionList"); c.innerHTML="";\n  var keys=Object.keys(catalog);\n  if(!keys.length){c.innerHTML=\'<div class="hint">没有目录 — 先在下页点"看集群负载"确保链活着,或在控制台页连接。</div>\';return;}\n  keys.forEach(function(rk){\n    var n=(catalog[rk]||[]).length;\n    var el=document.createElement("div"); el.className="opt";\n    el.innerHTML=\'<input type="checkbox" class="chk" value="\'+rk+\'"><span>\'+rk.toUpperCase()+\'</span><span class="cnt">\'+n+\' 个数据集</span>\';\n    var cb=el.querySelector("input");\n    cb.onchange=function(){\n      if(cb.checked){families.add(rk);(catalog[rk]||[]).forEach(function(d){selected.add(rk+":"+d.id);});}\n      else{families.delete(rk);(catalog[rk]||[]).forEach(function(d){selected.delete(rk+":"+d.id);});}\n    };\n    c.appendChild(el);\n  });\n}\nfunction renderDatasets(){\n  var c=document.getElementById("datasetList"); c.innerHTML="";\n  if(!families.size){c.innerHTML=\'<div class="hint">先在上一步勾选反应族。</div>\';return;}\n  families.forEach(function(rk){\n    var head=document.createElement("div");\n    head.innerHTML=\'<strong style="font-size:13px">\'+rk.toUpperCase()+\'</strong>\';\n    c.appendChild(head);\n    (catalog[rk]||[]).forEach(function(d){\n      var el=document.createElement("div"); el.className="opt";\n      el.innerHTML=\'<input type="checkbox" class="chk" value="\'+rk+":"+d.id+\'"><span>\'+d.id+\'</span><span class="cnt">\'+d.id+\'</span>\';\n      var cb=el.querySelector("input"); cb.checked=true;\n      cb.onchange=function(){if(cb.checked)selected.add(rk+":"+d.id);else selected.delete(rk+":"+d.id);};\n      c.appendChild(el);\n    });\n  });\n}\nasync function submitFit(){\n  var ids=Array.from(selected).map(function(s){return s.split(":")[1];}).join(",");\n  if(!ids){setSt("至少选一个数据集","err");return;}\n  setSt("提交中 …");\n  var r=await call("submit_fit_from_selection",{\n    base_card:val("basecard"),selected_ids:ids,out_card:"agent_selection_"+Date.now()%100000+".py",\n    outdir:val("outdir"),prior_msr:val("prior"),\n    cores:parseInt(val("cores")||"4"),hours:parseInt(val("hours")||"2"),partition:val("part")});\n  if(r&&r.status==="fired"){submitted=true;setSt("已提交 ✓");}\n  else{setSt(r&&r.status?("提交状态: "+r.status):"提交失败","err");return;}\n  stepIdx=STEPS.length-1;sync();\n}\nasync function doConnect(){\n  var u=val("user"); if(!u){setSt("先填用户名","err");return;}\n  var t=await call("connect_hpc",{username:u});\n  document.getElementById("connst").textContent =\n    (t&&String(t).indexOf("reused")>=0)?"链已复用 · 无需凭证":(t&&String(t).indexOf("ok")>=0)?"已连接":(t&&String(t).indexOf("need pin_otp")>=0)?"需要凭证 — 去控制台页输入":String(t||"").slice(0,80);\n}\nasync function boot(){\n  var u=val("user");\n  if(u){await call("connect_hpc",{username:u});}\n  var r=await call("get_dataset_catalog",{card:val("basecard")});\n  catalog=(r&&r.catalog)||{};\n  renderReactions();\n  var stepsEl=document.getElementById("steps");\n  ["选反应","选数据集","预算","结果"].forEach(function(l,i){\n    var el=document.createElement("div"); el.className="step"+(i===0?" active":"");\n    el.textContent=(i+1)+" · "+l;\n    el.onclick=function(){if(!submitted){stepIdx=i;sync();}};\n    stepsEl.appendChild(el);\n  });\n  document.getElementById("nav").style.display="block";\n  sync();\n}\ndocument.getElementById("backBtn").onclick=function(){if(stepIdx>0){stepIdx--;sync();}};\ndocument.getElementById("nextBtn").onclick=function(){\n  if(stepIdx===STEPS.length-2){submitFit();return;}\n  if(stepIdx===0)renderDatasets();\n  stepIdx++;sync();\n};\nboot();\n</script></body></html>\n'


CONSOLE_HTML = '<!doctype html><html><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>JAM Gateway Console</title>\n<style>\nbody{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;\nmargin:0;background:#f6f7f9;color:#15202b}\n.wrap{max-width:860px;margin:0 auto;padding:24px}\nh1{font-size:20px;margin:0 0 4px}.sub{color:#5c6774;font-size:13px;margin-bottom:18px}\n.card{background:#fff;border:1px solid #e0e4ea;border-radius:12px;padding:18px;margin-bottom:14px}\n.card h2{font-size:15px;margin:0 0 10px}\nlabel{display:block;font-size:12.5px;font-weight:600;margin:8px 0 3px}\ninput,select{width:100%;font:inherit;padding:8px 10px;border:1px solid #ccd3dc;\nborder-radius:8px;box-sizing:border-box}\nbutton{font:inherit;font-weight:600;padding:8px 16px;border:0;border-radius:8px;\nbackground:#1d5fd0;color:#fff;cursor:pointer;margin-top:10px;margin-right:8px}\nbutton.ghost{background:#eef1f5;color:#15202b}\npre{background:#0f1319;color:#e8ecf2;padding:12px;border-radius:8px;font-size:12px;\nmax-height:300px;overflow:auto;white-space:pre-wrap}\n.row{display:flex;gap:10px}.row>div{flex:1}\n#st{font-size:13px;color:#1d5fd0;margin-top:8px}\n.err{color:#c0392b}\n</style></head><body><div class="wrap">\n<h1>JAM Gateway Console</h1>\n<div class="sub">qcd24s100 &rarr; scilogin &rarr; ifarm &middot; 每步都在这页点选,不用聊天</div>\n<div class="card">\n<h2>1 &middot; 连接(先只填用户名;链活着就不需要凭证)</h2>\n<label>JLab 用户名</label><input id="user" placeholder="chunyu">\n<label id="otplab" style="display:none">PIN+OTP(仅当提示需要时)</label>\n<input id="otp" style="display:none">\n<label id="pwlab" style="display:none">ifarm 密码(仅当提示需要时)</label>\n<input id="pw" type="password" style="display:none">\n<button onclick="doConnect()">连接</button>\n<button class="ghost" onclick="call(\'check_hpc\',{})">检查 HPC</button>\n<button class="ghost" onclick="call(\'list_jam_runs\',{})">清点 JAM 工作区</button>\n<div id="st"></div>\n</div>\n<div class="card">\n<h2>2 &middot; 提交拟合(点选)</h2>\n<div class="row"><div>\n<label>卡 card</label>\n<select id="card">\n<option>input_jam24_s1.py</option><option>input_jam24_s1_warm.py</option>\n<option>input_jam24_s2.py</option><option>input_jam24_s2_seed.py</option><option>input_jam24_s2_warm.py</option>\n<option>input_jam24_s3.py</option><option>input_jam24_s3_seed.py</option><option>input_jam24_s3_warm.py</option>\n<option>input_jam24_s4.py</option><option>input_jam24_s4_seed.py</option><option>input_jam24_s4_warm.py</option>\n<option>input_jam24_s5.py</option><option>input_jam24_s5_seed.py</option><option>input_jam24_s5_warm.py</option>\n<option>input_jam24_s6.py</option><option>input_jam24_s6_seed.py</option><option>input_jam24_s6_warm.py</option>\n<option>input_jam24_s7.py</option><option>input_jam24_s7_seed.py</option><option selected>input_jam24_s7_warm.py</option>\n</select>\n</div><div>\n<label>输出目录 outdir(相对 smoke4)</label><input id="outdir" placeholder="001/step7_claude">\n</div></div>\n<div class="row"><div>\n<label>prior .msr(留空 = 冷启动;冷启动请把 hours 设为 10+)</label>\n<input id="prior" placeholder="/w/ceph24/JAM/chunyu/jam24repro/smoke4/001/step6_v2/msr/8XW0AVCIGLJ8.msr">\n</div><div>\n<label>预算 hours</label><input id="hours" value="2" type="number">\n</div></div>\n<div class="row"><div>\n<label>cores</label><input id="cores" value="4" type="number">\n</div><div>\n<label>分区</label>\n<select id="part"><option selected>jupyter</option><option>production</option><option>priority</option><option>ifarm</option></select>\n</div></div>\n<button onclick="submitFit()">提交拟合</button>\n<button class="ghost" onclick="call(\'get_jam_result\',{outdir:val(\'outdir\')})">查状态</button>\n<button class="ghost" onclick="call(\'read_chi2\',{outdir:val(\'outdir\')})">读 &chi;²</button>\n<button class="ghost" onclick="call(\'evaluate_fit\',{outdir:val(\'outdir\')})">对照论文 band</button>\n</div>\n<div class="card"><h2>3 &middot; 输出</h2><pre id="out">(点上面任意按钮,结果显示在这里)</pre></div>\n</div>\n<script>\nvar sid="";\nfunction val(id){return document.getElementById(id).value.trim();}\nfunction show(id,s){document.getElementById(id).style.display=s?"":"none";}\nfunction setSt(t,c){var e=document.getElementById("st");e.textContent=t;e.className=c||"";}\nasync function call(tool,args){\n  setSt("调用 "+tool+" &hellip;");\n  try{\n    var hdrs={"Content-Type":"application/json","Accept":"application/json, text/event-stream"};\n    if(sid)hdrs["Mcp-Session-Id"]=sid;\n    var r=await fetch(location.pathname,{method:"POST",headers:hdrs,\n      body:JSON.stringify({jsonrpc:"2.0",id:Date.now()%100000,method:"tools/call",\n      params:{name:tool,arguments:args}})});\n    var h=r.headers.get("Mcp-Session-Id"); if(h)sid=h;\n    var d=await r.json();\n    var t=(d.result&&d.result.content)?d.result.content.map(function(c){return c.text||"";}).join("\\n"):JSON.stringify(d,null,1);\n    document.getElementById("out").textContent=t;\n    setSt("完成 "+tool,"");\n    return t;\n  }catch(e){setSt("失败: "+e,"err");document.getElementById("out").textContent=String(e);}\n}\nasync function doConnect(){\n  var u=val("user"); if(!u){setSt("先填用户名","err");return;}\n  var t=await call("connect_hpc",{username:u});\n  if(t&&t.indexOf("need pin_otp")>=0){\n    show("otplab",true);show("otp",true);show("pwlab",true);show("pw",true);\n    setSt("链不活 &mdash; 需要凭证,填 PIN+OTP 和密码后再点一次连接","err");\n  } else if(t&&t.indexOf("reused")>=0){setSt("链已复用,无需凭证");}\n  else if(val("otp")&&val("pw")){\n    await call("connect_hpc",{username:u,pin_otp:val("otp"),password:val("pw")});\n  }\n}\nasync function submitFit(){\n  await call("submit_jam_fit",{card:val("card"),outdir:val("outdir"),prior_msr:val("prior"),\n    cores:parseInt(val("cores")||"4"),hours:parseInt(val("hours")||"10"),partition:val("part")});\n}\n</script></body></html>\n'


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        _hlog(f"GET {self.path} accept={self.headers.get('Accept')!r} "
              f"ua={self.headers.get('User-Agent','')[:40]!r}")
        if "text/html" in (self.headers.get("Accept") or ""):
            ui = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("ui", [""])[0]
            page = COMPOSER_HTML if ui == "composer" else CONSOLE_HTML
            body = page.encode()
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
                      "serverInfo": {"name": "jam-gateway", "version": "0.5.0"}}
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
