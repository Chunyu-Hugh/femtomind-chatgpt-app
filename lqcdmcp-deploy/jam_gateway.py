"""jam_gateway — one person's MCP gateway for JAM fits on an HPC site.

Launched by its owner as a Slurm job on the LQCDMCP platform (redeploy_jam.py
or the dashboard page), it registers with the proxy as jam-gateway-<user> and
serves, at https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<user>/mcp:
  * MCP tools for AI clients (Claude Code, VS Code, Claude Desktop, ...),
  * the operations panel (jam_panel.html): Connect / Project / Campaign /
    Collections / Analysis, as a browser page and as an MCP Apps resource.
Stdlib only; plain JSON-RPC over HTTP.

Access: every tool call needs the owner's LQCDMCP dashboard token (Bearer).
The gateway asks the dashboard who the token's user is and which MCP servers
they launched, and accepts the token only when that user is its owner and
this gateway is among them. The proxy itself checks nobody. As of October
2026 the dashboard answers for the user named in validate_user whatever the
token is, so this check does not keep other people out; the owner stops the
gateway when done (redeploy_jam.py cancel).

Cluster: a two-hop SSH chain per user (login host with PIN + one-time code,
then the compute host with the password). Credentials are typed on the
panel's Connect tab — connect_hpc also accepts them as arguments, but AI
clients are told never to ask for them. Everything site-specific is in SITE
(ifarm built in; ~/.jam_gateway/site.json overrides).

Fits: a campaign is the JAM app's composer (object and parameter table,
reactions, datasets and cuts, QCD settings, job config, review). The
parameter table is the workspace's own examples/input02.py (the library);
jamai/ copies that file and appends the choices, with only the parameters
the user changed, and a Slurm array runs one replica per task through
fitlib/ai.py (FitPack `ai` branch) inside the site's container. Collections:
stop or delete a campaign. Analysis: postprocess, fit summary, PDF plots,
chi2 and data vs theory by experiment, each replica's state and log, more
replicas (with a confirmation phrase). Any other job: LLM-guided Slurm
drafts checked with sbatch --test-only.

Sections, in order: constants and site · local and remote helpers · access ·
cluster chain · tools · campaigns · tool registry · http · boot.
"""

import base64
import getpass
import hashlib
import hmac
import json
import math
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

PROXY = "https://lqcdmcp.jlab.org:8000"
HERE = os.path.dirname(os.path.abspath(__file__))
CM_DIR = os.path.expanduser("~/fm-cm")          # ControlMaster sockets
FA_FWD_PORT_BASE = 2222                          # + crc32(user) % 400

# One instance per person: the owner is the account the job runs under, and
# the proxy route is the job name (jam-gateway-<user> from redeploy_jam.py).
GATEWAY_OWNER = os.environ.get("JAM_GW_OWNER") or getpass.getuser()
MCP_NAME = (os.environ.get("JAM_GW_NAME") or os.environ.get("SLURM_JOB_NAME")
            or f"jam-gateway-{GATEWAY_OWNER}")
# Several people's gateways can share a node, so 8080 is only a first choice.
PORT = int(os.environ.get("JAM_GW_PORT") or 0)
BIND = os.environ.get("JAM_GW_BIND", "0.0.0.0")   # 127.0.0.1 for a local test run
# Where users reach this gateway's /mcp endpoint, for the panel links it hands
# out. Defaults to its route on the LQCDMCP proxy; a gateway run elsewhere (a
# local test) sets its own, e.g. http://host:8090/mcp.
PUBLIC_URL = os.environ.get("JAM_GW_PUBLIC_URL", "").rstrip("/")

# Where this instance keeps its log, site override and app folder (mode 700).
ACCESS_DIR = os.path.expanduser(os.environ.get("JAM_GW_ACCESS_DIR", "~/.jam_gateway"))
HTTPLOG = os.path.join(ACCESS_DIR, "http.log")

# ── site ────────────────────────────────────────────────────────────────────
#
# Everything that differs between HPC sites. {user} is filled in per call.
# A JSON file (JAM_GW_SITE_FILE, default ~/.jam_gateway/site.json) may
# override any key; nested dicts are replaced as a whole.

SITE_IFARM = {
    "id": "ifarm",
    "label": "JLab ifarm",
    # Two hops: the login host takes the first secret, the compute host the
    # second; the second hop rides a port forward through the first.
    "login_host": "scilogin.jlab.org",
    "login_secret": "PIN + one-time code",
    "compute_host": "ifarm.jlab.org",
    "compute_secret": "account password",
    # The user's own area: workspaces and the workspace marker.
    "area": "/w/ceph24/JAM/{user}",
    # fs_list / fs_read may look anywhere under these.
    "read_roots": ["/w/ceph24"],
    "container": {"image": "/u/scigroup/scicomp/jupyterhub/theory/theory-notebook.sif",
                  "binds": ["/w/ceph24"]},
    # Shared LHAPDF tables the replicas search (LHAPDF_DATA_PATH); never a
    # person's own folder.
    "lhapdf_extra": ["/w/ceph24/JAM/apps/lhapdf_tables/more/more1"],
    # Interpolation grids a fresh clone lacks (the repository ships only
    # grids/lqcd_itd). Each is linked, never copied, at <ws>/grids/<name>
    # when missing; nothing is ever written to the targets. grids/idis2.0 is
    # not here: obslib/idis creates and fills it itself.
    # Only the shared grid area, as RedstarFM does — never a person's own folder.
    "grid_links": {n: f"/w/ceph24/JAM/share/fitpack_grids/{n}"
                   for n in ("dy", "dypion", "jet", "pjet", "pp_hadron", "wasym", "wc", "wzrv", "zrap")},
    # Partition when none is asked for: jobs that fit in short_hours go to the
    # short partition (fastest to start), longer ones to the batch partition.
    "partition": "production",          # ifarm batch default, 2-day limit
    "short_partition": "priority",      # 4-hour limit
    "short_hours": 4,
    "partitions": ["production", "priority", "ifarm", "jupyter"],
    # Every card the gateway derives reads its grids from here.
    "grid_root": "/w/ceph24/JAM/share/fitpack_grids",
}


def _load_site():
    site = dict(SITE_IFARM)
    path = os.path.expanduser(os.environ.get("JAM_GW_SITE_FILE",
                                             os.path.join(ACCESS_DIR, "site.json")))
    try:
        with open(path) as fh:
            over = json.load(fh)
        if isinstance(over, dict):
            site.update(over)
            print(f"site overrides loaded from {path}: {sorted(over)}", flush=True)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"ignoring unreadable site file {path}: {e}", flush=True)
    return site


SITE = _load_site()

# Shared mode: the gateway acts as the JLab user named in each call (the
# person who signed in on the panel's Connect tab), not only as its owner.
# For colleagues who cannot launch a gateway of their own, e.g. while they
# are not on the reservation's user list. JAM_GW_SHARED=1 at launch, or
# "shared": true in site.json. Each user has their own chain and workspace.
SHARED = os.environ.get("JAM_GW_SHARED", "").strip() == "1" or bool(SITE.get("shared"))
USERNAME_RE = r"[a-z0-9._-]{1,32}"


def _area(user):
    return SITE["area"].format(user=user)

# Access: the owner's LQCDMCP dashboard token is the only credential (see
# "dashboard tokens" below). JAM_GW_TEST_TOKEN is for a test instance that runs
# off the proxy and so is unknown to the dashboard; it is never set in a launch.
TEST_TOKEN = os.environ.get("JAM_GW_TEST_TOKEN", "")

# The operations panel. One HTML file, served as a browser page on GET and as
# an MCP Apps resource. Two URIs because ChatGPT still keys its widget on the
# legacy skybridge MIME while every other MCP Apps host uses the standard one.
PANEL_FILE = os.environ.get("JAM_GW_PANEL", os.path.join(HERE, "jam_panel.html"))
PANEL_URI = "ui://jam-gateway/panel-v1.html"
PANEL_URI_SKYBRIDGE = "ui://jam-gateway/panel-v1.skybridge.html"
PANEL_MIME = "text/html;profile=mcp-app"

# New workspaces clone this repository, so every collaborator runs the same
# validated code.
JAM_REPO_URL = "https://github.com/QCDHUB/fitpack_legacy.git"

# ── local helpers ───────────────────────────────────────────────────────────

def _run(cmd, timeout=90, secret=None, cap=4000, stdin=None):
    """Run a local command; optionally feed ONE secret via SSH_ASKPASS, and
    text on its standard input (never on a command line, which any user of
    the host can read)."""
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
                           timeout=timeout, env=env, input=stdin)
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
    Recorded in <area>/.jam_workspace at setup time; the default is the
    conventional clone target."""
    if user not in _WS_CACHE:
        area = _area(user)
        r = _remote(user, f"cat {area}/.jam_workspace 2>/dev/null", timeout=30)
        _WS_CACHE[user] = (r.get("out") or "").strip() or f"{area}/fitpack_legacy"
    return _WS_CACHE[user]


def _container_exec(fp, extra_path="", env=None):
    """apptainer exec prefix that runs python3 inside the site's image with the
    workspace (and extra_path, if any) on the path."""
    c = SITE["container"]
    binds = " ".join(f"--bind {b}" for b in c.get("binds", []))
    pypath = fp + (f":{extra_path}" if extra_path else "")
    envs = "".join(f" --env {k}={_shq(str(v))}" for k, v in (env or {}).items())
    return (f"apptainer exec {binds} --env PYTHONPATH={_shq(pypath)} --env FITPACK={_shq(fp)}{envs} "
            f"{c['image']} python3")


def _remote(user, cmd, timeout=120, cap=4000, stdin=None):
    """Run a shell command on the compute host through the user's chain;
    stdin, if given, is what the command reads on its standard input."""
    _, fa = _socks(user)
    return _run(["ssh", "-S", fa, "-p", str(_fwd_port(user)), f"{user}@localhost",
                 "bash -lc " + _shq(cmd)], timeout=timeout, cap=cap, stdin=stdin)


def _run_ifarm_py(user, code, apptainer=False, timeout=300, cap=4000, extra_path="", env=None):
    """Ship a python snippet to the compute host as base64 and run it (avoids
    all shell-quoting issues). apptainer=True runs inside the site's image
    (needed for fitpack/numpy imports)."""
    b64 = base64.b64encode(code.encode()).decode()
    if apptainer:
        run = f"echo {b64} | base64 -d | {_container_exec(_workspace(user), extra_path, env)} -"
    else:
        run = f"echo {b64} | base64 -d | python3"
    return _remote(user, run, timeout=timeout, cap=cap)


def _fill(template, **values):
    """A python snippet: template with each __NAME__ replaced by repr(value),
    in one pass, so a value that contains another placeholder (a campaign or
    folder name, say) stays as it is."""
    subs = {f"__{k}__": repr(v) for k, v in values.items()}
    return re.sub("|".join(map(re.escape, subs)), lambda m: subs[m.group(0)], template)


# ── access ──────────────────────────────────────────────────────────────────
#
# A request carries "Authorization: Bearer <dashboard token>". A token of the
# owner resolves to the owner, and every tool acts as the owner and nobody
# else. Tools take no username argument; the functions' username parameter
# serves calls between them, and _resolve_user refuses any other name.

_tls = threading.local()
AUTH_HELP = ("This gateway needs your LQCDMCP dashboard token (the one from the dashboard "
             "login you launched the gateway with). Configure the MCP client to send the header "
             "'Authorization: Bearer <token>', or paste it into the panel page when it asks for "
             "one. When the dashboard login expires, log in again and update the token.")


def _req_sid(hdrs):
    sid = (hdrs.get("Mcp-Session-Id") or hdrs.get("mcp-session-id") or "").strip()
    return sid or ("jam-" + secrets.token_hex(8))


def _sha(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _ensure_access_dir():
    os.makedirs(ACCESS_DIR, mode=0o700, exist_ok=True)
    os.chmod(ACCESS_DIR, 0o700)


def _identify(auth_header):
    """Resolve a bearer token to the username it acts as, or '' if unusable."""
    if not auth_header or auth_header[:7].lower() != "bearer ":
        return ""
    token = auth_header[7:].strip()
    if token[:7].lower() == "bearer ":
        # A client that was given "Bearer <token>" as the token sends it twice.
        token = token[7:].strip()
    if not token:
        return ""
    if TEST_TOKEN and hmac.compare_digest(token, TEST_TOKEN):
        return GATEWAY_OWNER
    return _dashboard_identify(token, _sha(token))


# ── dashboard tokens ─────────────────────────────────────────────────────────
#
# The LQCDMCP dashboard token (Globus login) opens this gateway: the
# dashboard is asked who the token's user is and which MCP servers they
# launched, and the token is the owner's when that user is this gateway's
# owner and the list holds this gateway (its name, owner and Slurm job). A
# listing the gateway cannot read is a refusal. The proxy itself forwards
# /cloud/<name>/mcp without checking anyone. As of October 2026 the dashboard
# does not verify the token either (validate_user answers for whatever
# username it is given), so this check only keeps out callers who name the
# wrong gateway; it is not what keeps a gateway with an open cluster chain
# private. Until the dashboard checks tokens, stopping the gateway is.

DASHBOARD_URL = os.environ.get("JAM_GW_DASHBOARD_URL", f"{PROXY}/jlab/")
DASHBOARD_AUTH = os.environ.get("JAM_GW_DASHBOARD_AUTH", "1") != "0"
_DASH_CACHE = {}                    # token hash -> (username or '', expires at)
_DASH_LOCK = threading.Lock()


def _dashboard_call(token, method, params, sid=None, timeout=20):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    hdrs = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}
    if sid:
        hdrs["Mcp-Session-Id"] = sid
    with urllib.request.urlopen(urllib.request.Request(DASHBOARD_URL, data=body, headers=hdrs),
                                timeout=timeout) as r:
        sid = r.headers.get("Mcp-Session-Id") or sid
        raw = r.read().decode(errors="replace")
    events = re.findall(r"^data: (.*)$", raw, re.M) or [raw]
    for ev in reversed(events):
        try:
            return json.loads(ev), sid
        except ValueError:
            continue
    return {}, sid


def _dashboard_json(resp):
    """The JSON a dashboard tool result carries: each content text that parses
    as JSON, and the structuredContent."""
    res = (resp or {}).get("result") or {}
    out = []
    for c in res.get("content") or []:
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            try:
                out.append(json.loads(c["text"]))
            except ValueError:
                pass
    if res.get("structuredContent") is not None:
        out.append(res["structuredContent"])
    return out


def _dicts_with(values, *keys):
    """Every dict, at any depth of values, that has one of keys."""
    found, todo = [], list(values)
    while todo:
        v = todo.pop()
        if isinstance(v, dict):
            if any(k in v for k in keys):
                found.append(v)
            todo.extend(v.values())
        elif isinstance(v, list):
            todo.extend(v)
    return found


def _dashboard_owns_gateway(token):
    """True when the dashboard says this token's user is the owner and
    launched this gateway."""
    _, sid = _dashboard_call(token, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                   "clientInfo": {"name": MCP_NAME, "version": "1"}})
    # The dashboard expects validate_user before other calls in a session.
    resp, _ = _dashboard_call(token, "tools/call", {"name": "validate_user",
                                                    "arguments": {"username": GATEWAY_OWNER}}, sid)
    if (resp.get("result") or {}).get("isError"):
        return False
    for info in _dicts_with(_dashboard_json(resp), "allow_mcp", "user_account", "user_id"):
        if info.get("allow_mcp") is False:
            return False
        names = [str(info[k]) for k in ("user_account", "user_id") if k in info]
        if names and GATEWAY_OWNER not in names:
            return False
    resp, _ = _dashboard_call(token, "tools/call", {"name": "get_my_mcp_servers",
                                                    "arguments": {"unused": ""}}, sid)
    if (resp.get("result") or {}).get("isError"):
        return False
    entries = _dicts_with(_dashboard_json(resp), "mcp_name")
    if not entries:
        _hlog("dashboard listing not recognised")
        return False
    job = os.environ.get("SLURM_JOB_ID", "")
    for e in entries:
        if e["mcp_name"] != MCP_NAME or ("owner" in e and e["owner"] != GATEWAY_OWNER):
            continue
        if job and e.get("slurm_job_id") not in (None, "") and str(e["slurm_job_id"]) != job:
            continue
        return True
    return False


def _dashboard_identify(token, h):
    if not DASHBOARD_AUTH or len(token) < 16:
        return ""
    now = time.time()
    with _DASH_LOCK:
        hit = _DASH_CACHE.get(h)
        if hit and hit[1] > now:
            return hit[0]
    try:
        user = GATEWAY_OWNER if _dashboard_owns_gateway(token) else ""
        ttl = 600 if user else 60
    except Exception as e:
        _hlog(f"dashboard check failed: {type(e).__name__}")
        user, ttl = "", 15
    with _DASH_LOCK:
        if len(_DASH_CACHE) > 256:
            _DASH_CACHE.clear()
        _DASH_CACHE[h] = (user, now + ttl)
    return user


def _resolve_user(username):
    """The user this request acts as — always the token's user."""
    bound = getattr(_tls, "user", "")
    if not bound:
        return "", {"status": "unauthorized", "message": AUTH_HELP}
    asked = (username or "").strip()
    if asked and asked != bound:
        return "", {"status": "error",
                    "message": f"this token belongs to {bound}; it cannot act as {asked}"}
    return bound, None


def _hops(u):
    """(login hop open, whole chain open) for u's two-hop chain."""
    gw, fa = _socks(u)
    login = _master_alive(gw, f"{u}@{SITE['login_host']}")
    return login, login and _master_alive(fa, f"{u}@localhost", ["-p", str(_fwd_port(u))])


def _chain_alive(u):
    return _hops(u)[1]


def _need_user(username):
    """Resolve the acting user AND require a live chain."""
    u, err = _resolve_user(username)
    if err:
        return u, err
    if not _chain_alive(u):
        return u, {"status": "disconnected", "username": u,
                   "message": f"no live chain for {u}; call connect_hpc first"}
    return u, None


# ── chain ───────────────────────────────────────────────────────────────────

def _close_master(sock, dest, opts=None):
    """Before a hop is rebuilt: close its master if it still answers, then
    remove a stale socket file."""
    if _master_alive(sock, dest, opts):
        _run(["ssh", "-S", sock, "-O", "exit"] + (opts or []) + [dest], timeout=15)
    try:
        os.unlink(sock)
    except OSError:
        pass


def connect_hpc(username="", pin_otp="", password=""):
    u, err = _resolve_user(username)
    if err:
        return dict(err, connected=False)
    os.makedirs(CM_DIR, mode=0o700, exist_ok=True)
    gw, fa = _socks(u)
    gwd = f"{u}@{SITE['login_host']}"
    port = str(_fwd_port(u))
    login_open, alive = _hops(u)
    # reuse FIRST — never touch a live chain
    if alive:
        check = _remote(u, "hostname; whoami; date -u", timeout=30)
        return {"status": "reused", "connected": True, "username": u,
                "note": "existing ControlMasters alive — no OTP needed",
                "remote": check["out"][:200], "check": check["rc"]}
    # What is needed is decided before any socket is touched: a live login
    # hop carries a new compute hop, so then only the password is asked for.
    needs = ([] if login_open or pin_otp else ["pin_otp"]) + ([] if password else ["password"])
    if needs:
        what = (f"{SITE['login_host']} is still open, so only the {SITE['compute_secret']} for "
                f"{SITE['compute_host']} (password) is needed" if login_open else
                f"Signing in needs the {SITE['login_secret']} for {SITE['login_host']} (pin_otp, "
                f"no space) and the {SITE['compute_secret']} for {SITE['compute_host']} (password)")
        return {"status": "error", "connected": False, "username": u, "needs": needs,
                "message": what + ". Credentials are typed on the panel's Connect tab "
                                  "(get_console tab=connect), never in chat."}
    hop_opts = ["-o", "ControlPersist=8h", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "NumberOfPasswordPrompts=2", "-o", "PubkeyAuthentication=no",
                "-o", "PreferredAuthentications=keyboard-interactive,password"]
    # The compute hop is always rebuilt: it is down, or it rode a login hop
    # that is. The login hop is rebuilt only when it is down.
    _close_master(fa, f"{u}@localhost", ["-p", port])
    if not login_open:
        _close_master(gw, gwd)
        r1 = _run(["setsid", "ssh", "-M", "-S", gw, *hop_opts, "-fN", gwd],
                  timeout=40, secret=pin_otp)
        if not _master_alive(gw, gwd):
            return {"status": "error", "connected": False, "step": "gateway", "username": u,
                    "message": f"{SITE['login_host']} failed: {r1['err'][-400:] or r1['out'][-200:]}"}
    # A forward the login hop already has is accepted as it is, so this also
    # restores one that a live login hop lost.
    r2 = _run(["ssh", "-S", gw, "-O", "forward",
               "-L", f"127.0.0.1:{port}:{SITE['compute_host']}:22", gwd], timeout=20)
    if r2["rc"] != 0:
        return {"status": "error", "connected": False, "step": "forward", "username": u,
                "message": r2["err"][-300:]}
    r3 = _run(["setsid", "ssh", "-M", "-S", fa, *hop_opts, "-fN",
               "-p", port, f"{u}@localhost"], timeout=40, secret=password)
    if not _master_alive(fa, f"{u}@localhost", ["-p", port]):
        return {"status": "error", "connected": False, "step": "compute", "username": u,
                "message": f"{SITE['compute_host']} failed: {r3['err'][-400:] or r3['out'][-200:]}"}
    check = _remote(u, "hostname; whoami; date -u", timeout=60)
    return {"status": "ok", "connected": True, "username": u, "site": SITE["id"],
            "chain": f"node->{SITE['login_host']}->{SITE['compute_host']}",
            "remote": check["out"][:300], "check": check["rc"]}


# ── tools (flat, one per function) ──────────────────────────────────────────

def ping():
    return "pong from LQCD gateway node"


PANEL_TABS = ("connect", "project", "campaign", "collections", "analysis")


def _panel_urls(tab="connect"):
    # Any browser GET of the gateway URL is answered with the panel.
    q = "?tab=" + urllib.parse.quote(tab)
    return [{"entry": "direct", "url": (PUBLIC_URL or f"{PROXY}/cloud/{MCP_NAME}/mcp") + q}]


def get_console(tab="connect"):
    tab = tab if tab in PANEL_TABS else "connect"
    return {
        "panel_urls": _panel_urls(tab),
        "guidance": "Give the user a panel_url to open in a browser. Tabs: Connect (sign in to "
                    "the cluster), Project (check or create the workspace), Campaign (build and "
                    "submit a fit), Collections (campaigns, the Slurm queue, stop or delete a "
                    "campaign) and Analysis (postprocess, fit summary, PDF plots, by experiment, "
                    "logs, more replicas). Cluster credentials are typed only on that page, never into "
                    "chat. The page asks once for the user's LQCDMCP dashboard token."}


def node_diag(cmd="id; hostname"):
    """Shell ON THE GATEWAY NODE (hot-redeploy + diagnostics)."""
    r = _run(["bash", "-lc", cmd], timeout=30)
    return {"rc": r["rc"], "out": r["out"], "err": r["err"]}


def _outside_read_roots(p):
    roots = SITE["read_roots"]
    if any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots):
        return None
    return {"status": "error", "message": "path must be under " + " or ".join(roots)}


def fs_list(path="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    p = os.path.normpath(path.strip() or _area(u))
    bad = _outside_read_roots(p)
    if bad:
        return bad
    out = _remote(u, f"ls -la {_shq(p)} 2>&1 | head -60", timeout=60)
    return {"status": "ok", "username": u, "path": p, **out}


def fs_read(path="", max_lines=200, username=""):
    u, err = _need_user(username)
    if err:
        return err
    p = os.path.normpath(path.strip())
    bad = _outside_read_roots(p)
    if bad:
        return bad
    ml = max(1, min(int(max_lines), 500))
    out = _remote(u, f"head -n {ml} {_shq(p)} 2>&1", timeout=60)
    return {"status": "ok", "username": u, "path": p, **out}


# A workspace path goes into shell commands and Slurm scripts on the cluster,
# so it may hold only these characters.
WORKSPACE_PATH_RE = r"/[A-Za-z0-9._/-]+"
# The recorded workspace folder was deleted or moved: a branch switch cannot help.
WORKSPACE_GONE = ("The workspace folder {} no longer exists: choose another workspace on the Project "
                  "tab or create a new one.")


def setup_workspace(repo_url="", github_token="", target_dir="", username="",
                    folder_name="", branch=""):
    """git clone the JAM code (the FitPack `ai` branch) to target_dir (default
    <area>/fitpack_legacy), record it as THE workspace and link the shared
    interpolation grids. A folder that is already there is not cloned again.

    repo_url defaults to the JAM repository. folder_name is the panel's way of
    naming target_dir inside the user's own area; branch picks the branch to
    clone."""
    u, err = _need_user(username)
    if err:
        return dict(err, ok=False)
    root = _area(u)
    folder = (folder_name or "").strip()
    if folder and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", folder):
        return {"status": "error", "ok": False,
                "message": "Folder name may use letters, numbers, '.', '_' and '-' only."}
    target = os.path.normpath((target_dir or "").strip()
                              or (f"{root}/{folder}" if folder else f"{root}/fitpack_legacy"))
    if not target.startswith(root + "/"):
        return {"status": "error", "ok": False,
                "message": f"The workspace must be inside your own area ({root})."}
    if not re.fullmatch(WORKSPACE_PATH_RE, target):
        return {"status": "error", "ok": False,
                "message": "The workspace path may use letters, numbers, '.', '_', '-' and '/' only."}
    # The FitPack `ai` branch carries fitlib/ai.py, which runs campaigns (as
    # the JAM app's "Clone / Sync FitPack" does); another branch on request.
    br = (branch or "").strip() or ("ai" if (repo_url or "").strip() in ("", JAM_REPO_URL) else "")
    if br and not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", br):
        return {"status": "error", "ok": False,
                "message": "Branch name may use letters, numbers, '.', '_', '/' and '-' only."}
    steps = []
    exists = _remote(u, f"test -d {_shq(target)} && echo YES || echo NO", timeout=30)
    present = "YES" in (exists.get("out") or "")
    if present:
        steps.append("workspace already present — skipped clone")
    else:
        url = repo_url.strip() or JAM_REPO_URL
        tok = (github_token or "").strip()
        bflag = f"-b {_shq(br)} " if br else ""
        # The token never goes on a command line, where anyone on either host
        # could read it: it comes in on ssh's standard input, and a credential
        # helper for the repository's host alone hands it to git. The helpers
        # the user has configured are switched off for this clone, so none
        # stores it, and the clone's .git/config never sees it.
        host = re.match(r"https://[^/]+", url) if tok else None
        auth = read = ""
        if host:
            helper = ('!f() { test "$1" = get && echo username=x-access-token && '
                      'echo "password=$JAM_GH_TOKEN"; }; f')
            key = f"credential.{host.group(0)}.helper"
            auth = f"-c credential.helper= -c {_shq(key + '=' + helper)} "
            read = "IFS= read -r JAM_GH_TOKEN; export JAM_GH_TOKEN; "
        # pipefail: without it the exit status is tail's, and a failed clone
        # was reported as success. GIT_TERMINAL_PROMPT=0 makes a private repo
        # without a token fail at once instead of waiting out the timeout.
        # The helper starts with "!" and the login shell on the compute host
        # expands history, so the command travels base64-encoded into a plain
        # bash (no profile, no history expansion); the token still arrives on stdin.
        script = (f"set -o pipefail; {read}mkdir -p {_shq(os.path.dirname(target))} && "
                  f"GIT_TERMINAL_PROMPT=0 git {auth}clone {bflag}{_shq(url)} "
                  f"{_shq(target)} 2>&1 | tail -3")
        b64 = base64.b64encode(script.encode()).decode()
        try:
            out = _remote(u, f'bash -c "$(echo {b64} | base64 -d)"', timeout=900,
                          stdin=tok + "\n" if host else None)
        except subprocess.TimeoutExpired:
            return {"status": "error", "ok": False, "username": u, "reason": "timeout",
                    "message": "git clone did not finish within 15 minutes and may still be running on "
                               "the cluster. Wait a few minutes, then look at the workspace report "
                               "(check_project, or the panel's Project tab) before trying again."}
        if tok:
            out["out"] = (out.get("out") or "").replace(tok, "***")
        steps.append(f"git clone: rc={out['rc']} {out['out'][-200:]}")
        if out["rc"] != 0:
            text = (out.get("out") or "") + (out.get("err") or "")
            auth = re.search(r"Authentication failed|could not read Username|"
                             r"Repository not found|403", text) is not None
            return {"status": "error", "ok": False, "username": u, "steps": steps,
                    "reason": "auth" if auth else "clone",
                    "message": ("The repository needs a GitHub token. Tokens are typed on "
                                "the panel's Project tab, never in chat: open panel_url, "
                                "fill in the folder name and the token, and press Create." if auth
                                else "git clone failed; see the detail below."),
                    "panel_url": _panel_urls("project")[0]["url"],
                    "detail": text.replace(tok, "***")[-400:] if tok else text[-400:]}
    out = _remote(u, f"printf '%s\\n' {_shq(target)} > {root}/.jam_workspace && echo DONE", timeout=60)
    steps.append("workspace marker: " + (out.get("out") or "").strip()[-60:])
    steps.append(_link_grids(u, target))
    _WS_CACHE.pop(u, None)
    _forget_library(u)
    message = (f"A folder named {os.path.relpath(target, root)} already exists in your area: it was "
               "not cloned again and is now your workspace. The report shows anything it lacks."
               if present else f"Workspace ready at {target}.")
    return {"status": "ok", "ok": True, "username": u, "workspace": target,
            "steps": steps, "message": message, "report": check_project(u)}


def _link_grids(u, ws):
    """Link each of the site's grids into <ws>/grids/ unless it is already
    there; a link whose target is gone is pointed at the site's grid again.
    A real folder or file is never touched. Returns a one-line summary."""
    links = SITE.get("grid_links") or {}
    if not links:
        return "grids: no links configured for this site"
    parts = []
    for name, target in sorted(links.items()):
        dest, src = _shq(f"{ws}/grids/{name}"), _shq(target)
        # -e follows links, so only a missing entry or a dangling link gets past it.
        parts.append(f"if [ -e {dest} ]; then echo KEPT; "
                     f"elif [ ! -e {src} ]; then echo NOSRC {_shq(name)}; "
                     f"elif [ -L {dest} ]; then ln -sfn {src} {dest} && echo LINKED; "
                     f"else mkdir -p $(dirname {dest}) && ln -s {src} {dest} && echo LINKED; fi")
    r = _remote(u, "; ".join(parts), timeout=60)
    out = r.get("out") or ""
    msg = (f"grids: {out.count('LINKED')} linked, {out.count('KEPT')} already there")
    missing = re.findall(r"NOSRC (\S+)", out)
    if missing:
        msg += f", source missing for: {', '.join(missing)}"
    return msg


def _site_public():
    """What the panel needs to label the sign-in steps for this site."""
    return {"id": SITE["id"], "label": SITE["label"],
            "hops": [{"host": SITE["login_host"], "secret": SITE["login_secret"]},
                     {"host": SITE["compute_host"], "secret": SITE["compute_secret"]}]}


def check_hpc_connection(username=""):
    """Cheap connection probe for the panel's Connect tab: is this user's
    two-hop chain open (and its login hop on its own), and which workspace is
    recorded."""
    u, err = _resolve_user(username)
    if err:
        return {"connected": False, "status": err.get("status"), "note": err["message"]}
    login_open, alive = _hops(u)
    account = {"username": u, "area": _area(u)}
    if alive:
        account["workspace"] = _workspace(u)
    return {"connected": alive, "login_open": login_open, "cluster_reachable": alive,
            "shared": SHARED, "owner": GATEWAY_OWNER,
            "account": account, "site": _site_public(), "sites": [_site_public()],
            "note": ("Ready to run fits." if alive else
                     "Not signed in to the cluster. Sign in on the Connect tab of the "
                     "panel page; the chain stays open until the gateway's daily "
                     "window closes.")}


# What a workspace needs for campaigns: (label, path inside it). The two .py
# paths are files the FitPack ai branch has; the rest are folders.
_PROJECT_ITEMS = [
    ("JAM code (fitpack_legacy)", ""), ("fitlib", "fitlib"), ("qcdlib", "qcdlib"),
    ("obslib", "obslib"), ("experimental data", "database"), ("interpolation grids", "grids"),
    ("campaign template (examples/input02.py)", "examples/input02.py"),
    ("campaign runner (fitlib/ai.py, FitPack ai branch)", "fitlib/ai.py")]


def check_project(username=""):
    """Workspace report for the Project tab: the recorded workspace, every
    checkout in the user's area, and whether each part is present."""
    u, err = _need_user(username)
    if err:
        return err
    root = _area(u)
    code = f"""
import os, json
root = {root!r}
items_spec = {_PROJECT_ITEMS!r}
grid_names = {sorted(SITE.get("grid_links") or {})!r}
marker = os.path.join(root, '.jam_workspace')
ws = open(marker).read().strip() if os.path.isfile(marker) else ''
cands = []
try:
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if os.path.isdir(os.path.join(p, 'fitlib')):
            cands.append(p)
except OSError:
    pass
default = os.path.join(root, 'fitpack_legacy')
active = ws or (default if os.path.isdir(default) else '')
items = []
if active:
    for label, rel in items_spec:
        p = os.path.join(active, rel) if rel else active
        if rel == 'grids' and grid_names:
            # Every grid the site links must resolve (a dangling link fails too).
            miss = [g for g in grid_names if not os.path.exists(os.path.join(p, g))]
            items.append({{'name': label, 'detail': p + ' — ' + str(len(grid_names) - len(miss))
                          + ' of ' + str(len(grid_names)) + ' grids', 'ok': not miss, 'missing': miss}})
            continue
        ok = os.path.exists(p) if rel.endswith('.py') else os.path.isdir(p)
        items.append({{'name': label, 'detail': p, 'ok': ok}})
print(json.dumps({{'marker': ws, 'active': active, 'candidates': cands, 'items': items}}))
"""
    r = _run_ifarm_py(u, code, timeout=60, cap=20000)
    try:
        d = json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        return {"status": "error", "username": u, "message": "could not read the workspace",
                "detail": r}
    cands = [{"path": p, "active": p == d["active"]} for p in d["candidates"]]
    if not d["active"]:
        return {"status": "ok", "username": u, "configured": False, "candidates": cands,
                "note": "No workspace chosen yet."}
    ai_files = {label for label, rel in _PROJECT_ITEMS if rel.endswith(".py")}
    # Each part by its path inside the workspace ("" is the workspace folder itself).
    have = {rel: it["ok"] for (_, rel), it in zip(_PROJECT_ITEMS, d["items"])}
    gone = not have[""]                 # then neither a branch switch nor new grid links can help
    for it in d["items"]:
        if it["ok"]:
            continue
        if it["name"] in ai_files and not gone:
            if have["fitlib/ai.py"]:
                # The runner is there, so the checkout is on the ai branch already and a
                # switch would not bring the template back.
                it["action"] = TEMPLATE_GONE
            else:
                it["action"] = ("This checkout is not on the FitPack ai branch. Switch it with 'Switch "
                                "to the ai branch' below, or create a new workspace.")
                it["fix"] = "use_ai_branch"
        elif "missing" in it and not gone:
            it["action"] = ("Missing: " + ", ".join(it["missing"]) + ". Press 'Use selected' below "
                            "to link the shared grids again.")
            it["fix"] = "use_workspace"
        else:
            it["action"] = "Run 'Set up a new workspace' to create a fresh checkout."
    ok = all(it["ok"] for it in d["items"])
    note = "" if ok else "Something is missing from this workspace — see the items marked ✕."
    if not d["marker"]:
        note = ("Using the default location; no workspace has been chosen explicitly. "
                + note).strip()
    return {"status": "ok", "username": u, "configured": True, "ok": ok,
            "workspace": d["active"], "items": d["items"], "candidates": cands, "note": note}


def use_workspace(path="", username=""):
    """Record an existing checkout in the user's own area as THE workspace."""
    u, err = _need_user(username)
    if err:
        return dict(err, ok=False)
    root = _area(u)
    p = os.path.normpath((path or "").strip())
    if not p.startswith(root + "/"):
        return {"ok": False, "message": f"The workspace must be inside your own area ({root})."}
    if not re.fullmatch(WORKSPACE_PATH_RE, p):
        return {"ok": False, "message": f"{p} has characters other than letters, numbers, '.', '_', '-' "
                                        "and '/'. Rename the folder, or choose another one."}
    out = _remote(u, f"test -d {_shq(p)}/fitlib && printf '%s\\n' {_shq(p)} > {root}/.jam_workspace "
                     f"&& echo USED", timeout=60)
    if "USED" not in (out.get("out") or ""):
        return {"ok": False, "message": f"{p} is not a fitpack checkout (no fitlib/ inside)."}
    _link_grids(u, p)
    _WS_CACHE.pop(u, None)
    _forget_library(u)
    return {"ok": True, "workspace": p, "report": check_project(u)}


def open_panel(tab="connect"):
    """Open the operations panel. Hosts that render MCP Apps show it inline;
    for the rest the result carries the browser URL."""
    tab = tab if tab in PANEL_TABS else "connect"
    conn = check_hpc_connection()
    return {"status": "ok", "tab": tab, "connection": conn, "panel_urls": _panel_urls(tab),
            "message": (f"Opened the FemtoMind panel on the {tab.capitalize()} tab. If it does "
                        "not appear inline, open a panel_url in a browser.")}


# ── campaigns (the JAM app composer, as tools) ─────────────────────────────
#
# The same pipeline as the FemtoMind JAM app: the user picks an object, its
# parameters, reactions, datasets and cuts, QCD settings and job settings;
# JAMAI copies the repository's examples/input02.py and appends the choices as
# an override block (input.py); a Slurm job array runs one replica per task
# through fitlib/ai.py (FitPack `ai` branch); postprocessing aggregates the
# replicas' fit_summary.json; plots are drawn from the replicas' .msr files.
#
# Fixed here relative to the app: parameter edits, cuts, datasets the template
# does not list, "Max iterations", "Optimizer" and TMC "off" now all reach the
# fit (see jamai/input_materializer.py and jamai/run_replica.py). The parameter
# table is read from the workspace's own examples/input02.py (the library),
# and only the rows the user changed go into the override block, so every
# other parameter stays exactly as the library has it.

JAMAI_DIR = os.environ.get("JAM_GW_JAMAI_DIR", os.path.join(HERE, "jamai"))
JAMAI_REMOTE = ".jamai"                 # <ws>/.jamai/modules/jam/jamai
CAMPAIGNS_DIR = "campaigns"             # <ws>/campaigns/<folder>/...
CAMPAIGN_TEMPLATE = "examples/input02.py"
# An ai-branch checkout (it has fitlib/ai.py) whose template was deleted:
# switching branches would not bring it back.
TEMPLATE_GONE = (f"{CAMPAIGN_TEMPLATE} is missing from this ai-branch checkout: restore it with git "
                 f"checkout origin/ai -- {CAMPAIGN_TEMPLATE} in the workspace, or create a new workspace.")
STAGE = "pdf_stage"

# Shown with the library's parameter table.
PDF_DETAILS = {
    "fit_basis_blocks": ["g1", "uv1", "dv1", "sea1", "sea2", "db1", "ub1", "s1", "sb1"],
    "auxiliary_blocks": ["c1", "cb1", "b1", "bb1", "g2", "uv2", "dv2", "mix"],
    "parametrization": "x f(x, Q0^2) = N x^a (1 - x)^b (1 + c sqrt(x) + d x)",
    "note": "Physical PDFs such as u, d, s, ub, db are derived combinations of these fitted "
            "basis blocks (the PDF set_moms() construction). g1 N, uv1 N and dv1 N are fixed "
            "by the momentum and valence sum rules.",
}
CAMPAIGN_OBJECTS = [
    {"value": "pdf", "label": "Unpolarized PDF", "enabled": True},
    {"value": "polarized_pdf", "label": "Polarized PDF", "enabled": False},
    {"value": "pion_ff", "label": "Pion FF", "enabled": False},
    {"value": "kaon_ff", "label": "Kaon FF", "enabled": False},
    {"value": "hadron_ff", "label": "Hadron FF", "enabled": False},
]
CAMPAIGN_REACTIONS = [
    {"key": "idis", "label": "DIS", "enabled": True},
    {"key": "dy", "label": "Drell-Yan", "enabled": True},
    {"key": "wasym", "label": "W asymmetry", "enabled": False},
    {"key": "zrap", "label": "Z rapidity", "enabled": False},
    {"key": "wzrv", "label": "WZ rapidity", "enabled": False},
    {"key": "wc", "label": "Weak charge", "enabled": False},
    {"key": "jets", "label": "Jets", "enabled": False},
    {"key": "sia", "label": "SIA", "enabled": False},
]
ENABLED_REACTIONS = {r["key"] for r in CAMPAIGN_REACTIONS if r["enabled"]}
DY_DATASETS = [
    {"id": "10001", "label": "E866 NuSea DY pp", "experiment": "E866 NuSea", "enabled": True},
    {"id": "10002", "label": "E866 NuSea DY pd", "experiment": "E866 NuSea", "enabled": False, "blueprint": True},
    {"id": "10003", "label": "E605 DY", "experiment": "E605", "enabled": False, "blueprint": True},
]
CAMPAIGN_DEFAULTS = {
    "cuts": {"idis": [{"variable": "Q2", "op": ">", "value": 1.69, "enabled": True},
                      {"variable": "W2", "op": ">", "value": 10, "enabled": True}],
             "dy": [{"variable": "Q2", "op": ">", "value": 0, "enabled": True}]},
    "qcd": {"order": "NLO", "Q20": 1.6129, "tmc": "AOT", "higherTwist": False,
            "dglapMode": "truncated", "asMode": "backward"},
    "parameters": {"initialization": "default", "randomSeed": None, "perReplicaSeed": True},
    "job": {"replicas": 10, "maxIterations": 500, "tolerance": "1e-6", "optimizer": "trf",
            "bootstrap": True, "campaignName": ""},
    "slurm": {"partition": "", "time": "00:30:00", "mem": "4G", "cpus": 1},
}
CAMPAIGN_CHOICES = {
    "order": ["LO", "NLO", "NNLO"], "tmc": ["off", "AOT"], "dglapMode": ["truncated", "iterated"],
    "asMode": ["backward", "forward"], "optimizer": ["trf", "dogbox"],
    "initialization": ["default", "random_within_bounds"],
}


def _confirm_phrase(kind, payload):
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:10]
    return f"CONFIRM JAM {kind} {digest}"


def _parse_hms(text):
    m = re.fullmatch(r"(?:(\d+)-)?(\d{1,2}):(\d{2}):(\d{2})", str(text or "").strip())
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def _auto_partition(hours):
    """Short jobs to the short partition, the rest to the batch partition."""
    short = SITE.get("short_partition")
    if short and int(hours) <= int(SITE.get("short_hours") or 0):
        return short
    return SITE["partition"]


def _auto_partition_seconds(seconds):
    return _auto_partition(max(1, -(-seconds // 3600)))


# ---- options and the dataset catalog ---------------------------------------

# A parameter line of the library card that is not commented out:
# conf['params']['pdf']['g1 a'] = {'value': ..., 'min': ..., 'max': ..., 'fixed': False}
_PDF_PARAM_LINE = r"""^\s*conf\[['"]params['"]\]\[['"]pdf['"]\]\[['"]([^'"]+)['"]\]\s*=\s*(\{.*\})\s*$"""

# The library's parameter table, in file order. A later line for the same
# parameter replaces the earlier one, as when the card runs. A 'fixed' that
# names another parameter is a tie: the row is fixed and says to what. When
# the file cannot be read, it says whether the workspace folder and the ai
# branch's fitlib/ai.py are there.
_LIBRARY = r'''
import ast, json, os, re
line_re = re.compile(__LINE_RE__)
try:
    lines = open(__PATH__, encoding='utf-8').read().splitlines()
except OSError:
    print(json.dumps({'found': False, 'rows': [], 'folder': os.path.isdir(__WS__),
                      'runner': os.path.isfile(os.path.join(__WS__, 'fitlib', 'ai.py'))}))
    raise SystemExit(0)
def num(v):
    return None if v is None else float(v)
rows, at = [], {}
for line in lines:
    m = line_re.match(line)
    if not m:
        continue
    try:
        d = ast.literal_eval(m.group(2))
        value, lo, hi = float(d['value']), num(d.get('min')), num(d.get('max'))
    except (ValueError, TypeError, KeyError, SyntaxError):
        continue
    name, fixed = m.group(1), d.get('fixed', False)
    block, _, par = name.partition(' ')
    row = {'block': block, 'parameter': par, 'name': name,
           'fixed': True if isinstance(fixed, str) else bool(fixed), 'value': value, 'min': lo, 'max': hi}
    if isinstance(fixed, str):
        row['tied_to'] = fixed
    if name in at:
        rows[at[name]] = row
    else:
        at[name] = len(rows)
        rows.append(row)
print(json.dumps({'found': True, 'rows': rows}))
'''
_LIB_CACHE = {}                         # user -> (workspace, rows, path, expires at)


def _library_parameters(u):
    """The parameter table of u's workspace library (examples/input02.py):
    (rows in file order, the file's path). Raises RuntimeError when the file
    is missing or has no parameter lines."""
    ws = _workspace(u)
    hit = _LIB_CACHE.get(u)
    if hit and hit[0] == ws and hit[3] > time.time():
        return hit[1], hit[2]
    path = f"{ws}/{CAMPAIGN_TEMPLATE}"
    r = _run_ifarm_py(u, _fill(_LIBRARY, LINE_RE=_PDF_PARAM_LINE, PATH=path, WS=ws), timeout=60, cap=200000)
    try:
        d = json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        raise RuntimeError(f"Could not read {path}: " + (r.get("err") or r.get("out") or "")[-300:])
    if not d["found"]:
        # A folder that is gone, or a checkout already on the ai branch: switching does not help.
        if not d["folder"]:
            raise RuntimeError(WORKSPACE_GONE.format(ws))
        if d["runner"]:
            raise RuntimeError(TEMPLATE_GONE)
        raise RuntimeError(f"The workspace has no {CAMPAIGN_TEMPLATE} (FitPack ai branch) to read "
                           "the parameter table from. Switch it to the FitPack ai branch (the "
                           "Project tab's 'Switch to the ai branch', or use_ai_branch) or create a "
                           "new workspace.")
    if not d["rows"]:
        # The file is there, so switching branches does not help.
        raise RuntimeError(f"The workspace's {CAMPAIGN_TEMPLATE} has no conf['params']['pdf'] lines "
                           "to read the parameter table from. Restore the ai branch's copy (git checkout "
                           f"origin/ai -- {CAMPAIGN_TEMPLATE} in the workspace) or create a new workspace.")
    _LIB_CACHE[u] = (ws, d["rows"], path, time.time() + 600)
    return d["rows"], path


def _forget_library(u):
    """Drop u's cached parameter table (its workspace or branch changed)."""
    _LIB_CACHE.pop(u, None)


def campaign_options(username=""):
    """Everything the composer offers; the parameter table is the workspace's
    library (examples/input02.py)."""
    u, err = _need_user(username)
    if err:
        return err
    try:
        rows, source = _library_parameters(u)
    except RuntimeError as e:
        return {"status": "error", "message": str(e)}
    return {"status": "ok",
            "steps": ["Non-perturbative object", "Reactions", "QCD settings", "Job config", "Review"],
            "objects": CAMPAIGN_OBJECTS,
            "parameters": {"pdf": {"rows": rows, "source": source, **PDF_DETAILS,
                                   "free": sum(1 for r in rows if not r["fixed"]),
                                   "fixed": sum(1 for r in rows if r["fixed"])}},
            "reactions": CAMPAIGN_REACTIONS,
            "defaults": CAMPAIGN_DEFAULTS, "choices": CAMPAIGN_CHOICES,
            "partitions": SITE.get("partitions", []),
            "partition_auto": {"short": SITE.get("short_partition"),
                               "short_hours": SITE.get("short_hours"), "long": SITE["partition"]},
            "template": CAMPAIGN_TEMPLATE}


_IDIS_CATALOG = r'''
import json, os, re
readme = os.path.join(__WS__, 'database', 'idis', 'README.md')
targets = {'p': 'proton', 'n': 'neutron', 'd': 'deuteron', 'h': 'helium-3', 't': 'tritium',
           'd/p': 'deuteron/proton', 'n/d': 'neutron/deuteron', 'h/t': 'helium-3/tritium',
           'h/d': 'helium-3/deuteron', 't/d': 'tritium/deuteron'}
def clean(v):
    v = re.sub(r'\$([^$]*)\$', r'\1', v or '')
    for a, b in (('\\sigma', 'σ'), ('\\rm', ''), ('\\', ''), ('{', ''), ('}', '')):
        v = v.replace(a, b)
    return re.sub(r'_\s+', '_', re.sub(r'\s+', ' ', v)).strip()
out, section, seen = [], None, set()
try:
    lines = open(readme, encoding='utf-8').read().splitlines()
except OSError:
    lines = []
for line in lines:
    line = line.strip()
    if line.startswith('## '):
        section = line[3:].strip(); continue
    if not line.startswith('|') or 'index' in line.lower() or ':--:' in line:
        continue
    cols = [c.strip() for c in line.strip('|').split('|')]
    if len(cols) < 5 or not re.fullmatch(r'\d+', cols[0]) or cols[0] in seen:
        continue
    seen.add(cols[0])
    target = targets.get(clean(cols[2]), clean(cols[2]))
    obs, exp = clean(cols[3]), clean(cols[4])
    has_data = os.path.isfile(os.path.join(__WS__, 'database', 'idis', 'expdata', cols[0] + '.xlsx'))
    out.append({'id': cols[0], 'label': ' '.join(x for x in (exp, obs, target) if x) or cols[0],
                'experiment': exp or 'Other', 'observable': obs, 'target': target,
                'section': section, 'enabled': has_data})
out.sort(key=lambda d: int(d['id']))
print(json.dumps(out))
'''


def campaign_datasets(reaction="idis", search="", username=""):
    """The datasets of one reaction, grouped by experiment (DIS from the
    repository's database/idis/README.md, as the JAM app does)."""
    u, err = _need_user(username)
    if err:
        return err
    reaction = (reaction or "").strip()
    if reaction not in ENABLED_REACTIONS:
        return {"status": "error", "message": f"{reaction!r} is not available yet; the composer "
                                              f"offers {sorted(ENABLED_REACTIONS)}."}
    if reaction == "dy":
        items = [dict(d) for d in DY_DATASETS]
    else:
        r = _run_ifarm_py(u, _fill(_IDIS_CATALOG, WS=_workspace(u)), timeout=60, cap=200000)
        try:
            items = json.loads(r["out"].strip().splitlines()[-1])
        except Exception:
            return {"status": "error", "message": "could not read database/idis/README.md",
                    "detail": (r.get("err") or "")[-300:]}
    q = (search or "").strip().lower()
    if q:
        items = [d for d in items if q in " ".join(str(d.get(k, "")) for k in
                                                   ("id", "label", "experiment", "observable", "target")).lower()]
    groups = {}
    for d in items:
        groups.setdefault(d.get("experiment") or "Other", []).append(d)
    return {"status": "ok", "reaction": reaction, "count": len(items),
            "experiments": [{"experiment": k, "datasets": v} for k, v in groups.items()],
            "default_cuts": CAMPAIGN_DEFAULTS["cuts"].get(reaction, [])}


# ---- the input card ---------------------------------------------------------

def _same_number(a, b):
    """Both None, or numbers equal up to a tiny relative tolerance."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 1e-9 * max(abs(a), abs(b))


def _normalize_card(card, library):
    """Accept the composer card (also the JAM app's shape) and return
    (normalized card, errors). library is the workspace's parameter table
    (_library_parameters); only the rows the user changed are checked, and
    only those are written into the card."""
    card = json.loads(json.dumps(card or {}))
    errors = []
    obj = card.get("object") or {}
    if isinstance(obj, str):
        obj = {"type": obj}
    otype = obj.get("type") or ""
    if not otype:
        errors.append("Select a non-perturbative object (Unpolarized PDF) on step 1.")
    elif otype != "pdf":
        errors.append("Select a non-perturbative object: only Unpolarized PDF is available.")
    # parameters: {"g1 a": {...}} or the app's parameterBounds {"g1:a": {min,max}}
    known = {r["name"]: r for r in library}
    overrides = {}
    for key, ov in (obj.get("parameterBounds") or {}).items():
        overrides[key.replace(":", " ")] = dict(ov or {})
    for key, ov in (obj.get("parameters") or {}).items():
        overrides.setdefault(key.replace(":", " "), {}).update(ov or {})
    params, changed = [], []
    for name, lib in known.items():
        ov = overrides.get(name, {})
        fixed = ov.get("fixed", lib["fixed"])
        if isinstance(fixed, str):
            # A client may spell the flag out; any other string (a tie, say) is refused.
            fixed = {"true": True, "fixed": True, "false": False, "free": False}.get(fixed.strip().lower(), fixed)
        row = {"name": name, "fixed": fixed,
               "value": ov.get("value", lib["value"]), "min": ov.get("min", lib["min"]),
               "max": ov.get("max", lib["max"])}
        params.append(row)
        if not isinstance(fixed, bool):
            changed.append(row)
            errors.append(f"Parameter {name}: fixed must be true (FIXED) or false (FREE).")
            continue
        try:
            row["value"] = float(row["value"])
            row["min"] = None if row["min"] is None else float(row["min"])
            row["max"] = None if row["max"] is None else float(row["max"])
        except (TypeError, ValueError):
            changed.append(row)
            errors.append(f"Parameter {name}: value, min and max must be numbers.")
            continue
        # float() also takes inf and nan, which no fit can use; an inf would
        # even pass as unchanged next to a finite library value.
        if not all(math.isfinite(row[k]) for k in ("value", "min", "max") if row[k] is not None):
            changed.append(row)
            errors.append(f"Parameter {name}: value, min and max must be finite numbers.")
            continue
        if row["fixed"] == lib["fixed"] and all(
                _same_number(row[k], lib[k]) for k in ("value", "min", "max")):
            continue
        changed.append(row)
        if row["fixed"]:
            # FitPack sets a tied parameter from its partner; a number of its
            # own written into the card would silently replace the tie.
            if lib.get("tied_to"):
                errors.append(f"Parameter {name} is tied to {lib['tied_to']}: leave its value, min and "
                              "max as they are, or switch it to FREE to give it its own.")
            continue
        if row["min"] is None or row["max"] is None:
            errors.append(f"Parameter {name} is FREE: give it a min and a max.")
        elif not row["min"] < row["max"]:
            errors.append(f"Parameter {name} is FREE: its min must be below its max.")
        elif not row["min"] <= row["value"] <= row["max"]:
            errors.append(f"Parameter {name}: the value must lie within [min, max].")
    for name in overrides:
        if name not in known:
            errors.append(f"Unknown parameter {name!r}.")
    # reactions: list or {"idis": true}
    rx = card.get("reactions") or []
    if isinstance(rx, dict):
        rx = [k for k, on in rx.items() if on]
    rx = [str(r).strip().lower() for r in rx if str(r).strip()]
    if not rx:
        errors.append("Select at least one reaction.")
    for r in rx:
        if r not in ENABLED_REACTIONS:
            errors.append(f"Reaction {r!r} is not available yet.")
    # datasets: {"idis": ["10010"]} or the app's {"idis": {"selected": [...], "cuts": [...]}}
    ds_in, cuts_in = card.get("datasets") or {}, dict(card.get("cuts") or {})
    datasets, cuts = {}, {}
    for r in rx:
        entry = ds_in.get(r)
        if isinstance(entry, dict):
            sel = entry.get("selected") or []
            if "cuts" in entry and r not in cuts_in:
                cuts_in[r] = entry["cuts"]
        else:
            sel = entry or []
        ids = []
        for x in sel:
            x = str(x).strip()
            if not re.fullmatch(r"\d{1,8}", x):
                errors.append(f"Reaction {r!r}: dataset id {x!r} is not a number.")
            elif x not in ids:
                ids.append(x)
        if not ids:
            errors.append(f'Reaction "{r}" must include at least one dataset.')
        datasets[r] = ids
        rc = cuts_in.get(r)
        if rc is None:
            rc = CAMPAIGN_DEFAULTS["cuts"].get(r, [])
        clean = []
        for c in rc:
            try:
                c = {"variable": str(c["variable"]), "op": str(c.get("op", ">")),
                     "value": float(c["value"]), "enabled": bool(c.get("enabled", True))}
            except (KeyError, TypeError, ValueError):
                errors.append(f'Reaction "{r}" has a cut with a non-numeric value.')
                continue
            if not c["variable"].isidentifier() or c["op"] not in (">", ">=", "<", "<="):
                errors.append(f'Reaction "{r}" has an invalid cut {c["variable"]}{c["op"]}.')
            clean.append(c)
        if not any(c["enabled"] for c in clean):
            errors.append(f'Reaction "{r}" must include at least one enabled cut.')
        cuts[r] = clean
    # QCD
    q = {**CAMPAIGN_DEFAULTS["qcd"], **(card.get("qcd") or {})}
    try:
        q["Q20"] = float(q["Q20"])
        if q["Q20"] <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("Q20 must be a positive number.")
    tmc = q.get("tmc")
    q["tmc"] = "AOT" if str(tmc).upper() == "AOT" else "off" if str(tmc).lower() in ("off", "false", "none", "") else tmc
    for k in ("order", "tmc", "dglapMode", "asMode"):
        if q[k] not in CAMPAIGN_CHOICES[k]:
            errors.append(f"QCD {k} must be one of {CAMPAIGN_CHOICES[k]}.")
    q["higherTwist"] = q.get("higherTwist") in (True, "on", "true", "True", 1)
    # parameter initialization
    pi = {**CAMPAIGN_DEFAULTS["parameters"], **(card.get("parameters") or {})}
    if pi["initialization"] not in CAMPAIGN_CHOICES["initialization"]:
        errors.append(f"Parameter initialization must be one of {CAMPAIGN_CHOICES['initialization']}.")
    if pi.get("randomSeed") in ("", None):
        pi["randomSeed"] = None
    else:
        try:
            pi["randomSeed"] = int(pi["randomSeed"])
        except (TypeError, ValueError):
            errors.append("Random seed must be an integer.")
    pi["perReplicaSeed"] = bool(pi.get("perReplicaSeed", True))
    # job
    j = {**CAMPAIGN_DEFAULTS["job"], **(card.get("job") or {})}
    try:
        j["replicas"] = int(j["replicas"])
        if not 1 <= j["replicas"] <= 2000:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("Replicas must be between 1 and 2000.")
    try:
        j["maxIterations"] = int(j["maxIterations"])
        if j["maxIterations"] <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("Max iterations must be greater than 0.")
    try:
        if float(str(j["tolerance"]).strip()) <= 0:
            raise ValueError
        j["tolerance"] = str(j["tolerance"]).strip()
    except (TypeError, ValueError):
        errors.append("Tolerance must be a positive number.")
    if j["optimizer"] not in CAMPAIGN_CHOICES["optimizer"]:
        errors.append(f"Optimizer must be one of {CAMPAIGN_CHOICES['optimizer']}.")
    j["bootstrap"] = j.get("bootstrap") in (True, "ensemble", "true", "True", 1)
    j["campaignName"] = re.sub(r"[^A-Za-z0-9._-]+", "_", str(j.get("campaignName") or "").strip()).strip("._-")[:40]
    if not j["campaignName"]:
        errors.append("Name the campaign.")
    # Slurm (the LLM may tailor these with the user)
    sl = {**CAMPAIGN_DEFAULTS["slurm"], **(card.get("slurm") or {})}
    secs = _parse_hms(sl.get("time"))
    if not secs:
        errors.append("Slurm time must look like HH:MM:SS (per replica).")
    if not re.fullmatch(r"\d{1,4}[MG]", str(sl.get("mem", ""))):
        errors.append("Slurm memory must look like 4G or 500M.")
    try:
        sl["cpus"] = int(sl["cpus"])
        if not 1 <= sl["cpus"] <= 64:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("Slurm cpus must be between 1 and 64.")
    sl["partition"] = str(sl.get("partition") or "").strip()
    if sl["partition"] and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", sl["partition"]):
        errors.append("Invalid partition name.")
    sl["partition_effective"] = sl["partition"] or (_auto_partition_seconds(secs) if secs else "")
    norm = {"object": {"type": "pdf", "label": "Unpolarized PDF"}, "parameters_table": params,
            "parameters_changed": changed, "reactions": rx, "datasets": datasets, "cuts": cuts,
            "qcd": q, "parameters": pi, "job": j, "slurm": sl}
    return norm, errors


def _card_summary(norm):
    free = [p["name"] for p in norm["parameters_table"] if not p["fixed"]]
    fixed = [p["name"] for p in norm["parameters_table"] if p["fixed"]]
    # Registry entries from before the library table have no parameters_changed.
    changed = [p["name"] for p in norm.get("parameters_changed") or []]
    return {"object": "Unpolarized PDF", "free_parameters": len(free), "fixed_parameters": len(fixed),
            "fixed": fixed, "changed_parameters": changed, "reactions": norm["reactions"],
            "datasets": {r: len(v) for r, v in norm["datasets"].items()},
            "cuts": {r: [f'{c["variable"]}{c["op"]}{c["value"]:g}' for c in cs if c["enabled"]]
                     for r, cs in norm["cuts"].items()},
            "qcd": norm["qcd"], "initialization": norm["parameters"]["initialization"],
            "inference": "ensemble (pseudo-data replicas)" if norm["job"]["bootstrap"]
                         else "central values only",
            "replicas": norm["job"]["replicas"], "max_iterations": norm["job"]["maxIterations"],
            "tolerance": norm["job"]["tolerance"], "optimizer": norm["job"]["optimizer"],
            "slurm": {k: norm["slurm"][k] for k in ("partition_effective", "time", "mem", "cpus")}}


def _campaign_yaml(ws, folder, norm, nreplicas):
    q = norm["qcd"]
    qcd = {"order": q["order"], "Q20": q["Q20"], "ftol": float(norm["job"]["tolerance"]),
           "grid_path": SITE.get("grid_root"), "tmc": "AOT" if q["tmc"] == "AOT" else False,
           "higherTwist": q["higherTwist"], "dglapMode": q["dglapMode"], "asMode": q["asMode"]}
    pi = norm["parameters"]
    init = "random_within_bounds" if pi["initialization"] == "random_within_bounds" else "default_prior"
    seed = pi["randomSeed"] if pi["randomSeed"] is not None else 120000
    # Only the rows the user changed; the card keeps the library's own for the rest.
    sectors = [{"key": "pdf", "parameters": [
        {"name": p["name"], "value": p["value"], "min": p["min"], "max": p["max"], "fixed": p["fixed"]}
        for p in norm["parameters_changed"]]}]
    return json.dumps({
        "campaign": {"name": folder, "fitpack_root": ws, "template_path": CAMPAIGN_TEMPLATE,
                     "campaign_root": f"{ws}/{CAMPAIGNS_DIR}/{folder}", "default_qcd": qcd,
                     "seed_policy": {"mode": "deterministic", "base_seed": 120000}},
        "stages": [{"name": STAGE, "reactions": norm["reactions"], "fit_objects": ["pdf"],
                    "frozen_objects": [], "input_template": CAMPAIGN_TEMPLATE,
                    "nreplicas": nreplicas, "previous_stage": None, "output_dir": f"stages/{STAGE}",
                    "datasets": {r: [int(x) for x in ids] for r, ids in norm["datasets"].items()},
                    "cuts": norm["cuts"],
                    "params": {"initializationMode": init, "randomSeed": pi["randomSeed"],
                               "perReplicaSeed": pi["perReplicaSeed"], "baseSeed": seed,
                               "sectors": sectors},
                    "qcd": qcd,
                    "job": {"replicas": nreplicas, "maxIterations": norm["job"]["maxIterations"],
                            "tolerance": norm["job"]["tolerance"], "optimizer": norm["job"]["optimizer"],
                            "bootstrap": norm["job"]["bootstrap"],
                            "campaignName": norm["job"]["campaignName"]}}]}, indent=2)


def _array_script(ws, folder, norm, first, last):
    """The Slurm job array: one replica per task, inside the site's image."""
    sl, c = norm["slurm"], SITE["container"]
    stage = f"{ws}/{CAMPAIGNS_DIR}/{folder}/stages/{STAGE}"
    binds = " ".join(f"--bind {b}" for b in c.get("binds", []))
    lhapdf = ":".join(SITE.get("lhapdf_extra") or [])
    lhapdf_env = f"--env LHAPDF_DATA_PATH={lhapdf} " if lhapdf else ""
    qs, pypath = _shq(stage), _shq(f"{ws}:{ws}/{JAMAI_REMOTE}")
    return f"""#!/bin/bash
#SBATCH --job-name=jam_{folder[:40]}
#SBATCH --partition={sl["partition_effective"]}
#SBATCH --time={sl["time"]}
#SBATCH --mem={sl["mem"]}
#SBATCH --cpus-per-task={sl["cpus"]}
#SBATCH --array={first}-{last}
#SBATCH --output={stage}/logs/stage_%A_%a.out
set -euo pipefail
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
# fitlib.maxlike clears the screen at every status print; without a terminal
# that only writes "TERM environment variable not set." on each step.
export TERM=dumb
REPLICA_ID="$SLURM_ARRAY_TASK_ID"
cd {_shq(ws)}
apptainer exec {binds} \\
  --env PYTHONPATH={pypath} --env FITPACK={_shq(ws)} --env TERM=dumb \\
  --env JAMAI_MAX_NFEV={norm["job"]["maxIterations"]} --env JAMAI_METHOD={norm["job"]["optimizer"]} \\
  {lhapdf_env}--env MPLCONFIGDIR={qs}/.matplotlib \\
  {c["image"]} \\
  python3 -m modules.jam.jamai.run_replica --manifest {qs}/manifest.resolved.yaml \\
    --output {qs}/current/replica_${{REPLICA_ID}} --replica "${{REPLICA_ID}}" -n {sl["cpus"]}
"""


# ---- the collection registry (<ws>/campaigns/collections.json) ---------------

_REGISTRY = r'''
import json, os, sys
path = os.path.join(__WS__, 'campaigns', 'collections.json')
op = __OP__
try:
    items = json.load(open(path))
    if not isinstance(items, list): items = []
except (OSError, ValueError):
    items = []
if op['kind'] == 'upsert':
    items = [e for e in items if e.get('collection_id') != op['entry']['collection_id']] + [op['entry']]
elif op['kind'] == 'delete':
    items = [e for e in items if e.get('collection_id') != op['id']]
if op['kind'] != 'read':
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    json.dump(items, open(tmp, 'w'), indent=1); os.replace(tmp, path)
print(json.dumps(items))
'''


def _registry(u, op):
    r = _run_ifarm_py(u, _fill(_REGISTRY, WS=_workspace(u), OP=op), timeout=60, cap=2000000)
    try:
        return json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        raise RuntimeError("could not read campaigns/collections.json: " + (r.get("err") or "")[-200:])


def _collection(u, cid):
    for e in _registry(u, {"kind": "read"}):
        if e.get("collection_id") == cid:
            return e
    return None


def _ai_branch_files(u):
    """(the workspace has the campaign runner fitlib/ai.py, it has the campaign template)."""
    ws = _shq(_workspace(u))
    r = _remote(u, f"test -f {ws}/fitlib/ai.py && echo READY; test -f {ws}/{CAMPAIGN_TEMPLATE} && echo TEMPLATE",
                timeout=30)
    out = r.get("out") or ""
    return "READY" in out, "TEMPLATE" in out


def use_ai_branch(username=""):
    """Check out the FitPack `ai` branch (fitlib/ai.py, the campaign runner) in
    the workspace. Refuses when the checkout has uncommitted changes."""
    u, err = _need_user(username)
    if err:
        return err
    ws = _workspace(u)
    runner, template = _ai_branch_files(u)
    if runner and template:
        return {"status": "ok", "workspace": ws, "message": "The workspace already has the ai branch."}
    if runner:
        # Already on the ai branch: a switch would not bring the template back.
        return {"status": "error", "workspace": ws,
                "message": f"This checkout already has the ai branch's runner (fitlib/ai.py) but no "
                           f"{CAMPAIGN_TEMPLATE}, so switching branches does not help. Restore the file "
                           f"(git checkout origin/ai -- {CAMPAIGN_TEMPLATE} in the workspace) or create "
                           "a new workspace."}
    # Without the folder, stop: git must never run in the login directory instead.
    r = _remote(u, f"cd {_shq(ws)} || {{ echo NOWS; exit 0; }}; "
                   f"git rev-parse --verify -q origin/ai >/dev/null || "
                   f"GIT_TERMINAL_PROMPT=0 git fetch -q origin ai 2>&1; "
                   f"if [ -n \"$(git status --porcelain --untracked-files=no)\" ]; then echo DIRTY; "
                   f"else git checkout -q -B ai origin/ai 2>&1 && echo SWITCHED; fi", timeout=180)
    out = (r.get("out") or "") + (r.get("err") or "")
    if "NOWS" in (r.get("out") or "").split():
        return {"status": "error", "workspace": ws, "message": WORKSPACE_GONE.format(ws)}
    if "DIRTY" in out:
        return {"status": "error", "workspace": ws,
                "message": "This checkout has uncommitted changes to tracked files, so it is not "
                           "switched. Create a new workspace instead (it clones the ai branch)."}
    if "SWITCHED" not in out:
        return {"status": "error", "workspace": ws, "message": "Could not check out the ai branch.",
                "detail": out[-400:]}
    _forget_library(u)
    return {"status": "ok", "workspace": ws, "message": "Switched the workspace to the FitPack ai branch."}


def _upload_jamai(u, ws):
    files = {}
    for n in sorted(os.listdir(JAMAI_DIR)):
        if n.endswith(".py"):
            with open(os.path.join(JAMAI_DIR, n), "rb") as fh:
                files[n] = fh.read().decode()
    # One shell argument is capped at 128 KiB, so the package travels compressed.
    blob = base64.b64encode(zlib.compress(json.dumps(files).encode(), 9)).decode()
    code = f"""
import base64, json, os, zlib
root = {f'{ws}/{JAMAI_REMOTE}'!r}
pkg = os.path.join(root, 'modules', 'jam', 'jamai')
os.makedirs(pkg, exist_ok=True)
for init in (os.path.join(root, 'modules', '__init__.py'), os.path.join(root, 'modules', 'jam', '__init__.py')):
    open(init, 'a').close()
files = json.loads(zlib.decompress(base64.b64decode({blob!r})))
for name, text in files.items():
    open(os.path.join(pkg, name), 'w', encoding='utf-8').write(text)
print('JAMAI-OK', len(files))
"""
    r = _run_ifarm_py(u, code, timeout=90)
    if "JAMAI-OK" not in (r.get("out") or ""):
        raise RuntimeError("uploading the JAMAI package failed: " + (r.get("err") or "")[-300:])


# ---- validate / submit / append ------------------------------------------------

def _checked_card(u, card):
    """Normalize card against the workspace's library parameter table and check
    the workspace has what it needs: (norm, None), or (None, the result to
    return). validate_campaign and submit_campaign both use it, so the phrase
    and the submitted card come from one reading of the table."""
    try:
        library, _ = _library_parameters(u)
    except RuntimeError as e:
        return None, {"status": "error", "valid": False, "message": str(e)}
    norm, errors = _normalize_card(card, library)
    if not errors:
        ws = _workspace(u)
        checks = " ".join(f"{r}/expdata/{x}.xlsx" for r, ids in norm["datasets"].items() for x in ids)
        r = _remote(u, f"cd {_shq(ws)}/database 2>/dev/null && for f in {checks}; do "
                       f"[ -f \"$f\" ] || echo MISSING $f; done; "
                       f"[ -f {_shq(ws)}/{CAMPAIGN_TEMPLATE} ] || echo NOTEMPLATE; "
                       f"[ -f {_shq(ws)}/fitlib/ai.py ] || echo NOAI", timeout=60)
        out = r.get("out") or ""
        for m in re.findall(r"MISSING (\S+)", out):
            errors.append(f"No data file database/{m} in this workspace.")
        if "NOTEMPLATE" in out:
            errors.append(f"The workspace has no {CAMPAIGN_TEMPLATE}.")
        if "NOAI" in out:
            errors.append("The workspace is not on the FitPack ai branch (no fitlib/ai.py): switch it "
                          "(the Project tab's 'Switch to the ai branch', or use_ai_branch), or create a "
                          "new workspace.")
    if errors:
        return None, {"status": "invalid", "valid": False, "errors": errors}
    return norm, None


def _validated(u, norm):
    """validate_campaign's result for a card that passed _checked_card."""
    return {"status": "ok", "valid": True, "summary": _card_summary(norm),
            "slurm_script": _array_script(_workspace(u), "<campaign-folder>", norm, 1, norm["job"]["replicas"]),
            "confirmation_phrase": _confirm_phrase("SUBMIT", norm),
            "next": "Show the user this summary and the Slurm script. Submit with "
                    "submit_campaign(card, confirm=<the phrase>) only after they confirm."}


# The data a card selects, read as FitPack reads it: the reaction's reader
# first derives the variables cuts use (idis as obslib/idis/reader.py: Q2
# from q2, col from exp, X from W2, W or x, Y from the beam energy, then
# W2 = M^2 + Q2/X - Q2; dy as obslib/dy/reader.py: tau, S, Y and Q2 = tau S
# from Rtau, RS and xF), then tools/reader.py applies each cut with
# tab.query and silently skips one that cannot be evaluated; a dataset with
# no points left is dropped from the fit. report holds the columns FitPack's
# status report reads for each reaction.
_DATA_CHECK = r'''
import json, os
import numpy as np
import pandas as pd
ws = __WS__; datasets = __DATASETS__; cuts = __CUTS__; report = __REPORT__
M = 0.93891897                      # the nucleon mass of qcdlib/aux.py
M2 = M * M
def idis(t):
    have = set(t.columns)
    if 'value' not in have and '*value' in have:
        t['value'] = t['*value']
    if 'col' not in have and 'exp' in have:
        t['col'] = t['exp']
    if 'q2' in have:
        t['Q2'] = t['q2']
    # A column FitPack cannot derive stays missing; the cuts then say so.
    try:
        if 'X' not in t.columns:
            if 'W2' in t.columns: t['X'] = t['Q2'] / (t['W2'] - M2 + t['Q2'])
            elif 'W' in t.columns: t['X'] = t['Q2'] / (t['W'] ** 2 - M2 + t['Q2'])
            elif 'x' in t.columns: t['X'] = t['x']
            elif 'x*' in t.columns: t['X'] = t['x*']
        if 'Y' not in t.columns:
            e = next((c for c in ('Elab', 'E', 'Ebeam') if c in t.columns), None)
            if e: t['Y'] = t['Q2'] / (2 * M * t['X'] * t[e])
            elif 'RS' in t.columns: t['Y'] = t['Q2'] / (t['RS'] ** 2 - M2) / t['X']
    except Exception:
        pass
    try:
        if 'W2' not in t.columns: t['W2'] = M2 + t['Q2'] / t['X'] - t['Q2']
    except Exception:
        pass
def dy(t):
    def named(n):
        return next(c for c in t.columns if str(c).lower().strip() == n)
    try: t['tau'] = t[named('rtau')] ** 2
    except Exception: pass
    try: t['S'] = t[named('rs')] ** 2
    except Exception: pass
    try: t['Y'] = np.arcsinh(0.5 * t['xF'] / t['tau'] ** 0.5)
    except Exception: pass
    try: t['Q2'] = t['tau'] * t['S']
    except Exception: pass
out = []
for r, ids in datasets.items():
    for x in ids:
        d = {'reaction': r, 'id': x}
        out.append(d)
        try:
            t = pd.read_excel(os.path.join(ws, 'database', r, 'expdata', x + '.xlsx'))
        except Exception as e:
            d['error'] = ' '.join(('%s: %s' % (type(e).__name__, e)).split())[:300]
            continue
        {'idis': idis, 'dy': dy}.get(r, lambda t: None)(t)
        applied, skipped = [], []
        for f in cuts.get(r, []):
            try:
                t = t.query(f)
                applied.append(f)
            except Exception as e:
                skipped.append([f, ' '.join(str(e).split())[:200] or type(e).__name__])
        d.update(points=int(len(t.index)), applied=applied, skipped=skipped,
                 missing=[c for c in report.get(r, ()) if c not in t.columns])
print(json.dumps(out))
'''
# What FitPack's status report reads from each data table, and what it names.
_REPORT_COLUMNS = {"idis": ("col", "obs", "target"), "dy": ("col", "reaction")}
_COLUMN_NAMES = {"col": "experiment name", "obs": "observable", "target": "target",
                 "reaction": "reaction"}


def _data_warnings(u, norm):
    """Warnings about the data a card selects, which never block submission,
    and the points each dataset keeps after the cuts ({"idis:10005": 33});
    one container call for all datasets."""
    cuts = {r: [f'{c["variable"]}{c["op"]}{c["value"]:g}' for c in cs if c["enabled"]]
            for r, cs in norm["cuts"].items()}
    code = _fill(_DATA_CHECK, WS=_workspace(u), DATASETS=norm["datasets"], CUTS=cuts,
                 REPORT=_REPORT_COLUMNS)
    r = {}
    try:
        r = _run_ifarm_py(u, code, apptainer=True, timeout=120, cap=200000)
        rows = json.loads(r["out"].strip().splitlines()[-1])
        if not isinstance(rows, list):
            raise ValueError("unexpected answer")
    except subprocess.TimeoutExpired:
        return ["Could not check the data files: the check took longer than 2 minutes."], {}
    except Exception as e:
        # The login shell on the compute host prints an unrelated profile warning.
        why = " ".join(l.strip() for l in ((r.get("err") or "") + "\n" + (r.get("out") or "")).splitlines()
                       if l.strip() and "syscshrc" not in l)
        return [f"Could not check the data files: {why[-300:] or type(e).__name__}"], {}
    warnings, points = [], {}
    for d in rows:
        name = f"{d['reaction']} {d['id']}"
        if d.get("error"):
            warnings.append(f"Could not read the data file of dataset {name}: {d['error']}")
            continue
        points[f"{d['reaction']}:{d['id']}"] = d["points"]
        for cut, why in d["skipped"]:
            warnings.append(f"The cut {cut} cannot be evaluated on dataset {name} ({why}), so FitPack "
                            "skips it for this dataset.")
        if not d["points"]:
            warnings.append((f"Dataset {name} has no points left after the cuts {', '.join(d['applied'])}, "
                             "so it adds nothing to the fit.") if d["applied"] else
                            f"Dataset {name} has no data points, so it adds nothing to the fit.")
            continue
        for c in d["missing"]:
            warnings.append(f"Dataset {name} has no '{c}' column in its data file; the fit runs (the "
                            "gateway works around FitPack's status report), but its "
                            f"{_COLUMN_NAMES.get(c, c)} is unknown in reports.")
    return warnings, points


def validate_campaign(card=None, username=""):
    """Validate an input card and show what would run, with the confirmation
    phrase submit_campaign needs, and warnings about the selected data."""
    u, err = _need_user(username)
    if err:
        return err
    norm, err = _checked_card(u, card)
    if err:
        return err
    warnings, points = _data_warnings(u, norm)
    out = {"status": "ok", "valid": True, "warnings": warnings}
    out.update(_validated(u, norm))
    out["summary"]["points_after_cuts"] = points
    if warnings:
        out["next"] = ("Show the user this summary, the warnings (they do not block submission) and the "
                       "Slurm script. Submit with submit_campaign(card, confirm=<the phrase>) only after "
                       "they confirm.")
    return out


def submit_campaign(card=None, confirm="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    norm, err = _checked_card(u, card)
    if err:
        return err
    v = _validated(u, norm)
    if (confirm or "").strip() != v["confirmation_phrase"]:
        return {"status": "needs_confirmation", "confirmation_phrase": v["confirmation_phrase"],
                "summary": v["summary"], "slurm_script": v["slurm_script"],
                "message": "Not submitted. Show the user the summary and Slurm script; when they "
                           "confirm, call again with confirm set to the phrase."}
    ws = _workspace(u)
    folder = norm["job"]["campaignName"] + "_" + time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    croot = f"{ws}/{CAMPAIGNS_DIR}/{folder}"
    stage = f"{croot}/stages/{STAGE}"
    n = norm["job"]["replicas"]
    _upload_jamai(u, ws)
    yaml_b64 = base64.b64encode(_campaign_yaml(ws, folder, norm, n).encode()).decode()
    script_b64 = base64.b64encode(_array_script(ws, folder, norm, 1, n).encode()).decode()
    qc, qs = _shq(croot), _shq(stage)
    mat = (f"mkdir -p {qc} && echo {yaml_b64} | base64 -d > {qc}/campaign.request.json && "
           f"cd {_shq(ws)} && {_container_exec(ws, f'{ws}/{JAMAI_REMOTE}')} -m modules.jam.jamai.campaign_manifest "
           f"{qc}/campaign.request.json --materialize > {qc}/materialize.json 2>&1 && "
           f"{_container_exec(ws)} -m py_compile {qs}/input.py && "
           f"mkdir -p {qs}/logs {qs}/current {qs}/slurm && "
           f"echo {script_b64} | base64 -d > {qs}/slurm/array_1_{n}.sh && "
           f"sbatch {qs}/slurm/array_1_{n}.sh")
    r = _remote(u, mat, timeout=600)
    out = (r.get("out") or "") + (r.get("err") or "")
    m = re.search(r"Submitted batch job (\d+)", out)
    if not m:
        tail = _remote(u, f"tail -c 1500 {qc}/materialize.json 2>/dev/null", timeout=30).get("out")
        return {"status": "error", "message": "The campaign was not submitted.",
                "detail": out[-600:], "materialize_log": (tail or "")[-1500:]}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = {"collection_id": folder, "campaign_name": norm["job"]["campaignName"],
             "campaign_folder": folder, "created_at": now, "updated_at": now,
             "workspace_path": ws, "replicas": n,
             "jobs": [{"job_id": m.group(1), "first": 1, "last": n, "submitted_at": now}],
             "slurm_job_id": m.group(1), "status": "submitted", "input_card": norm}
    _registry(u, {"kind": "upsert", "entry": entry})
    return {"status": "submitted", "collection_id": folder, "job_id": m.group(1), "replicas": n,
            "partition": norm["slurm"]["partition_effective"], "campaign_folder": croot,
            "log_path": f"{stage}/logs/stage_{m.group(1)}_<replica>.out", "summary": v["summary"]}


def append_replicas(collection_id="", count=0, confirm="", username=""):
    """Add replicas to an existing campaign (same stage, new array indices).
    Always asks for the confirmation phrase first."""
    u, err = _need_user(username)
    if err:
        return err
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}; list_campaigns shows them."}
    try:
        count = int(count)
        if not 1 <= count <= 2000:
            raise ValueError
    except (TypeError, ValueError):
        return {"status": "error", "message": "count must be between 1 and 2000."}
    first, last = int(e["replicas"]) + 1, int(e["replicas"]) + count
    plan = {"collection_id": collection_id, "current_replicas": e["replicas"], "add": count,
            "new_total": last, "replica_range": f"{first}-{last}",
            "partition": e["input_card"]["slurm"]["partition_effective"]}
    phrase = _confirm_phrase("APPEND", plan)
    if (confirm or "").strip() != phrase:
        return {"status": "needs_confirmation", "plan": plan, "confirmation_phrase": phrase,
                "message": "Validated and launch-ready. Ask the user to confirm with the phrase; "
                           "nothing is queued until then."}
    ws = _workspace(u)
    stage = f"{ws}/{CAMPAIGNS_DIR}/{e['campaign_folder']}/stages/{STAGE}"
    sb = base64.b64encode(_array_script(ws, e["campaign_folder"], e["input_card"], first, last).encode()).decode()
    qs = _shq(stage)
    r = _remote(u, f"echo {sb} | base64 -d > {qs}/slurm/array_{first}_{last}.sh && "
                   f"sbatch {qs}/slurm/array_{first}_{last}.sh", timeout=60)
    m = re.search(r"Submitted batch job (\d+)", (r.get("out") or "") + (r.get("err") or ""))
    if not m:
        return {"status": "error", "message": "sbatch refused the job.", "detail": (r.get("err") or r.get("out"))[-400:]}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    e["jobs"] = list(e.get("jobs") or []) + [{"job_id": m.group(1), "first": first, "last": last, "submitted_at": now}]
    e.update(replicas=last, updated_at=now, slurm_job_id=m.group(1), status="submitted")
    _registry(u, {"kind": "upsert", "entry": e})
    return {"status": "submitted", "job_id": m.group(1), **plan}


# ---- collections and status ----------------------------------------------------

# squeue can fail (a busy controller, a timeout). The queue is then unknown,
# which is never taken for empty: stop and delete refuse, and the lists say so.
QUEUE_UNKNOWN = "Could not read the Slurm queue, so what is still queued is unknown. Try again in a minute."
QUEUE_UNKNOWN_REFUSED = "Could not read the Slurm queue, so nothing was changed. Try again in a minute."

# How a finished replica ended, from its fit_summary.json. When the
# optimization fails, FitPack's fitlib/ai.py computes chi2 at the starting
# values instead and still writes status 'success', with mode
# 'validation_only' and no .msr file. So a replica is 'fitted' only with
# status 'success' and mode 'fit' (a summary from before 'mode' was written:
# when its .msr file exists); any other 'success' is 'not_fitted'; any other
# status, or a summary that cannot be read, is 'failed'. The status,
# postprocess and log snippets all start with it, so they always agree.
_REPLICA_STATE = r'''
import json, os
def replica_state(path):
    # (state, summary); state None while the replica has no fit_summary.json.
    if not os.path.isfile(path):
        return None, None
    try:
        s = json.load(open(path))
    except Exception:
        return 'failed', None
    if not isinstance(s, dict):
        return 'failed', None
    if s.get('status') != 'success':
        return 'failed', s
    if s.get('mode'):
        return ('fitted' if s['mode'] == 'fit' else 'not_fitted'), s
    msr = s.get('msr_path')
    return ('fitted' if msr and os.path.isfile(str(msr)) else 'not_fitted'), s
'''

_STATUS = _REPLICA_STATE + r'''
import json, glob, os, subprocess
stage = __STAGE__
jobs = __JOBS__
total = __TOTAL__
# Finished replicas (those with a fit_summary.json), by how they ended.
done = ok = not_fitted = 0
for i in range(1, total + 1):
    state, _ = replica_state(os.path.join(stage, 'current', 'replica_%d' % i, 'fit_summary.json'))
    if state:
        done += 1
        ok += state == 'fitted'
        not_fitted += state == 'not_fitted'
# Tasks of these jobs still in the queue, one line per array task (-r). The
# user's whole queue is read and filtered here: squeue -j also fails for a job
# Slurm has already forgotten, so only this way does a failure mean that the
# queue could not be read. It is then None, never empty. Tasks that have just
# ended are left out.
ended = {'COMPLETED', 'CANCELLED', 'FAILED', 'TIMEOUT', 'PREEMPTED', 'BOOT_FAIL', 'NODE_FAIL',
         'DEADLINE', 'OUT_OF_MEMORY', 'REVOKED', 'SPECIAL_EXIT'}
states, queued_jobs = {}, set()
ids = {str(j['job_id']) for j in jobs}
try:
    q = subprocess.run(['squeue', '-h', '-r', '-u', __USER__, '-o', '%F|%T'], capture_output=True,
                       text=True, timeout=30) if ids else None
    if q and q.returncode != 0:
        raise RuntimeError(q.stderr)
    for line in (q.stdout if q else '').splitlines():
        job, _, s = line.strip().partition('|')
        if job in ids and s and s not in ended:
            states[s] = states.get(s, 0) + 1
            queued_jobs.add(job)
except Exception:
    states, queued_jobs = None, set()
print(json.dumps({'finished': done, 'succeeded': ok, 'not_fitted': not_fitted, 'failed': done - ok - not_fitted,
                  'queue': states, 'queued_jobs': sorted(queued_jobs)}))
'''


def _campaign_progress(u, e):
    """Replicas finished, and of those: succeeded (really fitted), not_fitted
    (the optimization failed; only chi2 at the starting values) and failed;
    and the campaign's tasks in the Slurm queue by state (queue None: squeue
    could not be read)."""
    code = _fill(_STATUS, STAGE=_stage_root(u, e), JOBS=e.get("jobs") or [],
                 TOTAL=int(e.get("replicas") or 0), USER=u)
    r = _run_ifarm_py(u, code, timeout=60)
    try:
        return json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        return {"finished": None, "succeeded": None, "not_fitted": None, "failed": None,
                "queue": None, "queued_jobs": []}


def _slurm_queue(u):
    """The user's whole Slurm queue, as squeue shows it (pending array tasks
    folded into one line), so jobs from draft_slurm_job show up too; None when
    squeue fails."""
    r = _remote(u, f"squeue -h -u {_shq(u)} -o '%i|%j|%T|%M|%P'", timeout=60, cap=200000)
    if r.get("rc") != 0:
        return None
    queue = []
    for line in (r.get("out") or "").splitlines():
        f = line.split("|")
        if len(f) == 5:
            queue.append({"job_id": f[0], "name": f[1], "state": f[2], "elapsed": f[3], "partition": f[4]})
    return queue


def list_campaigns(username=""):
    """Every campaign in this workspace (Collections), newest first, with live
    replica counts, and the user's whole Slurm queue."""
    u, err = _need_user(username)
    if err:
        return err
    out = []
    for e in sorted(_registry(u, {"kind": "read"}), key=lambda x: x.get("created_at", ""), reverse=True):
        prog = _campaign_progress(u, e)
        q = prog.get("queue")
        queued = None if q is None else sum(q.values())
        stored = e.get("status", "submitted")
        finished, not_fitted = prog.get("finished"), prog.get("not_fitted")
        # An unread queue keeps the stored status: the campaign may still run.
        # Nothing queued and not one finished replica fitted: 'not fitted'.
        state = ("running" if queued else stored if queued is None
                 else "not fitted" if finished and not_fitted == finished
                 else stored if stored in ("postprocessed", "cancelled")
                 else "completed" if finished else stored)
        out.append({"collection_id": e["collection_id"], "campaign_name": e.get("campaign_name"),
                    "created_at": e.get("created_at"), "replicas": e.get("replicas"),
                    "job_ids": [j["job_id"] for j in e.get("jobs") or []], "status": state,
                    "finished": finished, "succeeded": prog.get("succeeded"),
                    "not_fitted": not_fitted, "failed": prog.get("failed"),
                    "queue": q, "queued": queued, "reactions": e["input_card"]["reactions"],
                    "datasets": e["input_card"]["datasets"]})
    queue = _slurm_queue(u)
    res = {"status": "ok", "campaigns": out, "count": len(out), "queue": queue}
    if queue is None or any(c["queued"] is None for c in out):
        res["message"] = QUEUE_UNKNOWN
    return res


def campaign_status(collection_id="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}."}
    prog = _campaign_progress(u, e)
    # A replica's log is under the job whose first-last range holds the replica.
    out = {"status": "ok", "collection_id": collection_id, "replicas": e["replicas"],
           "jobs": e.get("jobs"), **prog,
           "campaign_folder": f"{_workspace(u)}/{CAMPAIGNS_DIR}/{e['campaign_folder']}",
           "log_pattern": f"{_stage_root(u, e)}/logs/stage_<job_id>_<replica>.out",
           "summary": _card_summary(e["input_card"])}
    if prog.get("queue") is None:
        out["note"] = QUEUE_UNKNOWN
    return out


def cancel_campaign(collection_id="", confirm="", username=""):
    """Stop a campaign: scancel its queued and running Slurm tasks. Always
    asks for the confirmation phrase first."""
    u, err = _need_user(username)
    if err:
        return err
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}."}
    ids = [str(j["job_id"]) for j in e.get("jobs") or []]
    prog = _campaign_progress(u, e)
    if prog.get("queue") is None:
        return {"status": "error", "message": QUEUE_UNKNOWN_REFUSED}
    queued = sum(prog["queue"].values())
    if not queued:
        return {"status": "ok", "cancelled": [], "message": "Nothing of this campaign is queued or running."}
    plan = {"collection_id": collection_id, "job_ids": ids, "queued_tasks": queued}
    phrase = _confirm_phrase("CANCEL", {"collection_id": collection_id, "job_ids": ids})
    if (confirm or "").strip() != phrase:
        return {"status": "needs_confirmation", "plan": plan, "confirmation_phrase": phrase,
                "message": "Nothing is cancelled yet. Ask the user to confirm with the phrase."}
    # Only the jobs that still have tasks in the queue, so an error from
    # scancel is a real one and not about a job that has already ended.
    live = [j for j in prog["queued_jobs"] if j in ids] or ids
    r = _remote(u, "scancel " + " ".join(_shq(j) for j in live), timeout=60)
    if r.get("rc") != 0:
        return {"status": "error", "message": "scancel refused to stop the campaign.",
                "detail": ((r.get("err") or "") + (r.get("out") or ""))[-400:]}
    e.update(status="cancelled", updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    _registry(u, {"kind": "upsert", "entry": e})
    return {"status": "cancelled", "collection_id": collection_id, "job_ids": ids, "cancelled": live,
            "message": "Stopped. Replicas that had already finished keep their results; postprocess "
                       "them as usual."}


def delete_campaign(collection_id="", delete_folder=False, confirm="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    if isinstance(delete_folder, str):
        delete_folder = {"true": True, "false": False}.get(delete_folder.strip().lower(), delete_folder)
    if not isinstance(delete_folder, bool):
        return {"status": "error", "message": "delete_folder must be true or false."}
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}."}
    q = _campaign_progress(u, e).get("queue")
    if q is None:
        return {"status": "error", "message": QUEUE_UNKNOWN_REFUSED}
    queued = sum(q.values())
    if queued:
        return {"status": "error", "message": f"This campaign still has {queued} task(s) in the Slurm "
                                              "queue. Stop it first (cancel_campaign), then delete it."}
    plan = {"collection_id": collection_id, "delete_folder": bool(delete_folder)}
    phrase = _confirm_phrase("DELETE", plan)
    if (confirm or "").strip() != phrase:
        return {"status": "needs_confirmation", "plan": plan, "confirmation_phrase": phrase}
    _registry(u, {"kind": "delete", "id": collection_id})
    removed = False
    if delete_folder:
        folder = f"{_workspace(u)}/{CAMPAIGNS_DIR}/{e['campaign_folder']}"
        if re.fullmatch(r"[A-Za-z0-9._-]+", e["campaign_folder"]):
            removed = "GONE" in (_remote(u, f"rm -rf {_shq(folder)} && echo GONE", timeout=120).get("out") or "")
    return {"status": "ok", "deleted": collection_id, "folder_removed": removed}


# ---- analysis ---------------------------------------------------------------------

_POSTPROCESS = _REPLICA_STATE + r'''
import glob, json, os, statistics
stage = __STAGE__
total = __TOTAL__
paths = sorted(glob.glob(os.path.join(stage, 'current', 'replica_*', 'fit_summary.json')))
# Only replicas that really fitted enter the statistics; those that did not fit
# (chi2 at the starting values only) are counted apart from the failed ones.
completed = failed = not_fitted = 0; chi2s = []; best = None; per = {}; cv = []
for p in paths:
    state, s = replica_state(p)
    if state == 'not_fitted':
        not_fitted += 1
    elif state == 'fitted' and s.get('chi2_total') is not None:
        completed += 1
        c = float(s['chi2_total']); chi2s.append(c)
        if best is None or c < best['chi2_total']:
            best = {'replica': s.get('replica'), 'chi2_total': c, 'summary_path': p, 'msr_path': s.get('msr_path')}
        if s.get('central_value_chi2_per_point') is not None:
            cv.append(float(s['central_value_chi2_per_point']))
        for k, v in (s.get('chi2_per_dataset') or {}).items():
            b = per.setdefault(k, {'chi2': [], 'npts': []})
            b['chi2'].append(float(v.get('chi2') if isinstance(v, dict) else v))
            if isinstance(v, dict) and v.get('npts') is not None: b['npts'].append(float(v['npts']))
    else:
        failed += 1
agg = {'status': 'success' if completed else 'failed', 'mode': 'aggregate', 'stage_name': 'pdf_stage',
       'summary_path': os.path.join(stage, 'current', 'fit_summary.json'),
       'total_replicas': total, 'completed_replicas': completed, 'failed_replicas': failed,
       'not_fitted_replicas': not_fitted, 'missing_replicas': max(total - len(paths), 0),
       'good_fraction': completed / total if total else None, 'best_replica': best,
       'mean_total_chi2': statistics.mean(chi2s) if chi2s else None,
       'median_total_chi2': statistics.median(chi2s) if chi2s else None,
       'min_total_chi2': min(chi2s) if chi2s else None, 'max_total_chi2': max(chi2s) if chi2s else None,
       'mean_central_value_chi2_per_point': statistics.mean(cv) if cv else None,
       'chi2_per_dataset': {}}
for k, b in per.items():
    m = statistics.mean(b['chi2']); n = statistics.mean(b['npts']) if b['npts'] else None
    agg['chi2_per_dataset'][k] = {'mean': m, 'min': min(b['chi2']), 'max': max(b['chi2']),
                                  'npts': int(n) if n else None, 'chi2_per_npts': m / n if n else None}
os.makedirs(os.path.join(stage, 'current'), exist_ok=True)
json.dump(agg, open(agg['summary_path'], 'w'), indent=2, sort_keys=True)
print(json.dumps(agg))
'''


def _stage_root(u, e):
    return f"{_workspace(u)}/{CAMPAIGNS_DIR}/{e['campaign_folder']}/stages/{STAGE}"


def _not_fitted_note(n, total):
    return (f"{n} of {total} replicas did not fit: the optimization failed and only chi2 at the "
            "starting values was computed. campaign_logs (the panel's Logs button) shows why.")


def postprocess_campaign(collection_id="", username=""):
    """Aggregate every replica's fit_summary.json into the campaign summary:
    the statistics over the replicas that really fitted."""
    u, err = _need_user(username)
    if err:
        return err
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}."}
    code = _fill(_POSTPROCESS, STAGE=_stage_root(u, e), TOTAL=int(e["replicas"]))
    r = _run_ifarm_py(u, code, timeout=120, cap=2000000)
    try:
        agg = json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        return {"status": "error", "message": "postprocess failed", "detail": (r.get("err") or "")[-400:]}
    e.update(status="postprocessed", result_summary={k: agg[k] for k in agg if k != "chi2_per_dataset"},
             updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    _registry(u, {"kind": "upsert", "entry": e})
    out = {"status": "ok", "collection_id": collection_id, "message": "Campaign postprocess completed.",
           "summary": {k: agg[k] for k in agg if k != "chi2_per_dataset"}}
    if agg.get("not_fitted_replicas"):
        out["note"] = out["not_fitted_note"] = _not_fitted_note(agg["not_fitted_replicas"],
                                                                agg["total_replicas"])
    return out


def campaign_summary(collection_id="", username=""):
    """The fit summary: replicas fitted (completed) / not fitted / failed, chi2
    statistics over the fitted ones, best replica, and chi2 per dataset (Load
    Fit Summary)."""
    u, err = _need_user(username)
    if err:
        return err
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}."}
    p = f"{_stage_root(u, e)}/current/fit_summary.json"
    r = _run_ifarm_py(u, f"import json; print(json.dumps(json.load(open({p!r}))))", timeout=60, cap=2000000)
    try:
        agg = json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        return {"status": "ok", "collection_id": collection_id, "summary": None,
                "message": "No fit summary yet. Run postprocess_campaign once replicas have finished."}
    rows = []
    for k, v in (agg.get("chi2_per_dataset") or {}).items():
        reac, _, did = k.partition(":")
        rows.append({"reaction": reac, "dataset_id": did, "chi2": v.get("mean"), "npts": v.get("npts"),
                     "chi2_per_npt": v.get("chi2_per_npts"), "chi2_min": v.get("min"), "chi2_max": v.get("max")})
    rows.sort(key=lambda x: -(x["chi2_per_npt"] or 0))
    out = {"status": "ok", "collection_id": collection_id,
           "summary": {k: agg[k] for k in agg if k != "chi2_per_dataset"}, "datasets": rows}
    prog, total, notes = _campaign_progress(u, e), int(e["replicas"]), []
    # A summary from before replicas that did not fit were counted apart may
    # count them as completed; replicas that finished after the last
    # postprocess are not in it yet.
    counted = sum(agg.get(k) or 0 for k in ("completed_replicas", "not_fitted_replicas", "failed_replicas"))
    finished = prog.get("finished")
    if "not_fitted_replicas" not in agg:
        notes.append("This summary was made before the gateway told replicas that fitted from replicas "
                     "that did not fit, so it may count both as completed: run postprocess_campaign to "
                     "redo it.")
    elif ((prog.get("succeeded") or 0) > (agg.get("completed_replicas") or 0)
          or (finished or 0) > counted or total != agg.get("total_replicas")):
        notes.append(f"This summary covers {counted} finished replicas, but "
                     + (f"{finished} of {total} have finished now" if finished is not None
                        else f"the campaign has {total} replicas now")
                     + ": run postprocess_campaign to include them.")
    if notes:
        out["stale"] = True
    # How many did not fit now (the summary's own count may be older).
    n = prog.get("not_fitted")
    n = agg.get("not_fitted_replicas") if n is None else n
    if n:
        out["not_fitted_note"] = _not_fitted_note(n, total)
        notes.append(out["not_fitted_note"])
    if notes:
        out["note"] = " ".join(notes)
    return out


_PDF_PLOTS = r'''
import glob, json, os, base64
stage = __STAGE__; flavors = __FLAVORS__; q2 = __Q2__; mean_overlay = __MEAN__
plot_dir = os.path.join(os.path.dirname(os.path.dirname(stage)), 'analysis', 'pdf_plots')
os.makedirs(plot_dir, exist_ok=True)
os.environ['MPLCONFIGDIR'] = os.path.join(plot_dir, '.matplotlib')
records = []
for p in sorted(glob.glob(os.path.join(stage, 'current', 'replica_*', 'fit_summary.json'))):
    try: s = json.load(open(p))
    except Exception: continue
    if s.get('status') == 'success' and s.get('msr_path') and os.path.isfile(s['msr_path']):
        records.append(s)
if not records:
    print(json.dumps({'status': 'missing_inputs', 'message': 'No finished replica with an .msr file yet.'})); raise SystemExit(0)
import matplotlib; matplotlib.use('Agg')
from matplotlib import pyplot as plt
import numpy as np
from fitlib.resman import RESMAN
from tools.config import conf, load_config
from tools.tools import load
conf.clear(); load_config(os.path.join(stage, 'input.py'))
resman = RESMAN(nworkers=1, parallel=False, datasets=False)
pdf = conf['pdf']
xs = np.logspace(-3, np.log10(0.9), 120)
curves = {f: [] for f in flavors}
for s in records:
    m = load(s['msr_path'])
    order = m.get('order', {}).get(0); params = m.get('params', {}).get(0)
    if order is not None: resman.parman.order = order
    resman.parman.set_new_params(params, initial=True)
    for f in flavors:
        curves[f].append([pdf.get_xF(float(x), q2, f) for x in xs])
resman.shutdown()
names = {'ub': r'\bar{u}', 'db': r'\bar{d}', 'sb': r'\bar{s}'}
out = []
for f in flavors:
    arr = np.array(curves[f], dtype=float)
    fig, ax = plt.subplots(figsize=(6.0, 4.0))
    for c in arr: ax.plot(xs, c, lw=1.0, alpha=0.5)
    if mean_overlay and len(arr) > 1:
        ax.fill_between(xs, np.percentile(arr, 16, axis=0), np.percentile(arr, 84, axis=0), color='#0f172a', alpha=0.10, lw=0)
    if mean_overlay:
        ax.plot(xs, arr.mean(axis=0), color='#111', lw=2.2, ls='--', label='mean'); ax.legend(frameon=False)
    ax.axhline(0, color='#64748b', lw=0.8, ls='--'); ax.set_xscale('log')
    ax.set_xlabel('$x$'); ax.set_ylabel('$x %s$' % names.get(f, f))
    ax.set_title('%s PDF, %d replicas, $Q^2=%g$ GeV$^2$' % (f, len(arr), q2)); ax.grid(alpha=0.25, which='both')
    path = os.path.join(plot_dir, 'pdf_%s_q2_%g_replicas.png' % (f, q2))
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)
    out.append({'flavor': f, 'path': path, 'n_replicas': int(len(arr)),
                'png_b64': base64.b64encode(open(path, 'rb').read()).decode()})
print(json.dumps({'status': 'success', 'plots': out}))
'''


def campaign_pdf_plots(collection_id="", flavors="u,d,g,s", q2=10.0, mean_overlay=True, username=""):
    """Draw the PDFs of every fitted replica at Q2 (View PDF Plots)."""
    u, err = _need_user(username)
    if err:
        return err
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}."}
    fl = [f.strip() for f in (flavors or "").split(",") if f.strip() in ("g", "u", "d", "s", "ub", "db", "sb")]
    if not fl:
        return {"status": "error", "message": "flavors: a comma list of g, u, d, s, ub, db, sb"}
    try:
        q2 = float(q2)
        assert q2 > 0
    except Exception:
        return {"status": "error", "message": "q2 must be a positive number"}
    code = _fill(_PDF_PLOTS, STAGE=_stage_root(u, e), FLAVORS=fl, Q2=q2, MEAN=bool(mean_overlay))
    ws = _workspace(u)
    r = _run_ifarm_py(u, code, apptainer=True, timeout=600, cap=20000000,
                      env={"MPLCONFIGDIR": f"{ws}/{CAMPAIGNS_DIR}/.matplotlib"})
    try:
        d = json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        return {"status": "error", "message": "plotting failed", "detail": (r.get("err") or r.get("out") or "")[-600:]}
    if d.get("status") != "success":
        return {"status": d.get("status"), "message": d.get("message")}
    plots = [{"flavor": p["flavor"], "path": p["path"], "n_replicas": p["n_replicas"],
              "data_url": "data:image/png;base64," + p["png_b64"]} for p in d["plots"]]
    return {"status": "ok", "collection_id": collection_id, "q2": q2,
            "plots": [{k: v for k, v in p.items() if k != "data_url"} for p in plots],
            "_images": [{"data": p["png_b64"], "mimeType": "image/png"} for p in d["plots"]],
            "_panel_plots": plots}


_BY_EXPERIMENT = r'''
import glob, json, os, base64
stage = __STAGE__; want = __WANT__; plot = __PLOT__
summ = os.path.join(stage, 'current', 'fit_summary.json')
best = None
try:
    best = (json.load(open(summ)).get('best_replica') or {}).get('msr_path')
except Exception:
    pass
if not best:
    for p in sorted(glob.glob(os.path.join(stage, 'current', 'replica_*', 'fit_summary.json'))):
        try: s = json.load(open(p))
        except Exception: continue
        if s.get('status') == 'success' and s.get('msr_path') and (best is None or s['chi2_total'] < best[1]):
            best = (s['msr_path'], s['chi2_total'])
    best = best[0] if best else None
if not best or not os.path.isfile(best):
    print(json.dumps({'status': 'missing_inputs', 'message': 'No finished replica yet.'})); raise SystemExit(0)
import numpy as np
from fitlib.resman import RESMAN
from tools.config import conf, load_config
from tools.tools import load
conf.clear(); load_config(os.path.join(stage, 'input.py')); conf['bootstrap'] = False
resman = RESMAN(nworkers=1, parallel=False, datasets=True)
m = load(best)
if m.get('order', {}).get(0) is not None: resman.parman.order = m['order'][0]
resman.parman.set_new_params(m['params'][0], initial=True)
resman.get_residuals(resman.parman.par, initial=True)
chi2 = resman.get_chi2()
def first(tab, *keys):
    for k in keys:
        if k in tab:
            v = tab[k]
            try: return str(v[0]) if hasattr(v, '__len__') and not isinstance(v, str) else str(v)
            except Exception: return str(v)
    return ''
groups, points = {}, {}
for reac, attr in (('idis', 'idis_res'), ('dy', 'dy_res'), ('wasym', 'wasym_res'), ('zrap', 'zrap_res'),
                   ('wzrv', 'wzrv_res'), ('jet', 'jet_res'), ('wc', 'wc_res')):
    res = getattr(resman, attr, None)
    tabs = getattr(res, 'tabs', None)
    if not isinstance(tabs, dict): continue
    for idx, tab in tabs.items():
        exp = first(tab, 'col') or 'unknown'
        info = (chi2.get(reac) or {}).get(idx) or {}
        g = groups.setdefault(exp, {'experiment': exp, 'datasets': [], 'chi2': 0.0, 'npts': 0})
        n = int(info.get('npts') or len(tab.get('value', [])))
        c = float(info.get('chi2') or 0.0)
        g['datasets'].append({'reaction': reac, 'dataset_id': str(idx), 'target': first(tab, 'target', 'reaction'),
                              'observable': first(tab, 'obs'), 'npts': n, 'chi2': c, 'chi2_per_npt': c / n if n else None})
        g['chi2'] += c; g['npts'] += n
        if plot and (not want or exp.lower() in want):
            xk = next((k for k in ('X', 'xF', 'Y', 'pT', 'eta', 'Q2') if k in tab), None)
            thy = tab.get('thy', tab.get('prediction'))
            if thy is None: continue
            points[(exp, reac, idx)] = {'x': list(np.asarray(tab[xk] if xk else range(len(tab['value'])), float)),
                                        'xlabel': xk or 'point', 'value': list(np.asarray(tab['value'], float)),
                                        'alpha': list(np.asarray(tab.get('alpha', np.zeros(len(tab['value']))), float)),
                                        'thy': list(np.asarray(thy, float)),
                                        'title': '%s %s %s' % (reac, idx, first(tab, 'obs'))}
resman.shutdown()
rows = sorted(groups.values(), key=lambda g: -(g['chi2'] / g['npts'] if g['npts'] else 0))
for g in rows: g['chi2_per_npt'] = g['chi2'] / g['npts'] if g['npts'] else None
images = []
if plot and points:
    import matplotlib; matplotlib.use('Agg')
    from matplotlib import pyplot as plt
    out_dir = os.path.join(os.path.dirname(os.path.dirname(stage)), 'analysis', 'by_experiment')
    os.makedirs(out_dir, exist_ok=True)
    keys = list(points)[:12]
    ncol = 2 if len(keys) > 1 else 1; nrow = (len(keys) + ncol - 1) // ncol
    fig, axes = plt.subplots(nrow, ncol, figsize=(6.0 * ncol, 3.4 * nrow), squeeze=False)
    for ax, key in zip(axes.flat, keys):
        p = points[key]
        ax.errorbar(p['x'], p['value'], yerr=p['alpha'], fmt='o', ms=3, lw=0.8, color='#111', label='data')
        ax.plot(p['x'], p['thy'], 's', ms=3, color='#c2410c', alpha=0.85, label='theory (best replica)')
        if p['xlabel'] in ('X',): ax.set_xscale('log')
        ax.set_xlabel(p['xlabel']); ax.set_title('%s  ·  %s' % (key[0], p['title']), fontsize=9)
        ax.grid(alpha=0.25); ax.legend(fontsize=7, frameon=False)
    for ax in list(axes.flat)[len(keys):]: ax.axis('off')
    fig.tight_layout()
    path = os.path.join(out_dir, 'data_vs_theory_%s.png' % ('_'.join(sorted(want))[:60] or 'all'))
    fig.savefig(path, dpi=100); plt.close(fig)
    images.append({'path': path, 'png_b64': base64.b64encode(open(path, 'rb').read()).decode(), 'panels': len(keys)})
print(json.dumps({'status': 'success', 'best_msr': best, 'experiments': rows, 'images': images}))
'''


def analyze_by_experiment(collection_id="", experiments="", plot=True, username=""):
    """chi2 of the best replica grouped by experiment, and data vs theory plots
    for the experiments named (comma list; empty = all)."""
    u, err = _need_user(username)
    if err:
        return err
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}."}
    want = [x.strip().lower() for x in (experiments or "").split(",") if x.strip()]
    code = _fill(_BY_EXPERIMENT, STAGE=_stage_root(u, e), WANT=want, PLOT=bool(plot))
    ws = _workspace(u)
    r = _run_ifarm_py(u, code, apptainer=True, timeout=900, cap=20000000,
                      env={"MPLCONFIGDIR": f"{ws}/{CAMPAIGNS_DIR}/.matplotlib"})
    try:
        d = json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        return {"status": "error", "message": "analysis failed", "detail": (r.get("err") or r.get("out") or "")[-800:]}
    if d.get("status") != "success":
        return {"status": d.get("status"), "message": d.get("message")}
    imgs = d.get("images") or []
    return {"status": "ok", "collection_id": collection_id, "best_msr": d["best_msr"],
            "experiments": d["experiments"],
            "plots": [{"path": i["path"], "panels": i["panels"]} for i in imgs],
            "_images": [{"data": i["png_b64"], "mimeType": "image/png"} for i in imgs],
            "_panel_plots": [{"path": i["path"], "data_url": "data:image/png;base64," + i["png_b64"]} for i in imgs]}


# Each replica's state and Slurm log (Logs). Replica i's log is
# logs/stage_<job>_<i>.out of the job whose first-last range holds i (each
# append is a job of its own). Slurm merges the replica's stdout and stderr
# there, and stdout is buffered, so a Python traceback can sit anywhere in
# the file, often near its start: the whole file is searched. A traceback
# block runs from "Traceback (most recent call last):" to its exception line,
# chained exceptions included. The overview searches the logs for up to a
# minute; a log it had no time for has has_traceback None. The queue is read
# as in the status snippet: a failure leaves it unknown, never empty.
_LOGS = _REPLICA_STATE + r'''
import subprocess, time
stage = __STAGE__
jobs = __JOBS__
total = __TOTAL__
want = __REPLICA__
nlines = __LINES__
MARK = 'Traceback (most recent call last):'
CHAINED = ('During handling of the above exception, another exception occurred:',
           'The above exception was the direct cause of the following exception:')
PROBLEMS = ('not_fitted', 'failed', 'no_output')
WIDTH = 1000                    # the longest line returned
deadline = time.time() + 60
ended = {'COMPLETED', 'CANCELLED', 'FAILED', 'TIMEOUT', 'PREEMPTED', 'BOOT_FAIL', 'NODE_FAIL',
         'DEADLINE', 'OUT_OF_MEMORY', 'REVOKED', 'SPECIAL_EXIT'}
ids = set(str(j.get('job_id')) for j in jobs)
queue, queue_read = {}, True    # replica (the array task index) -> its Slurm state
try:
    q = subprocess.run(['squeue', '-h', '-r', '-u', __USER__, '-o', '%F|%K|%T'], capture_output=True,
                       text=True, timeout=30) if ids else None
    if q and q.returncode != 0:
        raise RuntimeError(q.stderr)
    for line in (q.stdout if q else '').splitlines():
        job, task, s = (line.strip().split('|') + ['', ''])[:3]
        if job in ids and task.isdigit() and s and s not in ended:
            queue[int(task)] = s
except Exception:
    queue, queue_read = {}, False

def clip(line):
    return line if len(line) <= WIDTH else line[:WIDTH] + ' ...'

def num(s, key):
    try:
        v = float(s[key])
    except Exception:
        return None
    return v if abs(v) < float('inf') else None     # NaN and infinities are not JSON

def job_of(i):
    found = None
    for j in jobs:
        try:
            if int(j['first']) <= i <= int(j['last']):
                found = str(j['job_id'])
        except (KeyError, TypeError, ValueError):
            pass
    return found

def has_traceback(path):
    mark, prev = MARK.encode(), b''
    with open(path, 'rb') as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                return False
            if mark in prev + chunk:
                return True
            prev = chunk[-len(mark):]

def tracebacks(path):
    # (the log's last traceback block, the exception line of each block in file order)
    last, excs, cur, frames, held = [], [], None, False, []
    with open(path, 'rb') as fh:
        for raw in fh:
            line = clip(raw.decode('utf-8', 'replace').rstrip('\r\n'))
            if cur is not None and frames:
                cur.append(line)
                frames = not line.strip() or line[0].isspace()    # the first unindented line is the exception
                continue
            if line.startswith(MARK):
                if cur is not None and any(h.strip() in CHAINED for h in held):
                    cur.extend(held)                               # a chained exception: the same block
                else:
                    if cur is not None:
                        last = cur; excs.append(cur[-1])
                    cur = []
                cur.append(line)
                frames, held = True, []
            elif cur is not None:
                held.append(line)
                if (line.strip() and line.strip() not in CHAINED) or len(held) > 3:
                    last = cur; excs.append(cur[-1])
                    cur, held = None, []
    if cur is not None:
        last = cur; excs.append(cur[-1])
    return last, excs

def tail(path, n):
    with open(path, 'rb') as fh:
        fh.seek(0, 2)
        end = pos = fh.tell()
        data = b''
        while pos > 0 and data.count(b'\n') <= n and end - pos < (4 << 20):
            step = min(1 << 16, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    lines = data.decode('utf-8', 'replace').split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    if pos > 0:
        lines = lines[1:]               # the first line read may be cut
    # Older jobs ran without TERM: fitlib's screen clear wrote this line at every step.
    lines = [l for l in lines if l.strip() != 'TERM environment variable not set.']
    out = []
    for line in lines[-n:]:
        # What a terminal shows of a line that carriage returns rewrote.
        parts = [p for p in line.split('\r') if p.strip()]
        out.append(clip(parts[-1] if parts else ''))
    return out

def row_of(i, scan):
    state, s = replica_state(os.path.join(stage, 'current', 'replica_%d' % i, 'fit_summary.json'))
    if state is None:
        if i in queue:
            state = 'running' if queue[i] in ('RUNNING', 'COMPLETING') else 'queued'
        else:
            state = 'no_output' if queue_read else 'unknown'
    job = job_of(i)
    log = os.path.join(stage, 'logs', 'stage_%s_%d.out' % (job, i)) if job else None
    row = {'replica': i, 'state': state, 'chi2_total': num(s, 'chi2_total'),
           'central_value_chi2_per_point': num(s, 'central_value_chi2_per_point'),
           'job_id': job, 'log': log, 'log_exists': bool(log and os.path.isfile(log)), 'has_traceback': False}
    if state in ('running', 'queued'):
        row['slurm_state'] = queue[i]
    if row['log_exists']:
        row['has_traceback'] = None
        if scan and time.time() < deadline:
            try:
                if state in PROBLEMS:
                    excs = tracebacks(log)[1]
                    row['has_traceback'] = bool(excs)
                    if excs:
                        row['error'] = excs[-1]
                else:
                    row['has_traceback'] = has_traceback(log)
            except OSError:
                pass
    return row, s

def detail_of(i):
    row, s = row_of(i, False)
    row['fit_summary'] = None if s is None else {
        'status': s.get('status'), 'mode': s.get('mode'), 'requested_mode': s.get('requested_mode'),
        'chi2_total': num(s, 'chi2_total'),
        'central_value_chi2_per_point': num(s, 'central_value_chi2_per_point')}
    row.update(traceback='', exceptions=[], tail='', tail_lines=0)
    if row['log_exists']:
        try:
            last, excs = tracebacks(row['log'])
            lines = tail(row['log'], nlines)
        except OSError as e:
            row['log_error'] = str(e)
        else:
            if len(last) > 60:
                last = last[:15] + ['    ... (%d lines omitted) ...' % (len(last) - 59)] + last[-44:]
            row.update(has_traceback=bool(excs), traceback='\n'.join(last), exceptions=excs[-10:],
                       tail='\n'.join(lines), tail_lines=len(lines))
            if excs:
                row['error'] = excs[-1]
    return row

if want:
    print(json.dumps({'queue_read': queue_read, 'detail': detail_of(want)}))
else:
    rows = [row_of(i, True)[0] for i in range(1, total + 1)]
    first = next((r['replica'] for r in rows if r['state'] in PROBLEMS), 0)
    print(json.dumps({'queue_read': queue_read, 'rows': rows, 'detail': detail_of(first) if first else None}))
'''
_LOG_STATES = ("fitted", "not_fitted", "failed", "running", "queued", "no_output")
# What a state means, for the user; and how the overview message counts it.
_LOG_MEANING = {
    "fitted": "The optimization ran and this replica was fitted.",
    "not_fitted": "The optimization failed, so FitPack only computed chi2 at the starting values: this "
                  "replica has no fitted parameters.",
    "failed": "The replica ended without success: its fit_summary.json reports a failure or cannot be read.",
    "running": "The replica is running.",
    "queued": "The replica is waiting in the Slurm queue.",
    "no_output": "The replica wrote no fit_summary.json and nothing of it is queued: it stopped before "
                 "writing results (an error, the time limit, or it was cancelled).",
    "unknown": "The replica has no fit_summary.json yet, and the Slurm queue could not be read, so whether "
               "it is still queued is unknown.",
}
_LOG_COUNTED = {"not_fitted": "did not fit", "failed": "failed", "running": "running", "queued": "queued",
                "no_output": "left no output", "unknown": "unknown (the Slurm queue could not be read)"}


def campaign_logs(collection_id="", replica=0, lines=80, username=""):
    """Each replica's state and Slurm log (the Analysis tab's Logs). replica 0:
    every replica, plus the Python error and the end of the log of the first
    one that did not fit, failed or left no output; replica N: that one."""
    u, err = _need_user(username)
    if err:
        return err
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}."}
    total = int(e.get("replicas") or 0)
    try:
        replica, lines = int(replica or 0), int(lines or 80)
    except (TypeError, ValueError):
        return {"status": "error", "message": "replica and lines must be whole numbers."}
    if not 0 <= replica <= total:
        return {"status": "error", "message": f"Campaign {collection_id!r} has replicas 1 to {total}; "
                                              f"there is no replica {replica}."}
    code = _fill(_LOGS, STAGE=_stage_root(u, e), JOBS=e.get("jobs") or [], TOTAL=total,
                 REPLICA=replica, LINES=max(1, min(lines, 400)), USER=u)
    try:
        r = _run_ifarm_py(u, code, timeout=150, cap=4000000)
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "Reading the logs took longer than 2.5 minutes. Ask for one "
                                              "replica at a time (replica=N)."}
    try:
        d = json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        return {"status": "error", "message": "Could not read the campaign's logs.",
                "detail": (r.get("err") or r.get("out") or "")[-400:]}
    detail = d.get("detail")
    if detail:
        detail["meaning"] = _LOG_MEANING.get(detail["state"], "")
    out = {"status": "ok", "collection_id": collection_id, "replicas": total}
    if replica:
        out.update(replica=replica, replica_detail=detail)
    else:
        rows = d.get("rows") or []
        counts = {s: sum(1 for x in rows if x["state"] == s) for s in _LOG_STATES}
        unknown = sum(1 for x in rows if x["state"] == "unknown")
        if unknown:
            counts["unknown"] = unknown
        message = ", ".join([f"{counts['fitted']} of {total} replicas fitted"]
                            + [f"{counts[s]} {_LOG_COUNTED[s]}" for s in _LOG_COUNTED if counts.get(s)]) + "."
        if detail:
            message += (f" Replica {detail['replica']} is the first that {_LOG_COUNTED[detail['state']]}: "
                        + ("replica_detail has its Python error and the end of its Slurm log."
                           if detail.get("traceback") else
                           "replica_detail has the end of its Slurm log, which holds no Python error."
                           if detail.get("log_exists") else "it has no Slurm log."))
        out.update(counts=counts, overview=rows, replica_detail=detail, message=message)
    if not d.get("queue_read", True):
        out["note"] = QUEUE_UNKNOWN
    return out


# ---- LLM-guided Slurm --------------------------------------------------------------

SLURM_NAME_RE = r"[A-Za-z0-9._-]{1,40}"


def slurm_partitions(username=""):
    """Partitions with their time limits and the current load, for choosing
    resources with the user."""
    u, err = _need_user(username)
    if err:
        return err
    r = _remote(u, "sinfo -h -o '%P|%l|%D|%C|%m' 2>/dev/null", timeout=60)
    parts = []
    for line in (r.get("out") or "").splitlines():
        f = line.split("|")
        if len(f) == 5:
            a, i, o, t = (f[3].split("/") + ["", "", "", ""])[:4]
            parts.append({"partition": f[0].rstrip("*"), "default": f[0].endswith("*"),
                          "time_limit": f[1], "nodes": f[2], "cpus_allocated": a, "cpus_idle": i,
                          "cpus_total": t, "memory_mb_per_node": f[4]})
    return {"status": "ok", "partitions": parts,
            "rule": f"auto: {SITE.get('short_partition')} for jobs up to {SITE.get('short_hours')} h, "
                    f"{SITE['partition']} otherwise"}


def draft_slurm_job(name="", command="", time_limit="01:00:00", cpus=1, mem="4G", partition="",
                    array="", in_container=True, workdir="", username=""):
    """Write a Slurm script from the resources agreed with the user, check it
    with `sbatch --test-only` (no job is queued), and return it with the
    confirmation phrase submit_slurm_job needs."""
    u, err = _need_user(username)
    if err:
        return err
    if not re.fullmatch(SLURM_NAME_RE, name or ""):
        return {"status": "error", "message": "name: letters, numbers, '.', '_' and '-' (up to 40)"}
    if not (command or "").strip():
        return {"status": "error", "message": "command: the shell command(s) the job runs"}
    secs = _parse_hms(time_limit)
    if not secs:
        return {"status": "error", "message": "time_limit must look like HH:MM:SS or D-HH:MM:SS"}
    try:
        cpus = int(cpus)
        assert 1 <= cpus <= 64
    except Exception:
        return {"status": "error", "message": "cpus must be between 1 and 64"}
    if not re.fullmatch(r"\d{1,4}[MG]", str(mem)):
        return {"status": "error", "message": "mem must look like 4G or 500M"}
    if array and not re.fullmatch(r"\d{1,5}(-\d{1,5})?(%\d{1,4})?", array):
        return {"status": "error", "message": "array must look like 1-10 or 1-100%10"}
    part = (partition or "").strip() or _auto_partition_seconds(secs)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", part):
        return {"status": "error", "message": "invalid partition"}
    ws, area = _workspace(u), _area(u)
    wd = os.path.normpath((workdir or ws).strip())
    if not (wd == area or wd.startswith(area + "/")):
        return {"status": "error", "message": f"workdir must be inside your own area ({area})"}
    sdir = f"{ws}/slurm"
    c = SITE["container"]
    body = command.strip()
    if in_container:
        binds = " ".join(f"--bind {b}" for b in c.get("binds", []))
        body = (f"apptainer exec {binds} --env PYTHONPATH={_shq(ws)} --env FITPACK={_shq(ws)} {c['image']} "
                f"bash -lc {_shq(body)}")
    script = "\n".join([
        "#!/bin/bash", f"#SBATCH --job-name={name}", f"#SBATCH --partition={part}",
        f"#SBATCH --time={time_limit}", f"#SBATCH --cpus-per-task={cpus}", f"#SBATCH --mem={mem}",
        *([f"#SBATCH --array={array}"] if array else []),
        f"#SBATCH --output={sdir}/{name}_%A{'_%a' if array else ''}.out",
        "set -euo pipefail", f"cd {_shq(wd)}", body, ""])
    b64 = base64.b64encode(script.encode()).decode()
    qd = _shq(sdir)
    r = _remote(u, f"mkdir -p {qd} && echo {b64} | base64 -d > {qd}/{name}.sh && "
                   f"sbatch --test-only {qd}/{name}.sh 2>&1", timeout=60)
    # The login shell on the compute host prints an unrelated profile warning.
    test = "\n".join(l for l in ((r.get("out") or "") + (r.get("err") or "")).splitlines()
                     if "syscshrc" not in l).strip()
    ok = "to start at" in test and "error" not in test.lower()
    phrase = _confirm_phrase("SLURM", {"name": name, "script": script})
    return {"status": "ok" if ok else "rejected", "script_path": f"{sdir}/{name}.sh", "script": script,
            "test_only": test[-400:], "partition": part, "confirmation_phrase": phrase,
            "next": "Show the user the script and the sbatch --test-only result. Submit with "
                    "submit_slurm_job(name, confirm=<phrase>) only after they confirm."}


def submit_slurm_job(name="", confirm="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    if not re.fullmatch(SLURM_NAME_RE, name or ""):
        return {"status": "error", "message": "invalid name"}
    path = f"{_workspace(u)}/slurm/{name}.sh"
    r = _remote(u, f"cat {_shq(path)}", timeout=30, cap=200000)
    if r.get("rc") != 0:
        return {"status": "error", "message": f"No draft {name}; run draft_slurm_job first."}
    script = r.get("out") or ""
    phrase = _confirm_phrase("SLURM", {"name": name, "script": script})
    if (confirm or "").strip() != phrase:
        return {"status": "needs_confirmation", "confirmation_phrase": phrase, "script": script}
    s = _remote(u, f"sbatch {_shq(path)}", timeout=60)
    m = re.search(r"Submitted batch job (\d+)", (s.get("out") or "") + (s.get("err") or ""))
    if not m:
        return {"status": "error", "message": "sbatch refused the job", "detail": (s.get("err") or s.get("out"))[-400:]}
    return {"status": "submitted", "job_id": m.group(1), "script_path": path}


# ── tool registry ───────────────────────────────────────────────────────────

TOOLS = {
    "ping": {"desc": "Reply pong.",
             "schema": {"type": "object", "properties": {}},
             "fn": lambda a: ping()},
    "get_console": {
        "desc": "Return the browser URLs of the operations panel (Connect / Project / "
                "Campaign / Collections / Analysis). Hand the user a panel_url when they "
                "need to sign in to the cluster, set up a workspace, or build and submit "
                "a fit by pointing and clicking. Optional tab opens the panel there.",
        "schema": {"type": "object",
                   "properties": {"tab": {"type": "string", "enum": list(PANEL_TABS)}}},
        "fn": lambda a: get_console(a.get("tab", "connect"))},
    "connect_hpc": {
        "desc": "Open (or reuse) your two-hop chain to the cluster (login host, then the "
                "compute host; see check_hpc_connection for this site's hosts). It acts as "
                "the user of your dashboard token. Call it with no credentials first: "
                "status=reused means the chain is live and nothing else is needed. If it is "
                "not live, do NOT ask for the PIN, one-time code or password in chat — give "
                "the user the panel's Connect tab (get_console tab=connect) and let them sign "
                "in there.",
        "schema": {"type": "object",
                   "properties": {"pin_otp": {"type": "string"},
                                  "password": {"type": "string"}}},
        "fn": lambda a: connect_hpc(pin_otp=a.get("pin_otp", ""), password=a.get("password", ""))},
    "node_diag": {"hidden": True,
                  "desc": "Run a shell command on the gateway node (hot_redeploy.py and "
                          "diagnostics). Not in tools/list; callable by name.",
                  "schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
                  "fn": lambda a: node_diag(a.get("cmd", "id; hostname"))},
    "fs_list": {"desc": "List a directory on the cluster (defaults to the user's own area).",
                "schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                "fn": lambda a: fs_list(a.get("path", ""))},
    "fs_read": {"desc": "Read a text file on the cluster, e.g. a campaign's artifacts: a replica's "
                       "log <campaign>/stages/pdf_stage/logs/stage_<job>_<replica>.out (campaign_status "
                       "gives the full path as log_pattern), fit_summary.json, input.py. Path must be under "
                       + " or ".join(SITE["read_roots"]) + ".",
                "schema": {"type": "object",
                           "properties": {"path": {"type": "string"},
                                          "max_lines": {"type": "integer"}}},
                "fn": lambda a: fs_read(a.get("path", ""), int(a.get("max_lines", 200)))},
    "setup_workspace": {
        "desc": "Create a workspace: clone the FitPack ai branch (repo_url defaults to "
                + JAM_REPO_URL + ") into a new folder of your own area ("
                + SITE["area"].format(user="<you>") + "), record it as the workspace and link "
                "the shared interpolation grids. A GitHub token for a private repository is "
                "typed on the panel's Project tab, never in chat. A folder that already exists "
                "is not cloned again: it becomes the workspace as it is. Run once before the "
                "first campaign.",
        "schema": {"type": "object",
                   "properties": {"repo_url": {"type": "string"},
                                  "target_dir": {"type": "string"},
                                  "github_token": {"type": "string"},
                                  "folder_name": {"type": "string"},
                                  "branch": {"type": "string"}}},
        "fn": lambda a: setup_workspace(repo_url=a.get("repo_url", ""),
                                        github_token=a.get("github_token", ""),
                                        target_dir=a.get("target_dir", ""),
                                        folder_name=a.get("folder_name", ""),
                                        branch=a.get("branch", ""))},
    "open_panel": {
        "desc": "Show the FemtoMind operations panel (Connect / Project / Campaign / "
                "Collections / Analysis). Clients that render MCP Apps display it inline; "
                "otherwise the result carries a browser URL to hand the user.",
        "schema": {"type": "object",
                   "properties": {"tab": {"type": "string", "enum": list(PANEL_TABS)}}},
        "meta": {"ui": {"resourceUri": PANEL_URI},
                 "openai/outputTemplate": PANEL_URI_SKYBRIDGE,
                 "openai/widgetAccessible": True},
        "fn": lambda a: open_panel(a.get("tab", "connect"))},
    "check_hpc_connection": {
        "desc": "Is your cluster chain open, which site and hosts it uses, and which "
                "workspace is recorded? login_open says the login hop alone is still open "
                "(signing in then needs only the password). Cheap; use it before anything "
                "that needs the cluster.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: check_hpc_connection()},
    "check_project": {
        "desc": "Report on your workspace: which checkout is in use, every checkout in "
                "your area, and whether code, data, grids, the campaign template and the "
                "campaign runner (FitPack ai branch) are all present. A missing item says "
                "how to fix it (fix: use_ai_branch or use_workspace).",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: check_project()},
    "use_workspace": {
        "desc": "Make an existing checkout in your own area the workspace that fits "
                "run in. path must be inside your own area ("
                + SITE["area"].format(user="<you>") + ") and contain fitlib/.",
        "schema": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"]},
        "fn": lambda a: use_workspace(a.get("path", ""))},
    "campaign_options": {
        "desc": "The fit composer's choices, as in the JAM app: objects, the Unpolarized PDF "
                "parameter table read from the workspace's examples/input02.py (the library: "
                "block, parameter, FIXED/FREE, value, min, max; source names the file), the "
                "reactions, default cuts, QCD and job settings with their allowed values, and the "
                "Slurm partitions. Needs the cluster chain and a workspace. Call it first and walk "
                "the user through each step.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: campaign_options()},
    "campaign_datasets": {
        "desc": "The datasets of one reaction (idis or dy), grouped by experiment, with labels "
                "(experiment, observable, target). Optional search text. Nothing is preselected.",
        "schema": {"type": "object", "properties": {"reaction": {"type": "string", "enum": ["idis", "dy"]},
                                                    "search": {"type": "string"}},
                   "required": ["reaction"]},
        "fn": lambda a: campaign_datasets(a.get("reaction", "idis"), a.get("search", ""))},
    "use_ai_branch": {
        "desc": "Switch the workspace to the FitPack ai branch (fitlib/ai.py runs campaigns). "
                "Refuses if tracked files have uncommitted changes.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: use_ai_branch()},
    "validate_campaign": {
        "desc": "Create Input Card: validate the composer card and return the summary (free/fixed "
                "parameters, the ones changed from the library, datasets, cuts, QCD, job, and "
                "points_after_cuts per dataset), the Slurm array script, the confirmation phrase and "
                "warnings (a dataset with no points left after the cuts, a data file without a column "
                "FitPack's status report reads); warnings never block submission. " + 'card = {"object": {"type": "pdf", "parameters": {"g1 a": {"value", "min", "max", "fixed": true|false}, ...}}, "reactions": ["idis", "dy"], "datasets": {"idis": ["10010", ...]}, "cuts": {"idis": [{"variable": "Q2", "op": ">", "value": 1.69, "enabled": true}]}, "qcd": {"order", "Q20", "tmc": "off"|"AOT", "higherTwist", "dglapMode", "asMode"}, "parameters": {"initialization": "default"|"random_within_bounds", "randomSeed", "perReplicaSeed"}, "job": {"campaignName", "replicas", "maxIterations", "tolerance", "optimizer", "bootstrap"}, "slurm": {"partition" ("" = auto), "time" (per replica, HH:MM:SS), "mem", "cpus"}}. Only parameters the user changed need to be listed; the rest keep the library values, and only changed rows are written into the card. A parameter switched to FREE needs a min and a max.',
        "schema": {"type": "object", "properties": {"card": {"type": "object"}}, "required": ["card"]},
        "fn": lambda a: validate_campaign(a.get("card"))},
    "submit_campaign": {
        "desc": "Submit Jobs: materialize the card and submit one Slurm array task per replica. "
                "Needs confirm = the phrase from validate_campaign, after the user has seen the "
                "summary and the Slurm script and agreed.",
        "schema": {"type": "object", "properties": {"card": {"type": "object"}, "confirm": {"type": "string"}},
                   "required": ["card"]},
        "fn": lambda a: submit_campaign(a.get("card"), a.get("confirm", ""))},
    "list_campaigns": {
        "desc": "Collections: every campaign in this workspace with its status (running, submitted, "
                "completed, not fitted, postprocessed, cancelled), job ids, how many replicas have "
                "finished: succeeded (really fitted), not_fitted (the optimization failed, so only chi2 "
                "at the starting values was computed) and failed; how many of its tasks are still "
                "queued, plus your whole Slurm queue (queue). When squeue cannot be read, queued and "
                "queue are null: unknown, not empty.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: list_campaigns()},
    "campaign_status": {
        "desc": "One campaign's Slurm state and replica progress: finished, succeeded (really fitted), "
                "not_fitted (the optimization failed, so only chi2 at the starting values was computed) "
                "and failed (queue is null when squeue cannot be read), its folder, and log_pattern: the "
                "path of a replica's log. campaign_logs shows why replicas did not fit or failed.",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string"}},
                   "required": ["collection_id"]},
        "fn": lambda a: campaign_status(a.get("collection_id", ""))},
    "postprocess_campaign": {
        "desc": "Postprocess: aggregate the finished replicas into the campaign fit summary. Only the "
                "replicas that really fitted enter the statistics; the ones that did not fit (only chi2 "
                "at the starting values) are counted apart (not_fitted_replicas).",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string"}},
                   "required": ["collection_id"]},
        "fn": lambda a: postprocess_campaign(a.get("collection_id", ""))},
    "campaign_summary": {
        "desc": "Load Fit Summary: replicas that fitted (completed_replicas), did not fit "
                "(not_fitted_replicas) and failed, chi2 statistics over the fitted ones, the best "
                "replica, and chi2/N per dataset (worst first). note says when replicas did not fit "
                "or the summary is out of date.",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string"}},
                   "required": ["collection_id"]},
        "fn": lambda a: campaign_summary(a.get("collection_id", ""))},
    "campaign_pdf_plots": {
        "desc": "View PDF Plots: draw x f(x, Q2) of every fitted replica (flavors: comma list of "
                "g, u, d, s, ub, db, sb; q2 in GeV^2; mean_overlay adds the mean and 68% band). "
                "Returns the images.",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string"},
                                                    "flavors": {"type": "string"},
                                                    "q2": {"type": "number"},
                                                    "mean_overlay": {"type": "boolean"}},
                   "required": ["collection_id"]},
        "fn": lambda a: campaign_pdf_plots(a.get("collection_id", ""), a.get("flavors", "u,d,g,s"),
                                           a.get("q2", 10.0), a.get("mean_overlay", True))},
    "analyze_by_experiment": {
        "desc": "Analysis by experiment: chi2/N of the best replica grouped by experiment (SLAC, "
                "BCDMS, NMC, E866 ...) and, for the experiments named, data vs theory plots per "
                "dataset. experiments: comma list, empty = all.",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string"},
                                                    "experiments": {"type": "string"},
                                                    "plot": {"type": "boolean"}},
                   "required": ["collection_id"]},
        "fn": lambda a: analyze_by_experiment(a.get("collection_id", ""), a.get("experiments", ""),
                                              a.get("plot", True))},
    "campaign_logs": {
        "desc": "Logs of a campaign: each replica's state (fitted, not fitted, failed, running, no "
                "output) and, for a replica that did not fit, the Python error and the end of its "
                "Slurm log. Use it whenever replicas fail or did not fit, and explain the error to the "
                "user.",
        "schema": {"type": "object", "properties": {
            "collection_id": {"type": "string"},
            "replica": {"type": "integer",
                        "description": "0 (default): every replica, with the Python error and the end of "
                                       "the log of the first one that did not fit, failed or left no "
                                       "output; N: replica N only."},
            "lines": {"type": "integer",
                      "description": "How many lines of the end of the log to show (default 80, "
                                     "at most 400)."}},
                   "required": ["collection_id"]},
        "fn": lambda a: campaign_logs(a.get("collection_id", ""), a.get("replica", 0), a.get("lines", 80))},
    "append_replicas": {
        "desc": "Generate More Replicas: add count replicas to a campaign. The first call returns "
                "the plan and a confirmation phrase; nothing is queued until the user confirms "
                "and the call is repeated with confirm = the phrase.",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string"},
                                                    "count": {"type": "integer"},
                                                    "confirm": {"type": "string"}},
                   "required": ["collection_id", "count"]},
        "fn": lambda a: append_replicas(a.get("collection_id", ""), a.get("count", 0), a.get("confirm", ""))},
    "cancel_campaign": {
        "desc": "Stop a campaign: cancel its queued and running Slurm tasks. The first call returns "
                "the plan and a confirmation phrase; nothing is cancelled until the user confirms "
                "and the call is repeated with confirm = the phrase.",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string"},
                                                    "confirm": {"type": "string"}},
                   "required": ["collection_id"]},
        "fn": lambda a: cancel_campaign(a.get("collection_id", ""), a.get("confirm", ""))},
    "delete_campaign": {
        "desc": "Remove a campaign from Collections (delete_folder also removes its folder and "
                "results). Refused while any of its tasks are in the Slurm queue: stop it first "
                "with cancel_campaign. Needs the confirmation phrase from a first call.",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string"},
                                                    "delete_folder": {"type": "boolean"},
                                                    "confirm": {"type": "string"}},
                   "required": ["collection_id"]},
        "fn": lambda a: delete_campaign(a.get("collection_id", ""), a.get("delete_folder") or False,
                                        a.get("confirm", ""))},
    "slurm_partitions": {
        "desc": "Partitions with time limits, idle/total CPUs and memory per node, for choosing "
                "Slurm resources with the user.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: slurm_partitions()},
    "draft_slurm_job": {
        "desc": "LLM-guided Slurm: write a job script from the resources agreed with the user "
                "(time_limit HH:MM:SS, cpus, mem like 4G, partition or empty for auto, optional "
                "array like 1-10), check it with sbatch --test-only (nothing is queued) and return "
                "it with a confirmation phrase. in_container runs the command in the site image "
                "with the workspace on PYTHONPATH.",
        "schema": {"type": "object", "properties": {"name": {"type": "string"},
                                                    "command": {"type": "string"},
                                                    "time_limit": {"type": "string"},
                                                    "cpus": {"type": "integer"},
                                                    "mem": {"type": "string"},
                                                    "partition": {"type": "string"},
                                                    "array": {"type": "string"},
                                                    "in_container": {"type": "boolean"},
                                                    "workdir": {"type": "string"}},
                   "required": ["name", "command"]},
        "fn": lambda a: draft_slurm_job(a.get("name", ""), a.get("command", ""), a.get("time_limit", "01:00:00"),
                                        a.get("cpus", 1), a.get("mem", "4G"), a.get("partition", ""),
                                        a.get("array", ""), a.get("in_container", True), a.get("workdir", ""))},
    "submit_slurm_job": {
        "desc": "Submit a drafted Slurm script. Needs confirm = the phrase from draft_slurm_job, "
                "after the user has read the script and agreed.",
        "schema": {"type": "object", "properties": {"name": {"type": "string"}, "confirm": {"type": "string"}},
                   "required": ["name"]},
        "fn": lambda a: submit_slurm_job(a.get("name", ""), a.get("confirm", ""))},
}


# ── http layer ──────────────────────────────────────────────────────────────

def _hlog(msg):
    try:
        with open(HTTPLOG, "a") as fh:
            fh.write(f"{time.time():.0f} {msg}\n")
    except OSError:
        pass


_FALLBACK_PANEL = ("<!doctype html><meta charset='utf-8'><title>JAM gateway</title>"
                   "<p style='font-family:sans-serif'>The panel file (jam_panel.html) is "
                   "missing on the gateway node. Re-run hot_redeploy.py to upload it.</p>")

INSTRUCTIONS = (
    f"JAM global-analysis gateway for {SITE['label']}: the FemtoMind JAM app's fitting "
    "campaign, as a conversation. Every call acts as the gateway's owner (the user whose "
    "dashboard token the client sends); never ask for or pass another username. Go one "
    "question at a time, in this order:\n"
    "1. HPC: check_hpc_connection; ask which HPC (its 'sites'). If not connected, the user "
    f"signs in ({SITE['login_secret']} for {SITE['login_host']}, {SITE['compute_secret']} for "
    f"{SITE['compute_host']}) on the panel's Connect tab — give the link (get_console "
    "tab=connect) and wait. Never ask for these in chat.\n"
    "2. Workspace: check_project lists the folders in the user's area and what the workspace "
    "has; ask which to use (use_workspace) or create a new one (setup_workspace; it clones the "
    "FitPack ai branch). If a clone needs a GitHub token, send the user to the panel's Project "
    "tab (the link in the result) — never ask for a token in chat. If the campaign runner or "
    "the campaign template is marked missing with fix: use_ai_branch, the checkout is not on the "
    "FitPack ai branch: offer use_ai_branch or a new workspace. A missing item without a fix (for "
    "example a workspace folder that no longer exists, or an ai-branch checkout without "
    "examples/input02.py): give the user its action; use_ai_branch does not help there.\n"
    "3. Non-perturbative object and parameters: only Unpolarized PDF is available. "
    "campaign_options returns the parameter table read from the workspace's "
    "examples/input02.py (the library): FREE/FIXED, value, min, max. Ask which to change; "
    "only changed rows are written into the card. A parameter switched to FREE needs a min "
    "and max from the user.\n"
    "4. Reactions: idis (DIS) or dy (Drell-Yan); the others are not available yet. Ask.\n"
    "5. Datasets and cuts: campaign_datasets per reaction, grouped by experiment; ask which "
    "ids. Show the reaction's cuts (e.g. Q2>1.69, W2>10) and ask whether to keep them.\n"
    "6. QCD settings: order, Q20, TMC, higher twist, DGLAP mode, alphaS mode.\n"
    "7. Job config: campaign name, replicas, parameter initialization (library values or "
    "random within bounds), inference (ensemble of pseudo-data replicas or central values), "
    "tolerance, max iterations, optimizer — and the Slurm resources per replica (time, "
    "memory, cpus, partition or auto), using slurm_partitions to explain the choice.\n"
    "Every setting shows its library value; the user keeps or changes it. Reactions and "
    "datasets are never preselected; never pick a value for the user.\n"
    "8. Review: validate_campaign; show the summary, its warnings (they never block "
    "submission) and the Slurm array script exactly as returned (not shortened); submit with "
    "submit_campaign(card, confirm=<phrase>) only after the user agrees.\n"
    "9. Collections and analysis: list_campaigns (it also returns the user's Slurm queue), "
    "campaign_status, postprocess_campaign, campaign_summary (fit summary), campaign_pdf_plots "
    "(PDF plots, returned as images), analyze_by_experiment (chi2 and data vs theory by "
    "experiment), campaign_logs (each replica's state and log), append_replicas (always "
    "confirmed with its phrase), cancel_campaign (stops its queued and running tasks) and "
    "delete_campaign (only when nothing of it is queued) — both also confirmed with their "
    "phrase. A replica counts as fitted only when its optimization ran: succeeded counts those, "
    "not_fitted counts replicas whose optimization failed (FitPack then only computed chi2 at "
    "the starting values). When replicas did not fit or failed, call campaign_logs and explain "
    "the error to the user in plain words. The panel's Collections and Analysis tabs show the "
    "same (Analysis has a Logs button).\n"
    "10. Any other cluster job: draft_slurm_job with the resources agreed with the user (it "
    "runs sbatch --test-only), show the returned script exactly as written, ask the user, "
    "then submit_slurm_job with the phrase.")

if SHARED:
    for _name, _spec in TOOLS.items():
        if _name != "node_diag":
            _spec["schema"].setdefault("properties", {})["username"] = {
                "type": "string",
                "description": "The JLab username to act as: the person who signed in on the panel. "
                               "Shared gateway; without it the gateway acts as its owner."}
    INSTRUCTIONS += (
        f"\nThis gateway is SHARED: it acts as the JLab user named in each call. First ask the user for "
        f"their JLab username and pass it as username in every tool call; without it, every call acts "
        f"as the gateway's owner ({GATEWAY_OWNER}). Each person signs in on the panel's Connect tab "
        "with their own username and credentials, and has their own workspace and campaigns.")

# Content-Security-Policy for the browser panel: it talks only to this origin,
# and it must not be framed elsewhere — it is where cluster credentials are typed.
PANEL_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
             "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
             "base-uri 'none'; form-action 'none'")


def _panel_html():
    try:
        with open(PANEL_FILE, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return _FALLBACK_PANEL


def _tool_entry(name, spec):
    entry = {"name": name, "description": spec["desc"], "inputSchema": spec["schema"]}
    if spec.get("meta"):
        entry["_meta"] = spec["meta"]
    return entry


def _tool_result(out, is_error=False):
    images = []
    if isinstance(out, dict):
        # Plots travel as MCP image content (the model sees them; MCP Apps
        # hosts and the panel render them); "_panel_plots" carries data URLs
        # for the panel only and stays out of the text.
        images = out.pop("_images", None) or []
        text_view = {k: v for k, v in out.items() if k != "_panel_plots"}
    else:
        text_view = out
    text = json.dumps(text_view, indent=2) if isinstance(text_view, (dict, list)) else str(text_view)
    res = {"content": [{"type": "text", "text": text}] +
                      [{"type": "image", "data": i["data"], "mimeType": i.get("mimeType", "image/png")}
                       for i in images],
           "isError": bool(is_error)}
    if isinstance(out, dict):
        res["structuredContent"] = out
    return res


def _request_identity(headers):
    """The user a request acts as, from its Authorization: Bearer <token>."""
    return _identify(headers.get("Authorization"))


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        _hlog(f"GET {self.path} accept={self.headers.get('Accept')!r} "
              f"ua={self.headers.get('User-Agent', '')[:40]!r}")
        accept = self.headers.get("Accept") or ""
        if "text/html" in accept:
            body = _panel_html().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", PANEL_CSP)
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

    def _reply(self, sid, status, payload=None):
        data = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", sid)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_POST(self):
        _hlog(f"POST {self.path} accept={self.headers.get('Accept')!r} "
              f"ua={self.headers.get('User-Agent', '')[:40]!r} "
              f"len={self.headers.get('Content-Length')}")
        sid = _req_sid(self.headers)
        _tls.user = _request_identity(self.headers)
        try:
            self._dispatch(sid)
        finally:
            _tls.user = ""

    def _dispatch(self, sid):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            req = json.loads(self.rfile.read(n))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return
        if not isinstance(req, dict):
            self._reply(sid, 200, {"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32600, "message": "batch requests are not supported"}})
            return
        method = req.get("method")
        params = req.get("params") or {}
        if "id" not in req:
            # A notification is acknowledged and never answered (MCP streamable HTTP).
            self._reply(sid, 202)
            return
        rid = req.get("id")
        error = None
        result = None
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2025-03-26"),
                      "capabilities": {"tools": {}, "resources": {}},
                      "serverInfo": {"name": MCP_NAME, "version": "0.9.0"},
                      "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            # Hidden tools (node_diag, for hot_redeploy.py) stay callable by name.
            result = {"tools": [_tool_entry(k, v) for k, v in TOOLS.items() if not v.get("hidden")]}
        elif method == "resources/list":
            result = {"resources": [
                {"uri": PANEL_URI, "name": "jam-panel", "mimeType": PANEL_MIME,
                 "description": "FemtoMind operations panel"},
                {"uri": PANEL_URI_SKYBRIDGE, "name": "jam-panel-chatgpt",
                 "mimeType": "text/html+skybridge",
                 "description": "FemtoMind operations panel (ChatGPT widget)"}]}
        elif method == "resources/read":
            uri = params.get("uri", "")
            mime = {PANEL_URI: PANEL_MIME,
                    PANEL_URI_SKYBRIDGE: "text/html+skybridge"}.get(uri)
            if not mime:
                error = {"code": -32002, "message": f"resource not found: {uri}"}
            else:
                result = {"contents": [{"uri": uri, "mimeType": mime, "text": _panel_html(),
                                        "_meta": {"ui": {"prefersBorder": True}}}]}
        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            spec = TOOLS.get(name)
            if spec is None:
                result = _tool_result("unknown tool", is_error=True)
            elif not getattr(_tls, "user", ""):
                result = _tool_result({"status": "unauthorized", "message": AUTH_HELP},
                                      is_error=True)
            else:
                asked = str(args.get("username") or "").strip().lower() if isinstance(args, dict) else ""
                if asked and asked != _tls.user and not SHARED:
                    result = _tool_result({"status": "error", "message":
                                           f"This gateway is not shared: it acts only as {GATEWAY_OWNER}."},
                                          is_error=True)
                elif asked and not re.fullmatch(USERNAME_RE, asked):
                    result = _tool_result({"status": "error", "message": "username: a JLab username"},
                                          is_error=True)
                else:
                    if asked:
                        _tls.user = asked
                    if spec.get("hidden") and _tls.user != GATEWAY_OWNER:
                        # The node shell is the owner's alone, shared mode or not.
                        result = _tool_result("unknown tool", is_error=True)
                    else:
                        try:
                            result = _tool_result(spec["fn"](args))
                        except Exception as e:
                            result = _tool_result(f"error: {e}", is_error=True)
        else:
            error = {"code": -32601, "message": f"method not found: {method}"}
        payload = {"jsonrpc": "2.0", "id": rid}
        if error:
            payload["error"] = error
        else:
            payload["result"] = result
        self._reply(sid, 200, payload)

    def log_message(self, *a):
        pass


# ── boot ────────────────────────────────────────────────────────────────────

def register(port):
    fqdn = socket.getfqdn() or socket.gethostname()
    url = f"http://{fqdn}:{port}/mcp"
    job_id = os.environ.get("SLURM_JOB_ID", "0")
    enc = urllib.parse.quote(url)
    target = (f"{PROXY}/server?mcp_name={MCP_NAME}&server_url={enc}"
              f"&owner={GATEWAY_OWNER}&job_id={job_id}&job_name={MCP_NAME}")
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


def _bind():
    """JAM_GW_PORT if set; otherwise 8080, then the next free port above it —
    another person's gateway may already hold 8080 on this node."""
    ports = [PORT] if PORT else list(range(8080, 8180))
    for p in ports:
        try:
            return ThreadingHTTPServer((BIND, p), H), p
        except OSError:
            if PORT:
                raise
    raise SystemExit("no free port in 8080-8179")


if __name__ == "__main__":
    _ensure_access_dir()
    server, port = _bind()
    print(f"jam-gateway '{MCP_NAME}' (owner {GATEWAY_OWNER}, site {SITE['id']}) "
          f"listening on {port}, host {socket.getfqdn()}", flush=True)
    if os.environ.get("SLURM_JOB_ID"):
        register(port)
    server.serve_forever()
