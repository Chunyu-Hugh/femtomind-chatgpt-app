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
proton parameter table is the workspace's examples/input02.py; pion settings
come from the trusted final-fit NumPy reference, restricted to one shape.
jamai/ copies input02.py and composes the choices, with only the parameters
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
import importlib.util
import inspect
import json
import math
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from jamai import analysis_cache, analysis_catalog, analysis_results, object_registry, ff_registry, ff_plotting, pdf_plotting
from jamai.dataset_catalog import (CONFIGURATION_DATASETS, CONFIGURATION_METADATA,
                                  CONFIGURATION_ONLY_REACTIONS, configuration_dataset_block)
from jamai.object_registry import OBJECTS, SELECTABLE_OBJECTS
from jamai.reaction_registry import get_required_objects, resolve_object_roles

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
    unpack = "base64 -d"
    if len(b64) > 60000:
        # The command line is one SSH argument (at most 128 KiB): a long
        # script travels compressed and the host's python3 unpacks it.
        b64 = base64.b64encode(zlib.compress(code.encode(), 9)).decode()
        unpack = ("base64 -d | python3 -c " +
                  _shq("import sys, zlib; sys.stdout.buffer.write(zlib.decompress(sys.stdin.buffer.read()))"))
    if apptainer:
        run = f"echo {b64} | {unpack} | {_container_exec(_workspace(user), extra_path, env)} -"
    else:
        run = f"echo {b64} | {unpack} | python3"
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
    "parametrization": "x f(x, Q0^2) = N x^a (1 - x)^b (1 + c sqrt(x) + d x)",
    "note": "Physical PDFs such as u, d, s, ub, db are derived combinations of these fitted "
            "basis blocks (the PDF set_moms() construction). g1 N, uv1 N and dv1 N are fixed "
            "by the momentum and valence sum rules.",
}
CAMPAIGN_OBJECTS = [
    *[{"value": key, "label": OBJECTS[key]["label"], "enabled": True}
      for key in SELECTABLE_OBJECTS],
    {"value": "polarized_pdf", "label": "Polarized PDF", "enabled": False},
]
CAMPAIGN_REACTIONS = [
    {"key": "idis", "label": "DIS", "enabled": True},
    {"key": "dy", "label": "Drell-Yan", "enabled": True},
    {"key": "dy-pion", "label": "Pion Drell-Yan", "enabled": True},
    {"key": "ln", "label": "Leading neutron", "enabled": True},
    {"key": "wasym", "label": "W asymmetry", "enabled": True, "cut_policy": "none",
     "cut_note": "No additional cuts; the campaign's global settings are preserved."},
    {"key": "zrap", "label": "Z rapidity", "enabled": True, "cut_policy": "none",
     "cut_note": "No additional cuts; the campaign's global settings are preserved."},
    {"key": "wzrv", "label": "W-lepton asymmetry", "enabled": True, "cut_policy": "none",
     "cut_note": "No additional cuts."},
    {"key": "wc", "label": "W+charm", "enabled": True, "cut_policy": "none",
     "cut_note": "No additional cuts."},
    {"key": "jet", "label": "Jet", "enabled": True, "cut_policy": "jet_pt",
     "cut_note": "Only pT > 8.0 GeV is applied."},
    {"key": "sidis", "label": "SIDIS — PDFs + FFs", "enabled": False,
     "configuration_only": True, "fit_ready": False, "cut_policy": "configuration",
     "cut_note": "Only Q2 > Q2cut, using the DIS Q² cutoff configured in this card. No W2SIDIS or Z cuts.",
     "requirements": "Requires PDFs and FFs; FF fitting is not implemented."},
    {"key": "sia", "label": "SIA — FFs only", "enabled": False,
     "configuration_only": True, "fit_ready": False, "cut_policy": "configuration",
     "cut_note": "Authoritative dataset-specific filters are preserved. No universal SIA cut is applied.",
     "requirements": "Requires FFs only; FF fitting is not implemented."},
]
# Enabled only after bounded FitPack integration checks pass.
FF_EXECUTION_ENABLED = True
ENABLED_REACTIONS = {r["key"] for r in CAMPAIGN_REACTIONS if r["enabled"]}
CATALOG_REACTIONS = ENABLED_REACTIONS | set(CONFIGURATION_ONLY_REACTIONS)
# Display labels only: edit/add string dataset IDs within their reaction.
# Missing entries keep the existing catalog or Excel-derived display name.
DATASET_LABELS = {
    "idis": {
        "10001": "JLab Hall C (E00-106) F_2 deuteron",
        "10002": "JLab Hall C (E00-116) σ_red deuteron",
        "10003": "JLab Hall C (E00-116) σ_red proton",
        "10004": "JLab Hall C (E00-106) σ_red deuteron",
        "10005": "HERMES F_2 proton",
        "10006": "HERMES F_2 deuteron",
        "10007": "HERMES σ_red proton",
        "10008": "HERMES σ_red deuteron",
        "10009": "HERMES σ_red deuteron/proton",
        "10010": "SLAC F_2 proton",
        "10011": "SLAC F_2 deuteron",
        "10012": "SLAC rebinned F_2 proton",
        "10013": "SLAC rebinned F_2 deuteron",
        "10014": "SLAC σ_red proton",
        "10015": "SLAC σ_red deuteron",
        "10016": "BCDMS F_2 proton",
        "10017": "BCDMS F_2 deuteron",
        "10018": "BCDMS σ_red proton",
        "10019": "BCDMS σ_red deuteron",
        "10020": "NMC F_2 proton",
        "10021": "NMC F_2 deuteron/proton",
        "10022": "NMC σ_red proton",
        "10026": "HERA II NC e+ (1) σ_red proton",
        "10027": "HERA II NC e+ (2) σ_red proton",
        "10028": "HERA II NC e+ (3) σ_red proton",
        "10029": "HERA II NC e+ (4) σ_red proton",
        "10030": "HERA II NC e- σ_red proton",
        "10031": "HERA II CC e+ σ_red proton",
        "10032": "HERA II CC e- σ_red proton",
        "10033": "BONUS F_2 neutron/deuteron",
        "10035": "SLAC-E140x F_2 proton",
        "10036": "SLAC-E140x F_2 deuteron",
        "10037": "HERA I and II σ_red^cc proton",
        "10038": "HERA I and II σ_red^bb proton",
        "10040": "NMC σ_red deuteron",
        "10041": "JLab Hall C (E03-103) σ_red helium-3/deuteron",
        "10042": "JLab Hall C (E12-10-002) F_2 deuteron/proton",
        "10045": "JLab E03-103 F_2 proton",
        "10046": "JLab E06-009 F_2 deuteron",
        "10050": "MARATHON F_2 deuteron/proton",
        "10051": "MARATHON F_2 helium-3/tritium",
        "10052": "MARATHON F_2 helium-3/deuteron",
        "10053": "MARATHON F_2 tritium/deuteron",
        "10054": "SLAC A_PV^e proton",
        "10055": "SLAC A_PV^e deuteron",
        "10056": "JLab A_PV^e deuteron",
        "10057": "CLAS6 F_2 proton",
        "10058": "CLAS6 F_2 deuteron",
        "10061": "BONUS F_2 neutron",
        "10062": "E665 F_2 proton",
        "10063": "E665 F_2 deuteron",
        "10077": "JLab E03-103 F_2 deuteron",
        "10086": "NMC σ_red deuteron/proton",
        "10090": "CLAS12 dσ/dWdQ^2 proton"
    },
    "dy": {
        "10001": "E866 NuSea DY pp",
        "20001": "E866 NuSea DY pd/2pp",
        "20002": "E906 SeaQuest DY pd/2pp"
    },
    "wzrv": {
        "2010": "CMS 8 TeV A_eta_mu pp",
        "2011": "CMS 7 TeV A_eta_mu pp",
        "2012": "CMS 7 TeV A_eta_e pp",
        "2013": "CMS 7 TeV A_eta_e pp",
        "2014": "CMS 7 TeV A_eta_mu pp",
        "2016": "LHCb 7 TeV A_eta_mu pp",
        "2017": "LHCb 8 TeV A_eta_mu pp",
        "2020": "STAR RW pp"
    },
    "wasym": {
        "1000": "CDF(W) W_asym ppb",
        "1001": "D0(W) W_asym ppb"
    },
    "zrap": {
        "1000": "CDF(Z) Z_xsec ppb",
        "1001": "D0(Z) Z_xsec_norm ppb"
    },
    "jet": {
        "10001": "D0 d2sigma/dy-dpt",
        "10002": "CDF d2sigma/dy-dpt",
        "10003": "STAR d2sigma/2-pi-dy-dpt",
        "10004": "STAR d2sigma/2-pi-dy-dpt"
    },
    "wc": {
        "10001": "ATLAS 7 TeV Wm sig pp",
        "10002": "ATLAS 7 TeV Wp sig pp",
        "20001": "CMS 7 TeV Wp+Wm pp",
        "20002": "CMS 7 TeV Wp/Wm pp",
        "20003": "CMS 13 TeV Wp+Wm pp"
    },
    "sidis": {
        "1105": "COMPASS deuteron π+ multiplicity",
        "1106": "COMPASS deuteron π− multiplicity",
        "1115": "COMPASS proton π+ multiplicity",
        "1116": "COMPASS proton π− multiplicity",
        "2105": "COMPASS deuteron K+ multiplicity",
        "2106": "COMPASS deuteron K− multiplicity",
        "2115": "COMPASS proton K+ multiplicity",
        "2116": "COMPASS proton K− multiplicity",
        "3100": "COMPASS deuteron h+ multiplicity",
        "3101": "COMPASS deuteron h− multiplicity",
        "3110": "COMPASS proton h+ multiplicity",
        "3111": "COMPASS proton h− multiplicity",
    },
    "sia": {
        "1001": "TASSO pion (√s = 12 GeV)",
        "1002": "TASSO pion (√s = 14 GeV)",
        "1003": "TASSO pion (√s = 22 GeV)",
        "1004": "TASSO pion (√s = 30 GeV)",
        "1005": "TASSO pion (√s = 34 GeV)",
        "1006": "TASSO pion (√s = 44 GeV)",
        "1007": "TPC 1984 pion (√s = 29 GeV)",
        "1008": "TPC 1988 pion (√s = 29 GeV)",
        "1010": "TPC pion (√s = 29 GeV, c-tagged)",
        "1011": "TPC pion (√s = 29 GeV, b-tagged)",
        "1012": "HRS pion (√s = 29 GeV)",
        "1013": "TOPAZ pion (√s = 58 GeV)",
        "1014": "SLD pion (√s = 91.28 GeV)",
        "1015": "SLD pion (√s = 91.28 GeV, uds-tagged)",
        "1016": "SLD pion (√s = 91.28 GeV, c-tagged)",
        "1017": "SLD pion (√s = 91.28 GeV, b-tagged)",
        "1018": "ALEPH pion (√s = 91.2 GeV)",
        "1019": "OPAL pion (√s = 91.2 GeV)",
        "1023": "OPAL pion (√s = 91.2 GeV, c-tagged)",
        "1024": "OPAL pion (√s = 91.2 GeV, b-tagged)",
        "1025": "DELPHI pion (√s = 91.2 GeV)",
        "1026": "DELPHI pion (√s = 91.2 GeV, uds-tagged)",
        "1027": "DELPHI pion (√s = 91.2 GeV, b-tagged)",
        "1028": "BABAR pion (√s = 10.54 GeV)",
        "1029": "BELLE pion (√s = 10.52 GeV)",
        "1030": "ARGUS pion (√s = 9.98 GeV)",
        "2001": "TASSO kaon (√s = 12 GeV)",
        "2002": "TASSO kaon (√s = 14 GeV)",
        "2003": "TASSO kaon (√s = 22 GeV)",
        "2004": "TASSO kaon (√s = 30 GeV)",
        "2005": "TASSO kaon (√s = 34 GeV)",
        "2006": "TASSO kaon (√s = 44 GeV)",
        "2007": "TPC 1984 kaon (√s = 29 GeV)",
        "2008": "TPC 1988 kaon (√s = 29 GeV)",
        "2012": "HRS kaon (√s = 29 GeV)",
        "2013": "TOPAZ kaon (√s = 58 GeV)",
        "2014": "SLD kaon (√s = 91.28 GeV)",
        "2015": "SLD kaon (√s = 91.28 GeV, uds-tagged)",
        "2016": "SLD kaon (√s = 91.28 GeV, c-tagged)",
        "2017": "SLD kaon (√s = 91.28 GeV, b-tagged)",
        "2018": "ALEPH kaon (√s = 91.2 GeV)",
        "2019": "OPAL kaon (√s = 91.2 GeV)",
        "2023": "OPAL kaon (√s = 91.2 GeV, c-tagged)",
        "2024": "OPAL kaon (√s = 91.2 GeV, b-tagged)",
        "2025": "DELPHI kaon (√s = 91.2 GeV)",
        "2026": "DELPHI kaon (√s = 91.2 GeV, uds-tagged)",
        "2027": "DELPHI kaon (√s = 91.2 GeV, b-tagged)",
        "2028": "BABAR kaon (√s = 10.54 GeV)",
        "2029": "BELLE kaon (√s = 10.52 GeV)",
        "2030": "ARGUS kaon (√s = 9.98 GeV)",
        "2031": "DELPHI 1995 kaon (√s = 91.2 GeV)",
        "4000": "ALEPH unidentified charged hadrons (√s = 91.2 GeV)",
        "4001": "DELPHI unidentified charged hadrons (√s = 91.2 GeV)",
        "4002": "SLD unidentified charged hadrons (√s = 91.28 GeV)",
        "4003": "TASSO unidentified charged hadrons (√s = 12 GeV)",
        "4004": "TPC unidentified charged hadrons (√s = 29 GeV)",
        "4005": "OPAL unidentified charged hadrons (√s = 91.2 GeV, b-tagged)",
        "4006": "OPAL unidentified charged hadrons (√s = 91.2 GeV, c-tagged)",
        "4007": "OPAL unidentified charged hadrons (√s = 91.2 GeV)",
        "4008": "TASSO unidentified charged hadrons (√s = 14 GeV)",
        "4009": "TASSO unidentified charged hadrons (√s = 22 GeV)",
        "4010": "TASSO unidentified charged hadrons (√s = 30 GeV)",
        "4011": "TASSO unidentified charged hadrons (√s = 35 GeV)",
        "4012": "TASSO unidentified charged hadrons (√s = 44 GeV)",
        "4013": "DELPHI unidentified charged hadrons (√s = 91.2 GeV, b-tagged)",
        "4014": "SLD unidentified charged hadrons (√s = 91.28 GeV, c-tagged)",
        "4015": "SLD unidentified charged hadrons (√s = 91.28 GeV, b-tagged)",
        "4016": "SLD unidentified charged hadrons (√s = 91.28 GeV, uds-tagged)",
        "4017": "DELPHI unidentified charged hadrons (√s = 91.2 GeV, uds-tagged)",
    },
}

# Experiment headings only: independent of DATASET_LABELS above.
# Equal headings group datasets together; missing entries retain catalog metadata.
DATASET_EXPERIMENTS = {
    "idis": {
        "10001": "JLab Hall C (E00-106)",
        "10002": "JLab Hall C (E00-116)",
        "10003": "JLab Hall C (E00-116)",
        "10004": "JLab Hall C (E00-106)",
        "10005": "HERMES",
        "10006": "HERMES",
        "10007": "HERMES",
        "10008": "HERMES",
        "10009": "HERMES",
        "10010": "SLAC",
        "10011": "SLAC",
        "10012": "SLAC rebinned",
        "10013": "SLAC rebinned",
        "10014": "SLAC",
        "10015": "SLAC",
        "10016": "BCDMS",
        "10017": "BCDMS",
        "10018": "BCDMS",
        "10019": "BCDMS",
        "10020": "NMC",
        "10021": "NMC",
        "10022": "NMC",
        "10026": "HERA",
        "10027": "HERA",
        "10028": "HERA",
        "10029": "HERA",
        "10030": "HERA",
        "10031": "HERA",
        "10032": "HERA",
        "10033": "BONUS",
        "10035": "SLAC-E140x",
        "10036": "SLAC-E140x",
        "10037": "HERA I and II",
        "10038": "HERA I and II",
        "10040": "NMC",
        "10041": "JLab Hall C (E03-103)",
        "10042": "JLab Hall C (E12-10-002)",
        "10045": "JLab E03-103",
        "10046": "JLab E06-009",
        "10050": "MARATHON",
        "10051": "MARATHON",
        "10052": "MARATHON",
        "10053": "MARATHON",
        "10054": "SLAC",
        "10055": "SLAC",
        "10056": "JLab",
        "10057": "CLAS6",
        "10058": "CLAS6",
        "10061": "BONUS",
        "10062": "E665",
        "10063": "E665",
        "10077": "JLab E03-103",
        "10086": "NMC",
        "10090": "CLAS12"
    },
    "dy": {
        "10001": "E866 NuSea",
        "20001": "E866 NuSea",
        "20002": "E906 SeaQuest"
    },
    "wzrv": {
        "2010": "CMS",
        "2011": "CMS",
        "2012": "CMS",
        "2013": "CMS",
        "2014": "CMS",
        "2016": "LHCb",
        "2017": "LHCb",
        "2020": "STAR"
    },
    "wasym": {
        "1000": "CDF",
        "1001": "D0"
    },
    "zrap": {
        "1000": "CDF",
        "1001": "D0"
    },
    "jet": {
        "10001": "D0",
        "10002": "CDF",
        "10003": "STAR",
        "10004": "STAR"
    },
    "wc": {
        "10001": "ATLAS",
        "10002": "ATLAS",
        "20001": "CMS",
        "20002": "CMS",
        "20003": "CMS"
    },
    "sidis": {
        "1105": "COMPASS",
        "1106": "COMPASS",
        "1115": "COMPASS",
        "1116": "COMPASS",
        "2105": "COMPASS",
        "2106": "COMPASS",
        "2115": "COMPASS",
        "2116": "COMPASS",
        "3100": "COMPASS",
        "3101": "COMPASS",
        "3110": "COMPASS",
        "3111": "COMPASS",
    },
    "sia": {
        "1001": "pion",
        "1002": "pion",
        "1003": "pion",
        "1004": "pion",
        "1005": "pion",
        "1006": "pion",
        "1007": "pion",
        "1008": "pion",
        "1010": "pion",
        "1011": "pion",
        "1012": "pion",
        "1013": "pion",
        "1014": "pion",
        "1015": "pion",
        "1016": "pion",
        "1017": "pion",
        "1018": "pion",
        "1019": "pion",
        "1023": "pion",
        "1024": "pion",
        "1025": "pion",
        "1026": "pion",
        "1027": "pion",
        "1028": "pion",
        "1029": "pion",
        "1030": "pion",
        "2001": "kaon",
        "2002": "kaon",
        "2003": "kaon",
        "2004": "kaon",
        "2005": "kaon",
        "2006": "kaon",
        "2007": "kaon",
        "2008": "kaon",
        "2012": "kaon",
        "2013": "kaon",
        "2014": "kaon",
        "2015": "kaon",
        "2016": "kaon",
        "2017": "kaon",
        "2018": "kaon",
        "2019": "kaon",
        "2023": "kaon",
        "2024": "kaon",
        "2025": "kaon",
        "2026": "kaon",
        "2027": "kaon",
        "2028": "kaon",
        "2029": "kaon",
        "2030": "kaon",
        "2031": "kaon",
        "4000": "hadron",
        "4001": "hadron",
        "4002": "hadron",
        "4003": "hadron",
        "4004": "hadron",
        "4005": "hadron",
        "4006": "hadron",
        "4007": "hadron",
        "4008": "hadron",
        "4009": "hadron",
        "4010": "hadron",
        "4011": "hadron",
        "4012": "hadron",
        "4013": "hadron",
        "4014": "hadron",
        "4015": "hadron",
        "4016": "hadron",
        "4017": "hadron",
    },
}

DY_DATASETS = [
    {"id": "10001", "label": "E866 NuSea DY pp", "experiment": "E866 NuSea", "enabled": True},
    {"id": "20001", "label": "E866 NuSea DY pd/pp", "experiment": "E866 NuSea", "enabled": True},
    {"id": "20002", "label": "E906 SeaQuest DY pd/pp", "experiment": "E906 SeaQuest", "enabled": True},
]
PION_DATASETS = {
    "dy-pion": [{"id": "10001", "label": "E615", "experiment": "E615"},
                {"id": "10002", "label": "NA10 194 GeV", "experiment": "NA10"},
                {"id": "10003", "label": "NA10 286 GeV", "experiment": "NA10"}],
    "ln": [{"id": "1000", "label": "H1", "experiment": "H1"},
           {"id": "2000", "label": "ZEUS", "experiment": "ZEUS"}],
}
POSITIVITY_OPTIONS = tuple(f'{obj}Positivity' for obj in ('pdf', 'ffpion', 'ffkaon', 'ffhadron'))
CAMPAIGN_DEFAULTS = {
    "cuts": {"idis": [{"variable": "Q2", "op": ">", "value": 1.69, "enabled": True},
                      {"variable": "W2", "op": ">", "value": 10, "enabled": True}],
             "dy": [{"variable": "Q2", "op": ">", "value": 0, "enabled": True}],
             **{reaction: object_registry.library_cuts(spec.get("filters", []))
                for reaction, spec in object_registry.PROTON_REACTION_DATASETS.items()}},
    "qcd": {"order": "NLO", "Q20": 1.6129, "tmc": "AOT", "higherTwist": False,
            "dglapMode": "truncated", "asMode": "backward",
            **dict.fromkeys(POSITIVITY_OPTIONS, True)},
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

def _object_registry_source():
    # Remote helpers execute as one source unit, not an installed package.
    sources = [inspect.getsource(ff_registry), inspect.getsource(object_registry)]
    return "\n".join("\n".join(line for line in source.splitlines()
                                if not line.startswith("from __future__ import"))
                      for source in sources)


def _object_libraries(u, keys):
    # The shared reader travels as source, so reading options does not deploy modules.
    code = _object_registry_source() + "\nimport json\nprint(json.dumps(load_object_libraries(" \
        + repr(_workspace(u)) + ", " + repr(list(keys)) + ")))\n"
    r = _run_ifarm_py(u, code, apptainer=any(key != "pdf" for key in keys), timeout=60, cap=500000)
    try:
        if r["rc"]:
            raise ValueError("remote reader failed")
        return json.loads(r["out"].strip().splitlines()[-1])
    except (ValueError, IndexError, KeyError) as exc:
        raise RuntimeError("Could not read object libraries: "
                           + (r.get("err") or r.get("out") or str(exc))[-600:]) from exc


def campaign_options(username=""):
    """Composer metadata from the proton library and one-shape pion reference."""
    u, err = _need_user(username)
    if err:
        return err
    try:
        libraries = _object_libraries(u, ["pdf"])
    except RuntimeError as e:
        return {"status": "error", "message": str(e)}
    objects = [dict(item) for item in CAMPAIGN_OBJECTS]
    try:
        libraries.update(_object_libraries(u, ["pdf-pion", "p->pi,n"]))
    except RuntimeError as e:
        for item in objects:
            if item["value"] == "pdf-pion":
                item.update(enabled=False, reason=str(e))
    for key in ff_registry.FF_OBJECTS:
        try:
            libraries.update(_object_libraries(u, [key]))
        except RuntimeError as e:
            for item in objects:
                if item["value"] == key:
                    item.update(enabled=False, reason=str(e))
    defaults = json.loads(json.dumps(CAMPAIGN_DEFAULTS))
    for key in ("pdf-pion", "p->pi,n"):
        for reaction, dataset in libraries.get(key, {}).get("datasets", {}).items():
            defaults["cuts"][reaction] = object_registry.library_cuts(dataset.get("filters", []))
    reactions = [dict(r, objects=[key for key in SELECTABLE_OBJECTS
                                  if r["key"] in OBJECTS[key]["reactions"]])
                 for r in CAMPAIGN_REACTIONS]
    for reaction in reactions:
        if reaction["key"] in CONFIGURATION_ONLY_REACTIONS:
            reaction.update(enabled=FF_EXECUTION_ENABLED,
                            configuration_only=not FF_EXECUTION_ENABLED,
                            fit_ready=FF_EXECUTION_ENABLED,
                            requirements=("SIDIS requires a proton PDF and the FFs for its selected hadrons."
                                          if reaction["key"] == "sidis" else
                                          "SIA requires the FFs for its selected hadrons; no proton PDF."))
    parameters = {key: {k: v for k, v in lib.items() if k not in ("params", "datasets", "fixed_companions")}
                  for key, lib in libraries.items()}
    parameters["pdf"].update(parametrization=PDF_DETAILS["parametrization"], note=PDF_DETAILS["note"])
    return {"status": "ok",
            "steps": ["Non-perturbative object", "Reactions", "QCD settings", "Job config", "Review"],
            "objects": objects, "parameters": parameters,
            "companions": {"ln": {"fitted": ["p->pi,n"], "fixed_unless_selected": ["pdf"]}},
            "compatibility": object_registry.DIS_COMPATIBILITY,
            "reactions": reactions,
            "defaults": defaults, "choices": CAMPAIGN_CHOICES,
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

_PROTON_CATALOG = r'''
import json, os
import pandas as pd
out = []
for idx, relative in __DATASETS__.items():
    path = os.path.join(__WS__, 'database', relative)
    item = {'id': str(idx), 'path': relative, 'enabled': os.path.isfile(path),
            'label': __REACTION__ + ' ' + str(idx), 'experiment': 'Other'}
    if item['enabled']:
        try:
            table = pd.read_excel(path, nrows=1)
            if len(table.index):
                for key, column in (('experiment', 'col'), ('observable', 'obs'), ('target', 'target')):
                    if column in table.columns and pd.notna(table.iloc[0][column]):
                        item[key] = str(table.iloc[0][column])
                item['label'] = ' '.join(item[k] for k in ('experiment', 'observable', 'target')
                                         if item.get(k) and item[k] != 'Other') or item['label']
        except (OSError, ValueError, KeyError, ImportError) as exc:
            item['metadata_note'] = str(exc)[:200]
    out.append(item)
print(json.dumps(out))
'''


def campaign_datasets(reaction="idis", search="", username=""):
    """Reaction-qualified datasets from the approved catalog or workspace library."""
    u, err = _need_user(username)
    if err:
        return err
    reaction = (reaction or "").strip()
    if reaction not in CATALOG_REACTIONS:
        return {"status": "error", "message": f"{reaction!r} is not available yet; the composer "
                                              f"offers {sorted(CATALOG_REACTIONS)}."}
    if reaction in CONFIGURATION_ONLY_REACTIONS:
        spec = CONFIGURATION_DATASETS[reaction]
        code = ("import json, os\nitems = " + repr([
            {"id": str(idx), "path": path, **CONFIGURATION_METADATA[reaction][idx]}
            for idx, path in sorted(spec["xlsx"].items())])
            + "\nfor item in items: item['enabled'] = os.path.isfile(os.path.join("
            + repr(_workspace(u)) + ", 'database', item['path']))\nprint(json.dumps(items))")
        r = _run_ifarm_py(u, code, timeout=60, cap=200000)
        try:
            if r["rc"]:
                raise ValueError("catalog file check failed")
            items = json.loads(r["out"].strip().splitlines()[-1])
        except (ValueError, IndexError, KeyError):
            return {"status": "error", "message": f"Could not inspect {reaction} dataset files.",
                    "detail": (r.get("err") or "")[-300:]}
    elif reaction in object_registry.PROTON_REACTION_DATASETS:
        spec = object_registry.PROTON_REACTION_DATASETS[reaction]
        r = _run_ifarm_py(u, _fill(_PROTON_CATALOG, WS=_workspace(u),
                                  REACTION=reaction, DATASETS=spec["xlsx"]),
                        apptainer=True, timeout=60, cap=200000)
        try:
            if r["rc"]:
                raise ValueError("catalog reader failed")
            items = json.loads(r["out"].strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"status": "error", "message": f"Could not read {reaction} datasets.",
                    "detail": (r.get("err") or "")[-300:]}
    elif reaction == "dy":
        items = [dict(d) for d in DY_DATASETS]
    elif reaction in PION_DATASETS:
        code = ("import json, os\nitems = " + repr(PION_DATASETS[reaction])
                + "\nfor item in items: item['enabled'] = os.path.isfile(os.path.join("
                + repr(_workspace(u)) + ", 'database', " + repr(reaction)
                + ", 'expdata', item['id'] + '.xlsx'))\nprint(json.dumps(items))")
        r = _run_ifarm_py(u, code, timeout=60)
        try:
            if r["rc"]:
                raise ValueError("catalog reader failed")
            items = json.loads(r["out"].strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"status": "error", "message": f"Could not read {reaction} datasets.",
                    "detail": r.get("err", "")}
    else:
        r = _run_ifarm_py(u, _fill(_IDIS_CATALOG, WS=_workspace(u)), timeout=60, cap=200000)
        try:
            items = json.loads(r["out"].strip().splitlines()[-1])
        except Exception:
            return {"status": "error", "message": "could not read database/idis/README.md",
                    "detail": (r.get("err") or "")[-300:]}
    # Normalization defaults come from the same workspace input card as the
    # PDF library. They are metadata only until the user edits them.
    try:
        if reaction in CONFIGURATION_ONLY_REACTIONS:
            lib = {"source": "Authoritative SIDIS/SIA configuration excerpt"}
            dataset = CONFIGURATION_DATASETS[reaction]
        else:
            key = "pdf-pion" if reaction in PION_DATASETS else "pdf"
            lib = _object_libraries(u, [key])[key]
            dataset = lib["datasets"].get(reaction, {})
        norms = {str(k): v for k, v in dataset.get("norm", {}).items()}
        default_cuts = (object_registry.library_cuts(dataset.get("filters", []))
                        if reaction in PION_DATASETS else CAMPAIGN_DEFAULTS["cuts"].get(reaction, []))
    except (RuntimeError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}
    for item in items:
        item["label"] = DATASET_LABELS.get(reaction, {}).get(str(item["id"]), item["label"])
        item["experiment"] = DATASET_EXPERIMENTS.get(reaction, {}).get(str(item["id"]), item.get("experiment") or "Other")
        if item.get("not_normally_used"):
            item["label"] += "*"
        if reaction in CONFIGURATION_ONLY_REACTIONS:
            item["configuration_only"] = not FF_EXECUTION_ENABLED
            item["fit_ready"] = FF_EXECUTION_ENABLED
            if reaction == "sia":
                item["filters"] = dataset["filters"].get(int(item["id"]))
        if str(item.get("id")) in norms:
            item["normalization"] = norms[str(item["id"])]
    q = (search or "").strip().lower()
    if q:
        items = [d for d in items if q in " ".join(str(d.get(k, "")) for k in
                                                   ("id", "label", "experiment", "observable", "target")).lower()]
    groups = {}
    for d in items:
        groups.setdefault(d.get("experiment") or "Other", []).append(d)
    return {"status": "ok", "reaction": reaction, "count": len(items),
            "experiments": [{"experiment": k, "datasets": v} for k, v in groups.items()],
            "default_cuts": default_cuts, "source": lib["source"],
            **({"configuration_only": not FF_EXECUTION_ENABLED, "fit_ready": FF_EXECUTION_ENABLED,
                "legend": "* indicates dataset is not normally used in JAM fits." if reaction == "sia" else "",
                "fit_unavailable_reason": "" if FF_EXECUTION_ENABLED else "FF fitting is not implemented; dataset configuration only."}
               if reaction in CONFIGURATION_ONLY_REACTIONS else {})}


# ---- the input card ---------------------------------------------------------

def _same_number(a, b):
    """Both None, or numbers equal up to a tiny relative tolerance."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 1e-9 * max(abs(a), abs(b))


def _normalize_ff_parameters(key, obj, library):
    overrides = {}
    errors = []
    for name, entry in (obj.get("parameterBounds") or {}).items():
        overrides[name.replace(":", " ")] = dict(entry or {})
    for name, entry in (obj.get("parameters") or {}).items():
        overrides.setdefault(name.replace(":", " "), {}).update(entry or {})
    for name, entry in overrides.items():
        target = entry.pop("equal_to", entry.pop("equalTo", None))
        if target:
            entry["fixed"] = target
        if "enabled" in entry and not isinstance(entry["enabled"], bool):
            errors.append(f"Parameter {name}: enabled must be true or false.")
    if errors:
        return [], [], errors
    try:
        active = ff_registry.resolve_ff_parameters(key, overrides)
    except (ValueError, TypeError, KeyError) as exc:
        return [], [], [str(exc)]
    rows = [dict(entry, name=name) for name, entry in active.items()]
    known = {row["name"]: row for row in library}
    changed = [row for row in rows if row["name"] in overrides
               or not known[row["name"]].get("enabled", True)]
    return rows, changed, []


def _normalize_parameters(obj, library):
    errors = []
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
        equal_to = ov.get("equal_to", ov.get("equalTo"))
        fixed = equal_to or ov.get("fixed", lib.get("tied_to", lib["fixed"]))
        if isinstance(fixed, str):
            # A string names the parameter whose numerical value this parameter
            # must share. This is the same representation used by FitPack's
            # input card for library ties.
            fixed = {"true": True, "fixed": True, "false": False, "free": False}.get(fixed.strip().lower(), fixed.strip())
        row = {"name": name, "fixed": fixed,
               "value": ov.get("value", lib["value"]), "min": ov.get("min", lib["min"]),
               "max": ov.get("max", lib["max"])}
        params.append(row)
        try:
            row["value"] = float(row["value"])
            row["min"] = None if row["min"] is None else float(row["min"])
            row["max"] = None if row["max"] is None else float(row["max"])
        except (TypeError, ValueError):
            changed.append(row)
            errors.append(f"Parameter {name}: value, min and max must be numbers.")
            continue
        if not all(math.isfinite(row[k]) for k in ("value", "min", "max") if row[k] is not None):
            changed.append(row)
            errors.append(f"Parameter {name}: value, min and max must be finite numbers.")
            continue
        if isinstance(fixed, str):
            if any(not _same_number(row[k], lib[k]) for k in ("value", "min", "max")):
                errors.append(f"Parameter {name} is tied to {fixed}: reset its numerical fields "
                              "to the library, or switch it to FREE before editing them.")
            if fixed not in known:
                changed.append(row)
                errors.append(f"Parameter {name}: equal-to parameter {fixed!r} does not exist.")
            elif fixed == name:
                changed.append(row)
                errors.append(f"Parameter {name}: a parameter cannot be equal to itself.")
            else:
                row["equal_to"] = fixed
                if fixed != lib.get("tied_to"):
                    changed.append(row)
            continue
        if not isinstance(fixed, bool):
            changed.append(row)
            errors.append(f"Parameter {name}: fixed must be true (FIXED) or false (FREE).")
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
    # Equality links must form a terminating chain. A cycle would leave
    # FitPack with no source value to copy.
    equal_links = {p["name"]: p["fixed"] for p in params if isinstance(p["fixed"], str)}
    for start in equal_links:
        seen = set()
        cur = start
        while cur in equal_links:
            if cur in seen:
                errors.append(f"Parameter {start}: equal-to settings contain a cycle.")
                break
            seen.add(cur)
            cur = equal_links[cur]
    return params, changed, errors


def _card_objects(card):
    objects = card.get("objects")
    if objects is None:
        objects = [card.get("object") or {}]
    if not isinstance(objects, list):
        raise ValueError("objects must be a list of object names or cards.")
    return [{"type": obj} if isinstance(obj, str) else obj for obj in objects]


def _normalize_cuts(reaction, raw, dataset_ids):
    if reaction in object_registry.PROTON_REACTION_DATASETS:
        expected = CAMPAIGN_DEFAULTS["cuts"][reaction]
        if not isinstance(raw, list):
            return [], [f'Reaction {reaction}: dataset-specific cuts are not supported.']
        try:
            actual_filters = object_registry.cut_filters(raw)
            expected_filters = object_registry.cut_filters(expected)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            return [], [f'Reaction {reaction}: invalid cuts.']
        if actual_filters != expected_filters:
            required = "pT > 8.0 only" if reaction == "jet" else "no additional cuts"
            return [], [f'Reaction {reaction} requires {required}.']
        return [dict(cut) for cut in expected], []
    if isinstance(raw, dict):
        if not dataset_ids:
            return {}, [f'Reaction {reaction}: dataset cuts must be a list.']
        clean, errors = {}, []
        for dataset_id in dataset_ids:
            if dataset_id not in raw:
                errors.append(f'Reaction {reaction} dataset {dataset_id}: supply dataset-specific cuts.')
                continue
            clean[dataset_id], problems = _normalize_cuts(
                f'{reaction} dataset {dataset_id}', raw[dataset_id], [])
            errors.extend(problems)
        return clean, errors
    if not isinstance(raw, list):
        return [], [f'Reaction {reaction}: cuts must be a list or dataset-id mapping.']
    clean, errors = [], []
    for cut in raw:
        try:
            cut = {"variable": str(cut["variable"]), "op": str(cut.get("op", ">")),
                   "value": float(cut["value"]), "enabled": bool(cut.get("enabled", True))}
            object_registry.cut_filters([dict(cut, enabled=True)])
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            errors.append(f'Reaction {reaction}: invalid cut {cut!r}.')
            continue
        clean.append(cut)
    if not any(c["enabled"] for c in clean):
        errors.append(f'Reaction {reaction} must include at least one enabled cut.')
    return clean, errors


def _normalize_card(card, library):
    """Accept legacy object or additive objects[], preserving parameter sectors."""
    card = json.loads(json.dumps(card or {}))
    errors, selected, params, changed = [], [], [], []
    if isinstance(library, list):
        library = {"pdf": {"rows": library}}
    try:
        objects = _card_objects(card)
    except ValueError as exc:
        objects = []
        errors.append(str(exc))
    for obj in objects:
        key = obj.get("type") if isinstance(obj, dict) else None
        if key not in SELECTABLE_OBJECTS:
            errors.append(f"Select an available non-perturbative object: {key!r}.")
        elif key in selected:
            errors.append(f"Object {key!r} was selected more than once.")
        else:
            selected.append(key)
    if not selected:
        errors.append("Select at least one non-perturbative object.")
    if ("pdf-pion" in selected and "pionReference" in card
            and card["pionReference"] != library.get("pdf-pion", {}).get("sha256")):
        errors.append("Pion reference changed: review and reset the outdated pion draft before validation.")
    # reactions: list or {"idis": true}
    rx = card.get("reactions") or []
    if isinstance(rx, dict):
        rx = [k for k, on in rx.items() if on]
    rx = [str(r).strip().lower() for r in rx if str(r).strip()]
    if not rx:
        errors.append("Select at least one reaction.")
    for r in rx:
        if r not in ENABLED_REACTIONS and not (FF_EXECUTION_ENABLED and r in CONFIGURATION_ONLY_REACTIONS):
            errors.append(f"Reaction {r!r} is not available yet.")
        elif not any(r in OBJECTS[key]["reactions"] for key in selected):
            errors.append(f"Reaction {r!r} does not constrain any selected object.")
    for key in selected:
        if not any(r in OBJECTS[key]["reactions"] for r in rx):
            errors.append(f"Select a reaction for {OBJECTS[key]['label']}.")
    try:
        fitted, frozen = resolve_object_roles(selected, rx, datasets=card.get("datasets") or {})
    except ValueError as exc:
        fitted, frozen = selected[:], []
        errors.append(str(exc))
    companions = card.get("companions") or {}
    for key in companions:
        if key not in fitted or key in selected:
            errors.append(f"Companion {key!r} is not active.")
    for key in fitted:
        lib = library.get(key)
        if not lib:
            errors.append(f"Missing parameter library for {key}.")
            continue
        obj = next((obj for obj in objects if isinstance(obj, dict) and obj.get("type") == key),
                   companions.get(key) or {})
        rows, edits, problems = (_normalize_ff_parameters(key, obj, lib["rows"])
                                  if key in ff_registry.FF_OBJECTS else
                                  _normalize_parameters(obj, lib["rows"]))
        params.extend(dict(row, sector=key) for row in rows)
        changed.extend(dict(row, sector=key) for row in edits)
        errors.extend(f"{key}: {problem}" for problem in problems)
    # datasets: {"idis": ["10010"]} or the app's {"idis": {"selected": [...], "cuts": [...]}}
    ds_in, cuts_in = card.get("datasets") or {}, dict(card.get("cuts") or {})
    normalization_in = card.get("normalizations") or {}
    datasets, cuts, normalizations = {}, {}, {}
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
        if r in CONFIGURATION_ONLY_REACTIONS:
            try:
                qrows = cuts_in.get("idis", CAMPAIGN_DEFAULTS["cuts"]["idis"])
                qcuts = [row for row in qrows if row.get("enabled", True)
                         and row.get("variable") == "Q2" and row.get("op", ">") == ">"]
                if r == "sidis" and len(qcuts) != 1:
                    raise ValueError("SIDIS requires exactly one configured DIS Q2 > cutoff")
                cutoff = float(qcuts[0]["value"]) if r == "sidis" else None
                block = configuration_dataset_block(r, ids, q2cut=cutoff)
                supplied_cuts = cuts_in.get(r)
                expected_cuts = block.get("filters", [])
                if supplied_cuts:
                    supplied = (object_registry.cut_filters(supplied_cuts)
                                if r == "sidis" else supplied_cuts)
                    expected = (object_registry.cut_filters(object_registry.library_cuts(expected_cuts))
                                if r == "sidis" else {str(idx): value for idx, value in expected_cuts.items()})
                    if supplied != expected:
                        raise ValueError(f"{r}: preserve the authoritative dataset filters")
                supplied_norms = normalization_in.get(r) or {}
                for idx, entry in supplied_norms.items():
                    if json.dumps(entry, sort_keys=True) != json.dumps(block["norm"].get(int(idx)), sort_keys=True):
                        raise ValueError(f"{r}:{idx}: preserve the authoritative normalization definition")
                normalizations[r] = {str(idx): dict(entry) for idx, entry in block["norm"].items()}
                # Keep complex SIA expressions verbatim rather than parsing/reformatting.
                cuts[r] = (object_registry.library_cuts(expected_cuts) if r == "sidis" else
                           {str(idx): list(value) for idx, value in expected_cuts.items()})
            except (ValueError, TypeError, KeyError) as exc:
                errors.append(str(exc))
                cuts[r], normalizations[r] = {}, {}
            continue
        canonical = object_registry.PROTON_REACTION_DATASETS.get(r)
        if canonical is not None:
            for dataset_id in ids:
                if int(dataset_id) not in canonical["xlsx"]:
                    errors.append(f'Reaction "{r}" dataset {dataset_id} is not in the approved catalog.')
        raw_norm = normalization_in.get(r) or {}
        if not isinstance(raw_norm, dict):
            errors.append(f'Reaction "{r}" normalization settings must be an object.')
            raw_norm = {}
        normalizations[r] = {}
        for dataset_id, value in raw_norm.items():
            dataset_id = str(dataset_id)
            if dataset_id not in ids:
                errors.append(f'Reaction "{r}" normalization {dataset_id!r} is not selected.')
                continue
            if canonical is not None and int(dataset_id) not in canonical["norm"]:
                errors.append(f'Reaction "{r}" dataset {dataset_id} has no normalization parameter.')
                continue
            if not isinstance(value, dict):
                errors.append(f'Reaction "{r}" dataset {dataset_id}: normalization must be an object.')
                continue
            try:
                nv = float(value.get("value"))
                nmin = None if value.get("min") is None else float(value.get("min"))
                nmax = None if value.get("max") is None else float(value.get("max"))
            except (TypeError, ValueError):
                errors.append(f'Reaction "{r}" dataset {dataset_id}: normalization values must be numbers.')
                continue
            nfixed = value.get("fixed", False)
            if isinstance(nfixed, str) and nfixed.strip().lower() in ("true", "fixed"):
                nfixed = True
            elif isinstance(nfixed, str) and nfixed.strip().lower() in ("false", "free"):
                nfixed = False
            elif isinstance(nfixed, str):
                nfixed = nfixed.strip()
                if nfixed not in ids or nfixed == dataset_id:
                    errors.append(f'Reaction "{r}" dataset {dataset_id}: invalid normalization equal-to dataset.')
                    continue
                if canonical is not None and int(nfixed) not in canonical["norm"]:
                    errors.append(f'Reaction "{r}" dataset {dataset_id}: normalization target has no parameter.')
                    continue
            elif not isinstance(nfixed, bool):
                errors.append(f'Reaction "{r}" dataset {dataset_id}: fixed must be true, false, or a dataset id.')
                continue
            if not all(math.isfinite(x) for x in (nv, nmin, nmax) if x is not None):
                errors.append(f'Reaction "{r}" dataset {dataset_id}: normalization values must be finite.')
                continue
            if isinstance(nfixed, bool) and not nfixed:
                if nmin is None or nmax is None or not nmin < nmax or not nmin <= nv <= nmax:
                    errors.append(f'Reaction "{r}" dataset {dataset_id}: FREE normalization needs a valid range.')
                    continue
            normalizations[r][dataset_id] = {"value": nv, "min": nmin, "max": nmax, "fixed": nfixed}
        rc = cuts_in.get(r)
        if rc is None:
            if r in PION_DATASETS and "pdf-pion" in library:
                rc = object_registry.library_cuts(
                    library["pdf-pion"]["datasets"][r].get("filters", []))
            else:
                rc = CAMPAIGN_DEFAULTS["cuts"].get(r, [])
        cuts[r], problems = _normalize_cuts(r, rc, ids)
        errors.extend(problems)
    for reaction, entries in normalizations.items():
        links = {dataset_id: p["fixed"] for dataset_id, p in entries.items() if isinstance(p["fixed"], str)}
        for start in links:
            seen = set()
            cur = start
            while cur in links:
                if cur in seen:
                    errors.append(f'Reaction "{reaction}" normalization settings contain a cycle.')
                    break
                seen.add(cur)
                cur = links[cur]
    # QCD
    profile_defaults = library.get("pdf-pion", {}).get("defaults", {}) if "pdf-pion" in selected else {}
    q = {**CAMPAIGN_DEFAULTS["qcd"], **profile_defaults.get("qcd", {}), **(card.get("qcd") or {})}
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
    for key in POSITIVITY_OPTIONS:
        if type(q[key]) is not bool:
            errors.append(f"QCD {key} must be true or false.")
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
    j = {**CAMPAIGN_DEFAULTS["job"], **profile_defaults.get("job", {}), **(card.get("job") or {})}
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
    normalized_objects = [{"type": key, "label": OBJECTS[key]["label"]} for key in selected]
    norm = {"object": normalized_objects[0] if normalized_objects else {},
            "objects": normalized_objects, "fit_objects": fitted, "frozen_objects": frozen,
            "dis_version": object_registry.dis_version_for(fitted),
            "library_sources": {key: {"source": library[key].get("source"),
                                      "sha256": library[key].get("sha256")}
                                for key in dict.fromkeys(fitted + frozen) if key in library},
            "parameters_table": params,
            "parameters_changed": changed, "reactions": rx, "datasets": datasets, "normalizations": normalizations, "cuts": cuts,
            "qcd": q, "parameters": pi, "job": j, "slurm": sl}
    if "pdf" in frozen and "pdf-pion" in library:
        norm["library_sources"]["pdf"] = {key: library["pdf-pion"][key] for key in ("source", "sha256")}
    return norm, errors


def _card_summary(norm):
    label = lambda p: (p.get("sector", "pdf") + ": " if len(norm.get("fit_objects", ["pdf"])) > 1 else "") + p["name"]
    free = [label(p) for p in norm["parameters_table"] if not p["fixed"]]
    fixed = [label(p) for p in norm["parameters_table"] if p["fixed"]]
    # Registry entries from before the library table have no parameters_changed.
    changed = [label(p) for p in norm.get("parameters_changed") or []]
    objects = norm.get("objects") or [norm.get("object") or {"type": "pdf", "label": "Unpolarized PDF"}]
    cuts = {}
    for reaction, rows in norm["cuts"].items():
        rendered = rows if reaction == "sia" else object_registry.cut_filters(rows)
        cuts[reaction] = ([f'{dataset_id}: {expression}' for dataset_id, expressions in rendered.items()
                           for expression in expressions] if isinstance(rendered, dict) else rendered)
    return {"object": " + ".join(o["label"] for o in objects), "objects": objects,
            "fit_objects": norm.get("fit_objects", ["pdf"]),
            "frozen_objects": norm.get("frozen_objects", []),
            "library_sources": norm.get("library_sources", {}),
            "dis_version": object_registry.dis_version_for(norm.get("fit_objects", ["pdf"])),
            "dis_compatibility": object_registry.DIS_COMPATIBILITY,
            "free_parameters": len(free), "fixed_parameters": len(fixed),
            "fixed": fixed, "changed_parameters": changed, "reactions": norm["reactions"],
            "datasets": {r: len(v) for r, v in norm["datasets"].items()},
            "normalization_parameters": {
                r: len(entries) for r, entries in norm.get("normalizations", {}).items() if entries},
            "normalizations": {r: entries for r, entries in norm.get("normalizations", {}).items() if entries},
            "cuts": cuts,
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
           "higherTwist": q["higherTwist"], "dglapMode": q["dglapMode"], "asMode": q["asMode"],
           **{key: q.get(key, True) for key in POSITIVITY_OPTIONS}}
    pi = norm["parameters"]
    init = "random_within_bounds" if pi["initialization"] == "random_within_bounds" else "default_prior"
    seed = pi["randomSeed"] if pi["randomSeed"] is not None else 120000
    # Only the rows the user changed; the card keeps the library's own for the rest.
    sectors = [{"key": key, "parameters": [
        {field: value for field, value in p.items() if field != "sector"}
        for p in (norm["parameters_table"] if key in ff_registry.FF_OBJECTS else norm["parameters_changed"])
        if p.get("sector", "pdf") == key]}
        for key in norm.get("fit_objects", ["pdf"])]
    norm_params = []
    for reaction, entries in norm.get("normalizations", {}).items():
        for dataset_id, p in entries.items():
            # A tie to another dataset's normalization: FitPack's parman looks the
            # reference up by its integer id, so a digit string becomes an int.
            fixed = int(p["fixed"]) if isinstance(p["fixed"], str) and p["fixed"].isdigit() else p["fixed"]
            norm_params.append({"id": f"norm_{reaction}_{dataset_id}", "value": p["value"],
                                "min": p["min"], "max": p["max"], "fixed": fixed})
    if norm_params:
        sectors.append({"key": "norm", "parameters": norm_params})
    return json.dumps({
        "campaign": {"name": folder, "fitpack_root": ws, "template_path": CAMPAIGN_TEMPLATE,
                     "campaign_root": f"{ws}/{CAMPAIGNS_DIR}/{folder}", "default_qcd": qcd,
                     "seed_policy": {"mode": "deterministic", "base_seed": 120000}},
        "stages": [{"name": STAGE, "reactions": norm["reactions"],
                    "fit_objects": norm.get("fit_objects", ["pdf"]),
                    "frozen_objects": norm.get("frozen_objects", []), "input_template": CAMPAIGN_TEMPLATE,
                    "nreplicas": nreplicas, "previous_stage": None, "output_dir": f"stages/{STAGE}",
                    "datasets": {r: [int(x) for x in ids] for r, ids in norm["datasets"].items()},
                    "cuts": norm["cuts"],
                    "params": {"initializationMode": init, "randomSeed": pi["randomSeed"],
                               "perReplicaSeed": pi["perReplicaSeed"], "baseSeed": seed,
                               "ff_parameters": {key: {p["name"]: {field: value for field, value in p.items()
                                                                        if field not in ("name", "sector")}
                                                               for p in norm["parameters_table"] if p.get("sector") == key}
                                                 for key in norm["fit_objects"] if key in ff_registry.FF_OBJECTS},
                               "sectors": sectors, "selected_objects": norm.get("objects", [norm["object"]]),
                               "compatibility": {"dis_version": object_registry.dis_version_for(
                                   norm.get("fit_objects", ["pdf"])), "temporary": True},
                               "library_sources": norm.get("library_sources", {})},
                    "qcd": qcd,
                    "job": {"replicas": nreplicas, "maxIterations": norm["job"]["maxIterations"],
                            "tolerance": norm["job"]["tolerance"], "optimizer": norm["job"]["optimizer"],
                            "bootstrap": norm["job"]["bootstrap"],
                            "campaignName": norm["job"]["campaignName"]}}]}, indent=2)


def _array_ranges(numbers):
    """[3, 4, 5, 9] -> '3-5,9' (a Slurm --array list)."""
    out, nums = [], sorted(set(numbers))
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ",".join(out)


def _array_script(ws, folder, norm, first, last, indices=None):
    """The Slurm job array: one replica per task, inside the site's image
    (replicas first-last, or exactly the replica numbers in indices)."""
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
#SBATCH --array={_array_ranges(indices) if indices else f"{first}-{last}"}
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
    """The campaign a collection id names, or, as people say it, its campaign
    name (case and separators aside): the newest submission of that name."""
    entries = _registry(u, {"kind": "read"})
    for e in entries:
        if e.get("collection_id") == cid:
            return e
    want = analysis_cache.norm_name(cid or "")
    if not want:
        return None
    named = [e for e in entries if analysis_cache.norm_name(e.get("campaign_name") or "") == want]
    if not named:
        named = [e for e in entries if analysis_cache.norm_name(e.get("collection_id") or "").startswith(want)]
    return max(named, key=lambda e: e.get("created_at", "")) if named else None


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
    if _configuration_reactions(card):
        return None, _unsupported_configuration_fit(card)
    try:
        keys = [obj.get("type") for obj in _card_objects(card or {}) if isinstance(obj, dict)
                and obj.get("type") in SELECTABLE_OBJECTS]
        if "pdf-pion" in keys:
            keys = list(dict.fromkeys(keys + ["p->pi,n", "pdf"]))
        if FF_EXECUTION_ENABLED and set(_card_reactions(card)).intersection(CONFIGURATION_ONLY_REACTIONS):
            keys = list(dict.fromkeys(keys + get_required_objects(_card_reactions(card), datasets=(card or {}).get("datasets") or {})))
        library = _object_libraries(u, keys)
    except (RuntimeError, ValueError) as e:
        return None, {"status": "error", "valid": False, "message": str(e)}
    norm, errors = _normalize_card(card, library)
    if not errors:
        ws = _workspace(u)
        for key, lib in library.items():
            if key in norm["fit_objects"] + norm["frozen_objects"]:
                for reaction in OBJECTS[key]["reactions"]:
                    if (reaction in norm["reactions"] and reaction not in CONFIGURATION_ONLY_REACTIONS
                            and reaction not in lib["datasets"]):
                        errors.append(f"The {key} library has no {reaction} dataset configuration.")
        checks = " ".join(_shq(object_registry.PROTON_REACTION_DATASETS[r]["xlsx"][int(x)]
                               if r in object_registry.PROTON_REACTION_DATASETS else
                               CONFIGURATION_DATASETS[r]["xlsx"][int(x)] if r in CONFIGURATION_DATASETS
                               else f"{r}/expdata/{x}.xlsx")
                          for r, ids in norm["datasets"].items() for x in ids)
        r = _remote(u, f"cd {_shq(ws)}/database 2>/dev/null && for f in {checks}; do "
                       f"[ -f \"$f\" ] || echo MISSING $f; done; "
                       f"[ -f {_shq(ws)}/{CAMPAIGN_TEMPLATE} ] || echo NOTEMPLATE; "
                       f"[ -f {_shq(ws)}/fitlib/ai.py ] || echo NOAI", timeout=60)
        out = r.get("out") or ""
        if r.get("rc"):
            errors.append("Could not check workspace inputs: " + (r.get("err") or out)[-500:])
        for m in re.findall(r"MISSING (\S+)", out):
            errors.append(f"No data file database/{m} in this workspace.")
        if "NOTEMPLATE" in out:
            errors.append(f"The workspace has no {CAMPAIGN_TEMPLATE}.")
        if "NOAI" in out:
            errors.append("The workspace is not on the FitPack ai branch (no fitlib/ai.py): switch it "
                          "(the Project tab's 'Switch to the ai branch', or use_ai_branch), or create a "
                          "new workspace.")
    if not errors and set(norm["reactions"]).intersection(CONFIGURATION_ONLY_REACTIONS):
        warnings, points = _data_warnings(u, norm)
        norm["_data_warnings"], norm["_data_points"] = warnings, points
        for reaction in set(norm["reactions"]).intersection(CONFIGURATION_ONLY_REACTIONS):
            selected = [f"{reaction}:{idx}" for idx in norm["datasets"][reaction]]
            if any(key not in points for key in selected):
                errors.append(f"{reaction}: the existing FitPack reader could not verify every selected dataset and required cut. "
                              + " ".join(warnings))
            elif sum(points[key] for key in selected) <= 0:
                errors.append(f"{reaction}: no data points remain after the authoritative cuts; choose at least one nonempty dataset.")
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
ws = __WS__; datasets = __DATASETS__; cuts = __CUTS__; report = __REPORT__; dis_version = __DIS_VERSION__
paths = __PATHS__
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
            relative = paths.get(r, {}).get(str(x), '%s/expdata/%s.xlsx' % (r, x))
            t = pd.read_excel(os.path.join(ws, 'database', relative))
            if r in ('dy-pion', 'ln', 'sidis', 'sia') or (r == 'idis' and dis_version == 0):
                from tools.config import conf
                from qcdlib.aux import AUX
                from importlib import import_module
                conf['aux'] = AUX()
                conf['datasets'] = {r: {'filters': []}}
                module = {'dy-pion': 'obslib.dypion.reader', 'ln': 'obslib.LeadingBaryon.reader',
                          'idis': 'obslib.idis_V0.reader', 'sidis': 'obslib.sidis.reader',
                          'sia': 'obslib.sia.reader'}[r]
                reader = import_module(module).READER()
                reader.reaction, reader.cut_by_dataset = r, int(x)
                t = reader.modify_table(t)
        except Exception as e:
            d['error'] = ' '.join(('%s: %s' % (type(e).__name__, e)).split())[:300]
            continue
        {'idis': idis, 'dy': dy}.get(r, lambda t: None)(t)
        applied, skipped = [], []
        reaction_cuts = cuts.get(r, [])
        for f in (reaction_cuts.get(str(x), []) if isinstance(reaction_cuts, dict) else reaction_cuts):
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
_REPORT_COLUMNS = {"idis": ("col", "obs", "target"), "dy": ("col", "reaction"),
                   **{r: ("col", "obs", "target") for r in ("wzrv", "wasym", "zrap", "wc")},
                   "jet": ("col", "obs")}
_COLUMN_NAMES = {"col": "experiment name", "obs": "observable", "target": "target",
                 "reaction": "reaction"}


def _data_warnings(u, norm):
    """Warnings about the data a card selects, which never block submission,
    and the points each dataset keeps after the cuts ({"idis:10005": 33});
    one container call for all datasets."""
    if "_data_warnings" in norm and "_data_points" in norm:
        return norm["_data_warnings"], norm["_data_points"]
    cuts = {r: (cs if r == "sia" else object_registry.cut_filters(cs)) for r, cs in norm["cuts"].items()}
    code = _fill(_DATA_CHECK, WS=_workspace(u), DATASETS=norm["datasets"], CUTS=cuts,
                 REPORT=_REPORT_COLUMNS, DIS_VERSION=object_registry.dis_version_for(norm["fit_objects"]),
                 PATHS={r: {str(idx): path for idx, path in block["xlsx"].items()}
                        for r, block in CONFIGURATION_DATASETS.items()})
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
        if d['reaction'] not in CONFIGURATION_ONLY_REACTIONS or not d["skipped"]:
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


def _card_reactions(card):
    """Canonical reaction keys shared by the preview and execution boundary."""
    raw = (card or {}).get("reactions") or []
    if isinstance(raw, dict):
        raw = [key for key, on in raw.items() if on]
    if not isinstance(raw, (list, tuple)):
        return []
    return list(dict.fromkeys(str(key).strip().lower() for key in raw if str(key).strip()))


def _configuration_reactions(card):
    return (set() if FF_EXECUTION_ENABLED else
            set(_card_reactions(card)).intersection(CONFIGURATION_ONLY_REACTIONS))


def _unsupported_configuration_fit(card):
    return {"status": "unsupported", "valid": False, "fit_ready": False,
            "message": "Configuration only: FF fitting is not implemented for "
                       + ", ".join(sorted(_configuration_reactions(card))) + ". No campaign was submitted."}


def _configuration_preview(u, card):
    """Dataset-only input fragment; no upload, materialization, or fit authorization."""
    card = json.loads(json.dumps(card or {}))
    reactions = _card_reactions(card)
    errors, warnings, blocks = [], [], {}
    selections = card.get("datasets") or {}
    cutoff = None
    if "sidis" in reactions:
        rows = (card.get("cuts") or {}).get("idis", CAMPAIGN_DEFAULTS["cuts"]["idis"])
        try:
            qcuts = [row for row in rows if row.get("enabled", True)
                     and row.get("variable") == "Q2" and row.get("op", ">") == ">"]
            if len(qcuts) != 1:
                raise ValueError("configure exactly one enabled DIS Q2 > cutoff")
            cutoff = float(qcuts[0]["value"])
            if not math.isfinite(cutoff) or cutoff <= 0:
                raise ValueError("the DIS Q² cutoff must be positive and finite")
        except (TypeError, KeyError, AttributeError, ValueError) as exc:
            errors.append("SIDIS uses the configured DIS Q² cutoff: " + str(exc))
    for reaction in reactions:
        if reaction not in CATALOG_REACTIONS:
            errors.append(f"Reaction {reaction!r} is not available.")
            continue
        if reaction not in CONFIGURATION_ONLY_REACTIONS:
            continue
        entry = selections.get(reaction) or []
        ids = entry.get("selected", []) if isinstance(entry, dict) else entry
        try:
            if not isinstance(ids, list) or not ids:
                raise ValueError(f"{reaction}: select at least one dataset")
            block = configuration_dataset_block(reaction, ids, q2cut=cutoff)
            custom_cuts = (card.get("cuts") or {}).get(reaction)
            if isinstance(entry, dict) and "cuts" in entry:
                custom_cuts = entry["cuts"]
            if custom_cuts:
                if reaction == "sidis" and isinstance(custom_cuts, list):
                    same = (object_registry.cut_filters(custom_cuts)
                            == object_registry.cut_filters(object_registry.library_cuts(block["filters"])))
                else:
                    same = custom_cuts == {str(idx): cuts for idx, cuts in block["filters"].items()}
                if not same:
                    raise ValueError(f"{reaction}: preserve the authoritative dataset filters")
            custom_norms = (card.get("normalizations") or {}).get(reaction) or {}
            for idx, norm in custom_norms.items():
                # JSON booleans must not compare equal to integer tied references.
                canonical = block["norm"].get(int(idx))
                if canonical is None or json.dumps(norm, sort_keys=True) != json.dumps(canonical, sort_keys=True):
                    raise ValueError(f"{reaction}:{idx}: preserve the authoritative normalization definition")
            blocks[reaction] = block
            if reaction == "sia":
                missing = sorted(int(idx) for idx in ids if int(idx) not in block["filters"])
                if missing:
                    warnings.append("SIA datasets " + ", ".join(map(str, missing))
                                    + " have no filter definition in the authoritative excerpt; none was invented.")
        except (TypeError, KeyError, ValueError, OverflowError) as exc:
            errors.append(str(exc))
    # Existing reactions keep their existing normalization and cut validation.
    fitting_reactions = [r for r in reactions if r in ENABLED_REACTIONS]
    if fitting_reactions:
        draft = dict(card, reactions=fitting_reactions)
        draft["job"] = {**(card.get("job") or {}), "campaignName": "configuration_preview"}
        try:
            keys = [obj.get("type") for obj in _card_objects(card) if isinstance(obj, dict)
                    and obj.get("type") in SELECTABLE_OBJECTS]
            if "pdf-pion" in keys:
                keys = list(dict.fromkeys(keys + ["p->pi,n", "pdf"]))
            libraries = _object_libraries(u, keys)
            normalized, problems = _normalize_card(draft, libraries)
            errors.extend(problems)
            if not problems:
                available = {}
                for key in normalized["fit_objects"] + normalized["frozen_objects"]:
                    available.update(libraries[key].get("datasets", {}))
                for reaction in fitting_reactions:
                    source = available[reaction]
                    ids = {int(idx) for idx in normalized["datasets"][reaction]}
                    block = json.loads(json.dumps(source))
                    for field in ("xlsx", "norm"):
                        block[field] = {int(idx): value for idx, value in source.get(field, {}).items() if int(idx) in ids}
                    selected_filters = object_registry.cut_filters(normalized["cuts"][reaction])
                    if "filters" in source or selected_filters:
                        block["filters"] = selected_filters
                    for idx, value in normalized["normalizations"].get(reaction, {}).items():
                        value = dict(value)
                        if isinstance(value.get("fixed"), str) and value["fixed"].isdigit():
                            value["fixed"] = int(value["fixed"])
                        block["norm"][int(idx)] = value
                    blocks[reaction] = block
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
    fragment = "# Dataset configuration fragment only. FF parameters/theory are not implemented.\n"
    fragment += "# This is not a runnable fit input and does not authorize job submission.\n"
    fragment += "conf.setdefault('datasets', {})\n"
    fragment += "\n".join(f"conf['datasets'][{reaction!r}] = {block!r}" for reaction, block in blocks.items())
    return {"status": "configuration_only", "valid": False, "fit_ready": False,
            "configuration_valid": not errors, "errors": errors, "warnings": warnings,
            "message": "Dataset configuration only. SIDIS requires PDFs + FFs; SIA requires FFs only. "
                       "FF fitting is not implemented, so job submission is disabled.",
            "dataset_configuration": blocks, "input_preview": fragment if not errors else ""}


def validate_campaign(card=None, username=""):
    """Validate an input card and show what would run, with the confirmation
    phrase submit_campaign needs, and warnings about the selected data."""
    u, err = _need_user(username)
    if err:
        return err
    if _configuration_reactions(card):
        return _configuration_preview(u, card)
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
    if _configuration_reactions(card):
        return _unsupported_configuration_fit(card)
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
    collection_id = e["collection_id"]      # a campaign name resolves to its id
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


# Each replica's state and whether it is still in the Slurm queue (rerun_replicas).
_RERUN_STATE = r'''
import json, os, subprocess
stage = __STAGE__; total = __TOTAL__; jobs = __JOBS__
ended = {'COMPLETED', 'CANCELLED', 'FAILED', 'TIMEOUT', 'PREEMPTED', 'BOOT_FAIL', 'NODE_FAIL',
         'DEADLINE', 'OUT_OF_MEMORY', 'REVOKED', 'SPECIAL_EXIT'}
ids = {str(j.get('job_id')) for j in jobs}
queued, read = set(), True
try:
    q = subprocess.run(['squeue', '-h', '-r', '-u', __USER__, '-o', '%F|%K|%T'], capture_output=True,
                       text=True, timeout=30) if ids else None
    if q and q.returncode != 0:
        raise RuntimeError(q.stderr)
    for line in (q.stdout if q else '').splitlines():
        job, task, s = (line.strip().split('|') + ['', ''])[:3]
        if job in ids and task.isdigit() and s and s not in ended:
            queued.add(int(task))
except Exception:
    read = False
states = {}
for i in range(1, total + 1):
    state, summary = replica_state(os.path.join(stage, 'current', 'replica_%d' % i, 'fit_summary.json'))
    states[str(i)] = 'queued' if i in queued else (state or 'no_output')
print(json.dumps({'queue_read': read, 'states': states}))
'''

RERUNNABLE = ("failed", "not_fitted", "no_output")


def rerun_replicas(collection_id="", replicas="failed", confirm="", username=""):
    """Run replicas of a campaign again under their own numbers: the failed
    ones, the ones that did not fit or left no output (replicas='failed'),
    or the numbers given. Replicas still queued or running are never
    resubmitted; earlier results are moved to stages/<stage>/previous/, not
    deleted. Always asks for the confirmation phrase first."""
    u, err = _need_user(username)
    if err:
        return err
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}; list_campaigns shows them."}
    collection_id = e["collection_id"]      # a campaign name resolves to its id
    total = int(e.get("replicas") or 0)
    r = _run_ifarm_py(u, _analysis_code(_REPLICA_STATE + _RERUN_STATE, STAGE=_stage_root(u, e), TOTAL=total,
                                        JOBS=e.get("jobs") or [], USER=u), timeout=90, cap=2000000)
    d = _last_json(r)
    if not d:
        return {"status": "error", "message": "Could not read the replicas' state.", "detail": (r.get("err") or "")[-400:]}
    if not d["queue_read"]:
        return {"status": "error", "message": QUEUE_UNKNOWN_REFUSED}
    states = {int(k): v for k, v in d["states"].items()}
    spec = str(replicas or "failed").strip().lower()
    if spec in ("failed", "problems", "not_fitted", "all_failed"):
        wanted = [i for i, st in states.items() if st in RERUNNABLE]
    else:
        try:
            wanted = sorted(set(_numbers(replicas)))
        except ValueError:
            return {"status": "error", "message": "replicas: 'failed' or replica numbers such as 3,7 or 10-12."}
        outside = [i for i in wanted if not 1 <= i <= total]
        if outside:
            return {"status": "error", "message": f"This campaign has replicas 1-{total}; no replica {outside}."}
    busy = [i for i in wanted if states.get(i) == "queued"]
    wanted = [i for i in wanted if states.get(i) != "queued"]
    if not wanted:
        return {"status": "ok", "rerun": [], "still_queued": busy,
                "message": "Nothing to rerun: no failed, unfitted or missing replica" + (
                    " outside the ones still queued or running." if busy else ".")}
    plan = {"collection_id": collection_id, "replicas": wanted, "count": len(wanted),
            "array": _array_ranges(wanted), "states": {i: states.get(i) for i in wanted},
            "partition": (e["input_card"].get("slurm") or {}).get("partition_effective")}
    if busy:
        plan["still_queued_not_resubmitted"] = busy
    phrase = _confirm_phrase("RERUN", {"collection_id": collection_id, "replicas": wanted})
    if (confirm or "").strip() != phrase:
        return {"status": "needs_confirmation", "plan": plan, "confirmation_phrase": phrase,
                "message": "Nothing is queued yet. Show the user which replicas run again (their earlier "
                           "results are kept in previous/) and ask them to confirm with the phrase."}
    ws = _workspace(u)
    stage = _stage_root(u, e)
    qs, stamp = _shq(stage), time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    name = f"rerun_{stamp}"
    sb = base64.b64encode(_array_script(ws, e["campaign_folder"], e["input_card"], min(wanted), max(wanted),
                                        indices=wanted).encode()).decode()
    # The script is written first; earlier results move aside just before sbatch
    # and move back if sbatch refuses, so a failed submission changes nothing.
    moves = " ; ".join(f"if [ -e {qs}/current/replica_{i} ]; then mv {qs}/current/replica_{i} {qs}/previous/replica_{i}_{stamp}; fi"
                       for i in wanted)
    back = " ; ".join(f"if [ -e {qs}/previous/replica_{i}_{stamp} ]; then mv {qs}/previous/replica_{i}_{stamp} {qs}/current/replica_{i}; fi"
                      for i in wanted)
    r = _remote(u, f"mkdir -p {qs}/previous {qs}/slurm && echo {sb} | base64 -d > {qs}/slurm/{name}.sh && "
                   f"{{ {moves} ; }} && {{ sbatch {qs}/slurm/{name}.sh || {{ {back} ; false; }} ; }}", timeout=90)
    m = re.search(r"Submitted batch job (\d+)", (r.get("out") or "") + (r.get("err") or ""))
    if not m:
        return {"status": "error", "message": "sbatch refused the job.", "detail": (r.get("err") or r.get("out"))[-400:]}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    e["jobs"] = list(e.get("jobs") or []) + [{"job_id": m.group(1), "first": min(wanted), "last": max(wanted),
                                              "replicas": wanted, "rerun": True, "submitted_at": now}]
    e.update(updated_at=now, slurm_job_id=m.group(1), status="submitted")
    _registry(u, {"kind": "upsert", "entry": e})
    return {"status": "submitted", "job_id": m.group(1), **plan,
            "previous_results": f"{stage}/previous/replica_<n>_{stamp}",
            "message": "Queued. Run postprocess_campaign once they finish to update the fit summary."}


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
                    "datasets": e["input_card"]["datasets"],
                    "objects": e["input_card"].get("objects", [e["input_card"].get("object", {"type": "pdf"})]),
                    "available_ff_hadrons": ff_plotting.ff_available_hadrons(
                        [obj.get("type") for obj in e["input_card"].get("objects", [])]
                        + e["input_card"].get("frozen_objects", []))})
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
    collection_id = e["collection_id"]      # a campaign name resolves to its id
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
    collection_id = e["collection_id"]      # a campaign name resolves to its id
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
    collection_id = e["collection_id"]      # a campaign name resolves to its id
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


# ---- long analyses ----------------------------------------------------------------
#
# AI clients give up on a tool call after a minute or two, while a first data
# vs theory or PDF evaluation can take several. Such a call runs in a thread of
# its own: the call waits JOB_WAIT seconds and, if it is not done, answers with
# a job id; analysis_result(job_id) waits again and returns the result once it
# is there. The same request while one is running joins it instead of starting
# a second. Jobs live in this process only; their results also land in the
# campaign's cache, so a restarted gateway answers the next call from there.

JOB_WAIT = 40                       # seconds a call waits before answering with a job id
JOB_KEEP = 2 * 3600                 # a finished job is forgotten after this
_JOBS = {}
_JOBS_LOCK = threading.Lock()


def _job_reply(job, wait):
    if job["done"].wait(wait):
        out = dict(job["result"]) if isinstance(job["result"], dict) else {"status": "ok", "result": job["result"]}
        out["job_id"] = job["job_id"]
        return out
    return {"status": "running", "job_id": job["job_id"], "kind": job["kind"],
            "collection_id": job["params"].get("collection_id"), "request": job["params"],
            "elapsed_s": round(time.time() - job["started"]),
            "message": "Still computing on the cluster (the first evaluation of these replicas takes "
                       "minutes; later ones read the cache). Call analysis_result with this job_id to wait "
                       "for it. Do not start the analysis again."}


def _background(u, kind, params, fn):
    key = json.dumps([u, kind, params], sort_keys=True, default=str)
    now = time.time()
    with _JOBS_LOCK:
        for jid in [j for j, x in _JOBS.items() if x["done"].is_set() and now - x["finished"] > JOB_KEEP]:
            del _JOBS[jid]
        job = next((x for x in _JOBS.values() if x["key"] == key and not x["done"].is_set()), None)
        if job is None:
            job = {"job_id": secrets.token_hex(6), "key": key, "kind": kind, "params": params, "user": u,
                   "started": now, "finished": None, "done": threading.Event(), "result": None}
            _JOBS[job["job_id"]] = job

            def run():
                _tls.user = u
                try:
                    job["result"] = fn()
                except Exception as exc:
                    job["result"] = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
                finally:
                    job["finished"] = time.time()
                    job["done"].set()

            threading.Thread(target=run, name=f"job-{kind}", daemon=True).start()
    return _job_reply(job, JOB_WAIT)


def analysis_result(job_id="", username=""):
    """The result of a long analysis (analyze_by_experiment, campaign_pdf_plots)
    that answered with status running."""
    u, err = _need_user(username)
    if err:
        return err
    job = _JOBS.get(str(job_id or ""))
    if not job or job["user"] != u:
        return {"status": "error", "message": f"No analysis job {job_id!r} (jobs are kept {JOB_KEEP // 3600} h "
                                              "and not across a gateway restart): run the analysis again; "
                                              "what was already computed is cached."}
    return _job_reply(job, JOB_WAIT)


# ---- analysis ---------------------------------------------------------------------

_POSTPROCESS = _REPLICA_STATE + r'''
import glob, json, os, statistics
stage = __STAGE__
total = __TOTAL__
selection = __SELECTION__; write = __WRITE__
paths = sorted(glob.glob(os.path.join(stage, 'current', 'replica_*', 'fit_summary.json')))
# A replica selection (select_replicas) restricts the statistics to some fitted
# replicas; the others are counted as deselected. Only the unselected summary
# is written to the campaign.
fitted = []
for p in paths:
    state, s = replica_state(p)
    if state == 'fitted' and s.get('chi2_total') is not None:
        fitted.append(dict(s, identity=os.path.basename(os.path.dirname(p))))
kept, dropped = select_replicas(fitted, selection)
keep = {s['identity'] for s in kept}
# Only replicas that really fitted enter the statistics; those that did not fit
# (chi2 at the starting values only) are counted apart from the failed ones.
completed = failed = not_fitted = deselected = 0; chi2s = []; best = None; per = {}; cv = []
for p in paths:
    state, s = replica_state(p)
    if state == 'not_fitted':
        not_fitted += 1
    elif state == 'fitted' and s.get('chi2_total') is not None and os.path.basename(os.path.dirname(p)) not in keep:
        deselected += 1
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
if selection:
    agg['deselected_replicas'] = deselected
    agg['replica_selection'] = selection_report(selection, kept, len(fitted), dropped)
for k, b in per.items():
    m = statistics.mean(b['chi2']); n = statistics.mean(b['npts']) if b['npts'] else None
    agg['chi2_per_dataset'][k] = {'mean': m, 'min': min(b['chi2']), 'max': max(b['chi2']),
                                  'npts': int(n) if n else None, 'chi2_per_npts': m / n if n else None}
if write:
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
    collection_id = e["collection_id"]      # a campaign name resolves to its id
    code = _analysis_code(_POSTPROCESS, STAGE=_stage_root(u, e), TOTAL=int(e["replicas"]), SELECTION=None, WRITE=True)
    r = _run_ifarm_py(u, code, timeout=120, cap=2000000)
    try:
        agg = json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        return {"status": "error", "message": "postprocess failed", "detail": (r.get("err") or "")[-400:]}
    e.update(status="postprocessed", result_summary={k: agg[k] for k in agg if k != "chi2_per_dataset"},
             updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    _registry(u, {"kind": "upsert", "entry": e})
    out = {"status": "ok", "collection_id": collection_id, "message": "Campaign postprocess completed.",
           "objects": e["input_card"].get("objects", [e["input_card"].get("object", {"type": "pdf"})]),
           "summary": {k: agg[k] for k in agg if k != "chi2_per_dataset"}}
    if agg.get("not_fitted_replicas"):
        out["note"] = out["not_fitted_note"] = _not_fitted_note(agg["not_fitted_replicas"],
                                                                agg["total_replicas"])
    return out


def campaign_summary(collection_id="", username="", replicas=None):
    """The fit summary: replicas fitted (completed) / not fitted / failed, chi2
    statistics over the fitted ones, best replica, and chi2 per dataset (Load
    Fit Summary)."""
    u, err = _need_user(username)
    if err:
        return err
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}."}
    collection_id = e["collection_id"]      # a campaign name resolves to its id
    try:
        selection = _replica_selection(replicas)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    if selection:
        # The selected replicas' statistics, computed now and not stored.
        r = _run_ifarm_py(u, _analysis_code(_POSTPROCESS, STAGE=_stage_root(u, e), TOTAL=int(e["replicas"]),
                                            SELECTION=selection, WRITE=False), timeout=120, cap=2000000)
    else:
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
           "objects": e["input_card"].get("objects", [e["input_card"].get("object", {"type": "pdf"})]),
           "summary": {k: agg[k] for k in agg if k not in ("chi2_per_dataset", "replica_selection")}, "datasets": rows}
    if agg.get("replica_selection"):
        out["replica_selection"] = agg["replica_selection"]
    prog, total, notes = _campaign_progress(u, e), int(e["replicas"]), []
    # A summary from before replicas that did not fit were counted apart may
    # count them as completed; replicas that finished after the last
    # postprocess are not in it yet.
    counted = sum(agg.get(k) or 0 for k in ("completed_replicas", "not_fitted_replicas", "failed_replicas",
                                            "deselected_replicas"))
    finished = prog.get("finished")
    if "not_fitted_replicas" not in agg:
        notes.append("This summary was made before the gateway told replicas that fitted from replicas "
                     "that did not fit, so it may count both as completed: run postprocess_campaign to "
                     "redo it.")
    elif ((prog.get("succeeded") or 0) > (agg.get("completed_replicas") or 0) + (agg.get("deselected_replicas") or 0)
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


_PDF_PLOTS = _REPLICA_STATE + r'''
import glob, json, os, base64, uuid
stage = __STAGE__; flavors = __FLAVORS__; q2 = __Q2__; mean_overlay = __MEAN__
object_key = __OBJECT__; export_png = __PNG__; cache = __CACHE__
credible_interval = validate_credible_interval(__INTERVAL__)
interval_text = str(credible_interval)
if interval_text.endswith('.0'): interval_text = interval_text[:-2]
interval_label = interval_text + '% pointwise credible interval'
if credible_interval == 100:
    interval_label += ' (full replica envelope)'
percentiles = [(100 - credible_interval) / 2, 100 - (100 - credible_interval) / 2]
plot_dir = os.path.join(os.path.dirname(os.path.dirname(stage)), 'analysis', 'pdf_plots')
os.makedirs(plot_dir, exist_ok=True)
os.environ['MPLCONFIGDIR'] = os.path.join(plot_dir, '.matplotlib')
records = []
excluded = {'not_fitted': 0, 'failed': 0, 'missing_msr': 0}
for p in sorted(glob.glob(os.path.join(stage, 'current', 'replica_*', 'fit_summary.json'))):
    state, s = replica_state(p)
    if state != 'fitted':
        excluded[state or 'failed'] += 1
    elif not s.get('msr_path') or not os.path.isfile(s['msr_path']):
        excluded['missing_msr'] += 1
    else: records.append(dict(s, identity=os.path.basename(os.path.dirname(p))))
notes = []
if credible_interval == 100:
    notes.append('100% covers all available fitted replicas at each x, not the entire underlying posterior distribution.')
if any(excluded.values()):
    notes.append('Excluded replicas: %d not fitted, %d failed/unreadable summaries, %d missing .msr files.'
                 % (excluded['not_fitted'], excluded['failed'], excluded['missing_msr']))
metadata = {'credible_interval': credible_interval, 'percentiles': percentiles,
            'interval_kind': 'pointwise_equal_tailed', 'central_estimator': 'mean',
            'mean_overlay': mean_overlay, 'band_drawn': False,
            'n_replicas': len(records), 'excluded_replicas': excluded}
if not records:
    print(json.dumps({'status': 'missing_inputs', **metadata,
                      'message': 'No valid fitted replicas with .msr files. At least two are required for a credible interval. '
                                 + ' '.join(notes)})); raise SystemExit(0)
band_drawn = mean_overlay and len(records) > 1
if len(records) < 2:
    notes.append('Only one valid fitted replica is available. At least two are required for a credible interval; no band is drawn.')
metadata.update(band_drawn=band_drawn, note=' '.join(notes))
import numpy as np
xs = np.logspace(-3, np.log10(0.9), 120)
# Replica curves per flavor are cached (analysis/cache/pdf): a new Q2 or flavor
# is evaluated once, every later redraw only reads them.
sig = [[s['identity'], int(os.path.getmtime(s['msr_path']))] for s in records]
cache_file = lambda f: os.path.join(cache, 'pdf', '%s_q2_%s_%s.json' % (object_key, ('%g' % q2).replace('.', 'p'), f))
curves, todo = {}, []
for f in flavors:
    doc = read_json(cache_file(f)) or {}
    if doc.get('signature') == sig and len(doc.get('curves') or []) == len(records):
        curves[f] = np.asarray(doc['curves'], dtype=float)
    else:
        todo.append(f)
if todo:
    from fitlib.resman import RESMAN
    from tools.config import conf, load_config
    from tools.tools import load
    conf.clear(); load_config(os.path.join(stage, 'input.py'))
    resman = RESMAN(nworkers=1, parallel=False, datasets=False)
    pdf = conf[object_key]
    fresh = {f: [] for f in todo}
    try:
        for s in records:
            try:
                m = load(s['msr_path'])
                order = m['order'][0]; params = m['params'][0]
                if order is None or params is None or not np.isfinite(np.asarray(params, dtype=float)).all():
                    raise ValueError('Missing or nonfinite fitted parameters/order.')
                resman.parman.order = order
                resman.parman.set_new_params(params, initial=True)
                for f in todo:
                    values = np.asarray([distribution_value(pdf, float(x), q2, f, object_key) for x in xs], dtype=float)
                    if values.shape != xs.shape or not np.isfinite(values).all():
                        raise ValueError('Invalid or nonfinite plotted curve for %s %s.' % (object_key, f))
                    fresh[f].append(values)
            except (OSError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
                print(json.dumps({'status': 'error', **metadata, 'band_drawn': False,
                                  'message': 'Cannot plot fitted replica %s (%s): %s No plots were generated.'
                                             % (s.get('replica', '?'), s['msr_path'], exc)}))
                raise SystemExit(0)
    finally:
        resman.shutdown()
    for f in todo:
        curves[f] = np.asarray(fresh[f], dtype=float)
        write_json(cache_file(f), {'version': VERSION, 'object': object_key, 'q2': q2, 'flavor': f,
                                   'signature': sig, 'x': xs.tolist(),
                                   'curves': [[float('%.8g' % v) for v in c] for c in curves[f]]})
# A replica selection keeps a subset of the (cached, complete) replica curves.
selection = __SELECTION__
kept, dropped = select_replicas(records, selection)
if selection:
    idx = [records.index(s) for s in kept]
    if not idx:
        print(json.dumps({'status': 'error', **metadata, 'message': 'The replica selection leaves no fitted replica.'})); raise SystemExit(0)
    curves = {f: np.asarray(curves[f])[idx] for f in curves}
    band_drawn = mean_overlay and len(idx) > 1
    metadata.update(n_replicas=len(idx), band_drawn=band_drawn,
                    replica_selection=selection_report(selection, kept, len(records), dropped))
# The interactive chart gets the mean, the bands at the offered levels and up to
# 60 replica curves; the full set stays in the cache.
rnd = lambda a: [float('%.6g' % v) for v in a]
levels = sorted(set(LEVELS) | {credible_interval})
chart = {'x': rnd(xs), 'q2': q2, 'object': object_key, 'n_replicas': metadata['n_replicas'], 'curves': []}
for f in flavors:
    arr = curves[f]
    item = {'flavor': f, 'mean': rnd(arr.mean(axis=0)), 'bands': {},
            'replicas': [rnd(c) for c in arr[:60]]}
    if len(arr) > 1:
        for L in levels:
            lo, hi = np.percentile(arr, [(100 - L) / 2, 100 - (100 - L) / 2], axis=0)
            item['bands']['%g' % L] = [rnd(lo), rnd(hi)]
    chart['curves'].append(item)
out = []
if export_png:
    import matplotlib; matplotlib.use('Agg')
    from matplotlib import pyplot as plt
    names = {'ub': r'\bar{u}', 'db': r'\bar{d}', 'sb': r'\bar{s}'}
    request_id = uuid.uuid4().hex
    for f in flavors:
        arr = curves[f]
        fig, ax = plt.subplots(figsize=(6.0, 4.0))
        for c in arr: ax.plot(xs, c, lw=1.0, alpha=0.5)
        if band_drawn:
            low, high = pointwise_credible_interval(arr, credible_interval)
            ax.fill_between(xs, low, high, color='#0f172a', alpha=0.10, lw=0, label=interval_label)
        if mean_overlay:
            ax.plot(xs, arr.mean(axis=0), color='#111', lw=2.2, ls='--', label='mean'); ax.legend(frameon=False)
        ax.axhline(0, color='#64748b', lw=0.8, ls='--'); ax.set_xscale('log')
        ax.set_xlabel('$x$'); ax.set_ylabel('$x %s$' % names.get(f, f))
        caption = interval_label if band_drawn else interval_label + ' not drawn'
        ax.set_title('%s %s, %d replicas, $Q^2=%g$ GeV$^2$\n%s' % (object_key, f, len(arr), q2, caption)); ax.grid(alpha=0.25, which='both')
        path = os.path.join(plot_dir, '%s_%s_q2_%g_ci_%s_%s.png' % (object_key, f, q2, interval_text, request_id))
        fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)
        out.append({'object': object_key, 'flavor': f, 'path': path, **metadata, 'interval_label': caption,
                    'png_b64': base64.b64encode(open(path, 'rb').read()).decode()})
print(json.dumps({'status': 'success', **metadata, 'computed_flavors': todo, 'chart': chart, 'plots': out}))
'''


_PROTON_PDF_PLOTS = _REPLICA_STATE + r'''
import glob, json, os, base64, uuid
import numpy as np
stage = __STAGE__; flavors = __FLAVORS__; q2 = __Q2__
options = __DISPLAY__; mean_overlay = __MEAN__; interval = __INTERVAL__
campaign_name = __CAMPAIGN__; export_png = __PNG__; cache = __CACHE__
plot_dir = os.path.join(os.path.dirname(os.path.dirname(stage)), 'analysis', 'pdf_plots')
os.makedirs(plot_dir, exist_ok=True)
os.environ['MPLCONFIGDIR'] = os.path.join(plot_dir, '.matplotlib')
import matplotlib; matplotlib.use('Agg')
from matplotlib import pyplot as plt
from matplotlib.colors import to_rgba
try:
    to_rgba(options['band_color'])
except ValueError:
    print(json.dumps({'status': 'error', 'message': 'Unknown band color; use a Matplotlib named color or hexadecimal color.'}))
    raise SystemExit(0)
records = []
excluded = {'not_fitted': 0, 'failed': 0, 'missing_msr': 0}
for p in sorted(glob.glob(os.path.join(stage, 'current', 'replica_*', 'fit_summary.json'))):
    state, s = replica_state(p)
    if state != 'fitted':
        excluded[state or 'failed'] += 1
    elif not s.get('msr_path') or not os.path.isfile(s['msr_path']):
        excluded['missing_msr'] += 1
    else: records.append(dict(s, identity=os.path.basename(os.path.dirname(p))))
metadata = {'credible_interval': interval, 'percentiles': [(100-interval)/2, 100-(100-interval)/2],
            'interval_kind': 'pointwise_equal_tailed', 'central_estimator': 'mean',
            'mean_overlay': mean_overlay, 'band_drawn': False,
            'n_replicas': len(records), 'excluded_replicas': excluded, **options}
if len(records) < 2:
    print(json.dumps({'status': 'missing_inputs', **metadata,
                      'message': 'At least two valid fitted replicas with .msr files are required for a proton PDF credible interval.'}))
    raise SystemExit(0)
xs = proton_x_grid(options['x_min'], options['x_max'])
# Replica curves (and the d/u display breaks) are cached per flavor, Q2 and x
# range (analysis/cache/pdf): only flavors not seen before run FitPack.
sig = [[s['identity'], int(os.path.getmtime(s['msr_path']))] for s in records]
tag = lambda v: ('%g' % v).replace('.', 'p')
cache_file = lambda f: os.path.join(cache, 'pdf', 'proton_q2_%s_x_%s_%s_%s.json'
                                    % (tag(q2), tag(options['x_min']), tag(options['x_max']), f.replace('/', 'over')))
curves, display_breaks, todo = {}, {}, []
for f in flavors:
    doc = read_json(cache_file(f)) or {}
    if doc.get('signature') == sig and len(doc.get('curves') or []) == len(records) and len(doc.get('x') or []) == len(xs):
        curves[f] = [np.asarray([np.nan if v is None else v for v in row], dtype=float) for row in doc['curves']]
        if doc.get('breaks') is not None: display_breaks[f] = np.asarray(doc['breaks'], dtype=bool)
    else:
        todo.append(f)
if todo:
    from fitlib.resman import RESMAN
    from tools.config import conf, load_config
    from tools.tools import load
    conf.clear(); load_config(os.path.join(stage, 'input.py'))
    resman = RESMAN(nworkers=1, parallel=False, datasets=False)
    pdf = conf['pdf']
    fresh = {f: [] for f in todo}; fresh_breaks = {}
    try:
        for s in records:
            try:
                m = load(s['msr_path'])
                order = m['order'][0]; params = m['params'][0]
                if order is None or params is None or not np.isfinite(np.asarray(params, dtype=float)).all():
                    raise ValueError('Missing or nonfinite fitted parameters/order.')
                resman.parman.order = order
                resman.parman.set_new_params(params, initial=True)
                replica_breaks = {}
                replica = evaluate_proton_curves(pdf, xs, q2, todo, display_breaks=replica_breaks)
                for f, breaks in replica_breaks.items():
                    fresh_breaks[f] = fresh_breaks.get(f, np.zeros(len(xs), dtype=bool)) | breaks
                for f in todo: fresh[f].append(replica[f])
            except (OSError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
                print(json.dumps({'status': 'error', **metadata,
                                  'message': 'Cannot plot fitted replica %s (%s): %s No plots were generated.'
                                             % (s.get('replica', '?'), s['msr_path'], exc)}))
                raise SystemExit(0)
    finally:
        resman.shutdown()
    for f in todo:
        curves[f] = fresh[f]
        if f in fresh_breaks: display_breaks[f] = fresh_breaks[f]
        write_json(cache_file(f), {'version': VERSION, 'object': 'pdf', 'q2': q2, 'flavor': f, 'signature': sig,
                                   'x': [float(x) for x in xs],
                                   'curves': [[None if not np.isfinite(v) else float('%.8g' % v) for v in c] for c in fresh[f]],
                                   'breaks': fresh_breaks[f].tolist() if f in fresh_breaks else None})
selection = __SELECTION__
kept, dropped = select_replicas(records, selection)
if selection:
    idx = [records.index(s) for s in kept]
    if len(idx) < 2:
        print(json.dumps({'status': 'error', **metadata, 'message': 'The replica selection keeps fewer than two fitted replicas; a band needs two.'})); raise SystemExit(0)
    curves = {f: [curves[f][i] for i in idx] for f in curves}
    metadata.update(n_replicas=len(idx), replica_selection=selection_report(selection, kept, len(records), dropped))
try:
    bands = proton_bands(curves, interval, pointwise_credible_interval)
    for f, breaks in display_breaks.items():
        bands[f]['break_before'] = breaks
except ValueError as exc:
    print(json.dumps({'status': 'error', **metadata, 'message': str(exc)})); raise SystemExit(0)
interval_label = '%g%% pointwise credible interval' % interval
notes = []
if interval == 100:
    interval_label += ' (full replica envelope)'
    notes.append('100% covers all available fitted replicas at each x, not the entire underlying posterior distribution.')
if any(excluded.values()): notes.append('Excluded replica summaries: ' + str(excluded))
omitted = {f: [float(x) for x in xs[~bands[f]['valid']]] for f in flavors if not bands[f]['valid'].all()}
if omitted:
    notes.append('Numerically unresolved ratio points omitted for the entire ensemble (no replicas dropped): '
                 + ', '.join('%s: %d x points' % (f, len(points)) for f, points in omitted.items()))
metadata.update(band_drawn=True, note=' '.join(notes), omitted_ratio_points=omitted)
# The interactive chart: the same ensemble rule as the figure (an x where any
# replica is unresolved is left out for all), at each offered interval.
rnd = lambda a: [None if not np.isfinite(v) else float('%.6g' % v) for v in a]
chart = {'x': rnd(xs), 'q2': q2, 'object': 'pdf', 'n_replicas': metadata['n_replicas'], 'curves': []}
for f in flavors:
    arr = np.asarray(curves[f], dtype=float)
    item = {'flavor': f, 'mean': rnd(bands[f]['mean']), 'bands': {},
            'breaks': display_breaks[f].tolist() if f in display_breaks else None,
            'replicas': [rnd(c) for c in arr[:60]]}
    for L in sorted(set(LEVELS) | {interval}):
        b = proton_bands({f: arr}, L, pointwise_credible_interval)[f]
        item['bands']['%g' % L] = [rnd(b['low']), rnd(b['high'])]
    chart['curves'].append(item)
plots = []
if export_png:
    # The figure needs real LaTeX (draw_proton_figure refuses any other
    # renderer). If it cannot be drawn, the chart still comes back and the
    # reason is reported; nothing is drawn another way.
    try:
        fig, layout = draw_proton_figure(xs, bands, flavors, q2, campaign_name, interval_label,
                                         mean_overlay=mean_overlay, **options)
        path = os.path.join(plot_dir, 'pdf_combined_q2_%g_%s.png' % (q2, uuid.uuid4().hex))
        fig.savefig(path, dpi=150); plt.close(fig)
        plots.append({'object': 'pdf', 'flavors': flavors, 'path': path, **metadata,
                      'interval_label': interval_label, 'layout': layout,
                      'png_b64': base64.b64encode(open(path, 'rb').read()).decode()})
    except (RuntimeError, ValueError, OSError) as exc:
        plt.close('all')
        metadata['figure_error'] = str(exc)
        metadata['note'] = (metadata['note'] + ' Proton PDF figure not rendered: ' + str(exc)).strip()
print(json.dumps({'status': 'success', **metadata, 'computed_flavors': todo, 'chart': chart, 'plots': plots}))
'''


def campaign_pdf_plots(collection_id="", flavors="", q2=10.0, mean_overlay=None, username="", object_key="",
                       credible_interval=95, x_min=0.01, x_max=1.0, band_color="red", band_alpha=0.95,
                       export_png=True, replicas=None):
    """Fitted replicas' PDFs at Q2: the mean and pointwise credible bands for the
    interactive chart, and the figure (proton: the two-column band figure; pion:
    the replica plot) unless export_png is false."""
    try:
        credible_interval = object_registry.validate_credible_interval(credible_interval)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    u, err = _need_user(username)
    if err:
        return err
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}."}
    collection_id = e["collection_id"]      # a campaign name resolves to its id
    selected = [o["type"] for o in e["input_card"].get("objects", [e["input_card"].get("object", {"type": "pdf"})])]
    object_key = object_key or ("pdf" if "pdf" in selected else selected[0])
    if object_key not in selected or object_key not in SELECTABLE_OBJECTS:
        return {"status": "error", "message": f"Select a fitted distribution object from {selected}."}
    flavors = flavors or ("valence,sea,g" if object_key == "pdf-pion" else "u,d,g,s")
    fl = list(dict.fromkeys(f.strip() for f in _names(flavors)))
    allowed = pdf_plotting.PROTON_FLAVORS if object_key == "pdf" else OBJECTS[object_key]["flavors"]
    if not fl or any(f not in allowed for f in fl):
        return {"status": "error", "message": f"flavors for {object_key}: {allowed}"}
    if mean_overlay is None:
        mean_overlay = object_key != "pdf"
    try:
        selection = _replica_selection(replicas)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    try:
        q2 = float(q2)
        assert math.isfinite(q2) and q2 > 0
    except Exception:
        return {"status": "error", "message": "q2 must be a positive number"}
    if object_key == "pdf":
        try:
            display = pdf_plotting.validate_pdf_display(x_min, x_max, band_color, band_alpha)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    def run():
        if object_key == "pdf":
            code = inspect.getsource(object_registry) + inspect.getsource(pdf_plotting) + "\n" + _analysis_code(
                _PROTON_PDF_PLOTS, STAGE=_stage_root(u, e), FLAVORS=fl, Q2=q2,
                MEAN=bool(mean_overlay), INTERVAL=credible_interval, DISPLAY=display, PNG=bool(export_png),
                CACHE=_cache_dir(u, e), CAMPAIGN=e.get("campaign_name") or e["input_card"].get("name") or collection_id,
                SELECTION=selection)
        else:
            code = inspect.getsource(object_registry) + "\n" + _analysis_code(
                _PDF_PLOTS, STAGE=_stage_root(u, e), FLAVORS=fl, Q2=q2, MEAN=bool(mean_overlay), OBJECT=object_key,
                INTERVAL=credible_interval, PNG=bool(export_png), CACHE=_cache_dir(u, e), SELECTION=selection)
        ws = _workspace(u)
        r = _run_ifarm_py(u, code, apptainer=True, timeout=1800, cap=60000000,
                          env={"MPLCONFIGDIR": f"{ws}/{CAMPAIGNS_DIR}/.matplotlib"})
        d = _last_json(r)
        if not d:
            return {"status": "error", "message": "plotting failed",
                    "detail": (r.get("err") or r.get("out") or "")[-600:]}
        if d.get("status") != "success":
            return d
        metadata = {k: d[k] for k in ("credible_interval", "percentiles", "interval_kind", "central_estimator",
                                      "mean_overlay", "band_drawn", "n_replicas", "excluded_replicas", "note")}
        metadata["replica_selection"] = d.get("replica_selection")
        if object_key == "pdf":
            metadata.update({k: d[k] for k in ("x_min", "x_max", "band_color", "band_alpha", "omitted_ratio_points")})
            if d.get("figure_error"):
                metadata["figure_error"] = d["figure_error"]
        plots = [{**{k: v for k, v in p.items() if k != "png_b64"},
                  "data_url": "data:image/png;base64," + p["png_b64"]} for p in d["plots"]]
        # What the model reads: the mean at a few x per flavor, enough to talk about the curves.
        chart = d["chart"]
        picks = [i for i, x in enumerate(chart["x"]) if i % 20 == 0] + [len(chart["x"]) - 1]
        summary = {c["flavor"]: [[chart["x"][i], c["mean"][i]] for i in picks] for c in chart["curves"]}
        return {"status": "ok", "collection_id": e["collection_id"], "q2": q2, "object": object_key,
                **metadata, "flavors": fl, "computed_flavors": d.get("computed_flavors", []),
                "mean_at_x": summary,
                "plots": [{k: v for k, v in p.items() if k != "data_url"} for p in plots],
                "_images": [{"data": p["png_b64"], "mimeType": "image/png"} for p in d["plots"]],
                "_ui": {"view": "pdf", "chart": chart, "credible_interval": credible_interval,
                        "panel_plots": plots}}

    request = {"collection_id": e["collection_id"], "object": object_key, "flavors": fl, "q2": q2,
               "interval": credible_interval, "mean": bool(mean_overlay), "png": bool(export_png), "replicas": selection}
    if object_key == "pdf":
        request["display"] = display
    return _background(u, "campaign_pdf_plots", request, run)


_FF_PLOTS = _REPLICA_STATE + r"""
import glob, json, os, base64, uuid
stage = __STAGE__; flavors = __FLAVORS__; q2 = __Q2__
configured = __CONFIGURED__; credible_interval = validate_credible_interval(__INTERVAL__)
options = __DISPLAY__; campaign_name = __CAMPAIGN__; export_png = __PNG__; cache = __CACHE__
plot_dir = os.path.join(os.path.dirname(os.path.dirname(stage)), 'analysis', 'ff_plots')
os.makedirs(plot_dir, exist_ok=True)
os.environ['MPLCONFIGDIR'] = os.path.join(plot_dir, '.matplotlib')
records = []
excluded = {'not_fitted': 0, 'failed': 0, 'missing_msr': 0}
if not configured:
    for path in sorted(glob.glob(os.path.join(stage, 'current', 'replica_*', 'fit_summary.json'))):
        state, record = replica_state(path)
        if state != 'fitted': excluded[state or 'failed'] += 1
        elif not record.get('msr_path') or not os.path.isfile(record['msr_path']): excluded['missing_msr'] += 1
        else: records.append(dict(record, identity=os.path.basename(os.path.dirname(path))))
    if len(records) < 2:
        print(json.dumps({'status': 'missing_inputs', 'message': 'At least two fitted FF replicas are required for a credible interval. Configured parameters are a separate deterministic preview.'})); raise SystemExit(0)
import matplotlib; matplotlib.use('Agg')
from matplotlib import pyplot as plt
from matplotlib.colors import to_rgba
import numpy as np
try:
    to_rgba(options['band_color'])
except ValueError as exc:
    print(json.dumps({'status':'error','message':str(exc)}));raise SystemExit(0)
zs = np.linspace(options['z_min'], options['z_max'], 181)
# Each replica's curves (every FF object) are cached per flavor, Q2 and z range
# (analysis/cache/ff): a redraw, another interval or other hadrons only read
# them; a new flavor, Q2 or z range runs FitPack for what is missing.
if configured:
    sig = [['configured', int(os.path.getmtime(os.path.join(stage, 'input.py')))]]
else:
    sig = [[s['identity'], int(os.path.getmtime(s['msr_path']))] for s in records]
tag = lambda v: ('%g' % v).replace('.', 'p')
cache_file = lambda f: os.path.join(cache, 'ff', '%s_q2_%s_z_%s_%s_%s.json' % (
    'configured' if configured else 'replicas', tag(q2), tag(options['z_min']), tag(options['z_max']), f))
nrows = 1 if configured else len(records)
curves, todo = {}, []
for f in flavors:
    doc = read_json(cache_file(f)) or {}
    if (doc.get('version') == VERSION and doc.get('signature') == sig and doc.get('curves')
            and all(len(rows) == nrows for rows in doc['curves'].values())):
        for key, rows in doc['curves'].items(): curves.setdefault(key, {})[f] = rows
    else:
        todo.append(f)
if todo:
    from fitlib.resman import RESMAN
    from tools.config import conf, load_config
    from tools.tools import load
    conf.clear(); load_config(os.path.join(stage, 'input.py'))
    resman = RESMAN(nworkers=1, parallel=False, datasets=False)
    fresh = {}
    try:
        for record in ([None] if configured else records):
            if record is not None:
                replica = load(record['msr_path'])
                order, params = replica['order'][0], replica['params'][0]
                if order is None or params is None or not np.isfinite(np.asarray(params, dtype=float)).all():
                    raise ValueError('Missing or nonfinite fitted FF parameters/order')
                resman.parman.order = order
                resman.parman.set_new_params(params, initial=True)
            evaluated = evaluate_ff_curves(conf, zs, q2, todo)
            for key, rows in evaluated.items():
                for flavor, values in rows.items(): fresh.setdefault(key, {}).setdefault(flavor, []).append(values)
    except (OSError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
        print(json.dumps({'status':'error','message':'Cannot evaluate FF replica: '+str(exc)+'. No replicas were silently dropped.'}));raise SystemExit(0)
    finally:
        resman.shutdown()
    for f in todo:
        rows = {key: fresh[key][f] for key in fresh}
        for key, values in rows.items(): curves.setdefault(key, {})[f] = values
        write_json(cache_file(f), {'version': VERSION, 'q2': q2, 'flavor': f, 'configured': configured,
                                   'signature': sig, 'z': [float(z) for z in zs],
                                   'curves': {key: [[float('%.8g' % v) for v in row] for row in values]
                                              for key, values in rows.items()}})
selection = None if configured else __SELECTION__
kept, dropped = select_replicas(records, selection)
if selection:
    idx = [records.index(s) for s in kept]
    if len(idx) < 2:
        print(json.dumps({'status': 'error', 'message': 'The replica selection keeps fewer than two fitted replicas; a band needs two.'})); raise SystemExit(0)
    curves = {key: {f: [rows[i] for i in idx] for f, rows in by_flavor.items()} for key, by_flavor in curves.items()}
available = ff_available_hadrons(curves)
hadrons = options['hadrons'] or available
if not hadrons or any(h not in available for h in hadrons):
    print(json.dumps({'status':'error','message':'Select available hadrons: '+', '.join(available),'available_ff_hadrons':available}));raise SystemExit(0)
try:
    bands = ff_bands(curves, hadrons, flavors, credible_interval, pointwise_credible_interval, configured)
except (ValueError, RuntimeError, OSError) as exc:
    print(json.dumps({'status':'error','message':'FF plotting failed: '+str(exc)}));raise SystemExit(0)
nreplicas = len(kept) if not configured else 0
band_drawn = not configured
label = '%g%% C.I.' % credible_interval if band_drawn else 'Configured values; no uncertainty band'
note = ('Configured starting parameters, not fitted results; no uncertainty band.' if configured else
        'Replica combinations precede the equal-tailed pointwise intervals; no individual replica curves.')
if credible_interval == 100 and band_drawn: note += ' 100% is the available replica envelope, not the entire posterior.'
metadata = {'available_objects': list(curves), 'unavailable_objects': [key for key in FF_PLOT_OBJECTS if key not in curves],
            'available_ff_hadrons':available, 'hadrons':hadrons, 'modes':options['modes'],
            'n_replicas': nreplicas, 'configured': configured, 'band_drawn': band_drawn, 'mean_overlay':False,
            'credible_interval': credible_interval, 'interval_label': label,
            'quantity': 'zD(z,Q2)', 'charge_convention': 'native positive charged hadron; no extra factor of two',
            'excluded_replicas': excluded, 'note': note, 'z_min':options['z_min'],'z_max':options['z_max'],
            'dpi':FF_PREVIEW_DPI,
            'replica_selection': selection_report(selection, kept, len(records), dropped) if selection else None}
# The interactive chart: Trey's bands (ff_bands) at each offered interval; a
# configured preview has one curve and no band.
rnd = lambda a: [float('%.6g' % v) for v in a]
level_bands = {} if configured else {('%g' % L): ff_bands(curves, hadrons, flavors, L, pointwise_credible_interval)
                                     for L in sorted(set(LEVELS) | {credible_interval})}
chart = {'z': rnd(zs), 'q2': q2, 'configured': configured, 'n_replicas': nreplicas, 'hadrons': hadrons,
         'flavors': flavors, 'modes': options['modes'],
         'colors': {h: FF_HADRONS[h][2] for h in hadrons},
         'curves': [{'hadron': h, 'flavor': f, 'mean': rnd(bands[h][f]['mean']),
                     'bands': {L: [rnd(b[h][f]['low']), rnd(b[h][f]['high'])] for L, b in level_bands.items()}}
                    for h in hadrons for f in flavors]}
plots = []; request_id = uuid.uuid4().hex; limitations=[]
if export_png:
    # The figures need real LaTeX (_pdf_typography refuses any other renderer).
    # If they cannot be drawn, the chart still comes back with the reason.
    try:
        typography = _pdf_typography()
        figures = draw_ff_figures(zs, bands, flavors, hadrons, options['modes'], q2, campaign_name,
                                  credible_interval, typography=typography, band_color=options['band_color'],
                                  band_alpha=options['band_alpha'], configured=configured)
        for fig, info in figures:
            name = info['mode'] + ('_'+info['hadrons'][0] if info['mode']=='individual' else '')
            path = os.path.join(plot_dir, 'ff_%s_q2_%g_%s.png' % (name,q2,request_id))
            fig.savefig(path, dpi=FF_PREVIEW_DPI); plt.close(fig)
            limitations.extend(item for item in info['log_limitations'] if item['nonpositive_lower'] or item['nonpositive_upper'])
            plots.append({'object':'ff', 'label':name.replace('_',' '), 'flavors':flavors, 'path':path, **metadata, **info,
                          'png_b64':base64.b64encode(open(path,'rb').read()).decode()})
    except (ValueError, RuntimeError, OSError) as exc:
        plt.close('all')
        reason = str(exc).replace('Proton PDF plots require', 'The FF figures require')
        metadata['figure_error'] = reason
        metadata['note'] += ' FF figures not rendered: ' + reason
if limitations:
    metadata['note'] += ' Log-axis limitation: intervals crossing zero extend below the display frame; intervals entirely nonpositive are not drawable. Numerical values are unchanged; see log_limitations.'
metadata['log_limitations']=limitations
print(json.dumps({'status':'success', **metadata, 'computed_flavors': todo, 'chart': chart, 'plots':plots}))
"""


def campaign_ff_plots(collection_id="", flavors="", q2=100.0, mean_overlay=False,
                      username="", credible_interval=95, configured=False, modes="combined",
                      hadrons="", z_min=0.02, z_max=0.9, band_color="red", band_alpha=0.9, export_png=True,
                      replicas=None):
    """Native fitted FF bands, with independently selectable combined/individual art,
    and the same bands as an interactive chart; replica curves are cached."""
    u, error = _need_user(username)
    if error:
        return error
    entry = _collection(u, collection_id)
    if not entry:
        return {"status":"error", "message":f"No campaign {collection_id!r}."}
    collection_id = entry["collection_id"]      # a campaign name resolves to its id
    objects = [obj.get("type") for obj in entry['input_card'].get('objects',[])]
    if not set(objects).intersection(ff_registry.FF_OBJECTS):
        return {"status":"error", "message":"This campaign has no fitted FF parameter group."}
    fl = list(dict.fromkeys(part.strip() for part in (flavors or 'up,dp,sp,c,b,g').split(',') if part.strip()))
    try:
        interval = object_registry.validate_credible_interval(credible_interval)
        q2 = float(q2)
        if not math.isfinite(q2) or q2 <= 0:
            raise ValueError('Q² must be positive and finite')
        if not fl or any(flavor not in ff_plotting.FF_FLAVORS for flavor in fl):
            raise ValueError('FF flavors: '+', '.join(ff_plotting.FF_FLAVORS))
        if type(configured) is not bool:
            raise ValueError('configured must be true or false')
        display = ff_plotting.validate_ff_display(z_min,z_max,modes,hadrons,band_color,band_alpha)
        selection = _replica_selection(replicas)
    except (ValueError,TypeError,OverflowError) as exc:
        return {'status':'error','message':str(exc)}

    def run():
        code = (_object_registry_source()+'\n'+inspect.getsource(pdf_plotting)+'\n'+inspect.getsource(ff_plotting)
                + '\n' + _analysis_code(
                    _FF_PLOTS, STAGE=_stage_root(u,entry),FLAVORS=fl,Q2=q2,INTERVAL=interval,CONFIGURED=configured,
                    DISPLAY=display,CAMPAIGN=entry.get('campaign_name') or entry['input_card'].get('name') or collection_id,
                    PNG=bool(export_png), CACHE=_cache_dir(u, entry), SELECTION=selection))
        result = _run_ifarm_py(u,code,apptainer=True,timeout=1800,cap=60000000,
                               env={'MPLCONFIGDIR':f'{_workspace(u)}/{CAMPAIGNS_DIR}/.matplotlib'})
        data = _last_json(result)
        if not data:
            return {'status':'error','message':'FF plotting failed','detail':(result.get('err') or result.get('out') or '')[-600:]}
        if data.get('status')!='success': return data
        plots = [{**{k:v for k,v in row.items() if k!='png_b64'},
                  'data_url':'data:image/png;base64,'+row['png_b64']} for row in data['plots']]
        chart = data['chart']
        # What the model reads: the mean at a few z per hadron and flavor.
        picks = [i for i in range(len(chart['z'])) if i % 45 == 0] + [len(chart['z']) - 1]
        summary = {c['hadron']+':'+c['flavor']: [[chart['z'][i], c['mean'][i]] for i in picks] for c in chart['curves']}
        return {**{k:v for k,v in data.items() if k not in ('status','plots','chart')},'status':'ok',
                'collection_id':collection_id,'q2':q2, 'mean_at_z': summary,
                'plots':[{k:v for k,v in row.items() if k!='data_url'} for row in plots],
                '_images':[{'data':row['png_b64'],'mimeType':'image/png'} for row in data['plots']],
                '_ui':{'view':'ff','chart':chart,'credible_interval':interval,'panel_plots':plots}}

    return _background(u, "campaign_ff_plots",
                       {"collection_id": collection_id, "flavors": fl, "q2": q2, "interval": interval,
                        "configured": configured, "display": display, "png": bool(export_png), "replicas": selection}, run)


_THEORY_PLOTTING = r'''

import numpy as np

def theory_statistics(curves, interval, scale=1.0, offset=0.0, transform=None):
    interval = validate_credible_interval(interval)
    curves = np.asarray(curves, dtype=float)
    if curves.ndim != 2 or not all(curves.shape) or not np.isfinite(curves).all():
        raise ValueError('Theory must be a finite (fitted replicas, experimental points) array.')
    mean = curves.mean(axis=0)
    if transform is not None:
        displayed = np.asarray(transform(curves), dtype=float)
        central = np.asarray(transform(mean), dtype=float)
        if displayed.shape != curves.shape or central.shape != mean.shape:
            raise ValueError('Display transformation changed the experimental point ordering or shape.')
        if not np.isfinite(displayed).all() or not np.isfinite(central).all():
            raise ValueError('Nonfinite transformed theory; no interval was calculated.')
        bounds = pointwise_credible_interval(displayed, interval) if len(curves) > 1 else None
    else:
        central = mean
        bounds = pointwise_credible_interval(curves, interval) if len(curves) > 1 else None
    central = central * scale + offset
    if bounds is not None:
        a, b = bounds * scale + offset
        bounds = np.array([np.minimum(a, b), np.maximum(a, b)])
    if not np.isfinite(central).all() or (bounds is not None and not np.isfinite(bounds).all()):
        raise ValueError('Nonfinite displayed theory or interval.')
    return central, bounds

def theory_column(tab, key, n=None):
    if key not in tab:
        raise ValueError('Missing required dataset column: ' + key)
    values = np.asarray(tab[key], dtype=float)
    if values.ndim != 1 or (n is not None and len(values) != n) or not np.isfinite(values).all():
        raise ValueError('Missing, nonfinite or misaligned dataset column: ' + key)
    return values

def theory_plot_factors(tab):
    values = []
    for value in tab.get('plot-factor', np.ones(len(tab['value']))):
        parts = str(value).split('^')
        if len(parts) == 1: factor = float(parts[0])
        elif len(parts) == 2: factor = float(parts[0]) ** float(parts[1])
        else: raise ValueError('Invalid jet display factor: ' + str(value))
        if not np.isfinite(factor) or factor <= 0:
            raise ValueError('Jet display factors must be finite and positive.')
        values.append(factor)
    if len(values) != len(tab['value']): raise ValueError('Misaligned jet display factors.')
    return np.asarray(values)

def theory_identity(tab):
    import hashlib
    # SIA get_theory creates sigtot (the inclusive theory cross section).
    # It is not an experimental column; every data/kinematic/error field stays exact.
    dynamic = {'thy', 'prediction', 'shift', 'Shift', 'N', 'residuals', 'r-residuals', 'n-residuals', 'sigtot'}
    original = {str(k): np.asarray(v).tolist() for k, v in tab.items() if k not in dynamic}
    return hashlib.sha256(json.dumps(original, sort_keys=True, separators=(',', ':')).encode()).hexdigest()



# Adapted from thesis/dy.py:plot_ratios, original lines 42-212; SHA256 0b57fc011b0665c41932b73bda163b931174e19b3bd03f69ca960fb0da971231.
def thesis_dy_plot_ratios(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['dy']
    tabs = data
    (nrows, ncols) = (3, 1)
    fig = py.figure(figsize=(ncols * 7, nrows * 2.5))
    ax11 = py.subplot(nrows, ncols, (1, 2))
    ax21 = py.subplot(nrows, ncols, 3)
    tabs = datasets['dy']
    data = predictions['reactions']['dy']
    hand = {}
    thy_plot = {}
    thy_band = {}
    for idx in tabs:
        xt = tabs[idx]['xt']
        values = tabs[idx]['value']
        alpha = data[idx]['alpha']
        if idx == 20001:
            (color, marker, ms, cs, alp) = ('darkblue', '^', 5, 2.0, 0.8)
        if idx == 20002:
            (color, marker, ms, cs, alp) = ('darkred', '.', 10, 3.0, 1.0)
        hand[idx] = ax11.errorbar(xt, values, yerr=alpha, color=color, linestyle='none', marker=marker, ms=ms, capsize=cs, alpha=alp, zorder=5)
    for idx in tabs:
        for ic in range(nc):
            if idx == 20001:
                (color, alp) = ('blue', 0.2)
            if idx == 20002:
                (color, alp) = ('red', 0.5)
            xt = tabs[idx]['xt']
            thy = data[idx]['thy-%d' % ic]
            std_lower = data[idx]['dthy_lower-%d' % ic]
            std_upper = data[idx]['dthy_upper-%d' % ic]
            down = thy - std_lower
            up = thy + std_upper
            (thy_plot[idx],) = ax11.plot(xt, thy, color=color, alpha=0.5)
            thy_band[idx] = ax11.fill_between(xt, np.minimum(down, up), np.maximum(down, up), color=color, alpha=alp, where=thesis_band_mask(xt))
    for idx in tabs:
        for ic in range(nc):
            xt = datasets['dy'][idx]['xt']
            if nc > 1:
                color = colors[cluster[ic]]
            thy = data[idx]['thy-%d' % ic]
            ratio = thesis_ratio(data[idx]['value'], thy, error=False)
            alpha = data[idx]['alpha']
            thy = data[idx]['thy-%d' % ic]
            std_lower = data[idx]['dthy_lower-%d' % ic]
            std_upper = data[idx]['dthy_upper-%d' % ic]
            if idx == 20001:
                (color, marker, ms, cs, alp) = ('darkblue', '^', 5, 2.0, 0.8)
            if idx == 20002:
                (color, marker, ms, cs, alp) = ('darkred', '.', 10, 3.0, 1.0)
            ax21.errorbar(xt, ratio, yerr=thesis_ratio(alpha, thy, error=True), color=color, fmt=marker, ms=ms, capsize=cs, alpha=alp, zorder=5)
            if idx == 20001:
                (color, alp) = ('blue', 0.2)
            if idx == 20002:
                (color, alp) = ('red', 0.5)
            ax21.fill_between(xt, np.minimum(1 - thesis_ratio(std_lower, thy, error=False), 1 + thesis_ratio(std_upper, thy, error=False)), np.maximum(1 - thesis_ratio(std_lower, thy, error=False), 1 + thesis_ratio(std_upper, thy, error=False)), color=color, alpha=alp, where=thesis_band_mask(xt))
    for ax in [ax11]:
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=30, labelbottom=False)
    for ax in [ax21]:
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=30)
    (handles, labels) = ([], [])
    handles.append(hand.get(20002))
    handles.append(hand.get(20001))
    handles.append((thy_band.get(20002), thy_plot.get(20002)))
    handles.append((thy_band.get(20001), thy_plot.get(20001)))
    labels.append('\\textbf{\\textrm{SeaQuest}}')
    labels.append('\\textbf{\\textrm{NuSea}}')
    labels.append('\\textbf{\\textrm{JAM (SeaQuest)}}')
    labels.append('\\textbf{\\textrm{JAM (NuSea)}}')
    thesis_legend(ax11, handles, labels, frameon=False, fontsize=22, loc='upper left', ncol=2, handletextpad=0.3, handlelength=0.9, columnspacing=0.8)
    ax21.set_xlabel('\\boldmath$x_2$', size=40)
    ax11.set_ylim(0.59, 1.6)
    ax21.set_ylim(0.75, 1.25)
    ax11.axhline(1, 0, 1, color='black', ls='--', alpha=0.5)
    ax21.axhline(1, 0, 1, color='black', ls='--')
    ax11.text(0.05, 0.08, '\\boldmath$\\sigma_{pD}^{\\rm DY}/2\\sigma_{pp}^{\\rm DY}$', transform=ax11.transAxes, size=45)
    ax21.text(0.05, 0.75, '\\textbf{\\textrm{data/theory}}', transform=ax21.transAxes, size=35)
    if 20002 in data:
        ax11.text(0.06, 0.6, '$0.48 < x_1 < 0.69$', transform=ax11.transAxes, size=16, color='darkred')
    if 20001 in data:
        ax11.text(0.06, 0.32, '$0.36 < x_1 < 0.56$', transform=ax11.transAxes, size=16, color='darkblue')
    for ax in [ax11, ax21]:
        ax.xaxis.set_tick_params(which='major', length=6)
        ax.xaxis.set_tick_params(which='minor', length=3)
        ax.yaxis.set_tick_params(which='major', length=6)
        ax.yaxis.set_tick_params(which='minor', length=3)
        minorLocator = MultipleLocator(0.05)
        majorLocator = MultipleLocator(0.1)
        ax.xaxis.set_minor_locator(minorLocator)
        ax.xaxis.set_major_locator(majorLocator)
        ax.set_xticks([0.0, 0.2, 0.4])
        ax.set_xlim(0.0, 0.42)
    for ax in [ax11]:
        majorLocator = MultipleLocator(0.2)
        minorLocator = MultipleLocator(0.05)
        ax.yaxis.set_minor_locator(minorLocator)
        ax.yaxis.set_major_locator(majorLocator)
    for ax in [ax21]:
        minorLocator = MultipleLocator(0.05)
        majorLocator = MultipleLocator(0.1)
        ax.yaxis.set_minor_locator(minorLocator)
        ax.yaxis.set_major_locator(majorLocator)
        ax.yaxis.set_tick_params(which='major', length=6)
        ax.yaxis.set_tick_params(which='minor', length=3)
    ax11.set_yticks([0.8, 1, 1.2, 1.4])
    ax21.set_yticks([0.8, 1.0, 1.2])
    py.tight_layout()
    py.subplots_adjust(hspace=0, wspace=0)
    filename = output_file
    ax11.set_rasterized(True)
    ax21.set_rasterized(True)
    thesis_finish_panels(py.gcf(), 'thesis_dy_plot_ratios', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/dy.py:plot_xsec, original lines 214-360; SHA256 0b57fc011b0665c41932b73bda163b931174e19b3bd03f69ca960fb0da971231.
def thesis_dy_plot_xsec(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['dy']
    tabs = data
    data = predictions['reactions']['dy']
    Q2_bins = []
    Q2_bins.append([15, 20])
    Q2_bins.append([20, 24])
    Q2_bins.append([24, 28])
    Q2_bins.append([28, 35])
    Q2_bins.append([35, 40])
    Q2_bins.append([40, 45])
    Q2_bins.append([45, 52])
    Q2_bins.append([52, 60])
    Q2_bins.append([60, 68])
    Q2_bins.append([68, 75])
    Q2_bins.append([100, 140])
    Q2_bins.append([140, 160])
    Q2_bins.append([160, 280])
    (nrows, ncols) = (1, 1)
    fig = py.figure(figsize=(ncols * 16.0, nrows * 18.0))
    ax11 = py.subplot(nrows, ncols, 1)
    n = 10.0
    hand = {}
    for _ in data:
        if _ != 10001:
            continue
        for i in range(len(Q2_bins)):
            if i == 12:
                continue
            (Q2_min, Q2_max) = Q2_bins[i]
            for ic in range(kc.nc[istep]):
                if ic != 0:
                    continue
                thyk = data[_]['thy-%d' % ic]
                value = data[_]['value']
                df = pd.DataFrame(data[_])
                df = df.query('Q2>%f and Q2<%f' % (Q2_min, Q2_max))
                xF = df.xF
                thy = df['thy-%d' % ic] * n ** i
                dthy_lower = df['dthy_lower-%d' % ic] * n ** i
                dthy_upper = df['dthy_upper-%d' % ic] * n ** i
                value = df.value * n ** i
                alpha = df.alpha * n ** i
                zt = sorted(zip(xF, thy), key=lambda row: row[0])
                zv = sorted(zip(xF, value), key=lambda row: row[0])
                za = sorted(zip(xF, alpha), key=lambda row: row[0])
                zd_lower = sorted(zip(xF, dthy_lower), key=lambda row: row[0])
                zd_upper = sorted(zip(xF, dthy_upper), key=lambda row: row[0])
                thy = np.array([zt[i][1] for i in range(len(zt))])
                value = np.array([zv[i][1] for i in range(len(zv))])
                alpha = np.array([za[i][1] for i in range(len(za))])
                dthy_lower = np.array([zd_lower[i][1] for i in range(len(zd_lower))])
                dthy_upper = np.array([zd_upper[i][1] for i in range(len(zd_upper))])
                xF = sorted(xF)
                up = thy + dthy_upper
                down = thy - dthy_lower
                (ax, color, marker, ms) = (ax11, 'firebrick', '.', 10)
                hand[_] = ax.errorbar(xF, value, alpha, color=color, marker=marker, linestyle='none', ms=ms, capsize=3.0)
                (thy_plot,) = ax.plot(xF, thy, color='black', linestyle='-')
                thy_band = ax.fill_between(xF, np.minimum(down, up), np.maximum(down, up), color='gold', where=thesis_band_mask(xF))
    ax11.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=30)
    for ax in [ax11]:
        ax.semilogy()
        ax.set_xlim(-0.02, 1.15)
        ax.set_ylim(0.03, 300000000000.0)
        ax.yaxis.set_tick_params(which='major', length=10)
        ax.yaxis.set_tick_params(which='minor', length=5)
        ax.xaxis.set_tick_params(which='major', length=10)
        ax.xaxis.set_tick_params(which='minor', length=5)
        minorLocator = MultipleLocator(0.05)
        majorLocator = MultipleLocator(0.2)
        ax.xaxis.set_minor_locator(minorLocator)
        ax.xaxis.set_major_locator(majorLocator)
        ax.set_xticks([0, 0.2, 0.4, 0.6, 0.8, 1.0])
        ax.set_xticklabels(['$0$', '$0.2$', '$0.4$', '$0.6$', '$0.8$', '$1.0$'])
    ax11.text(0.62, 0.08, '$Q^2 \\, \\in \\,[15,20]\\,\\mathrm{GeV^2}$', transform=ax11.transAxes, fontsize=30)
    ax11.text(0.7, 0.13, '$Q^2 \\, \\in \\,[20,24]$', transform=ax11.transAxes, fontsize=30)
    ax11.text(0.7, 0.2, '$Q^2 \\, \\in \\,[24,28]$', transform=ax11.transAxes, fontsize=30)
    ax11.text(0.7, 0.28, '$Q^2 \\, \\in \\,[28,35]$', transform=ax11.transAxes, fontsize=30)
    ax11.text(0.7, 0.35, '$Q^2 \\, \\in \\,[35,40]$', transform=ax11.transAxes, fontsize=30)
    ax11.text(0.7, 0.42, '$Q^2 \\, \\in \\,[40,45]$', transform=ax11.transAxes, fontsize=30)
    ax11.text(0.7, 0.48, '$Q^2 \\, \\in \\,[45,52]$', transform=ax11.transAxes, fontsize=30)
    ax11.text(0.7, 0.55, '$Q^2 \\, \\in \\,[52,60]$', transform=ax11.transAxes, fontsize=30)
    ax11.text(0.7, 0.62, '$Q^2 \\, \\in \\,[60,68]$', transform=ax11.transAxes, fontsize=30)
    ax11.text(0.7, 0.68, '$Q^2 \\, \\in \\,[68,75]$', transform=ax11.transAxes, fontsize=30)
    ax11.text(0.7, 0.75, '$Q^2 \\, \\in \\,[100,140]$', transform=ax11.transAxes, fontsize=30)
    ax11.text(0.62, 0.83, '$Q^2 \\, \\in \\,[140,160]\\, (i=11)$', transform=ax11.transAxes, fontsize=30)
    ax11.text(0.7, 0.05, '$(i=0)$', transform=ax11.transAxes, fontsize=30)
    ax11.text(0.12, 0.05, '\\boldmath$M_{\\ell \\ell}^3 ~ \\frac{{\\rm d}^2 \\sigma_{pp}^{\\rm DY}}{{\\rm d} M_{\\ell \\ell} {\\rm d} x_F}$', transform=ax11.transAxes, size=60)
    ax11.text(0.42, 0.07, '$(\\times\\, 10^{\\, i})$', transform=ax11.transAxes, size=40)
    ax11.set_xlim(-0.02, 1.15)
    ax11.set_xlabel('\\boldmath$x_{\\rm F}$', size=50)
    ax11.set_xticks([0, 0.2, 0.4, 0.6, 0.8])
    ax11.set_xticklabels(['$0$', '$0.2$', '$0.4$', '$0.6$', '$0.8$'])
    handles = [hand.get(10001), (thy_band, thy_plot)]
    label1 = '\\textbf{\\textrm{FNAL E866}}'
    label2 = '\\textbf{\\textrm{JAM}}'
    labels = [label1, label2]
    thesis_legend(ax11, handles, labels, loc='upper right', fontsize=35, frameon=0, handletextpad=0.3, handlelength=1.0)
    py.tight_layout()
    filename = output_file
    ax11.set_rasterized(True)
    thesis_finish_panels(py.gcf(), 'thesis_dy_plot_xsec', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/idis.py:get_xbins, original lines 33-175; SHA256 92dc40105aa2214d4b8ea863286aad4601f0078010c0f2312ba5a1aabbb3ddcf.
def thesis_idis_get_xbins(data, kind):
    query = {}
    if kind == 'p':
        query[26] = data.query('X > 4.80e-3 and X <= 5.70e-3')
        query[25] = data.query('X > 7.20e-3 and X <= 9.30e-3')
        query[24] = data.query('X > 1.15e-2 and X <= 1.40e-2')
        query[23] = data.query('X > 1.65e-2 and X <= 1.90e-2')
        query[22] = data.query('X > 2.30e-2 and X <= 2.90e-2')
        query[21] = data.query('X > 3.40e-2 and X <= 3.80e-2')
        query[20] = data.query('X > 4.65e-2 and X <= 5.40e-2')
        query[19] = data.query('X > 6.50e-2 and X <= 7.30e-2')
        query[18] = data.query('X > 8.50e-2 and X <= 9.20e-2')
        query[17] = data.query('X > 9.80e-2 and X <= 10.3e-2')
        query[16] = data.query('X > 10.8e-2 and X <= 11.3e-2')
        query[15] = data.query('X > 13.6e-2 and X <= 14.6e-2')
        query[14] = data.query('X > 17.1e-2 and X <= 18.7e-2')
        query[13] = data.query('X > 21.7e-2 and X <= 23.7e-2')
        query[12] = data.query('X > 26.0e-2 and X <= 29.0e-2')
        query[11] = data.query('X > 33.0e-2 and X <= 36.0e-2')
        query[10] = data.query('X > 42.0e-2 and X <= 45.0e-2')
        query[9] = data.query('X > 45.0e-2 and X <= 48.0e-2')
        query[8] = data.query('X > 48.0e-2 and X <= 52.0e-2')
        query[7] = data.query('X > 52.0e-2 and X <= 55.0e-2')
        query[6] = data.query('X > 55.0e-2 and X <= 58.0e-2')
        query[5] = data.query('X > 58.0e-2 and X <= 60.0e-2')
        query[4] = data.query('X > 60.0e-2 and X <= 63.0e-2')
        query[3] = data.query('X > 63.0e-2 and X <= 66.0e-2')
        query[2] = data.query('X > 73.0e-2 and X <= 76.0e-2')
        query[1] = data.query('X > 84.0e-2 and X <= 86.0e-2')
        query[0] = data.query('X > 86.0e-2 and X <= 90.0e-2')
    if kind == 'd':
        query[18] = data.query('X > 6.50e-2 and X <= 7.30e-2')
        query[17] = data.query('X > 8.50e-2 and X <= 9.20e-2')
        query[16] = data.query('X > 9.80e-2 and X <= 10.3e-2')
        query[15] = data.query('X > 13.6e-2 and X <= 14.6e-2')
        query[14] = data.query('X > 17.1e-2 and X <= 18.7e-2')
        query[13] = data.query('X > 21.7e-2 and X <= 23.7e-2')
        query[12] = data.query('X > 26.0e-2 and X <= 29.0e-2')
        query[11] = data.query('X > 33.0e-2 and X <= 36.0e-2')
        query[10] = data.query('X > 42.0e-2 and X <= 45.0e-2')
        query[9] = data.query('X > 45.0e-2 and X <= 48.0e-2')
        query[8] = data.query('X > 48.0e-2 and X <= 52.0e-2')
        query[7] = data.query('X > 52.0e-2 and X <= 55.0e-2')
        query[6] = data.query('X > 55.0e-2 and X <= 58.0e-2')
        query[5] = data.query('X > 58.0e-2 and X <= 60.0e-2')
        query[4] = data.query('X > 60.0e-2 and X <= 63.0e-2')
        query[3] = data.query('X > 63.0e-2 and X <= 66.0e-2')
        query[2] = data.query('X > 73.0e-2 and X <= 76.0e-2')
        query[1] = data.query('X > 84.0e-2 and X <= 86.0e-2')
        query[0] = data.query('X > 86.0e-2 and X <= 90.0e-2')
    if kind == 'd/p':
        query[17] = data.query('X > 4.80e-3 and X <= 5.70e-3')
        query[16] = data.query('X > 7.20e-3 and X <= 9.30e-3')
        query[15] = data.query('X > 1.15e-2 and X <= 1.40e-2')
        query[14] = data.query('X > 1.65e-2 and X <= 1.90e-2')
        query[13] = data.query('X > 2.30e-2 and X <= 2.90e-2')
        query[12] = data.query('X > 2.90e-2 and X <= 3.80e-2')
        query[11] = data.query('X > 4.60e-2 and X <= 5.40e-2')
        query[10] = data.query('X > 6.50e-2 and X <= 7.30e-2')
        query[9] = data.query('X > 8.50e-2 and X <= 9.20e-2')
        query[8] = data.query('X > 10.7e-2 and X <= 11.3e-2')
        query[7] = data.query('X > 13.6e-2 and X <= 14.6e-2')
        query[6] = data.query('X > 14.6e-2 and X <= 18.7e-2')
        query[5] = data.query('X > 18.7e-2 and X <= 23.7e-2')
        query[4] = data.query('X > 23.7e-2 and X <= 29.0e-2')
        query[3] = data.query('X > 29.0e-2 and X <= 36.0e-2')
        query[2] = data.query('X > 36.0e-2 and X <= 47.6e-2')
        query[1] = data.query('X > 47.6e-2 and X <= 57.5e-2')
        query[0] = data.query('X > 57.5e-2 and X <= 70.5e-2')
    if kind == 'HERA NC':
        query[24] = data.query('X < 3.50e-5')
        query[23] = data.query('X > 3.50e-5 and X <= 5.5e-5')
        query[22] = data.query('X > 5.50e-5 and X <= 9.5e-5')
        query[21] = data.query('X > 9.50e-5 and X <= 1.5e-4')
        query[20] = data.query('X > 1.50e-4 and X <= 2.5e-4')
        query[19] = data.query('X > 2.50e-4 and X <= 3.5e-4')
        query[18] = data.query('X > 3.50e-4 and X <= 5.5e-4')
        query[17] = data.query('X > 5.50e-4 and X <= 6.6e-4')
        query[16] = data.query('X > 7.50e-4 and X <= 8.5e-4')
        query[15] = data.query('X > 8.50e-4 and X <= 0.98e-3')
        query[14] = data.query('X > 0.98e-3 and X <= 1.9e-3')
        query[13] = data.query('X > 0.0019  and X <= 0.0030')
        query[12] = data.query('X > 0.0030  and X <= 0.0040')
        query[11] = data.query('X > 4.80e-3 and X <= 5.6e-3')
        query[10] = data.query('X > 7.90e-3 and X <= 8.6e-3')
        query[9] = data.query('X > 1.00e-2 and X <= 1.5e-2')
        query[8] = data.query('X > 1.90e-2 and X <= 2.2e-2')
        query[7] = data.query('X > 3.00e-2 and X <= 3.4e-2')
        query[6] = data.query('X > 4.80e-2 and X <= 5.6e-2')
        query[5] = data.query('X > 7.90e-2 and X <= 8.8e-2')
        query[4] = data.query('X > 12.0e-2 and X <= 14e-2')
        query[3] = data.query('X > 17.0e-2 and X <= 19e-2')
        query[2] = data.query('X > 23.0e-2 and X <= 26e-2')
        query[1] = data.query('X > 38.0e-2 and X <= 42.0e-2')
        query[0] = data.query('X > 62.0e-2 and X <= 66e-2')
    if kind == 'HERA CC':
        query[7] = data.query('X > 0.0079 and X <= 0.0081')
        query[6] = data.query('X > 0.010  and X <= 0.015')
        query[5] = data.query('X > 0.030  and X <= 0.034')
        query[4] = data.query('X > 0.079  and X <= 0.081')
        query[3] = data.query('X > 0.120  and X <= 0.14')
        query[2] = data.query('X > 0.230  and X <= 0.27')
        query[1] = data.query('X > 0.380  and X <= 0.42')
        query[0] = data.query('X > 0.630  and X <= 0.66')
    if kind == 'HERA other':
        query[29] = data.query('X < 3.50e-5 ')
        query[28] = data.query('X > 3.50e-5 and X <= 9.5e-5')
        query[27] = data.query('X > 9.50e-5 and X <= 2.5e-4')
        query[26] = data.query('X > 2.50e-4 and X <= 3.5e-4')
        query[25] = data.query('X > 3.50e-4 and X <= 5.5e-4')
        query[24] = data.query('X > 5.50e-4 and X <= 6.6e-4')
        query[23] = data.query('X > 6.90e-4 and X <= 8.5e-4')
        query[22] = data.query('X > 8.50e-4 and X <= 0.98e-3')
        query[21] = data.query('X > 0.98e-3 and X <= 0.0014')
        query[20] = data.query('X > 1.3e-3  and X <= 1.9e-03')
        query[19] = data.query('X > 0.0019  and X <= 0.0029')
        query[18] = data.query('X > 0.0029  and X <= 0.0040')
        query[17] = data.query('X > 4.0e-3  and X <= 5.55e-3')
        query[16] = data.query('X > 5.55e-3 and X <= 7.95e-3')
        query[15] = data.query('X > 7.95e-3 and X <= 8.9e-3')
        query[14] = data.query('X > 9.00e-3 and X <= 9.9e-3')
        query[13] = data.query('X > 9.90e-3 and X <= 1.25e-2')
        query[12] = data.query('X > 1.25e-2 and X <= 1.5e-2')
        query[11] = data.query('X > 1.50e-2 and X <= 1.7e-2')
        query[10] = data.query('X > 1.70e-2 and X <= 1.99e-2')
        query[9] = data.query('X > 1.99e-2 and X <= 2.2e-2')
        query[8] = data.query('X > 2.30e-2 and X <= 2.7e-2')
        query[7] = data.query('X > 3.00e-2 and X <= 3.4e-2')
        query[6] = data.query('X > 4.80e-2 and X <= 5.6e-2')
        query[5] = data.query('X > 7.90e-2 and X <= 8.8e-2')
        query[4] = data.query('X > 12.0e-2 and X <= 14e-2')
        query[3] = data.query('X > 17.0e-2 and X <= 19e-2')
        query[2] = data.query('X > 23.0e-2 and X <= 26e-2')
        query[1] = data.query('X > 30.0e-2 and X <= 45e-2')
        query[0] = data.query('X > 60.0e-2 and X <= 70e-2')
    return query

# Adapted from thesis/idis.py:get_Q2bins, original lines 177-189; SHA256 92dc40105aa2214d4b8ea863286aad4601f0078010c0f2312ba5a1aabbb3ddcf.
def thesis_idis_get_Q2bins(data, kind):
    query = {}
    if kind == 'BONuS':
        query[5] = data.query('Q2 > 3.7')
        query[4] = data.query('Q2 > 3.2 and Q2 <= 3.7')
        query[3] = data.query('Q2 > 2.7 and Q2 <= 3.2')
        query[2] = data.query('Q2 > 2.3 and Q2 <= 2.7')
        query[1] = data.query('Q2 > 1.9 and Q2 <= 2.3')
        query[0] = data.query('Q2 > 1.5 and Q2 <= 1.9')
    if kind == 'MARATHON' or kind == 'JLab':
        query[0] = data.query('Q2 > 1.5 and Q2 <= 20')
    return query

# Adapted from thesis/idis.py:get_thetabins, original lines 191-201; SHA256 92dc40105aa2214d4b8ea863286aad4601f0078010c0f2312ba5a1aabbb3ddcf.
def thesis_idis_get_thetabins(data, kind):
    query = {}
    if kind == 'JLab':
        query[5] = data.query('theta > 65 and theta <= 75')
        query[4] = data.query('theta > 57 and theta <= 65')
        query[3] = data.query('theta > 50 and theta <= 57')
        query[2] = data.query('theta > 43 and theta <= 50')
        query[1] = data.query('theta > 40 and theta <= 43')
        query[0] = data.query('theta > 35 and theta <= 40')
    return query

# Adapted from thesis/idis.py:get_plot, original lines 203-228; SHA256 92dc40105aa2214d4b8ea863286aad4601f0078010c0f2312ba5a1aabbb3ddcf.
def thesis_idis_get_plot(query, cluster_i=0):
    plot = {_: {} for _ in ['theory', 'X', 'Q2', 'value', 'alpha', 'std_lower', 'std_upper']}
    for key in query:
        theory = query[key]['thy-%d' % cluster_i]
        std_lower = query[key]['dthy_lower-%d' % cluster_i]
        std_upper = query[key]['dthy_upper-%d' % cluster_i]
        X = query[key]['X']
        Q2 = query[key]['Q2']
        value = query[key]['value']
        alpha = query[key]['alpha']
        zx = sorted(zip(Q2, X), key=lambda row: row[0])
        zt = sorted(zip(Q2, theory), key=lambda row: row[0])
        zv = sorted(zip(Q2, value), key=lambda row: row[0])
        za = sorted(zip(Q2, alpha), key=lambda row: row[0])
        zs_lower = sorted(zip(Q2, std_lower), key=lambda row: row[0])
        zs_upper = sorted(zip(Q2, std_upper), key=lambda row: row[0])
        plot['X'][key] = np.array([zx[i][1] for i in range(len(zx))])
        plot['theory'][key] = np.array([zt[i][1] for i in range(len(zt))])
        plot['value'][key] = np.array([zv[i][1] for i in range(len(zv))])
        plot['alpha'][key] = np.array([za[i][1] for i in range(len(za))])
        plot['std_lower'][key] = np.array([zs_lower[i][1] for i in range(len(zs_lower))])
        plot['std_upper'][key] = np.array([zs_upper[i][1] for i in range(len(zs_upper))])
        plot['Q2'][key] = np.array(sorted(Q2))
    return plot

# Adapted from thesis/idis.py:get_theory, original lines 230-272; SHA256 92dc40105aa2214d4b8ea863286aad4601f0078010c0f2312ba5a1aabbb3ddcf.
def thesis_idis_get_theory(PLOT, nbins, loop=True, funcQ2=True, funcX=False):
    theory = {}
    if funcQ2:
        svar = 'Q2'
    if funcX:
        svar = 'X'
    theory = {_: {} for _ in [svar, 'value', 'std_lower', 'std_upper']}
    for key in range(nbins):
        var = []
        thy = []
        std_lower = []
        std_upper = []
        if loop:
            for exp in PLOT:
                var.extend(PLOT[exp][svar][key])
                thy.extend(PLOT[exp]['theory'][key])
                std_lower.extend(PLOT[exp]['std_lower'][key])
                std_upper.extend(PLOT[exp]['std_upper'][key])
        else:
            var.extend(PLOT[svar][key])
            thy.extend(PLOT['theory'][key])
            std_lower.extend(PLOT['std_lower'][key])
            std_upper.extend(PLOT['std_upper'][key])
        if len(var) == 0:
            continue
        vmin = np.min(var)
        vmax = np.max(var)
        theory[svar][key] = np.geomspace(vmin, vmax, 100)
        if len(var) > 1:
            theory['value'][key] = griddata(np.array(var), np.array(thy), theory[svar][key], method='linear')
            theory['std_lower'][key] = griddata(np.array(var), np.array(std_lower), theory[svar][key], method='linear')
            theory['std_upper'][key] = griddata(np.array(var), np.array(std_upper), theory[svar][key], method='linear')
        else:
            theory['value'][key] = np.ones(100) * thy
            theory['std_lower'][key] = np.ones(100) * std_lower
            theory['std_upper'][key] = np.ones(100) * std_upper
    return theory

# Adapted from thesis/idis.py:get_details, original lines 274-299; SHA256 92dc40105aa2214d4b8ea863286aad4601f0078010c0f2312ba5a1aabbb3ddcf.
def thesis_idis_get_details(exp):
    if exp == 'NMC':
        (color, marker, ms) = ('firebrick', '*', 8)
    if exp == 'SLAC':
        (color, marker, ms) = ('darkgreen', '^', 6)
    if exp == 'BCDMS':
        (color, marker, ms) = ('blue', 'o', 6)
    if exp == 'HERA 10026':
        (color, marker, ms) = ('black', '.', 8)
    if exp == 'HERA 10030':
        (color, marker, ms) = ('firebrick', 'D', 8)
    if exp == 'HERA 10031':
        (color, marker, ms) = ('blue', '^', 6)
    if exp == 'HERA 10032':
        (color, marker, ms) = ('darkgreen', 's', 6)
    if exp == 'HERA 10027':
        (color, marker, ms) = ('darkgreen', 's', 6)
    if exp == 'HERA 10028':
        (color, marker, ms) = ('firebrick', '.', 8)
    if exp == 'HERA 10029':
        (color, marker, ms) = ('blue', '^', 8)
    if exp == 'JLab d':
        (color, marker, ms) = ('firebrick', '*', 8)
    if exp == 'JLab p':
        (color, marker, ms) = ('darkgreen', '^', 8)
    if exp == 'BONuS':
        (color, marker, ms) = ('blue', 'o', 6)
    if exp == 'MARATHON dp':
        (color, marker, ms) = ('darkgreen', 'o', 6)
    if exp == 'MARATHON ht':
        (color, marker, ms) = ('darkgreen', '^', 5)
    if exp == 'JLab hd':
        (color, marker, ms) = ('firebrick', 'o', 5)
    return (color, marker, ms)

# Adapted from thesis/idis.py:plot_proton, original lines 301-777; SHA256 92dc40105aa2214d4b8ea863286aad4601f0078010c0f2312ba5a1aabbb3ddcf.
def thesis_idis_plot_proton(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['idis']
    tabs = data
    (nrows, ncols) = (2, 2)
    py.figure(figsize=(ncols * 12.0, nrows * 14.0))
    ax11 = py.subplot(nrows, ncols, 1)
    ax12 = py.subplot(nrows, ncols, 2)
    ax21 = py.subplot(nrows, ncols, 3)
    ax22 = py.subplot(nrows, ncols, 4)
    if any((i in data for i in [10010, 10016, 10020])):
        nbins = 27
        DATA = {}
        if 10020 in data:
            DATA['NMC'] = pd.DataFrame(data[10020])
        if 10010 in data:
            DATA['SLAC'] = pd.DataFrame(data[10010])
        if 10016 in data:
            DATA['BCDMS'] = pd.DataFrame(data[10016])
        PLOT = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_idis_get_xbins(DATA[exp], 'p')
                PLOT[exp] = thesis_idis_get_plot(query)
            theory = thesis_idis_get_theory(PLOT, nbins)
        hand = {}
        for exp in PLOT:
            for key in range(nbins):
                Q2 = PLOT[exp]['Q2'][key]
                val = PLOT[exp]['value'][key] * 2.0 ** float(key)
                alpha = PLOT[exp]['alpha'][key] * 2.0 ** float(key)
                (color, marker, ms) = thesis_idis_get_details(exp)
                hand[exp] = ax11.errorbar(Q2, val, alpha, marker=marker, color=color, ms=ms, capsize=3, linestyle='none')
        for key in theory['value']:
            Q2 = theory['Q2'][key]
            mean = theory['value'][key] * 2.0 ** float(key)
            std_lower = theory['std_lower'][key] * 2.0 ** float(key)
            std_upper = theory['std_upper'][key] * 2.0 ** float(key)
            down = mean - std_lower
            up = mean + std_upper
            (thy_plot,) = ax11.plot(Q2, mean, linestyle='solid', color='black')
            thy_band = ax11.fill_between(Q2, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(Q2))
        if any((len(v['value'].get(26, [])) for v in PLOT.values())):
            ax11.text(2.4, 30000000.0, '$x=0.005\\, (i=24)$', fontsize=22)
        if any((len(v['value'].get(25, [])) for v in PLOT.values())):
            ax11.text(3.7, 12000000.0, '$x=0.008$', fontsize=22)
        if any((len(v['value'].get(24, [])) for v in PLOT.values())):
            ax11.text(5.8, 6000000.0, '$x=0.013$', fontsize=22)
        if any((len(v['value'].get(23, [])) for v in PLOT.values())):
            ax11.text(7.5, 3000000.0, '$x=0.018$', fontsize=22)
        if any((len(v['value'].get(22, [])) for v in PLOT.values())):
            ax11.text(12.0, 1500000.0, '$x=0.026$', fontsize=22)
        if any((len(v['value'].get(21, [])) for v in PLOT.values())):
            ax11.text(15.0, 800000.0, '$x=0.036$', fontsize=22)
        if any((len(v['value'].get(20, [])) for v in PLOT.values())):
            ax11.text(21.0, 400000.0, '$x=0.05$', fontsize=22)
        if any((len(v['value'].get(19, [])) for v in PLOT.values())):
            ax11.text(28.0, 200000.0, '$x=0.07$', fontsize=22)
        if any((len(v['value'].get(18, [])) for v in PLOT.values())):
            ax11.text(28.0, 100000.0, '$x=0.09$', fontsize=22)
        if any((len(v['value'].get(17, [])) for v in PLOT.values())):
            ax11.text(41.0, 50000.0, '$x=0.10$', fontsize=22)
        if any((len(v['value'].get(16, [])) for v in PLOT.values())):
            ax11.text(38.0, 25000.0, '$x=0.11$', fontsize=22)
        if any((len(v['value'].get(15, [])) for v in PLOT.values())):
            ax11.text(62.0, 10000.0, '$x=0.14$', fontsize=22)
        if any((len(v['value'].get(14, [])) for v in PLOT.values())):
            ax11.text(70.0, 5000.0, '$x=0.18$', fontsize=22)
        if any((len(v['value'].get(13, [])) for v in PLOT.values())):
            ax11.text(93.0, 2500.0, '$x=0.23$', fontsize=22)
        if any((len(v['value'].get(12, [])) for v in PLOT.values())):
            ax11.text(125.0, 1100.0, '$x=0.28$', fontsize=22)
        if any((len(v['value'].get(11, [])) for v in PLOT.values())):
            ax11.text(150.0, 400.0, '$x=0.35$', fontsize=22)
        if any((len(v['value'].get(10, [])) for v in PLOT.values())):
            ax11.text(200.0, 100.0, '$x=0.43$', fontsize=22)
        if any((len(v['value'].get(9, [])) for v in PLOT.values())):
            ax11.text(65.0, 60.0, '$x=0.46$', fontsize=22)
        if any((len(v['value'].get(8, [])) for v in PLOT.values())):
            ax11.text(15.0, 30.0, '$x=0.50$', fontsize=22)
        if any((len(v['value'].get(7, [])) for v in PLOT.values())):
            ax11.text(250.0, 8.0, '$x=0.53$', fontsize=22)
        if any((len(v['value'].get(6, [])) for v in PLOT.values())):
            ax11.text(22.0, 5.0, '$x=0.56$', fontsize=22)
        if any((len(v['value'].get(5, [])) for v in PLOT.values())):
            ax11.text(23.0, 2.5, '$x=0.59$', fontsize=22)
        if any((len(v['value'].get(4, [])) for v in PLOT.values())):
            ax11.text(24.0, 1.0, '$x=0.61$', fontsize=22)
        if any((len(v['value'].get(3, [])) for v in PLOT.values())):
            ax11.text(250.0, 0.2, '$x=0.65$', fontsize=22)
        if any((len(v['value'].get(2, [])) for v in PLOT.values())):
            ax11.text(250.0, 0.03, '$x=0.75$', fontsize=22)
        if any((len(v['value'].get(1, [])) for v in PLOT.values())):
            ax11.text(30.0, 0.008, '$x=0.85$', fontsize=22)
        if any((len(v['value'].get(0, [])) for v in PLOT.values())):
            ax11.text(30.0, 0.002, '$x=0.88\\, (i=0)$', fontsize=22)
        ax11.semilogy()
        ax11.semilogx()
        ax11.set_xlim(1.5, 500.0)
        ax11.set_ylim(0.0005, 100000000.0)
        ax11.tick_params(axis='both', labelsize=30)
        ax11.yaxis.set_tick_params(which='major', length=10)
        ax11.set_yticks([0.001, 0.01, 0.1, 1.0, 10.0, 100.0, 1000.0, 10000.0, 100000.0, 1000000.0, 10000000.0, 100000000.0])
        ax11.set_yticklabels(['$10^{-3}$', '$10^{-2}$', '$10^{-1}$', '$1$', '$10$', '$10^2$', '$10^3$', '$10^4$', '$10^5$', '$10^6$', '$10^7$', '$10^8$'])
        locmin = matplotlib.ticker.LogLocator(base=10.0, subs=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9), numticks=12)
        ax11.yaxis.set_minor_locator(locmin)
        ax11.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax11.yaxis.set_tick_params(which='minor', length=5)
        ax11.xaxis.set_tick_params(which='major', length=10)
        ax11.xaxis.set_tick_params(which='minor', length=5)
        ax11.set_xticks([2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0])
        xtick_labels = [2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0]
        ax11.set_xticklabels(['$%0.0f$' % x for x in xtick_labels])
        ax11.text(0.05, 0.05, '\\boldmath$F_2^p$', transform=ax11.transAxes, size=60)
        ax11.text(0.15, 0.05, '$(\\times\\, 2^{\\, i})$', transform=ax11.transAxes, size=40)
        ax11.set_xlabel('\\boldmath$Q^2$' + '  ' + '\\textbf{\\textrm{(GeV}}' + '\\boldmath$^2)$', size=40)
        ax11.yaxis.set_ticks_position('both')
        ax11.xaxis.set_ticks_position('both')
        ax11.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax11.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        handles = [hand.get('BCDMS'), hand.get('NMC'), hand.get('SLAC'), (thy_band, thy_plot)]
        label1 = '\\textbf{\\textrm{BCDMS}}'
        label2 = '\\textbf{\\textrm{NMC}}'
        label3 = '\\textbf{\\textrm{SLAC}}'
        label4 = '\\textbf{\\textrm{JAM}}'
        labels = [label1, label2, label3, label4]
        thesis_legend(ax11, handles, labels, loc='upper right', fontsize=35, frameon=0, handletextpad=0.3, handlelength=1.0)
    if any((i in data for i in [10003])):
        E = 5.5
        Mp = 0.939
        theta = lambda X, Q2, M: np.arcsin(np.sqrt(Q2 / (4 * E ** 2 - 2 * E * Q2 / M / X))) * 2 * 180 / np.pi
        data[10003]['theta'] = theta(data[10003]['X'], data[10003]['Q2'], Mp)
        nbins = 6
        DATA = {}
        if 10003 in data:
            DATA['JLab p'] = pd.DataFrame(data[10003])
        theory = {}
        PLOT = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_idis_get_thetabins(DATA[exp], 'JLab')
                PLOT[exp] = thesis_idis_get_plot(query)
                theory[exp] = thesis_idis_get_theory(PLOT[exp], nbins, loop=False)
        hand = {}
        for exp in PLOT:
            for key in range(nbins):
                Q2 = PLOT[exp]['Q2'][key]
                val = PLOT[exp]['value'][key] * 2.0 ** float(key)
                alpha = PLOT[exp]['alpha'][key] * 2.0 ** float(key)
                (color, marker, ms) = thesis_idis_get_details(exp)
                hand[exp] = ax12.errorbar(Q2, val, alpha, marker=marker, color=color, ms=ms, capsize=3, linestyle='none')
            for key in theory[exp]['value']:
                Q2 = theory[exp]['Q2'][key]
                mean = theory[exp]['value'][key] * 2.0 ** float(key)
                std_lower = theory[exp]['std_lower'][key] * 2.0 ** float(key)
                std_upper = theory[exp]['std_upper'][key] * 2.0 ** float(key)
                down = mean - std_lower
                up = mean + std_upper
                (thy_plot,) = ax12.plot(Q2, mean, linestyle='solid', color='black')
                thy_band = ax12.fill_between(Q2, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(Q2))
        if any((len(v['value'].get(5, [])) for v in PLOT.values())):
            ax12.text(6.55, 0.9, '$\\theta=70^{\\circ} \\, (i=5)$', fontsize=30)
        if any((len(v['value'].get(4, [])) for v in PLOT.values())):
            ax12.text(6.2, 0.5, '$\\theta=60^{\\circ} $', fontsize=30)
        if any((len(v['value'].get(3, [])) for v in PLOT.values())):
            ax12.text(5.9, 0.28, '$\\theta=55^{\\circ} $', fontsize=30)
        if any((len(v['value'].get(2, [])) for v in PLOT.values())):
            ax12.text(5.2, 0.18, '$\\theta=45^{\\circ} $', fontsize=30)
        if any((len(v['value'].get(1, [])) for v in PLOT.values())):
            ax12.text(4.9, 0.1, '$\\theta=41^{\\circ} $', fontsize=30)
        if any((len(v['value'].get(0, [])) for v in PLOT.values())):
            ax12.text(4.6, 0.055, '$\\theta=38^{\\circ}  \\, (i=0)$', fontsize=30)
        ax12.semilogy()
        ax12.set_xlim(3.5, 7.5)
        ax12.set_ylim(0.04, 3.5)
        ax12.set_xticks([4, 5, 6, 7])
        ax12.set_xticklabels(['$4$', '$5$', '$6$', '$7$'])
        lo12cmin = matplotlib.ticker.LogLocator(base=10.0, subs=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9), numticks=12)
        ax12.yaxis.set_minor_locator(lo12cmin)
        ax12.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax12.yaxis.set_tick_params(which='minor', length=5)
        ax12.set_yticks([0.1, 1.0])
        ax12.set_yticklabels(['$10^{-1}$', '$10^{0}$'])
        ax12.tick_params(axis='both', labelsize=30)
        ax12.set_xlabel('\\boldmath$Q^2$' + '  ' + '\\textbf{\\textrm{(GeV}}' + '\\boldmath$^2)$', size=40)
        ax12.text(0.05, 0.7, '\\boldmath$F_2^p$', transform=ax12.transAxes, size=60)
        ax12.text(0.15, 0.7, '$(\\, \\times\\, 2^{\\, i})$', transform=ax12.transAxes, size=40)
        ax12.xaxis.set_tick_params(which='major', length=10)
        ax12.xaxis.set_tick_params(which='minor', length=5)
        ax12.yaxis.set_tick_params(which='major', length=10)
        ax12.yaxis.set_tick_params(which='minor', length=5)
        ax12.yaxis.set_ticks_position('both')
        ax12.xaxis.set_ticks_position('both')
        ax12.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax12.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        minorLocator = MultipleLocator(0.2)
        ax12.xaxis.set_minor_locator(minorLocator)
        handles = [hand.get('JLab p')]
        label1 = '\\textbf{\\textrm{Hall C}}'
        labels = [label1]
        thesis_legend(ax12, handles, labels, loc='upper right', fontsize=35, frameon=0, handletextpad=0.3, handlelength=1.0)
    if any((i in data for i in [10026, 10030])):
        nbins = 25
        DATA = {}
        if 10026 in data:
            DATA['HERA 10026'] = pd.DataFrame(data[10026])
        if 10030 in data:
            DATA['HERA 10030'] = pd.DataFrame(data[10030])
        theory = {}
        PLOT = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_idis_get_xbins(DATA[exp], 'HERA NC')
                PLOT[exp] = thesis_idis_get_plot(query)
                theory[exp] = thesis_idis_get_theory(PLOT[exp], nbins, loop=False)
        hand = {}
        for exp in PLOT:
            for key in range(nbins):
                Q2 = PLOT[exp]['Q2'][key]
                val = PLOT[exp]['value'][key] * 2.0 ** float(key)
                alpha = PLOT[exp]['alpha'][key] * 2.0 ** float(key)
                (color, marker, ms) = thesis_idis_get_details(exp)
                hand[exp] = ax21.errorbar(Q2, val, alpha, marker=marker, color=color, ms=ms, capsize=3, linestyle='none')
            for key in theory[exp]['value']:
                Q2 = theory[exp]['Q2'][key]
                mean = theory[exp]['value'][key] * 2.0 ** float(key)
                std_lower = theory[exp]['std_lower'][key] * 2.0 ** float(key)
                std_upper = theory[exp]['std_upper'][key] * 2.0 ** float(key)
                down = mean - std_lower
                up = mean + std_upper
                (thy_plot,) = ax21.plot(Q2, mean, linestyle='solid', color='black')
                thy_band = ax21.fill_between(Q2, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(Q2))
        if any((len(v['value'].get(24, [])) for v in PLOT.values())):
            ax21.text(3.4, 16500000.0, '$x=2.8\\cdot 10^{-5}\\, (i=24)$', fontsize=22)
        if any((len(v['value'].get(23, [])) for v in PLOT.values())):
            ax21.text(5.5, 8650000.0, '$x=4.6\\cdot 10^{-5}$', fontsize=22)
        if any((len(v['value'].get(22, [])) for v in PLOT.values())):
            ax21.text(8.0, 4600000.0, '$x=7.3\\cdot 10^{-5}$', fontsize=22)
        if any((len(v['value'].get(21, [])) for v in PLOT.values())):
            ax21.text(15.0, 2700000.0, '$x=1.2\\cdot 10^{-4}$', fontsize=22)
        if any((len(v['value'].get(20, [])) for v in PLOT.values())):
            ax21.text(22.0, 1400000.0, '$x=1.9\\cdot 10^{-4}$', fontsize=22)
        if any((len(v['value'].get(19, [])) for v in PLOT.values())):
            ax21.text(35.0, 750000.0, '$x=3.1\\cdot 10^{-4}$', fontsize=22)
        if any((len(v['value'].get(18, [])) for v in PLOT.values())):
            ax21.text(45.0, 390000.0, '$x=4.4\\cdot 10^{-4}$', fontsize=22)
        if any((len(v['value'].get(17, [])) for v in PLOT.values())):
            ax21.text(60.0, 180000.0, '$x=6.2\\cdot 10^{-4}$', fontsize=22)
        if any((len(v['value'].get(16, [])) for v in PLOT.values())):
            ax21.text(80.0, 85000.0, '$x=7.7\\cdot 10^{-4}$', fontsize=22)
        if any((len(v['value'].get(15, [])) for v in PLOT.values())):
            ax21.text(86.0, 40000.0, '$x=9.1 \\cdot 10^{-4}$', fontsize=22)
        if any((len(v['value'].get(14, [])) for v in PLOT.values())):
            ax21.text(160.0, 20000.0, '$x=0.0014$', fontsize=22)
        if any((len(v['value'].get(13, [])) for v in PLOT.values())):
            ax21.text(260.0, 10000.0, '$x=0.0024$', fontsize=22)
        if any((len(v['value'].get(12, [])) for v in PLOT.values())):
            ax21.text(440.0, 4500, '$x=0.0035$', fontsize=22)
        if any((len(v['value'].get(11, [])) for v in PLOT.values())):
            ax21.text(740.0, 2000, '$x=0.0056$', fontsize=22)
        if any((len(v['value'].get(10, [])) for v in PLOT.values())):
            ax21.text(1500.0, 970, '$x=0.0083$', fontsize=22)
        if any((len(v['value'].get(9, [])) for v in PLOT.values())):
            ax21.text(1700.0, 410, '$x=0.013$', fontsize=22)
        if any((len(v['value'].get(8, [])) for v in PLOT.values())):
            ax21.text(2700.0, 190, '$x=0.021$', fontsize=22)
        if any((len(v['value'].get(7, [])) for v in PLOT.values())):
            ax21.text(3900.0, 77, '$x=0.032$', fontsize=22)
        if any((len(v['value'].get(6, [])) for v in PLOT.values())):
            ax21.text(6500.0, 35, '$x=0.052$', fontsize=22)
        if any((len(v['value'].get(5, [])) for v in PLOT.values())):
            ax21.text(11500.0, 19, '$x=0.084$', fontsize=22)
        if any((len(v['value'].get(4, [])) for v in PLOT.values())):
            ax21.text(12.0, 5.5, '$x=0.13$', fontsize=22)
        if any((len(v['value'].get(3, [])) for v in PLOT.values())):
            ax21.text(18.0, 2.25, '$x=0.18$', fontsize=22)
        if any((len(v['value'].get(2, [])) for v in PLOT.values())):
            ax21.text(36.0, 0.97, '$x=0.25$', fontsize=22)
        if any((len(v['value'].get(1, [])) for v in PLOT.values())):
            ax21.text(43.0, 0.25, '$x=0.40$', fontsize=22)
        if any((len(v['value'].get(0, [])) for v in PLOT.values())):
            ax21.text(30.0, 0.02, '$x=0.65 \\, (i=0)$', fontsize=22)
        ax21.semilogy()
        ax21.semilogx()
        ax21.set_xlim(1.0, 200000.0)
        ax21.set_ylim(0.004, 40000000.0)
        ax21.set_xticks([1.0, 10.0, 100.0, 1000.0, 10000.0, 100000.0])
        ax21.set_xticklabels(['$1$', '$10$', '$10^2$', '$10^3$', '$10^4$', '$10^5$'])
        locmin = matplotlib.ticker.LogLocator(base=10.0, subs=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9), numticks=12)
        ax21.yaxis.set_minor_locator(locmin)
        ax21.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax21.yaxis.set_tick_params(which='minor', length=5)
        ax21.set_yticks([0.01, 0.1, 1.0, 10.0, 100.0, 1000.0, 10000.0, 100000.0, 1000000.0, 10000000.0])
        ax21.set_yticklabels(['$10^{-2}$', '$10^{-1}$', '$1$', '$10$', '$10^2$', '$10^3$', '$10^4$', '$10^5$', '$10^6$', '$10^7$'])
        ax21.tick_params(axis='both', labelsize=30, pad=10)
        ax21.set_xlabel('\\boldmath$Q^2$' + '  ' + '\\textbf{\\textrm{(GeV}}' + '\\boldmath$^2)$', size=40)
        ax21.text(0.7, 0.75, '\\textrm{\\textbf{HERA}}', transform=ax21.transAxes, size=50)
        ax21.text(0.05, 0.12, '\\boldmath$\\sigma_{\\rm red}^{\\rm NC}$', transform=ax21.transAxes, size=60)
        ax21.text(0.05, 0.04, '$(\\, \\times\\, 2^{\\, i})$', transform=ax21.transAxes, size=40)
        ax21.text(2000.0, 30000.0, '$\\sqrt{s}=318 \\,\\rm{GeV}$', fontsize=40)
        ax21.xaxis.set_tick_params(which='major', length=10)
        ax21.xaxis.set_tick_params(which='minor', length=5)
        ax21.yaxis.set_tick_params(which='major', length=10)
        ax21.yaxis.set_tick_params(which='minor', length=5)
        ax21.yaxis.set_ticks_position('both')
        ax21.xaxis.set_ticks_position('both')
        ax21.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax21.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        handles = [hand.get('HERA 10026'), hand.get('HERA 10030')]
        label1 = '\\boldmath$e^+p$'
        label2 = '\\boldmath$e^-p$'
        labels = [label1, label2]
        thesis_legend(ax21, handles, labels, loc='upper right', fontsize=35, frameon=0, handletextpad=0.3, handlelength=1.0)
    if any((i in data for i in [10027, 10028, 10029])):
        nbins = 30
        DATA = {}
        if 10027 in data:
            DATA['HERA 10027'] = pd.DataFrame(data[10027])
        if 10028 in data:
            DATA['HERA 10028'] = pd.DataFrame(data[10028])
        if 10029 in data:
            DATA['HERA 10029'] = pd.DataFrame(data[10029])
        theory = {}
        PLOT = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_idis_get_xbins(DATA[exp], 'HERA other')
                PLOT[exp] = thesis_idis_get_plot(query)
                theory[exp] = thesis_idis_get_theory(PLOT[exp], nbins, loop=False)
        hand = {}
        for exp in PLOT:
            for key in range(nbins):
                Q2 = PLOT[exp]['Q2'][key]
                val = PLOT[exp]['value'][key] * 2.0 ** float(key)
                alpha = PLOT[exp]['alpha'][key] * 2.0 ** float(key)
                (color, marker, ms) = thesis_idis_get_details(exp)
                hand[exp] = ax22.errorbar(Q2, val, alpha, marker=marker, color=color, ms=ms, capsize=3, linestyle='none')
            for key in theory[exp]['value']:
                Q2 = theory[exp]['Q2'][key]
                mean = theory[exp]['value'][key] * 2.0 ** float(key)
                std_lower = theory[exp]['std_lower'][key] * 2.0 ** float(key)
                std_upper = theory[exp]['std_upper'][key] * 2.0 ** float(key)
                down = mean - std_lower
                up = mean + std_upper
                (thy_plot,) = ax22.plot(Q2, mean, linestyle='solid', color=color)
                thy_band = ax22.fill_between(Q2, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(Q2))
        if any((len(v['value'].get(29, [])) for v in PLOT.values())):
            ax22.text(2.5, 450000000.0, '$x=3.3 \\cdot 10^{-5}\\, (i=29)$', fontsize=20)
        if any((len(v['value'].get(28, [])) for v in PLOT.values())):
            ax22.text(5.7, 250000000.0, '$x=5.9 \\cdot 10^{-5}$', fontsize=20)
        if any((len(v['value'].get(27, [])) for v in PLOT.values())):
            ax22.text(19.0, 170000000.0, '$x=1.6 \\cdot 10^{-4}$', fontsize=20)
        if any((len(v['value'].get(26, [])) for v in PLOT.values())):
            ax22.text(35.0, 80000000.0, '$x=3.1 \\cdot 10^{-4}$', fontsize=20)
        if any((len(v['value'].get(25, [])) for v in PLOT.values())):
            ax22.text(35.0, 44000000.0, '$x=4.5 \\cdot 10^{-4}$', fontsize=20)
        if any((len(v['value'].get(24, [])) for v in PLOT.values())):
            ax22.text(45.0, 21000000.0, '$x=6.2 \\cdot 10^{-4}$', fontsize=20)
        if any((len(v['value'].get(23, [])) for v in PLOT.values())):
            ax22.text(57.0, 11000000.0, '$x=7.8 \\cdot 10^{-4}$', fontsize=20)
        if any((len(v['value'].get(22, [])) for v in PLOT.values())):
            ax22.text(50.0, 5000000.0, '$x=9.1 \\cdot 10^{-4}$', fontsize=20)
        if any((len(v['value'].get(21, [])) for v in PLOT.values())):
            ax22.text(90.0, 2700000.0, '$x=0.0011$', fontsize=20)
        if any((len(v['value'].get(20, [])) for v in PLOT.values())):
            ax22.text(100.0, 1150000.0, '$x=0.0015$', fontsize=20)
        if any((len(v['value'].get(19, [])) for v in PLOT.values())):
            ax22.text(170.0, 600000.0, '$x=0.0022$', fontsize=20)
        if any((len(v['value'].get(18, [])) for v in PLOT.values())):
            ax22.text(250.0, 290000.0, '$x=0.0034$', fontsize=20)
        if any((len(v['value'].get(17, [])) for v in PLOT.values())):
            ax22.text(370.0, 120000.0, '$x=0.0046$', fontsize=20)
        if any((len(v['value'].get(16, [])) for v in PLOT.values())):
            ax22.text(450.0, 56000.0, '$x=0.0064$', fontsize=20)
        if any((len(v['value'].get(15, [])) for v in PLOT.values())):
            ax22.text(800.0, 28000.0, '$x=0.0085$', fontsize=20)
        if any((len(v['value'].get(14, [])) for v in PLOT.values())):
            ax22.text(600.0, 12000.0, '$x=0.0094$', fontsize=20)
        if any((len(v['value'].get(13, [])) for v in PLOT.values())):
            ax22.text(800.0, 6200.0, '$x=0.011$', fontsize=20)
        if any((len(v['value'].get(12, [])) for v in PLOT.values())):
            ax22.text(1500.0, 3200.0, '$x=0.0138$', fontsize=20)
        if any((len(v['value'].get(11, [])) for v in PLOT.values())):
            ax22.text(1000.0, 1400.0, '$x=0.0156$', fontsize=20)
        if any((len(v['value'].get(10, [])) for v in PLOT.values())):
            ax22.text(1000.0, 700.0, '$x=0.0183$', fontsize=20)
        if any((len(v['value'].get(9, [])) for v in PLOT.values())):
            ax22.text(1900.0, 320.0, '$x=0.020$', fontsize=20)
        if any((len(v['value'].get(8, [])) for v in PLOT.values())):
            ax22.text(1100.0, 150.0, '$x=0.025$', fontsize=20)
        if any((len(v['value'].get(7, [])) for v in PLOT.values())):
            ax22.text(2500.0, 67.0, '$x=0.032$', fontsize=20)
        if any((len(v['value'].get(6, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10028', 'HERA 10029']))):
            ax22.text(60.0, 28.0, '$x=0.05$', fontsize=20)
        if any((len(v['value'].get(5, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10028', 'HERA 10029']))):
            ax22.text(60.0, 12.0, '$x=0.08$', fontsize=20)
        if any((len(v['value'].get(4, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10028', 'HERA 10029']))):
            ax22.text(60.0, 5.3, '$x=0.13$', fontsize=20)
        if any((len(v['value'].get(3, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10028', 'HERA 10029']))):
            ax22.text(60.0, 2.15, '$x=0.18$', fontsize=20)
        if any((len(v['value'].get(2, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10028', 'HERA 10029']))):
            ax22.text(160.0, 0.85, '$x=0.25$', fontsize=20)
        if any((len(v['value'].get(1, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10028', 'HERA 10029']))):
            ax22.text(60.0, 0.3, '$x=0.40$', fontsize=20)
        if any((len(v['value'].get(0, [])) for v in PLOT.values())):
            ax22.text(70.0, 0.022, '$x=0.65 \\, (i=0)$', fontsize=20)
        if any((len(v['value'].get(6, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10027']))):
            ax22.text(4000.0, 28.0, '$x=0.05$', fontsize=20, color='darkgreen')
        if any((len(v['value'].get(5, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10027']))):
            ax22.text(6000.0, 11.0, '$x=0.08$', fontsize=20, color='darkgreen')
        if any((len(v['value'].get(4, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10027']))):
            ax22.text(1600.0, 4.6, '$x=0.13$', fontsize=20, color='darkgreen')
        if any((len(v['value'].get(3, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10027']))):
            ax22.text(2300.0, 2.0, '$x=0.18$', fontsize=20, color='darkgreen')
        if any((len(v['value'].get(2, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10027']))):
            ax22.text(2800.0, 0.8, '$x=0.25$', fontsize=20, color='darkgreen')
        if any((len(v['value'].get(1, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10027']))):
            ax22.text(3000.0, 0.2, '$x=0.40\\, (i=1)$', fontsize=20, color='darkgreen')
        ax22.semilogy()
        ax22.semilogx()
        ax22.set_xlim(1.0, 40000.0)
        ax22.set_ylim(0.01, 2000000000.0)
        ax22.set_xlabel('\\boldmath$Q^2$' + '  ' + '\\textbf{\\textrm{(GeV}}' + '\\boldmath$^2)$', size=40)
        ax22.text(0.62, 0.75, '\\textrm{\\textbf{HERA}}' + ' ' + '\\boldmath$e^-p$', transform=ax22.transAxes, size=50)
        ax22.text(0.05, 0.12, '\\boldmath$\\sigma_{\\rm red}^{\\rm NC}$', transform=ax22.transAxes, size=60)
        ax22.text(0.05, 0.04, '$(\\, \\times\\, 2^{\\, i})$', transform=ax22.transAxes, size=40)
        ax22.set_xticks([1.0, 10.0, 100.0, 1000.0, 10000.0])
        ax22.set_xticklabels(['$1$', '$10$', '$10^2$', '$10^3$', '$10^4$'])
        locmin = matplotlib.ticker.LogLocator(base=10.0, subs=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9), numticks=12)
        ax22.yaxis.set_minor_locator(locmin)
        ax22.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax22.yaxis.set_tick_params(which='minor', length=3)
        ax22.set_yticks([0.01, 0.1, 1.0, 10.0, 100.0, 1000.0, 10000.0, 100000.0, 1000000.0, 10000000.0, 100000000.0, 1000000000.0])
        ax22.set_yticklabels(['$10^{-2}$', '$10^{-1}$', '$1$', '$10$', '$10^2$', '$10^3$', '$10^4$', '$10^5$', '$10^6$', '$10^7$', '$10^8$', '$10^9$'])
        ax22.yaxis.set_ticks_position('both')
        ax22.xaxis.set_ticks_position('both')
        ax22.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax22.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        ax22.xaxis.set_tick_params(which='major', length=10)
        ax22.xaxis.set_tick_params(which='minor', length=5)
        ax22.yaxis.set_tick_params(which='major', length=10)
        ax22.yaxis.set_tick_params(which='minor', length=5)
        ax22.yaxis.set_ticks_position('both')
        ax22.xaxis.set_ticks_position('both')
        ax22.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax22.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        ax22.tick_params(axis='both', labelsize=30, pad=10)
        handles = [hand.get('HERA 10027'), hand.get('HERA 10028'), hand.get('HERA 10029')]
        label1 = '\\boldmath$\\sqrt{s}=300$' + ' ' + '\\textrm{\\textbf{GeV}}'
        label2 = '\\boldmath$\\sqrt{s}=251$' + ' ' + '\\textrm{\\textbf{GeV}}'
        label3 = '\\boldmath$\\sqrt{s}=225$' + ' ' + '\\textrm{\\textbf{GeV}}'
        labels = [label1, label2, label3]
        thesis_legend(ax22, handles, labels, loc='upper right', fontsize=30, frameon=0, handletextpad=0.3, handlelength=1.0)
    py.tight_layout()
    py.subplots_adjust(right=0.99)
    filename = output_file
    ax11.set_rasterized(True)
    ax12.set_rasterized(True)
    ax21.set_rasterized(True)
    ax22.set_rasterized(True)
    thesis_finish_panels(py.gcf(), 'thesis_idis_plot_proton', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/idis.py:plot_deuteron, original lines 779-1186; SHA256 92dc40105aa2214d4b8ea863286aad4601f0078010c0f2312ba5a1aabbb3ddcf.
def thesis_idis_plot_deuteron(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['idis']
    tabs = data
    (nrows, ncols) = (2, 2)
    py.figure(figsize=(ncols * 12, nrows * 14))
    ax11 = py.subplot(nrows, ncols, 1)
    ax12 = py.subplot(nrows, ncols, 2)
    ax21 = py.subplot(nrows, ncols, 3)
    ax22 = py.subplot(nrows, ncols, 4)
    if any((i in data for i in [10011, 10017])):
        nbins = 19
        DATA = {}
        if 10011 in data:
            DATA['SLAC'] = pd.DataFrame(data[10011])
        if 10017 in data:
            DATA['BCDMS'] = pd.DataFrame(data[10017])
        PLOT = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_idis_get_xbins(DATA[exp], 'd')
                PLOT[exp] = thesis_idis_get_plot(query)
            theory = thesis_idis_get_theory(PLOT, nbins)
        hand = {}
        for exp in PLOT:
            for key in range(nbins):
                Q2 = PLOT[exp]['Q2'][key]
                val = PLOT[exp]['value'][key] * 2.0 ** float(key)
                alpha = PLOT[exp]['alpha'][key] * 2.0 ** float(key)
                (color, marker, ms) = thesis_idis_get_details(exp)
                hand[exp] = ax11.errorbar(Q2, val, alpha, marker=marker, color=color, ms=ms, capsize=3, linestyle='none')
        for key in theory['value']:
            Q2 = theory['Q2'][key]
            mean = theory['value'][key] * 2.0 ** float(key)
            std_lower = theory['std_lower'][key] * 2.0 ** float(key)
            std_upper = theory['std_upper'][key] * 2.0 ** float(key)
            down = mean - std_lower
            up = mean + std_upper
            (thy_plot,) = ax11.plot(Q2, mean, linestyle='solid', color='black')
            thy_band = ax11.fill_between(Q2, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(Q2))
        if any((len(v['value'].get(18, [])) for v in PLOT.values())):
            ax11.text(21.0, 90000.0, '$x=0.07 \\,(i=15)$', fontsize=26)
        if any((len(v['value'].get(17, [])) for v in PLOT.values())):
            ax11.text(3.0, 40000.0, '$x=0.09$', fontsize=26)
        if any((len(v['value'].get(16, [])) for v in PLOT.values())):
            ax11.text(45.0, 20000.0, '$x=0.10$', fontsize=26)
        if any((len(v['value'].get(15, [])) for v in PLOT.values())):
            ax11.text(70.0, 10000.0, '$x=0.14$', fontsize=26)
        if any((len(v['value'].get(14, [])) for v in PLOT.values())):
            ax11.text(75.0, 4500.0, '$x=0.18$', fontsize=26)
        if any((len(v['value'].get(13, [])) for v in PLOT.values())):
            ax11.text(100.0, 2000.0, '$x=0.23$', fontsize=26)
        if any((len(v['value'].get(12, [])) for v in PLOT.values())):
            ax11.text(140.0, 900.0, '$x=0.28$', fontsize=26)
        if any((len(v['value'].get(11, [])) for v in PLOT.values())):
            ax11.text(170.0, 300.0, '$x=0.35$', fontsize=26)
        if any((len(v['value'].get(10, [])) for v in PLOT.values())):
            ax11.text(190.0, 100.0, '$x=0.43$', fontsize=26)
        if any((len(v['value'].get(9, [])) for v in PLOT.values())):
            ax11.text(19.0, 58.0, '$x=0.46$', fontsize=26)
        if any((len(v['value'].get(8, [])) for v in PLOT.values())):
            ax11.text(17.0, 20.0, '$x=0.50$', fontsize=26)
        if any((len(v['value'].get(7, [])) for v in PLOT.values())):
            ax11.text(255.0, 4.0, '$x=0.53$', fontsize=26)
        if any((len(v['value'].get(6, [])) for v in PLOT.values())):
            ax11.text(22.0, 3.0, '$x=0.56$', fontsize=26)
        if any((len(v['value'].get(5, [])) for v in PLOT.values())):
            ax11.text(23.0, 1.5, '$x=0.59$', fontsize=26)
        if any((len(v['value'].get(4, [])) for v in PLOT.values())):
            ax11.text(25.0, 0.6, '$x=0.61$', fontsize=26)
        if any((len(v['value'].get(3, [])) for v in PLOT.values())):
            ax11.text(250.0, 0.14, '$x=0.65$', fontsize=26)
        if any((len(v['value'].get(2, [])) for v in PLOT.values())):
            ax11.text(250.0, 0.02, '$x=0.75$', fontsize=26)
        if any((len(v['value'].get(1, [])) for v in PLOT.values())):
            ax11.text(35.0, 0.005, '$x=0.85$', fontsize=26)
        if any((len(v['value'].get(0, [])) for v in PLOT.values())):
            ax11.text(35.0, 0.0015, '$x=0.88 \\,(i=0)$', fontsize=26)
        ax11.semilogy()
        ax11.semilogx()
        ax11.set_xlim(1.5, 1000.0)
        ax11.set_ylim(0.001, 200000.0)
        ax11.tick_params(axis='both', labelsize=30)
        ax11.yaxis.set_tick_params(which='major', length=10)
        ax11.yaxis.set_tick_params(which='minor', length=5)
        ax11.set_yticks([0.01, 0.1, 1.0, 10.0, 100.0, 1000.0, 10000.0, 100000.0])
        ax11.set_yticklabels(['$10^{-2}$', '$10^{-1}$', '$1$', '$10$', '$10^2$', '$10^3$', '$10^4$', '$10^5$'])
        locmin = matplotlib.ticker.LogLocator(base=10.0, subs=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9), numticks=12)
        ax11.yaxis.set_minor_locator(locmin)
        ax11.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax11.yaxis.set_tick_params(which='minor', length=5)
        ax11.xaxis.set_tick_params(which='major', length=10)
        ax11.xaxis.set_tick_params(which='minor', length=5)
        ax11.set_xticks([2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0])
        xticklabels = [2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0]
        ax11.set_xticklabels(['$%0.0f$' % x for x in xticklabels])
        ax11.set_xlabel('\\boldmath$Q^2$' + '  ' + '\\textbf{\\textrm{(GeV}}' + '\\boldmath$^2)$', size=40)
        ax11.text(0.05, 0.05, '\\boldmath$F_2^D$', transform=ax11.transAxes, size=60)
        ax11.text(0.17, 0.05, '$(\\times\\, 2^{\\, i})$', transform=ax11.transAxes, size=40)
        ax11.yaxis.set_ticks_position('both')
        ax11.xaxis.set_ticks_position('both')
        ax11.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax11.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        handles = [hand.get('BCDMS'), hand.get('SLAC')]
        label1 = '\\textbf{\\textrm{BCDMS}}'
        label2 = '\\textbf{\\textrm{SLAC}}'
        labels = [label1, label2]
        thesis_legend(ax11, handles, labels, loc='upper right', fontsize=35, frameon=0, handletextpad=0.3, handlelength=1.0)
    if any((i in data for i in [10002])):
        E = 5.5
        Mp = 0.939
        theta = lambda X, Q2, M: np.arcsin(np.sqrt(Q2 / (4 * E ** 2 - 2 * E * Q2 / M / X))) * 2 * 180 / np.pi
        data[10002]['theta'] = theta(data[10002]['X'], data[10002]['Q2'], Mp)
        nbins = 6
        DATA = {}
        if 10002 in data:
            DATA['JLab d'] = pd.DataFrame(data[10002])
        theory = {}
        PLOT = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_idis_get_thetabins(DATA[exp], 'JLab')
                PLOT[exp] = thesis_idis_get_plot(query)
                theory[exp] = thesis_idis_get_theory(PLOT[exp], nbins, loop=False)
        hand = {}
        for exp in PLOT:
            for key in range(nbins):
                Q2 = PLOT[exp]['Q2'][key]
                val = PLOT[exp]['value'][key] * 2.0 ** float(key)
                alpha = PLOT[exp]['alpha'][key] * 2.0 ** float(key)
                (color, marker, ms) = thesis_idis_get_details(exp)
                hand[exp] = ax12.errorbar(Q2, val, alpha, marker=marker, color=color, ms=ms, capsize=3, linestyle='none')
            for key in theory[exp]['value']:
                Q2 = theory[exp]['Q2'][key]
                mean = theory[exp]['value'][key] * 2.0 ** float(key)
                std_lower = theory[exp]['std_lower'][key] * 2.0 ** float(key)
                std_upper = theory[exp]['std_upper'][key] * 2.0 ** float(key)
                down = mean - std_lower
                up = mean + std_upper
                (thy_plot,) = ax12.plot(Q2, mean, linestyle='solid', color='black')
                thy_band = ax12.fill_between(Q2, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(Q2))
        if any((len(v['value'].get(5, [])) for v in PLOT.values())):
            ax12.text(6.55, 0.8, '$\\theta=70^{\\circ} \\, (i=5)$', fontsize=30)
        if any((len(v['value'].get(4, [])) for v in PLOT.values())):
            ax12.text(6.2, 0.45, '$\\theta=60^{\\circ} $', fontsize=30)
        if any((len(v['value'].get(3, [])) for v in PLOT.values())):
            ax12.text(5.9, 0.24, '$\\theta=55^{\\circ} $', fontsize=30)
        if any((len(v['value'].get(2, [])) for v in PLOT.values())):
            ax12.text(5.2, 0.16, '$\\theta=45^{\\circ} $', fontsize=30)
        if any((len(v['value'].get(1, [])) for v in PLOT.values())):
            ax12.text(4.9, 0.09, '$\\theta=41^{\\circ} $', fontsize=30)
        if any((len(v['value'].get(0, [])) for v in PLOT.values())):
            ax12.text(4.6, 0.05, '$\\theta=38^{\\circ}  \\, (i=0)$', fontsize=30)
        ax12.semilogy()
        ax12.set_xlim(3.5, 7.5)
        ax12.set_ylim(0.04, 3.5)
        ax12.set_xticks([4, 5, 6, 7])
        ax12.set_xticklabels(['$4$', '$5$', '$6$', '$7$'])
        locmin = matplotlib.ticker.LogLocator(base=10.0, subs=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9), numticks=12)
        ax12.yaxis.set_minor_locator(locmin)
        ax12.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax12.yaxis.set_tick_params(which='minor', length=5)
        ax12.set_yticks([0.1, 1.0])
        ax12.set_yticklabels(['$10^{-1}$', '$10^{0}$'])
        ax12.tick_params(axis='both', labelsize=30)
        ax12.set_xlabel('\\boldmath$Q^2$' + '  ' + '\\textbf{\\textrm{(GeV}}' + '\\boldmath$^2)$', size=40)
        ax12.text(0.05, 0.7, '\\boldmath$F_2^D$', transform=ax12.transAxes, size=60)
        ax12.text(0.17, 0.7, '$(\\, \\times\\, 2^{\\, i})$', transform=ax12.transAxes, size=40)
        ax12.xaxis.set_tick_params(which='major', length=10)
        ax12.xaxis.set_tick_params(which='minor', length=5)
        ax12.yaxis.set_tick_params(which='major', length=10)
        ax12.yaxis.set_tick_params(which='minor', length=5)
        ax12.yaxis.set_ticks_position('both')
        ax12.xaxis.set_ticks_position('both')
        ax12.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax12.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        minorLocator = MultipleLocator(0.2)
        ax12.xaxis.set_minor_locator(minorLocator)
        handles = [hand.get('JLab d'), (thy_band, thy_plot)]
        label1 = '\\textbf{\\textrm{Hall C}}'
        label2 = '\\textbf{\\textrm{JAM}}'
        labels = [label1, label2]
        thesis_legend(ax12, handles, labels, loc='upper right', fontsize=35, frameon=0, handletextpad=0.3, handlelength=1.0)
    if any((i in data for i in [10021, 10050])):
        nbins = 18
        DATA = {}
        if 10021 in data:
            DATA['NMC'] = pd.DataFrame(data[10021])
        if 10050 in data:
            DATA['MARATHON dp'] = pd.DataFrame(data[10050])
        PLOT = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_idis_get_xbins(DATA[exp], 'd/p')
                PLOT[exp] = thesis_idis_get_plot(query)
            theory = thesis_idis_get_theory(PLOT, nbins)
        hand = {}
        for exp in PLOT:
            for key in range(nbins):
                Q2 = PLOT[exp]['Q2'][key]
                val = PLOT[exp]['value'][key] * 2.0 ** float(key)
                alpha = PLOT[exp]['alpha'][key] * 2.0 ** float(key)
                (color, marker, ms) = thesis_idis_get_details(exp)
                hand[exp] = ax21.errorbar(Q2, val, alpha, marker=marker, color=color, ms=ms, capsize=3, linestyle='none')
        for key in theory['value']:
            Q2 = theory['Q2'][key]
            mean = theory['value'][key] * 2.0 ** float(key)
            std_lower = theory['std_lower'][key] * 2.0 ** float(key)
            std_upper = theory['std_upper'][key] * 2.0 ** float(key)
            down = mean - std_lower
            up = mean + std_upper
            (thy_plot,) = ax21.plot(Q2, mean, linestyle='solid', color='black')
            thy_band = ax21.fill_between(Q2, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(Q2))
        if any((len(v['value'].get(17, [])) for v in PLOT.values())):
            ax21.text(2.0, 120000.0, '$x=0.005 \\, (i=17)$', fontsize=26)
        if any((len(v['value'].get(16, [])) for v in PLOT.values())):
            ax21.text(4.0, 63000.0, '$x=0.008$', fontsize=26)
        if any((len(v['value'].get(15, [])) for v in PLOT.values())):
            ax21.text(6.3, 31000.0, '$x=0.013$', fontsize=26)
        if any((len(v['value'].get(14, [])) for v in PLOT.values())):
            ax21.text(8.2, 15000.0, '$x=0.018$', fontsize=26)
        if any((len(v['value'].get(13, [])) for v in PLOT.values())):
            ax21.text(10.5, 7800.0, '$x=0.025$', fontsize=26)
        if any((len(v['value'].get(12, [])) for v in PLOT.values())):
            ax21.text(16.5, 3600.0, '$x=0.035$', fontsize=26)
        if any((len(v['value'].get(11, [])) for v in PLOT.values())):
            ax21.text(22.0, 1900.0, '$x=0.05$', fontsize=26)
        if any((len(v['value'].get(10, [])) for v in PLOT.values())):
            ax21.text(30.0, 900.0, '$x=0.07$', fontsize=26)
        if any((len(v['value'].get(9, [])) for v in PLOT.values())):
            ax21.text(40, 470.0, '$x=0.09$', fontsize=26)
        if any((len(v['value'].get(8, [])) for v in PLOT.values())):
            ax21.text(52.0, 220.0, '$x=0.11$', fontsize=26)
        if any((len(v['value'].get(7, [])) for v in PLOT.values())):
            ax21.text(56.0, 110.0, '$x=0.14$', fontsize=26)
        if any((len(v['value'].get(6, [])) for v in PLOT.values())):
            ax21.text(74.0, 54.0, '$x=0.18$', fontsize=26)
        if any((len(v['value'].get(5, [])) for v in PLOT.values())):
            ax21.text(75.0, 25.0, '$x=0.23$', fontsize=26)
        if any((len(v['value'].get(4, [])) for v in PLOT.values())):
            ax21.text(108, 12.2, '$x=0.28$', fontsize=26)
        if any((len(v['value'].get(3, [])) for v in PLOT.values())):
            ax21.text(110, 6.0, '$x=0.35$', fontsize=26)
        if any((len(v['value'].get(2, [])) for v in PLOT.values())):
            ax21.text(110, 2.9, '$x=0.45$', fontsize=26)
        if any((len(v['value'].get(1, [])) for v in PLOT.values())):
            ax21.text(110, 1.35, '$x=0.55$', fontsize=26)
        if any((len(v['value'].get(0, [])) for v in PLOT.values())):
            ax21.text(110, 0.7, '$x=0.68 \\,(i=0)$', fontsize=26)
        ax21.semilogy()
        ax21.semilogx()
        ax21.set_xlim(1.5, 385.0)
        ax21.set_ylim(0.5, 200000.0)
        ax21.tick_params(axis='both', labelsize=30)
        ax21.yaxis.set_tick_params(which='major', length=10)
        ax21.yaxis.set_tick_params(which='minor', length=5)
        ax21.set_yticks([1.0, 10.0, 100.0, 1000.0, 10000.0, 100000.0])
        ax21.set_yticklabels(['$1$', '$10$', '$10^2$', '$10^3$', '$10^4$', '$10^5$'])
        locmin = matplotlib.ticker.LogLocator(base=10.0, subs=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9), numticks=12)
        ax21.yaxis.set_minor_locator(locmin)
        ax21.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax21.yaxis.set_tick_params(which='minor', length=5)
        ax21.xaxis.set_tick_params(which='major', length=10)
        ax21.xaxis.set_tick_params(which='minor', length=5)
        ax21.set_xticks([2.0, 5.0, 10.0, 20.0, 50.0, 100.0])
        xticklabels = [2.0, 5.0, 10.0, 20.0, 50.0, 100.0]
        ax21.set_xticklabels(['$%0.0f$' % x for x in xticklabels])
        ax21.set_xlabel('\\boldmath$Q^2$' + '  ' + '\\textbf{\\textrm{(GeV}}' + '\\boldmath$^2)$', size=40)
        ax21.text(0.57, 0.78, '\\boldmath$F_2^D/F_2^p$', transform=ax21.transAxes, size=60)
        ax21.text(0.84, 0.78, '$(\\, \\times\\, 2^{\\, i})$', transform=ax21.transAxes, size=40)
        ax21.yaxis.set_ticks_position('both')
        ax21.xaxis.set_ticks_position('both')
        ax21.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax21.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        handles = [hand.get('NMC'), hand.get('MARATHON dp')]
        label1 = '\\textbf{\\textrm{NMC}}'
        label2 = '\\textbf{\\textrm{MARATHON}}'
        labels = [label1, label2]
        thesis_legend(ax21, handles, labels, loc='upper right', fontsize=35, frameon=0, handletextpad=0.3, handlelength=1.0)
    if any((i in data for i in [10033])):
        nbins = 6
        DATA = {}
        if 10033 in data:
            DATA['BONuS'] = pd.DataFrame(data[10033])
        PLOT = {}
        theory = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_idis_get_Q2bins(DATA[exp], 'BONuS')
                PLOT[exp] = thesis_idis_get_plot(query)
                theory[exp] = thesis_idis_get_theory(PLOT[exp], nbins, loop=False, funcX=True)
        hand = {}
        for exp in PLOT:
            for key in range(nbins):
                X = PLOT[exp]['X'][key]
                val = PLOT[exp]['value'][key] * 4.0 ** float(key)
                alpha = PLOT[exp]['alpha'][key] * 4.0 ** float(key)
                (color, marker, ms) = thesis_idis_get_details(exp)
                hand[exp] = ax22.errorbar(X, val, alpha, marker=marker, color=color, ms=ms, capsize=3, linestyle='none')
            for key in theory[exp]['value']:
                X = theory[exp]['X'][key]
                mean = theory[exp]['value'][key] * 4.0 ** float(key)
                std_lower = theory[exp]['std_lower'][key] * 4.0 ** float(key)
                std_upper = theory[exp]['std_upper'][key] * 4.0 ** float(key)
                down = mean - std_lower
                up = mean + std_upper
                (thy_plot,) = ax22.plot(X, mean, linestyle='solid', color=color)
                thy_band = ax22.fill_between(X, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(X))
        if any((len(v['value'].get(5, [])) for v in PLOT.values())):
            ax22.text(0.47, 0.05, '$Q^2 = 4.0 $' + '$\\, (i=5)$', transform=ax22.transAxes, fontsize=30)
        if any((len(v['value'].get(4, [])) for v in PLOT.values())):
            ax22.text(0.55, 0.2, '$Q^2 = 3.4 $', transform=ax22.transAxes, fontsize=30)
        if any((len(v['value'].get(3, [])) for v in PLOT.values())):
            ax22.text(0.62, 0.35, '$Q^2 = 2.9 $', transform=ax22.transAxes, fontsize=30)
        if any((len(v['value'].get(2, [])) for v in PLOT.values())):
            ax22.text(0.74, 0.5, '$Q^2 = 2.4 $', transform=ax22.transAxes, fontsize=30)
        if any((len(v['value'].get(1, [])) for v in PLOT.values())):
            ax22.text(0.78, 0.64, '$Q^2 = 2.0 $', transform=ax22.transAxes, fontsize=30)
        if any((len(v['value'].get(0, [])) for v in PLOT.values())):
            ax22.text(0.3, 0.8, '$Q^2 = 1.7 \\: \\rm{GeV^2}$' + '$\\, (i=0)$', transform=ax22.transAxes, fontsize=30)
        ax22.semilogy()
        ax22.set_xlim(0.2, 0.75)
        ax22.set_ylim(0.5, 4000.0)
        ax22.set_xticks([0.2, 0.3, 0.4, 0.5, 0.6, 0.7])
        ax22.set_xticklabels(['$0.2$', '$0.3$', '$0.4$', '$0.5$', '$0.6$', '$0.7$'])
        ax22.set_yticks([1.0, 10.0, 100.0, 1000.0])
        ax22.set_yticklabels(['$1$', '$10$', '$10^2$', '$10^3$'])
        ax22.tick_params(axis='both', labelsize=30)
        ax22.set_xlabel('\\boldmath$x$', size=40)
        ax22.text(0.05, 0.9, '\\boldmath$F_2^n/F_2^D$', transform=ax22.transAxes, size=60)
        ax22.text(0.32, 0.9, '$(\\, \\times\\, 4^{\\, i})$', transform=ax22.transAxes, size=40)
        ax22.xaxis.set_tick_params(which='major', length=10)
        ax22.xaxis.set_tick_params(which='minor', length=5)
        ax22.yaxis.set_tick_params(which='major', length=10)
        ax22.yaxis.set_tick_params(which='minor', length=5)
        ax22.yaxis.set_ticks_position('both')
        ax22.xaxis.set_ticks_position('both')
        ax22.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax22.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        minorLocator = MultipleLocator(0.02)
        ax22.xaxis.set_minor_locator(minorLocator)
        handles = [hand.get('BONuS')]
        label1 = '\\textbf{\\textrm{BONuS}}'
        labels = [label1]
        thesis_legend(ax22, handles, labels, loc='upper right', fontsize=35, frameon=0, handletextpad=0.1, handlelength=1.0)
    py.tight_layout()
    filename = output_file
    ax11.set_rasterized(True)
    ax12.set_rasterized(True)
    ax21.set_rasterized(True)
    ax22.set_rasterized(True)
    thesis_finish_panels(py.gcf(), 'thesis_idis_plot_deuteron', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/idis.py:plot_CC, original lines 1188-1301; SHA256 92dc40105aa2214d4b8ea863286aad4601f0078010c0f2312ba5a1aabbb3ddcf.
def thesis_idis_plot_CC(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['idis']
    tabs = data
    (nrows, ncols) = (1, 1)
    fig = py.figure(figsize=(ncols * 12, nrows * 14))
    ax = {}
    ax = fig.add_subplot(nrows, ncols, 1)
    if any((i in data for i in [10031, 10032])):
        nbins = 8
        DATA = {}
        if 10031 in data:
            DATA['HERA 10031'] = pd.DataFrame(data[10031])
        if 10032 in data:
            DATA['HERA 10032'] = pd.DataFrame(data[10032])
        PLOT = {}
        theory = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_idis_get_xbins(DATA[exp], 'HERA CC')
                PLOT[exp] = thesis_idis_get_plot(query)
                theory[exp] = thesis_idis_get_theory(PLOT[exp], nbins, loop=False)
        hand = {}
        for exp in PLOT:
            for key in range(nbins):
                Q2 = PLOT[exp]['Q2'][key]
                val = PLOT[exp]['value'][key] * 5.0 ** float(key)
                alpha = PLOT[exp]['alpha'][key] * 5.0 ** float(key)
                (color, marker, ms) = thesis_idis_get_details(exp)
                hand[exp] = ax.errorbar(Q2, val, alpha, marker=marker, color=color, ms=ms, capsize=3, linestyle='none')
            for key in theory[exp]['value']:
                Q2 = theory[exp]['Q2'][key]
                mean = theory[exp]['value'][key] * 5.0 ** float(key)
                std_lower = theory[exp]['std_lower'][key] * 5.0 ** float(key)
                std_upper = theory[exp]['std_upper'][key] * 5.0 ** float(key)
                down = mean - std_lower
                up = mean + std_upper
                (thy_plot,) = ax.plot(Q2, mean, linestyle='solid', color='black')
                thy_band = ax.fill_between(Q2, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(Q2))
        if any((len(v['value'].get(7, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10032']))):
            ax.text(370.0, 140000.0, '$x=0.008 \\, (i=7)$', fontsize=28, color='darkgreen')
        if any((len(v['value'].get(6, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10032']))):
            ax.text(1200.0, 13000.0, '$x=0.013$', fontsize=28, color='darkgreen')
        if any((len(v['value'].get(5, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10032']))):
            ax.text(3700.0, 2000.0, '$x=0.032$', fontsize=28, color='darkgreen')
        if any((len(v['value'].get(4, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10032']))):
            ax.text(6000.0, 400, '$x=0.08$', fontsize=28, color='darkgreen')
        if any((len(v['value'].get(3, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10032']))):
            ax.text(9800.0, 70, '$x=0.13$', fontsize=28, color='darkgreen')
        if any((len(v['value'].get(2, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10032']))):
            ax.text(400.0, 11.5, '$x=0.25$', fontsize=28, color='darkgreen')
        if any((len(v['value'].get(1, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10032']))):
            ax.text(600.0, 1.4, '$x=0.40$', fontsize=28, color='darkgreen')
        if any((len(v['value'].get(0, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10032']))):
            ax.text(1300.0, 0.035, '$x=0.65 \\, (i=0) $', fontsize=28, color='darkgreen')
        if any((len(v['value'].get(7, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10031']))):
            ax.text(550.0, 60000.0, '$x=0.008 \\, (i=7)$', fontsize=28, color='blue')
        if any((len(v['value'].get(6, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10031']))):
            ax.text(1200.0, 8000.0, '$x=0.013$', fontsize=28, color='blue')
        if any((len(v['value'].get(5, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10031']))):
            ax.text(3700.0, 1000, '$x=0.032$', fontsize=28, color='blue')
        if any((len(v['value'].get(4, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10031']))):
            ax.text(4300.0, 180, '$x=0.08$', fontsize=28, color='blue')
        if any((len(v['value'].get(3, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10031']))):
            ax.text(8000.0, 23, '$x=0.13$', fontsize=28, color='blue')
        if any((len(v['value'].get(2, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10031']))):
            ax.text(400.0, 5.2, '$x=0.25$', fontsize=28, color='blue')
        if any((len(v['value'].get(1, [])) for v in (v for (name, v) in PLOT.items() if name in ['HERA 10031']))):
            ax.text(370.0, 0.3, '$x=0.40 \\, (i=1)$', fontsize=28, color='blue')
        ax.semilogy()
        ax.semilogx()
        ax.set_xlim(150.0, 40000.0)
        ax.set_ylim(0.01, 800000.0)
        ax.set_xticks([200.0, 1000.0, 10000.0])
        ax.set_xticklabels(['$200$', '$10^3$', '$10^4$'])
        ax.set_yticks([0.01, 0.1, 1.0, 10.0, 100.0, 1000.0, 10000.0, 100000.0])
        ax.set_yticklabels(['$10^{-2}$', '$10^{-1}$', '$1$', '$10$', '$10^2$', '$10^3$', '$10^4$', '$10^5$'])
        ax.tick_params(axis='x', pad=10)
        ax.tick_params(axis='both', labelsize=30)
        ax.set_xlabel('\\boldmath$Q^2$' + '  ' + '\\textbf{\\textrm{(GeV}}' + '\\boldmath$^2)$', size=40)
        ax.text(0.05, 0.1, '\\boldmath$\\sigma_{\\rm red}^{\\rm CC}$', transform=ax.transAxes, size=60)
        ax.text(0.05, 0.03, '$(\\, \\times\\, 5^{\\, i})$', transform=ax.transAxes, size=40)
        ax.xaxis.set_tick_params(which='major', length=10)
        ax.xaxis.set_tick_params(which='minor', length=5)
        ax.yaxis.set_tick_params(which='major', length=10)
        ax.yaxis.set_tick_params(which='minor', length=5)
        ax.yaxis.set_ticks_position('both')
        ax.xaxis.set_ticks_position('both')
        ax.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        handles = [hand.get('HERA 10031'), hand.get('HERA 10032'), (thy_band, thy_plot)]
        label1 = '\\textbf{\\textrm{HERA}} \\boldmath$e^+p$'
        label2 = '\\textbf{\\textrm{HERA}} \\boldmath$e^-p$'
        label3 = '\\textbf{\\textrm{JAM}}'
        labels = [label1, label2, label3]
        thesis_legend(ax, handles, labels, loc='upper right', fontsize=35, frameon=0, handletextpad=0.3, handlelength=1.0)
    py.tight_layout()
    filename = output_file
    ax.set_rasterized(True)
    thesis_finish_panels(py.gcf(), 'thesis_idis_plot_CC', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/idis.py:plot_A3, original lines 1303-1468; SHA256 92dc40105aa2214d4b8ea863286aad4601f0078010c0f2312ba5a1aabbb3ddcf.
def thesis_idis_plot_A3(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['idis']
    tabs = data
    (nrows, ncols) = (1, 2)
    py.figure(figsize=(ncols * 5, nrows * 6))
    ax11 = py.subplot(nrows, ncols, 1)
    ax12 = py.subplot(nrows, ncols, 2)
    if any((i in data for i in [10041])):
        DATA = {}
        if 10041 in data:
            DATA['JLab hd'] = pd.DataFrame(data[10041])
        PLOT = {}
        theory = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_idis_get_Q2bins(DATA[exp], 'JLab')
                nbins = len(query)
                PLOT[exp] = thesis_idis_get_plot(query)
                theory[exp] = thesis_idis_get_theory(PLOT[exp], nbins, loop=False, funcX=True)
        hand = {}
        for exp in PLOT:
            for key in range(nbins):
                X = PLOT[exp]['X'][key]
                val = PLOT[exp]['value'][key] * 2.0 ** float(key)
                alpha = PLOT[exp]['alpha'][key] * 2.0 ** float(key)
                (color, marker, ms) = thesis_idis_get_details(exp)
                hand[exp] = ax11.errorbar(X, val, alpha, marker=marker, color=color, ms=ms, capsize=2.5, linestyle='none')
            for key in theory[exp]['value']:
                X = theory[exp]['X'][key]
                mean = theory[exp]['value'][key] * 2.0 ** float(key)
                std_lower = theory[exp]['std_lower'][key] * 2.0 ** float(key)
                std_upper = theory[exp]['std_upper'][key] * 2.0 ** float(key)
                down = mean - std_lower
                up = mean + std_upper
                (thy_plot,) = ax11.plot(X, mean, linestyle='solid', color='black')
                thy_band = ax11.fill_between(X, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(X))
        ax11.set_xlim(0.28, 0.78)
        ax11.set_ylim(1.02, 1.09)
        ax11.tick_params(axis='both', which='both', direction='in', top=True, right=True, labelsize=20)
        ax11.yaxis.set_tick_params(which='major', length=5)
        ax11.yaxis.set_tick_params(which='minor', length=2.5)
        ax11.xaxis.set_tick_params(which='major', length=5)
        ax11.xaxis.set_tick_params(which='minor', length=2.5)
        ax11.set_xlabel('\\boldmath$x_{\\rm bj}$', size=30)
        ax11.text(0.05, 0.85, '\\boldmath$F_2^{^3\\rm{He}}/F_2^{D}$', transform=ax11.transAxes, size=40)
        minorLocator = MultipleLocator(0.02)
        majorLocator = MultipleLocator(0.1)
        ax11.xaxis.set_minor_locator(minorLocator)
        ax11.xaxis.set_major_locator(majorLocator)
        minorLocator = MultipleLocator(0.004)
        majorLocator = MultipleLocator(0.02)
        ax11.yaxis.set_minor_locator(minorLocator)
        ax11.yaxis.set_major_locator(majorLocator)
        ax11.set_xticks([0.3, 0.4, 0.5, 0.6, 0.7])
        ax11.set_yticks([1.04, 1.06, 1.08])
        (handles, labels) = ([], [])
        handles.append(hand.get('JLab hd'))
        handles.append((thy_band, thy_plot))
        labels.append('\\textbf{\\textrm{JLab Hall C}}')
        labels.append('\\textbf{\\textrm{JAM}}')
        thesis_legend(ax11, handles, labels, loc='lower right', fontsize=25, frameon=0, handletextpad=0.3, handlelength=1.0)
    if any((i in data for i in [10051])):
        DATA = {}
        if 10051 in data:
            DATA['MARATHON ht'] = pd.DataFrame(data[10051])
        PLOT = {}
        theory = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_idis_get_Q2bins(DATA[exp], 'MARATHON')
                nbins = len(query)
                PLOT[exp] = thesis_idis_get_plot(query)
                theory[exp] = thesis_idis_get_theory(PLOT[exp], nbins, loop=False, funcX=True)
        hand = {}
        for exp in PLOT:
            for key in range(nbins):
                X = PLOT[exp]['X'][key]
                val = PLOT[exp]['value'][key] * 2.0 ** float(key)
                alpha = PLOT[exp]['alpha'][key] * 2.0 ** float(key)
                (color, marker, ms) = thesis_idis_get_details(exp)
                hand[exp] = ax12.errorbar(X, val, alpha, marker=marker, color=color, ms=ms, capsize=2.5, linestyle='none')
            for key in theory[exp]['value']:
                X = theory[exp]['X'][key]
                mean = theory[exp]['value'][key] * 2.0 ** float(key)
                std_lower = theory[exp]['std_lower'][key] * 2.0 ** float(key)
                std_upper = theory[exp]['std_upper'][key] * 2.0 ** float(key)
                down = mean - std_lower
                up = mean + std_upper
                (thy_plot,) = ax12.plot(X, mean, linestyle='solid', color='black')
                thy_band = ax12.fill_between(X, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(X))
        ax12.set_xlim(0.1, 0.95)
        ax12.set_ylim(1.05, 1.32)
        ax12.tick_params(axis='both', which='both', direction='in', top=True, right=True, labelsize=20)
        ax12.yaxis.set_tick_params(which='major', length=5)
        ax12.yaxis.set_tick_params(which='minor', length=2.5)
        ax12.xaxis.set_tick_params(which='major', length=5)
        ax12.xaxis.set_tick_params(which='minor', length=2.5)
        ax12.set_xlabel('\\boldmath$x_{\\rm bj}$', size=30)
        ax12.text(0.05, 0.85, '\\boldmath$F_2^{^3\\rm{He}}/F_2^{^3\\rm{H}}$', transform=ax12.transAxes, size=40)
        minorLocator = MultipleLocator(0.04)
        majorLocator = MultipleLocator(0.2)
        ax12.xaxis.set_minor_locator(minorLocator)
        ax12.xaxis.set_major_locator(majorLocator)
        minorLocator = MultipleLocator(0.02)
        majorLocator = MultipleLocator(0.1)
        ax12.yaxis.set_minor_locator(minorLocator)
        ax12.yaxis.set_major_locator(majorLocator)
        ax12.set_xticks([0.2, 0.4, 0.6, 0.8])
        (handles, labels) = ([], [])
        handles.append(hand.get('MARATHON ht'))
        labels.append('\\textrm{\\textbf{MARATHON}}')
        thesis_legend(ax12, handles, labels, loc='lower right', fontsize=25, frameon=0, handletextpad=0.3, handlelength=1.0)
    py.tight_layout()
    filename = output_file
    ax11.set_rasterized(True)
    ax12.set_rasterized(True)
    thesis_finish_panels(py.gcf(), 'thesis_idis_plot_A3', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/marathon.py:get_Q2bins, original lines 34-53; SHA256 9284146d2eee085e89cc2bd0e708ec392cf4d554449bf85cb99e066372872efa.
def thesis_marathon_get_Q2bins(data, kind):
    query = {}
    if kind == 'MARATHON':
        query[0] = data.query('Q2 > 1.5 and Q2 <= 20')
    if kind == 'NMC':
        query[0] = data.query('X > 0.15 and X <= 0.40')
    return query

# Adapted from thesis/marathon.py:get_plot, original lines 55-83; SHA256 9284146d2eee085e89cc2bd0e708ec392cf4d554449bf85cb99e066372872efa.
def thesis_marathon_get_plot(query, cluster_i=0):
    plot = {_: {} for _ in ['theory', 'X', 'Q2', 'value', 'alpha', 'std_lower', 'std_upper', 'shifts']}
    for key in query:
        theory = query[key]['thy-%d' % cluster_i]
        std_lower = query[key]['dthy_lower-%d' % cluster_i]
        std_upper = query[key]['dthy_upper-%d' % cluster_i]
        X = query[key]['X']
        Q2 = query[key]['Q2']
        value = query[key]['value']
        alpha = query[key]['alpha']
        shifts = query[key]['shifts']
        zx = sorted(zip(Q2, X), key=lambda row: row[0])
        zt = sorted(zip(Q2, theory), key=lambda row: row[0])
        zv = sorted(zip(Q2, value), key=lambda row: row[0])
        za = sorted(zip(Q2, alpha), key=lambda row: row[0])
        zs_lower = sorted(zip(Q2, std_lower), key=lambda row: row[0])
        zs_upper = sorted(zip(Q2, std_upper), key=lambda row: row[0])
        zsh = sorted(zip(Q2, shifts), key=lambda row: row[0])
        plot['X'][key] = np.array([zx[i][1] for i in range(len(zx))])
        plot['theory'][key] = np.array([zt[i][1] for i in range(len(zt))])
        plot['value'][key] = np.array([zv[i][1] for i in range(len(zv))])
        plot['alpha'][key] = np.array([za[i][1] for i in range(len(za))])
        plot['std_lower'][key] = np.array([zs_lower[i][1] for i in range(len(zs_lower))])
        plot['std_upper'][key] = np.array([zs_upper[i][1] for i in range(len(zs_upper))])
        plot['shifts'][key] = np.array([zsh[i][1] for i in range(len(zsh))])
        plot['Q2'][key] = np.array(sorted(Q2))
    return plot

# Adapted from thesis/marathon.py:get_theory, original lines 85-128; SHA256 9284146d2eee085e89cc2bd0e708ec392cf4d554449bf85cb99e066372872efa.
def thesis_marathon_get_theory(PLOT, nbins, loop=True, funcQ2=True, funcX=False):
    theory = {}
    if funcQ2:
        svar = 'Q2'
    if funcX:
        svar = 'X'
    theory = {_: {} for _ in [svar, 'value', 'std_lower', 'std_upper']}
    for key in range(nbins):
        var = []
        thy = []
        std_lower = []
        std_upper = []
        if loop:
            for exp in PLOT:
                var.extend(PLOT[exp][svar][key])
                thy.extend(PLOT[exp]['theory'][key])
                std_lower.extend(PLOT[exp]['std_lower'][key])
                std_upper.extend(PLOT[exp]['std_upper'][key])
        else:
            var.extend(PLOT[svar][key])
            thy.extend(PLOT['theory'][key])
            std_lower.extend(PLOT['std_lower'][key])
            std_upper.extend(PLOT['std_upper'][key])
        if len(var) == 0:
            continue
        vmin = np.min(var)
        vmax = np.max(var)
        theory[svar][key] = np.geomspace(vmin, vmax, 100)
        if len(var) > 1:
            theory['value'][key] = griddata(np.array(var), np.array(thy), theory[svar][key], method='linear')
            theory['std_lower'][key] = griddata(np.array(var), np.array(std_lower), theory[svar][key], method='linear')
            theory['std_upper'][key] = griddata(np.array(var), np.array(std_upper), theory[svar][key], method='linear')
        else:
            theory['value'][key] = np.ones(100) * thy
            theory['std_lower'][key] = np.ones(100) * std_lower
            theory['std_upper'][key] = np.ones(100) * std_upper
    return theory

# Adapted from thesis/marathon.py:get_details, original lines 130-136; SHA256 9284146d2eee085e89cc2bd0e708ec392cf4d554449bf85cb99e066372872efa.
def thesis_marathon_get_details(exp):
    if exp == 'MARATHON dp':
        (color, marker, ms) = ('black', 'o', 4)
    if exp == 'MARATHON ht':
        (color, marker, ms) = ('black', 'o', 4)
    return (color, marker, ms)

# Adapted from thesis/marathon.py:plot_main, original lines 138-383; SHA256 9284146d2eee085e89cc2bd0e708ec392cf4d554449bf85cb99e066372872efa.
def thesis_marathon_plot_main(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['idis']
    tabs = data
    (nrows, ncols) = (1, 2)
    py.figure(figsize=(ncols * 5, nrows * 6))
    ax11 = py.subplot(nrows, ncols, 1)
    ax12 = py.subplot(nrows, ncols, 2)
    j = 0
    JAMcolor = 'red'
    JAMlw = 2
    for data in [datasets['idis']]:
        DATA = {}
        if 10050 in data:
            DATA['MARATHON dp'] = pd.DataFrame(data[10050])
        PLOT = {}
        theory = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_marathon_get_Q2bins(DATA[exp], 'MARATHON')
                nbins = len(query)
                PLOT[exp] = thesis_marathon_get_plot(query)
                theory[exp] = thesis_marathon_get_theory(PLOT[exp], nbins, loop=False, funcX=True)
        hand = {}
        for exp in PLOT:
            for key in PLOT[exp]['X']:
                X = PLOT[exp]['X'][key]
                val = PLOT[exp]['value'][key]
                alpha = PLOT[exp]['alpha'][key]
                (color, marker, ms) = thesis_marathon_get_details(exp)
                hand[exp] = ax11.errorbar(X, val, alpha, marker=marker, color=color, ms=ms, capsize=2.5, linestyle='none', zorder=4)
                if j == 0:
                    center = 0.77
                    shifts = PLOT[exp]['shifts'][key]
                    (pos, neg) = (np.zeros(len(alpha)), np.zeros(len(alpha)))
                    for i in range(len(shifts)):
                        if shifts[i] > 0.0:
                            pos[i] = shifts[i]
                        else:
                            neg[i] = shifts[i]
            for key in theory[exp]['value']:
                X = theory[exp]['X'][key]
                mean = theory[exp]['value'][key]
                std_lower = theory[exp]['std_lower'][key]
                std_upper = theory[exp]['std_upper'][key]
                down = mean - std_lower
                up = mean + std_upper
                if j == 0:
                    (thy_plot,) = ax11.plot(X, mean, linestyle='solid', lw=JAMlw, color=JAMcolor, zorder=3)
                    thy_band = ax11.fill_between(X, np.minimum(down, up), np.maximum(down, up), color=JAMcolor, alpha=0.2, zorder=3, where=thesis_band_mask(X))
                if j == 1:
                    (nooff_plot,) = ax11.plot(X, mean, linestyle='dashed', color='green', zorder=2, lw=2)
                    nooff_band = ax11.fill_between(X, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=0.0, zorder=2, where=thesis_band_mask(X))
        DATA = {}
        if 10051 in data:
            DATA['MARATHON ht'] = pd.DataFrame(data[10051])
        PLOT = {}
        theory = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_marathon_get_Q2bins(DATA[exp], 'MARATHON')
                nbins = len(query)
                PLOT[exp] = thesis_marathon_get_plot(query)
                theory[exp] = thesis_marathon_get_theory(PLOT[exp], nbins, loop=False, funcX=True)
        for exp in PLOT:
            for key in range(nbins):
                X = PLOT[exp]['X'][key]
                val = PLOT[exp]['value'][key] * 2.0 ** float(key)
                alpha = PLOT[exp]['alpha'][key] * 2.0 ** float(key)
                (color, marker, ms) = thesis_marathon_get_details(exp)
                hand[exp] = ax12.errorbar(X, val, alpha, marker=marker, color=color, ms=ms, capsize=2.5, linestyle='none', zorder=4)
                if j == 0:
                    center = 1.1
                    shifts = PLOT[exp]['shifts'][key]
                    (pos, neg) = (np.zeros(len(alpha)), np.zeros(len(alpha)))
                    for i in range(len(shifts)):
                        if shifts[i] > 0.0:
                            pos[i] = shifts[i]
                        else:
                            neg[i] = shifts[i]
            for key in theory[exp]['value']:
                X = theory[exp]['X'][key]
                mean = theory[exp]['value'][key] * 2.0 ** float(key)
                std_lower = theory[exp]['std_lower'][key] * 2.0 ** float(key)
                std_upper = theory[exp]['std_upper'][key] * 2.0 ** float(key)
                down = mean - std_lower
                up = mean + std_upper
                if j == 0:
                    (thy_plot,) = ax12.plot(X, mean, linestyle='solid', color=JAMcolor, lw=JAMlw, zorder=3)
                    thy_band = ax12.fill_between(X, np.minimum(down, up), np.maximum(down, up), color='red', alpha=0.2, zorder=3, where=thesis_band_mask(X))
                if j == 1:
                    (nooff_plot,) = ax12.plot(X, mean, linestyle='dashed', color='green', zorder=2, lw=2)
                    nooff_band = ax12.fill_between(X, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=0.0, zorder=2, where=thesis_band_mask(X))
        j += 1
    ax11.set_xlim(0.15, 0.44)
    ax11.set_ylim(0.74, 0.88)
    ax11.tick_params(axis='both', which='both', direction='in', top=True, right=True, labelsize=20)
    ax11.yaxis.set_tick_params(which='major', length=5)
    ax11.yaxis.set_tick_params(which='minor', length=2.5)
    ax11.xaxis.set_tick_params(which='major', length=5)
    ax11.xaxis.set_tick_params(which='minor', length=2.5)
    ax11.set_xlabel('\\boldmath$x_{\\rm bj}$', size=30)
    minorLocator = MultipleLocator(0.02)
    majorLocator = MultipleLocator(0.1)
    ax11.xaxis.set_minor_locator(minorLocator)
    ax11.xaxis.set_major_locator(majorLocator)
    minorLocator = MultipleLocator(0.01)
    majorLocator = MultipleLocator(0.05)
    ax11.yaxis.set_minor_locator(minorLocator)
    ax11.yaxis.set_major_locator(majorLocator)
    ax12.set_xlim(0.1, 0.95)
    ax12.set_ylim(1.07, 1.33)
    ax12.tick_params(axis='both', which='both', direction='in', top=True, right=True, labelsize=20)
    ax12.yaxis.set_tick_params(which='major', length=5)
    ax12.yaxis.set_tick_params(which='minor', length=2.5)
    ax12.xaxis.set_tick_params(which='major', length=5)
    ax12.xaxis.set_tick_params(which='minor', length=2.5)
    ax12.set_xlabel('\\boldmath$x_{\\rm bj}$', size=30)
    ax12.text(0.37, 0.05, '$Q^2 = 14 x_{\\rm bj} ~ {\\rm GeV^2}$', transform=ax12.transAxes, size=25)
    minorLocator = MultipleLocator(0.04)
    majorLocator = MultipleLocator(0.2)
    ax12.xaxis.set_minor_locator(minorLocator)
    ax12.xaxis.set_major_locator(majorLocator)
    minorLocator = MultipleLocator(0.02)
    majorLocator = MultipleLocator(0.1)
    ax12.yaxis.set_minor_locator(minorLocator)
    ax12.yaxis.set_major_locator(majorLocator)
    ax12.set_xticks([0.2, 0.4, 0.6, 0.8])
    ax11.text(0.53, 0.85, '\\boldmath$F_2^D/F_2^p$', transform=ax11.transAxes, size=35)
    ax12.text(0.07, 0.85, '\\boldmath$F_2^{^3\\rm{He}}/F_2^{^3\\rm{H}}$', transform=ax12.transAxes, size=35)
    (handles, labels) = ([], [])
    handles.append(hand.get('MARATHON ht', hand.get('MARATHON dp')))
    handles.append((thy_band, thy_plot))
    labels.append('\\textbf{\\textrm{{\\huge MARATHON}}}')
    labels.append('\\textbf{\\textrm{JAM}}')
    thesis_legend(ax11, handles, labels, loc='lower left', fontsize=20, frameon=0, handletextpad=0.3, handlelength=1.4)
    py.tight_layout()
    filename = output_file
    ax11.set_rasterized(True)
    ax12.set_rasterized(True)
    thesis_finish_panels(py.gcf(), 'thesis_marathon_plot_main', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/jets.py:find_eta_bins, original lines 31-52; SHA256 9ecd1644972d1c2f270cbc3a1dc61e7655f8c0e0c68a1fe17a0fab396f741943.
def thesis_jets_find_eta_bins(table):
    eta_headers = [_ for _ in table if 'eta' in _]
    absolute_eta = False
    for _ in eta_headers:
        if 'abs' in _:
            absolute_eta = True
            break
    eta_bin_indices = []
    eta_bin_indices.append([0])
    if absolute_eta:
        eta_key = 'eta-abs-min'
    else:
        eta_key = 'eta-min'
    for i in range(len(table[eta_key]) - 1):
        if table[eta_key][i] != table[eta_key][i + 1]:
            eta_bin_indices[-1].append(i + 1)
            eta_bin_indices.append([i + 1])
    eta_bin_indices[-1].append(len(table[eta_key]))
    return (eta_headers, absolute_eta, eta_bin_indices)

# Adapted from thesis/jets.py:get_eta_label, original lines 54-91; SHA256 9ecd1644972d1c2f270cbc3a1dc61e7655f8c0e0c68a1fe17a0fab396f741943.
def thesis_jets_get_eta_label(data, absolute_eta, eta_bins, i_bin, plot_factor_base, combined=True):
    if plot_factor_base == 1.0:
        if absolute_eta:
            eta_min = str(data['eta-abs-min'][eta_bins[i_bin][0]])
            eta_max = str(data['eta-abs-max'][eta_bins[i_bin][0]])
            if combined:
                eta_label = '|\\eta_{\\rm jet}| \\in [%s, %s]' % (eta_min, eta_max)
            else:
                eta_label = ('|\\eta_{\\rm jet}|~\\textrm{range}', '[%s, %s]' % (eta_min, eta_max))
        else:
            eta_min = str(data['eta-min'][eta_bins[i_bin][0]])
            eta_max = str(data['eta-max'][eta_bins[i_bin][0]])
            if combined:
                eta_label = '\\eta_{\\rm jet} \\in [%s, %s]' % (eta_min, eta_max)
            else:
                eta_label = ('\\eta_{\\rm jet}~\\textrm{range}', '[%s, %s]' % (eta_min, eta_max))
    elif absolute_eta:
        eta_min = str(data['eta-abs-min'][eta_bins[i_bin][0]])
        eta_max = str(data['eta-abs-max'][eta_bins[i_bin][0]])
        plot_factor_power = round(np.log(data['plot-factor'][eta_bins[i_bin][0]]) / np.log(plot_factor_base))
        if combined:
            eta_label = '|\\eta_{\\rm jet}| \\in [%s, %s] \\, (\\times %i^{%i})' % (eta_min, eta_max, plot_factor_base, plot_factor_power)
        else:
            eta_label = ('|\\eta_{\\rm jet}|~\\textrm{range}', '[%s, %s]' % (eta_min, eta_max), '\\times %i^{%i}' % (plot_factor_base, plot_factor_power))
    else:
        eta_min = str(data['eta-min'][eta_bins[i_bin][0]])
        eta_max = str(data['eta-max'][eta_bins[i_bin][0]])
        plot_factor_power = round(np.log(data['plot-factor'][eta_bins[i_bin][0]]) / np.log(plot_factor_base))
        if combined:
            eta_label = '\\eta_{\\rm jet} \\in [%s, %s] \\, (\\times %i^{%i})' % (eta_min, eta_max, plot_factor_base, plot_factor_power)
        else:
            eta_label = ('\\eta_{\\rm jet}~\\textrm{range}', '[%s, %s]' % (eta_min, eta_max), '\\times %i^{%i}' % (plot_factor_base, plot_factor_power))
    return eta_label

# Adapted from thesis/jets.py:get_extra_label, original lines 93-103; SHA256 9ecd1644972d1c2f270cbc3a1dc61e7655f8c0e0c68a1fe17a0fab396f741943.
def thesis_jets_get_extra_label(dataset):
    if dataset == 10001:
        text = ''
    elif dataset == 10002:
        text = ''
    elif dataset == 10003:
        text = '2003'
    elif dataset == 10004:
        text = '2004'
    return text

# Adapted from thesis/jets.py:find_plot_factor_base, original lines 105-123; SHA256 9ecd1644972d1c2f270cbc3a1dc61e7655f8c0e0c68a1fe17a0fab396f741943.
def thesis_jets_find_plot_factor_base(plot_factors):
    if all((_ == 1.0 for _ in plot_factors)):
        return (False, 1.0)
    for _ in plot_factors:
        if _ != 1.0:
            plot_factor = _
            break
    bases = [2, 3, 5, 6, 7, 10]
    power_differences = {}
    for base in bases:
        plot_factor_power = np.log(plot_factor) / np.log(base)
        power_differences[base] = abs(round(plot_factor_power) - plot_factor_power)
    difference_min = np.inf
    for base in bases:
        if power_differences[base] < difference_min:
            difference_min = power_differences[base]
            true_base = base
    return (True, true_base)

# Adapted from thesis/jets.py:plot_replicas, original lines 125-134; SHA256 9ecd1644972d1c2f270cbc3a1dc61e7655f8c0e0c68a1fe17a0fab396f741943.
def thesis_jets_plot_replicas(ax, pt, data, eta_bins, i_bin, factor, z_order=1, interval=95):
    cluster = list(data)[0]
    theory_replicas = data[cluster]
    thy = [theory_replicas[j][eta_bins[i_bin][0]:eta_bins[i_bin][1]] for j in range(len(theory_replicas))]
    ys = np.mean(thy, axis=0)
    ys_d_lower = thesis_deviation(thy, interval, 'lower')
    ys_d_upper = thesis_deviation(thy, interval, 'upper')
    ax.fill_between(pt, np.minimum((ys - ys_d_lower) * factor, (ys + ys_d_upper) * factor), np.maximum((ys - ys_d_lower) * factor, (ys + ys_d_upper) * factor), color='gold', alpha=0.8, zorder=z_order, where=thesis_band_mask(pt))
    return ax

# Adapted from thesis/jets.py:plot_data, original lines 136-362; SHA256 9ecd1644972d1c2f270cbc3a1dc61e7655f8c0e0c68a1fe17a0fab396f741943.
def thesis_jets_plot_data(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['jet']
    tabs = data
    datasets = sorted(list(data))
    (n_rows, n_columns) = (2, 2)
    figure = pyplot.figure(figsize=(n_columns * 7.5, n_rows * 5.0), constrained_layout=True)
    gs = figure.add_gridspec(4, 2)
    axs = {}
    axs[0] = figure.add_subplot(gs[0:2, 0])
    axs[1] = figure.add_subplot(gs[0:2, 1])
    axs[2] = figure.add_subplot(gs[2:4, 0])
    axs[3] = figure.add_subplot(gs[2, 1])
    axs[4] = figure.add_subplot(gs[3, 1])
    i_last_row = range(len(axs) - 1, len(axs) - 1 - n_columns, -1)
    eta_size = 20
    collaboration_size = 30
    styles = ['-']
    colors = ['b', 'g', 'm', 'darkorange', 'c', 'r']
    rhic_colors = {10003: 'b', 10004: 'g'}
    (handle_left, label_left) = ([], [])
    (handle_right, label_right) = ([], [])
    for i in range(len(datasets)):
        dataset = datasets[i]
        i = {10001: 0, 10002: 1, 10003: 2, 10004: 2}[dataset]
        (_, absolute_eta, eta_bins) = thesis_jets_find_eta_bins(data[dataset])
        if 'plot-factor' not in data[dataset]:
            data[dataset]['plot-factor'] = np.ones(len(data[dataset]['value']))
        (plot_with_factor, plot_factor_base) = thesis_jets_find_plot_factor_base(data[dataset]['plot-factor'])
        for j in range(len(eta_bins)):
            eta_range = thesis_jets_get_eta_label(data[dataset], absolute_eta, eta_bins, j, plot_factor_base)
            pt = np.array(data[dataset]['pT'][eta_bins[j][0]:eta_bins[j][1]])
            plot_factor = np.array(data[dataset]['plot-factor'][eta_bins[j][0]:eta_bins[j][1]])
            value = np.array(data[dataset]['value'][eta_bins[j][0]:eta_bins[j][1]]) * plot_factor
            alpha = np.array(data[dataset]['alpha'][eta_bins[j][0]:eta_bins[j][1]]) * plot_factor
            theory = np.array(data[dataset]['thy'][eta_bins[j][0]:eta_bins[j][1]]) * plot_factor
            if dataset in list(rhic_colors):
                if dataset == 10003:
                    z_order = 4
                else:
                    z_order = 3
                axs[i].errorbar(pt, value, alpha, fmt='.', color=rhic_colors[dataset], ms=5.0, capsize=0.0, zorder=z_order)
            else:
                axs[i].errorbar(pt, value, alpha, fmt='.', color=colors[j], ms=5.0, capsize=0.0, zorder=3)
            if dataset in list(rhic_colors):
                if dataset == 10003:
                    z_order = 2.5
                    style = 'dashed'
                else:
                    z_order = 2
                    style = 'solid'
                label = '%s' % data[dataset]['col'][0].upper()
                label += '~' + thesis_jets_get_extra_label(dataset).replace(' ', '~')
                axs[i].plot(pt, theory, color=rhic_colors[dataset], linestyle=style, zorder=z_order, label='\\boldmath{$\\mathrm{%s}$}' % label)
            elif dataset == 10002:
                (_, eta_label, _) = thesis_jets_get_eta_label(data[dataset], absolute_eta, eta_bins, j, plot_factor_base, combined=False)
                if j in [0, 1]:
                    (handle,) = axs[i].plot(pt, theory, color=colors[j], zorder=2)
                    handle_left.append(handle)
                    label_left.append('$%s$' % eta_label)
                else:
                    (handle,) = axs[i].plot(pt, theory, color=colors[j], zorder=2)
                    handle_right.append(handle)
                    label_right.append('$%s$' % eta_label)
            else:
                (_, eta_label, _) = thesis_jets_get_eta_label(data[dataset], absolute_eta, eta_bins, j, plot_factor_base, combined=False)
                axs[i].plot(pt, theory, color=colors[j], zorder=2, label='$%s$' % eta_label)
            if dataset == 10003:
                axs[i] = thesis_jets_plot_replicas(axs[i], pt, data[dataset]['thy-rep'], eta_bins, j, plot_factor, z_order=2, interval=interval)
            else:
                axs[i] = thesis_jets_plot_replicas(axs[i], pt, data[dataset]['thy-rep'], eta_bins, j, plot_factor, interval=interval)
        if dataset == 10002:
            legends_left = thesis_legend(axs[i], handle_left, label_left, fontsize=eta_size, frameon=False, handlelength=0.7, loc='upper center', bbox_to_anchor=(0.6, 1.0))
            legends_right = thesis_legend(axs[i], handle_right, label_right, fontsize=eta_size, frameon=False, handlelength=0.7, loc='upper right')
            axs[i].add_artist(legends_left)
            for line in legends_left.get_lines():
                line.set_linewidth(3.0)
            for line in legends_right.get_lines():
                line.set_linewidth(3.0)
        elif dataset == 10001:
            legends = axs[i].legend(fontsize=eta_size, frameon=False, handlelength=0.7, loc='upper right')
            for line in legends.get_lines():
                line.set_linewidth(3.0)
        else:
            legends = axs[i].legend(fontsize=collaboration_size, loc='best', frameon=False, handlelength=1.5)
            for line in legends.get_lines():
                line.set_linewidth(3.0)
        axs[i].set_yscale('log')
        unit = data[dataset]['units'][0]
        if data[dataset]['obs'][0].replace('<', '').replace('>', '') == 'd2_sigma_over_d_y_d_pt':
            y_label = '\\boldmath{$\\frac{\\mathrm{d}^2\\sigma^{\\rm jet}}{\\mathrm{d}\\eta_{\\rm jet} \\, \\mathrm{d} p_T^{\\rm jet}}~{\\scriptstyle \\left(\\frac{\\mathrm{%s}}{\\mathrm{GeV}} \\right)}$}' % unit
        elif data[dataset]['obs'][0].replace('<', '').replace('>', '') == 'd2_sigma_over_2_pi_d_y_d_pt':
            y_label = '\\boldmath{$\\frac{\\mathrm{d}^2\\sigma^{\\rm jet}}{2 \\pi \\, \\mathrm{d}\\eta_{\\rm jet} \\, \\mathrm{d} p_T^{\\rm jet}}~{\\scriptstyle \\left(\\frac{\\mathrm{%s}}{\\mathrm{GeV}} \\right)}$}' % unit
        if i % n_columns == 0:
            axs[i].set_ylabel(y_label, size=40)
        if dataset == 10003:
            axs[i].text(0.05, 0.1, '$%s$' % eta_range, color='k', transform=axs[i].transAxes, size=23)
        elif dataset == 10004:
            pass
        else:
            text = '%s' % data[dataset]['col'][0].upper()
            text += '~' + thesis_jets_get_extra_label(dataset).replace(' ', '~')
            if dataset == 10001:
                (x_, y_) = (0.2, 0.85)
            elif dataset == 10002:
                (x_, y_) = (0.8, 0.1)
            axs[i].text(x_, y_, '\\boldmath{$\\mathrm{%s}$}' % text, color='k', transform=axs[i].transAxes, size=collaboration_size)
    for i in range(len(rhic_colors)):
        dataset = list(rhic_colors)[i]
        if dataset not in data:
            continue
        (_, absolute_eta, eta_bins) = thesis_jets_find_eta_bins(data[dataset])
        for j in range(len(eta_bins)):
            pt = data[dataset]['pT'][eta_bins[j][0]:eta_bins[j][1]]
            plot_factor_base = 1.0
            eta_range = thesis_jets_get_eta_label(data[dataset], absolute_eta, eta_bins, j, plot_factor_base)
            theory = data[dataset]['thy'][eta_bins[j][0]:eta_bins[j][1]]
            value = data[dataset]['value'][eta_bins[j][0]:eta_bins[j][1]]
            alpha = data[dataset]['alpha'][eta_bins[j][0]:eta_bins[j][1]]
            axs[i + 3].errorbar(pt, thesis_ratio(value, theory, error=False), thesis_ratio(alpha, theory, error=True), fmt='.', color=rhic_colors[dataset], ms=5.0, capsize=3.5, zorder=2)
            axs[i + 3].axhline(1.0, color='black', ls='dashdot', linewidth=0.5, zorder=0)
            axs[i + 3] = thesis_jets_plot_replicas(axs[i + 3], pt, data[dataset]['thy-rep'], eta_bins, j, thesis_ratio(1.0, theory, error=False), interval=interval)
    for i in range(len(axs)):
        axs[i].tick_params(axis='both', which='both', right=True, top=True, direction='in', labelsize=21)
        axs[i].tick_params(axis='x', pad=10)
        axs[i].yaxis.set_tick_params(which='major', length=7.5)
        axs[i].xaxis.set_tick_params(which='major', length=7.5)
        axs[i].minorticks_off()
    for i in [2, 4]:
        axs[i].set_xlabel('\\boldmath$p_T^{\\rm jet}~(\\mathrm{GeV})$', size=30)
    axs[3].set_ylim(0.6, 1.2)
    axs[4].set_ylim(0.8, 1.2)
    axs[3].text(0.05, 0.2, '\\boldmath{$\\mathrm{data/theory}$}', color='k', transform=axs[3].transAxes, size=23)
    axs[4].text(0.3, 0.14, '\\boldmath{$\\mathrm{data/theory}$}', color='k', transform=axs[4].transAxes, size=23)
    axs[3].text(0.52, 0.25, '\\boldmath{$\\mathrm{STAR~2003}$}', color='k', transform=axs[3].transAxes, size=collaboration_size)
    axs[4].text(0.25, 0.74, '\\boldmath{$\\mathrm{STAR~2004}$}', color='k', transform=axs[4].transAxes, size=collaboration_size)
    if 10001 in data:
        dataset = 10001
        (_, absolute_eta, eta_bins) = thesis_jets_find_eta_bins(data[dataset])
        (plot_with_factor, plot_factor_base) = thesis_jets_find_plot_factor_base(data[dataset]['plot-factor'])
        (eta_name, _, factor) = thesis_jets_get_eta_label(data[dataset], absolute_eta, eta_bins, 5, plot_factor_base, combined=False)
        axs[0].text(0.7, 0.875, '$%s$' % eta_name, color='k', transform=axs[0].transAxes, size=23, horizontalalignment='right', verticalalignment='bottom')
        axs[0].text(0.29, 0.03, '$%s$' % factor, color=colors[5], transform=axs[0].transAxes, size=23, horizontalalignment='right', verticalalignment='bottom')
        (_, _, factor) = thesis_jets_get_eta_label(data[dataset], absolute_eta, eta_bins, 4, plot_factor_base, combined=False)
        axs[0].text(0.4, 0.015, '$%s$' % factor, color=colors[4], transform=axs[0].transAxes, size=23, horizontalalignment='left', verticalalignment='bottom')
        (_, _, factor) = thesis_jets_get_eta_label(data[dataset], absolute_eta, eta_bins, 3, plot_factor_base, combined=False)
        axs[0].text(0.54, 0.03, '$%s$' % factor, color=colors[3], transform=axs[0].transAxes, size=23, horizontalalignment='left', verticalalignment='bottom')
        (_, _, factor) = thesis_jets_get_eta_label(data[dataset], absolute_eta, eta_bins, 2, plot_factor_base, combined=False)
        axs[0].text(0.7, 0.03, '$%s$' % factor, color=colors[2], transform=axs[0].transAxes, size=23, horizontalalignment='left', verticalalignment='bottom')
        (_, _, factor) = thesis_jets_get_eta_label(data[dataset], absolute_eta, eta_bins, 1, plot_factor_base, combined=False)
        axs[0].text(0.86, 0.01, '$%s$' % factor, color=colors[1], transform=axs[0].transAxes, size=23, horizontalalignment='left', verticalalignment='bottom')
        (_, _, factor) = thesis_jets_get_eta_label(data[dataset], absolute_eta, eta_bins, 0, plot_factor_base, combined=False)
        axs[0].text(0.99, 0.18, '$%s$' % factor, color=colors[0], transform=axs[0].transAxes, size=23, horizontalalignment='right', verticalalignment='bottom')
    if 10002 in data:
        dataset = 10002
        (_, absolute_eta, eta_bins) = thesis_jets_find_eta_bins(data[dataset])
        (plot_with_factor, plot_factor_base) = thesis_jets_find_plot_factor_base(data[dataset]['plot-factor'])
        (eta_name, _, factor) = thesis_jets_get_eta_label(data[dataset], absolute_eta, eta_bins, 4, plot_factor_base, combined=False)
        axs[1].text(0.45, 0.875, '$%s$' % eta_name, color='k', transform=axs[1].transAxes, size=23, horizontalalignment='right', verticalalignment='bottom')
        axs[1].text(0.32, 0.03, '$%s$' % factor, color=colors[4], transform=axs[1].transAxes, size=23, horizontalalignment='right', verticalalignment='bottom')
        (_, _, factor) = thesis_jets_get_eta_label(data[dataset], absolute_eta, eta_bins, 3, plot_factor_base, combined=False)
        axs[1].text(0.41, 0.12, '$%s$' % factor, color=colors[3], transform=axs[1].transAxes, size=23, horizontalalignment='left', verticalalignment='bottom')
        (_, _, factor) = thesis_jets_get_eta_label(data[dataset], absolute_eta, eta_bins, 2, plot_factor_base, combined=False)
        axs[1].text(0.62, 0.23, '$%s$' % factor, color=colors[2], transform=axs[1].transAxes, size=23, horizontalalignment='left', verticalalignment='bottom')
        (_, _, factor) = thesis_jets_get_eta_label(data[dataset], absolute_eta, eta_bins, 1, plot_factor_base, combined=False)
        axs[1].text(0.8, 0.34, '$%s$' % factor, color=colors[1], transform=axs[1].transAxes, size=23, horizontalalignment='left', verticalalignment='bottom')
        (_, _, factor) = thesis_jets_get_eta_label(data[dataset], absolute_eta, eta_bins, 0, plot_factor_base, combined=False)
        axs[1].text(0.8, 0.5, '$%s$' % factor, color=colors[0], transform=axs[1].transAxes, size=23, horizontalalignment='left', verticalalignment='bottom')
    pyplot.tight_layout()
    filename = output_file
    axs[0].set_rasterized(True)
    axs[1].set_rasterized(True)
    axs[2].set_rasterized(True)
    axs[3].set_rasterized(True)
    axs[4].set_rasterized(True)
    thesis_finish_panels(py.gcf(), 'thesis_jets_plot_data', _selected_keys)
    pyplot.savefig(filename)

# Adapted from thesis/lepton.py:plot_asym, original lines 40-319; SHA256 26ca48c0cb3cf5831916f162eb1644c98d2dba5cd0c2c8dbc9555fcf942ab760.
def thesis_lepton_plot_asym(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['wzrv']
    tabs = data
    (nrows, ncols) = (4, 2)
    gridspec = dict(hspace=0, height_ratios=[0.7, 0.4, 0.4, 0.4])
    (fig, axs) = py.subplots(nrows=nrows, ncols=ncols, gridspec_kw=gridspec, figsize=(ncols * 7, nrows * 3))
    ax11 = axs[0][0]
    ax12 = axs[0][1]
    ax21 = axs[1][0]
    ax22 = axs[1][1]
    ax31 = axs[2][0]
    ax32 = axs[2][1]
    ax41 = axs[3][0]
    axs[3][1].axis('off')
    tabs = datasets['wzrv']
    data = predictions['reactions']['wzrv']
    hand = {}
    axes = {}
    axes['ax'] = {}
    axes['ax'][2010] = ax11
    axes['ax'][2011] = ax11
    axes['ax'][2012] = ax11
    axes['ax'][2013] = ax11
    axes['ax'][2014] = ax11
    axes['ax'][2016] = ax12
    axes['ax'][2017] = ax12
    axes['color'] = {}
    axes['color'][2010] = 'firebrick'
    axes['color'][2011] = 'darkturquoise'
    axes['color'][2012] = 'darkmagenta'
    axes['color'][2013] = 'darkturquoise'
    axes['color'][2014] = 'darkturquoise'
    axes['color'][2016] = 'firebrick'
    axes['color'][2017] = 'darkgreen'
    axes['marker'] = {}
    axes['marker'][2010] = '*'
    axes['marker'][2011] = '^'
    axes['marker'][2012] = '.'
    axes['marker'][2013] = '^'
    axes['marker'][2014] = '^'
    axes['marker'][2016] = 'o'
    axes['marker'][2017] = '*'
    for idx in tabs:
        if idx not in data:
            continue
        if idx not in axes['ax']:
            continue
        values = tabs[idx]['value']
        alpha = data[idx]['alpha']
        ax = axes['ax'][idx]
        color = axes['color'][idx]
        marker = axes['marker'][idx]
        if 'eta' in tabs[idx]:
            eta = tabs[idx]['eta']
            hand[idx] = ax.errorbar(eta, values, yerr=alpha, color=color, linestyle='none', marker=marker, ms=6.0, capsize=3.0)
        else:
            eta = (tabs[idx]['eta_min'] + tabs[idx]['eta_max']) / 2.0
            eta_min = tabs[idx]['eta_min']
            eta_max = tabs[idx]['eta_max']
            xerr = np.zeros((2, len(eta)))
            hand[idx] = ax.errorbar(eta, values, yerr=alpha, color=color, linestyle='none', marker=marker, ms=6.0, capsize=3.0)
    for idx in [2010, 2011, 2012, 2016, 2017]:
        if idx not in data:
            continue
        if idx not in data:
            continue
        for ic in range(nc):
            ax = axes['ax'][idx]
            color = axes['color'][idx]
            marker = axes['marker'][idx]
            if 'eta' in tabs[idx]:
                eta = tabs[idx]['eta']
            else:
                eta = (tabs[idx]['eta_min'] + tabs[idx]['eta_max']) / 2.0
            thy = data[idx]['thy-%d' % ic]
            std_lower = data[idx]['dthy_lower-%d' % ic]
            std_upper = data[idx]['dthy_upper-%d' % ic]
            down = thy - std_lower
            up = thy + std_upper
            (thy_plot,) = ax.plot(eta, thy, color='black')
            thy_band = ax.fill_between(eta, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(eta))
    for ax in [ax11, ax12]:
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=30, labelbottom=False)
    ax12.axhline(0, 0, 3, alpha=0.5, color='black', ls='--')
    for ax in [ax11, ax21, ax31, ax41]:
        ax.set_xlim(0.0, 2.6)
    for ax in [ax12, ax22, ax32]:
        ax.set_xlim(2.0, 4.6)
    ax11.text(0.05, 0.05, '\\boldmath$A_l$', transform=ax11.transAxes, fontsize=40)
    ax11.text(0.05, 0.82, '\\textrm{\\textbf{CMS}}', transform=ax11.transAxes, fontsize=40)
    ax12.text(0.7, 0.82, '\\textrm{\\textbf{LHCb}}', transform=ax12.transAxes, fontsize=40)
    ax11.set_ylim(0.0, 0.3)
    ax12.set_ylim(-0.55, 0.3)
    majorLocator = MultipleLocator(0.1)
    minorLocator = MultipleLocator(0.025)
    ax11.yaxis.set_minor_locator(minorLocator)
    ax11.yaxis.set_major_locator(majorLocator)
    majorLocator = MultipleLocator(0.2)
    minorLocator = MultipleLocator(0.05)
    ax12.yaxis.set_minor_locator(minorLocator)
    ax12.yaxis.set_major_locator(majorLocator)
    ax11.set_yticks([0.1, 0.2])
    for ax in [ax11, ax12]:
        minorLocator = MultipleLocator(0.1)
        majorLocator = MultipleLocator(0.5)
        ax.xaxis.set_minor_locator(minorLocator)
        ax.xaxis.set_major_locator(majorLocator)
        ax.xaxis.set_tick_params(which='major', length=6)
        ax.xaxis.set_tick_params(which='minor', length=3)
        ax.yaxis.set_tick_params(which='major', length=6)
        ax.yaxis.set_tick_params(which='minor', length=3)
    axes['ax'] = {}
    axes['ax'][2010] = ax31
    axes['ax'][2011] = ax21
    axes['ax'][2012] = ax41
    axes['ax'][2013] = ax21
    axes['ax'][2014] = ax21
    axes['ax'][2016] = ax22
    axes['ax'][2017] = ax32
    for idx in data:
        if idx not in axes['ax']:
            continue
        ax = axes['ax'][idx]
        color = axes['color'][idx]
        marker = axes['marker'][idx]
        ms = 8
        for ic in range(nc):
            (eta0, eta1) = ([], [])
            (thy0, thy1, ratio0, ratio1, alpha0, alpha1) = ([], [], [], [], [], [])
            boson = datasets['wzrv'][idx]['boson']
            if 'eta' in datasets['wzrv'][idx]:
                eta = datasets['wzrv'][idx]['eta']
            else:
                eta = (datasets['wzrv'][idx]['eta_min'] + datasets['wzrv'][idx]['eta_max']) / 2.0
            thy = data[idx]['thy-%d' % ic]
            value = data[idx]['value']
            alpha = data[idx]['alpha']
            std_lower = data[idx]['dthy_lower-%d' % ic]
            std_upper = data[idx]['dthy_upper-%d' % ic]
            if ax in [ax21, ax31, ax41]:
                measure = thesis_ratio(value, thy, error=False)
            if ax in [ax21, ax31, ax41]:
                yerr = alpha / np.abs(thy)
            if ax in [ax21, ax31, ax41]:
                thyerr_lower = thesis_ratio(std_lower, thy, error=False)
                thyerr_upper = thesis_ratio(std_upper, thy, error=False)
            if ax in [ax22, ax32]:
                measure = value - thy
            if ax in [ax22, ax32]:
                yerr = alpha
            if ax in [ax22, ax32]:
                thyerr_lower = std_lower
                thyerr_upper = std_upper
            ax.errorbar(eta, measure, yerr=yerr, color=color, linestyle='none', marker=marker, ms=ms, capsize=3.0)
            if ax in [ax21, ax31, ax41]:
                ax.fill_between(eta, np.minimum(1 - thyerr_lower, 1 + thyerr_upper), np.maximum(1 - thyerr_lower, 1 + thyerr_upper), color='gold', alpha=1.0, where=thesis_band_mask(eta))
            if ax in [ax22, ax32]:
                ax.fill_between(eta, np.minimum(0 - thyerr_lower, 0 + thyerr_upper), np.maximum(0 - thyerr_lower, 0 + thyerr_upper), color='gold', alpha=1.0, where=thesis_band_mask(eta))
    for ax in [ax21, ax22, ax31, ax32, ax41]:
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=30)
    for ax in [ax21, ax22, ax31]:
        ax.tick_params(labelbottom=False)
    for ax in [ax32, ax41]:
        ax.set_xlabel('\\boldmath$|\\eta_{\\ell}|$', size=30)
    for ax in [ax21, ax22, ax31, ax32, ax41]:
        minorLocator = MultipleLocator(0.1)
        majorLocator = MultipleLocator(1.0)
        ax.xaxis.set_minor_locator(minorLocator)
        ax.xaxis.set_major_locator(majorLocator)
        ax.xaxis.set_tick_params(which='major', length=6)
        ax.xaxis.set_tick_params(which='minor', length=3)
        ax.yaxis.set_tick_params(which='major', length=6)
        ax.yaxis.set_tick_params(which='minor', length=3)
    for ax in [ax21, ax31, ax41]:
        ax.set_ylim(0.85, 1.15)
        ax.axhline(1, 0, 3, alpha=1.0, color='black', ls='--')
        minorLocator = MultipleLocator(0.025)
        majorLocator = MultipleLocator(0.1)
        ax.yaxis.set_minor_locator(minorLocator)
        ax.yaxis.set_major_locator(majorLocator)
        ax.set_yticks([0.9, 1.0, 1.1])
    for ax in [ax22, ax32]:
        ax.set_ylim(-0.05, 0.05)
        ax.axhline(0, 0, 3, alpha=1.0, color='black', ls='--')
        minorLocator = MultipleLocator(0.01)
        majorLocator = MultipleLocator(0.04)
        ax.yaxis.set_minor_locator(minorLocator)
        ax.yaxis.set_major_locator(majorLocator)
    for ax in [ax21, ax31, ax41]:
        ax.set_xticks([1, 2])
    for ax in [ax22, ax32]:
        ax.set_xticks([3, 4])
    (handles, labels) = ([], [])
    handles.append(hand.get(2011))
    handles.append(hand.get(2012))
    handles.append(hand.get(2010))
    labels.append('\\boldmath$\\sqrt{s}=7,p_T^{\\ell}>25$')
    labels.append('\\boldmath$\\sqrt{s}=7,p_T^{\\ell}>35$')
    labels.append('\\boldmath$\\sqrt{s}=8,p_T^{\\ell}>25$')
    thesis_legend(ax11, handles, labels, frameon=False, fontsize=22, loc='lower right', ncol=1, handletextpad=0.3, handlelength=1.0)
    (handles, labels) = ([], [])
    handles.append((thy_band, thy_plot))
    handles.append(hand.get(2016))
    handles.append(hand.get(2017))
    labels.append('\\textbf{\\textrm{JAM}}')
    labels.append('\\boldmath$\\sqrt{s}=7,p_T^{\\ell}>20$')
    labels.append('\\boldmath$\\sqrt{s}=8,p_T^{\\ell}>20$')
    thesis_legend(ax12, handles, labels, frameon=False, fontsize=22, loc='lower left', ncol=1, handletextpad=0.3, handlelength=1.0)
    ax21.text(0.05, 0.8, '\\textrm{\\textbf{data/theory}}', transform=ax21.transAxes, fontsize=25)
    ax31.text(0.05, 0.8, '\\textrm{\\textbf{data/theory}}', transform=ax31.transAxes, fontsize=25)
    ax41.text(0.05, 0.8, '\\textrm{\\textbf{data/theory}}', transform=ax41.transAxes, fontsize=25)
    ax22.text(0.05, 0.8, '\\textrm{\\textbf{data-theory}}', transform=ax22.transAxes, fontsize=25)
    ax32.text(0.05, 0.8, '\\textrm{\\textbf{data-theory}}', transform=ax32.transAxes, fontsize=25)
    py.tight_layout()
    py.subplots_adjust(hspace=0.0, wspace=0.2)
    filename = output_file
    ax11.set_rasterized(True)
    ax12.set_rasterized(True)
    ax21.set_rasterized(True)
    ax22.set_rasterized(True)
    thesis_finish_panels(py.gcf(), 'thesis_lepton_plot_asym', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/lepton.py:plot_star, original lines 321-469; SHA256 26ca48c0cb3cf5831916f162eb1644c98d2dba5cd0c2c8dbc9555fcf942ab760.
def thesis_lepton_plot_star(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['wzrv']
    tabs = data
    (nrows, ncols) = (3, 1)
    fig = py.figure(figsize=(ncols * 7, nrows * 2.5))
    ax11 = py.subplot(nrows, ncols, (1, 2))
    ax21 = py.subplot(nrows, ncols, 3)
    tabs = datasets['wzrv']
    data = predictions['reactions']['wzrv']
    hand = {}
    for idx in tabs:
        values = tabs[idx]['value']
        alpha = data[idx]['alpha']
        if idx == 2020:
            (ax, color, marker) = (ax11, 'black', '.')
        eta = tabs[idx]['eta']
        xerr = np.zeros((2, len(eta)))
        hand[idx] = ax.errorbar(eta, values, yerr=alpha, color=color, linestyle='none', marker=marker, ms=10, capsize=3.0, zorder=5)
    for idx in [2020]:
        if idx not in data:
            continue
        for ic in range(nc):
            if idx == 2020:
                ax = ax11
            (eta0, eta1) = ([], [])
            (thy0, thy1) = ([], [])
            (std0, std1) = ([], [])
            (up0, up1) = ([], [])
            (down0, down1) = ([], [])
            boson = datasets['wzrv'][idx]['boson']
            eta = tabs[idx]['eta']
            thy = data[idx]['thy-%d' % ic]
            std_lower = data[idx]['dthy_lower-%d' % ic]
            std_upper = data[idx]['dthy_upper-%d' % ic]
            down = thy - std_lower
            up = thy + std_upper
            (thy_plot,) = ax.plot(eta, thy, color='red')
            thy_band = ax.fill_between(eta, np.minimum(down, up), np.maximum(down, up), color='red', alpha=0.5, where=thesis_band_mask(eta))
    for idx in [2020]:
        if idx not in data:
            continue
        if idx == 2020:
            (ax, color) = (ax21, 'black')
        for ic in range(nc):
            if nc > 1:
                color = colors[cluster[ic]]
            eta = tabs[idx]['eta']
            thy = data[idx]['thy-%d' % ic]
            ratio = thesis_ratio(data[idx]['value'], thy, error=False)
            alpha = data[idx]['alpha']
            ax.errorbar(eta, ratio, yerr=thesis_ratio(alpha, thy, error=True), color=color, fmt='.', ms=10, capsize=3.0, zorder=5)
            ax.fill_between(eta, np.minimum(1 - thesis_ratio(std_lower, thy, error=False), 1 + thesis_ratio(std_upper, thy, error=False)), np.maximum(1 - thesis_ratio(std_lower, thy, error=False), 1 + thesis_ratio(std_upper, thy, error=False)), color='red', alpha=0.5, where=thesis_band_mask(eta))
            ax.axhline(1, 0, 3, color='black', ls='--')
    for ax in [ax11]:
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=30, labelbottom=False)
    for ax in [ax21]:
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=30)
    handles = [hand.get(2020), (thy_band, thy_plot)]
    label1 = '\\textbf{\\textrm{STAR}}'
    label2 = '\\textbf{\\textrm{JAM}}'
    labels = [label1, label2]
    thesis_legend(ax11, handles, labels, frameon=False, fontsize=25, loc='upper right', ncol=1, handletextpad=0.3, handlelength=1.0)
    ax21.set_xlabel('\\boldmath$\\eta_{\\ell}$', size=40)
    ax11.set_ylim(0.5, 9.5)
    ax21.set_ylim(0.5, 1.5)
    ax11.text(0.05, 0.75, '\\boldmath$\\sigma_{pp}^{W^+}/\\sigma_{pp}^{W^-}$', transform=ax11.transAxes, size=50)
    ax11.text(0.35, 0.17, '$\\sqrt{s} = 510$' + ' ' + '\\textrm{GeV}', transform=ax11.transAxes, size=23)
    ax11.text(0.35, 0.05, '\\rm{$p_T^{\\ell}$ $>$ 25 GeV}', transform=ax11.transAxes, size=23)
    ax21.text(0.02, 0.8, '\\textbf{\\textrm{data/theory}}', transform=ax21.transAxes, size=35)
    for ax in [ax11, ax21]:
        ax.xaxis.set_tick_params(which='major', length=6)
        ax.xaxis.set_tick_params(which='minor', length=3)
        ax.yaxis.set_tick_params(which='major', length=6)
        ax.yaxis.set_tick_params(which='minor', length=3)
        majorLocator = MultipleLocator(0.5)
        minorLocator = MultipleLocator(0.1)
        ax.xaxis.set_minor_locator(minorLocator)
        ax.xaxis.set_major_locator(majorLocator)
        ax.set_xlim(-1.4, 1.4)
    for ax in [ax11]:
        majorLocator = MultipleLocator(2)
        minorLocator = MultipleLocator(0.5)
        ax.yaxis.set_minor_locator(minorLocator)
        ax.yaxis.set_major_locator(majorLocator)
    for ax in [ax21]:
        majorLocator = MultipleLocator(0.3)
        minorLocator = MultipleLocator(0.1)
        ax.yaxis.set_minor_locator(minorLocator)
        ax.yaxis.set_major_locator(majorLocator)
        ax.yaxis.set_tick_params(which='major', length=6)
        ax.yaxis.set_tick_params(which='minor', length=3)
        ax.set_yticks([0.6, 1.0, 1.4])
    py.tight_layout()
    py.subplots_adjust(hspace=0.0, wspace=0.0)
    filename = output_file
    ax11.set_rasterized(True)
    ax21.set_rasterized(True)
    thesis_finish_panels(py.gcf(), 'thesis_lepton_plot_star', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/wz.py:integrate, original lines 46-54; SHA256 9d102977864f0dd8f8e6903bddca2eb554a4553d87d8c77f8a65a86ce6c76531.
def thesis_wz_integrate(dsig, Y):
    (Ymin, Ymax) = (np.min(Y), np.max(Y))
    gauss_Y = 99
    (x, w) = np.polynomial.legendre.leggauss(gauss_Y)
    Y_int = 0.5 * (x * (Ymax - Ymin) + Ymin + Ymax)
    jac = 0.5 * (Ymax - Ymin)
    integrand = griddata(Y, dsig, Y_int, method='cubic')
    return np.sum(w * integrand * jac)

# Adapted from thesis/wz.py:plot_main, original lines 57-376; SHA256 9d102977864f0dd8f8e6903bddca2eb554a4553d87d8c77f8a65a86ce6c76531.
def thesis_wz_plot_main(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    (nrows, ncols) = (4, 2)
    fig = py.figure(figsize=(ncols * 7, nrows * 2))
    gs = gridspec.GridSpec(4, 2)
    ax11 = fig.add_subplot(gs[0:2, 0])
    ax21 = fig.add_subplot(gs[2, 0])
    ax31 = fig.add_subplot(gs[3, 0])
    tabs = datasets.get('zrap', {})
    data = predictions['reactions'].get('zrap', {})
    hand = {}
    for idx in tabs:
        Y = tabs[idx]['Y']
        values = tabs[idx]['value']
        alpha = data[idx]['alpha']
        if 'normalization' in tabs[idx]:
            norm = tabs[idx]['normalization'][0]
        else:
            norm = 2 * thesis_wz_integrate(tabs[idx]['value'], Y)
        if idx == 1000:
            color = 'darkgreen'
        if idx == 1001:
            color = 'firebrick'
        hand[idx] = ax11.errorbar(Y, values / norm, yerr=alpha / norm, color=color, fmt='o', ms=2, capsize=3.0)
    for idx in data:
        for ic in range(nc):
            Y = datasets.get('zrap', {})[idx]['Y']
            if 'normalization' in tabs[idx]:
                norm = tabs[idx]['normalization'][0]
            else:
                norm = 2 * thesis_wz_integrate(tabs[idx]['value'], Y)
            if nc > 1:
                color = colors[cluster[ic]]
            thy = data[idx]['thy-%d' % ic] / norm
            std_lower = data[idx]['dthy_lower-%d' % ic] / norm
            std_upper = data[idx]['dthy_upper-%d' % ic] / norm
            down = thy - std_lower
            up = thy + std_upper
            (thy_plot,) = ax11.plot(Y, thy, color='black')
            thy_band = ax11.fill_between(Y, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(Y))
    for idx in data:
        if idx == 1000:
            (ax, color, label) = (ax21, 'darkgreen', '\\textbf{\\textrm{CDF(Z)}}')
        if idx == 1001:
            (ax, color, label) = (ax31, 'firebrick', '\\textbf{\\textrm{D0(Z)}}')
        for ic in range(nc):
            Y = datasets.get('zrap', {})[idx]['Y']
            if nc > 1:
                color = colors[cluster[ic]]
            if 'normalization' in tabs[idx]:
                norm = tabs[idx]['normalization'][0]
            else:
                norm = 2 * thesis_wz_integrate(tabs[idx]['value'], Y)
            thy = data[idx]['thy-%d' % ic]
            std_lower = data[idx]['dthy_lower-%d' % ic]
            std_upper = data[idx]['dthy_upper-%d' % ic]
            ratio = thesis_ratio(data[idx]['value'], thy, error=False)
            alpha = data[idx]['alpha']
            ax.errorbar(Y, ratio, yerr=thesis_ratio(alpha, thy, error=True), color=color, fmt='.', ms=10, capsize=3.0)
            ax.fill_between(Y, np.minimum(1 - thesis_ratio(std_lower, thy, error=False), 1 + thesis_ratio(std_upper, thy, error=False)), np.maximum(1 - thesis_ratio(std_lower, thy, error=False), 1 + thesis_ratio(std_upper, thy, error=False)), color='gold', alpha=1.0, where=thesis_band_mask(Y))
            ax.text(0.02, 0.78, label, fontsize=25, transform=ax.transAxes)
            ax.axhline(1, 0, 3, color='black', ls='--')
    for ax in [ax11, ax21]:
        ax.tick_params(axis='both', which='both', top=True, right=True, labelbottom=False, direction='in', labelsize=30)
        ax.set_xlim(0, 3)
    for ax in [ax31]:
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=30)
        ax.set_xlim(0, 3)
    for ax in [ax11]:
        ax.set_ylim(0, 0.32)
        minorLocator = MultipleLocator(0.1)
        majorLocator = MultipleLocator(1.0)
        ax.xaxis.set_minor_locator(minorLocator)
        ax.xaxis.set_major_locator(majorLocator)
        ax.xaxis.set_tick_params(which='major', length=6)
        ax.xaxis.set_tick_params(which='minor', length=3)
        minorLocator = MultipleLocator(0.02)
        majorLocator = MultipleLocator(0.1)
        ax.yaxis.set_minor_locator(minorLocator)
        ax.yaxis.set_major_locator(majorLocator)
        ax.yaxis.set_tick_params(which='major', length=6)
        ax.yaxis.set_tick_params(which='minor', length=3)
        ax.set_yticks([0, 0.1, 0.2, 0.3])
        ax.set_yticklabels(['', '$0.1$', '$0.2$', '$0.3$'])
    for ax in [ax21, ax31]:
        ax.set_ylim(0.75, 1.25)
        minorLocator = MultipleLocator(0.1)
        majorLocator = MultipleLocator(1.0)
        ax.xaxis.set_minor_locator(minorLocator)
        ax.xaxis.set_major_locator(majorLocator)
        ax.xaxis.set_tick_params(which='major', length=6)
        ax.xaxis.set_tick_params(which='minor', length=3)
        minorLocator = MultipleLocator(0.04)
        majorLocator = MultipleLocator(0.2)
        ax.yaxis.set_minor_locator(minorLocator)
        ax.yaxis.set_major_locator(majorLocator)
        ax.yaxis.set_tick_params(which='major', length=6)
        ax.yaxis.set_tick_params(which='minor', length=3)
    ax31.set_xlabel('\\boldmath$Y_Z$', size=30)
    ax11.text(0.1, 0.045, '\\boldmath$\\frac{1}{\\sigma} \\frac{d\\sigma(Z/\\gamma^*)}{dy}$', size=50)
    ax21.text(0.1, 0.8, '\\textbf{\\textrm{data/theory}}', size=30)
    ax11.text(0.1, 0.15, '$\\sqrt{s} = 1.96$' + ' ' + '\\textrm{TeV}', fontsize=25)
    handles = [hand.get(1000), hand.get(1001), (thy_band, thy_plot)]
    label1 = '\\textbf{\\textrm{CDF(Z)}}'
    label2 = '\\textbf{\\textrm{D0(Z)}}'
    label3 = '\\textbf{\\textrm{JAM}}'
    labels = [label1, label2, label3]
    thesis_legend(ax11, handles, labels, frameon=False, fontsize=22, loc='upper right', handletextpad=0.5, handlelength=1.5)
    ax11.set_rasterized(True)
    ax21.set_rasterized(True)
    ax31.set_rasterized(True)
    ax11 = fig.add_subplot(gs[0:2, 1])
    ax21 = fig.add_subplot(gs[2, 1])
    ax31 = fig.add_subplot(gs[3, 1])
    tabs = datasets.get('wasym', {})
    data = predictions['reactions'].get('wasym', {})
    hand = {}
    for idx in tabs:
        Y = tabs[idx]['Y']
        values = tabs[idx]['value']
        alpha = data[idx]['alpha']
        if idx == 1000:
            color = 'darkgreen'
        if idx == 1001:
            color = 'firebrick'
        hand[idx] = ax11.errorbar(Y, values, yerr=alpha, color=color, fmt='o', ms=2.0, capsize=3.0)
    for idx in data:
        for ic in range(nc):
            Y = tabs[idx]['Y']
            if nc > 1:
                color = colors[cluster[ic]]
            thy = data[idx]['thy-%d' % ic]
            std_lower = data[idx]['dthy_lower-%d' % ic]
            std_upper = data[idx]['dthy_upper-%d' % ic]
            down = thy - std_lower
            up = thy + std_upper
            (thy_plot,) = ax11.plot(Y, thy, color='black')
            thy_band = ax11.fill_between(Y, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(Y))
    for idx in data:
        if idx == 1000:
            (ax, color, label) = (ax21, 'darkgreen', '\\textbf{\\textrm{CDF(W)}}')
        if idx == 1001:
            (ax, color, label) = (ax31, 'firebrick', '\\textbf{\\textrm{D0(W)}}')
        for ic in range(nc):
            Y = datasets.get('wasym', {})[idx]['Y']
            if nc > 1:
                color = colors[cluster[ic]]
            thy = data[idx]['thy-%d' % ic]
            std_lower = data[idx]['dthy_lower-%d' % ic]
            std_upper = data[idx]['dthy_upper-%d' % ic]
            ratio = thesis_ratio(data[idx]['value'], thy, error=False)
            alpha = data[idx]['alpha']
            ax.errorbar(Y, ratio, yerr=thesis_ratio(alpha, thy, error=True), color=color, fmt='.', ms=10, capsize=3.0)
            ax.fill_between(Y, np.minimum(1 - thesis_ratio(std_lower, thy, error=False), 1 + thesis_ratio(std_upper, thy, error=False)), np.maximum(1 - thesis_ratio(std_lower, thy, error=False), 1 + thesis_ratio(std_upper, thy, error=False)), color='gold', alpha=1.0, where=thesis_band_mask(Y))
            if idx == 1000:
                ax.text(0.74, 0.78, label, fontsize=25, transform=ax.transAxes)
            if idx == 1001:
                ax.text(0.79, 0.78, label, fontsize=25, transform=ax.transAxes)
            ax.axhline(1, 0, 3, color='black', ls='--')
    for ax in [ax11, ax21]:
        ax.tick_params(axis='both', which='both', top=True, right=True, labelbottom=False, direction='in', labelsize=30)
        ax.set_xlim(0, 3)
    for ax in [ax31]:
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=30)
        ax.set_xlim(0, 3)
    for ax in [ax11]:
        ax.set_ylim(0, 0.85)
        minorLocator = MultipleLocator(0.1)
        majorLocator = MultipleLocator(1.0)
        ax.xaxis.set_minor_locator(minorLocator)
        ax.xaxis.set_major_locator(majorLocator)
        ax.xaxis.set_tick_params(which='major', length=6)
        ax.xaxis.set_tick_params(which='minor', length=3)
        minorLocator = MultipleLocator(0.02)
        majorLocator = MultipleLocator(0.2)
        ax.yaxis.set_minor_locator(minorLocator)
        ax.yaxis.set_major_locator(majorLocator)
        ax.yaxis.set_tick_params(which='major', length=6)
        ax.yaxis.set_tick_params(which='minor', length=3)
        ax.set_yticks([0.2, 0.4, 0.6, 0.8])
        ax.set_yticklabels(['$0.2$', '$0.4$', '$0.6$', '$0.8$'])
    for ax in [ax21, ax31]:
        ax.set_ylim(0.75, 1.25)
        minorLocator = MultipleLocator(0.1)
        majorLocator = MultipleLocator(1.0)
        ax.xaxis.set_minor_locator(minorLocator)
        ax.xaxis.set_major_locator(majorLocator)
        ax.xaxis.set_tick_params(which='major', length=6)
        ax.xaxis.set_tick_params(which='minor', length=3)
        minorLocator = MultipleLocator(0.04)
        majorLocator = MultipleLocator(0.2)
        ax.yaxis.set_minor_locator(minorLocator)
        ax.yaxis.set_major_locator(majorLocator)
        ax.yaxis.set_tick_params(which='major', length=6)
        ax.yaxis.set_tick_params(which='minor', length=3)
    ax31.set_xlabel('\\boldmath$Y_W$', size=30)
    ax11.text(2.2, 0.04, '\\boldmath$A_W$', size=50)
    ax21.text(0.1, 0.8, '\\textbf{\\textrm{data/theory}}', size=30)
    ax11.text(0.1, 0.3, '$\\sqrt{s} = 1.96$' + ' ' + '\\textrm{TeV}', fontsize=25)
    (handles, labels) = ([], [])
    handles.append(hand.get(1000))
    handles.append(hand.get(1001))
    labels.append('\\textbf{\\textrm{CDF(W)}}')
    labels.append('\\textbf{\\textrm{D0(W)}}')
    thesis_legend(ax11, handles, labels, frameon=False, fontsize=22, loc='upper left', handletextpad=0.5, handlelength=1.5)
    py.tight_layout()
    py.subplots_adjust(hspace=0)
    filename = output_file
    ax11.set_rasterized(True)
    ax21.set_rasterized(True)
    ax31.set_rasterized(True)
    thesis_finish_panels(py.gcf(), 'thesis_wz_plot_main', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/pidis.py:get_Q2bins, original lines 36-173; SHA256 c6447a2b83bfa2d4b5008b0a926c14cde97718d4e7f25b5c45ae59e148c5ee72.
def thesis_pidis_get_Q2bins(data, kind):
    query = {}
    if kind == 'p A1':
        query[6] = data.query('Q2 > 60.0')
        query[5] = data.query('Q2 > 40.0 and Q2 <= 60.0')
        query[4] = data.query('Q2 > 20.0 and Q2 <= 40.0')
        query[3] = data.query('Q2 > 10.0 and Q2 <= 20.0')
        query[2] = data.query('Q2 > 5.00 and Q2 <= 10.0')
        query[1] = data.query('Q2 > 2.00 and Q2 <= 5.00')
        query[0] = data.query('Q2 > 1.00 and Q2 <= 2.00')
    if kind == 'p Apa':
        query[8] = data.query('Q2 > 20.0')
        query[7] = data.query('Q2 > 15.0 and Q2 <= 20.0')
        query[6] = data.query('Q2 > 10.0 and Q2 <= 15.0')
        query[5] = data.query('Q2 > 7.00 and Q2 <= 10.0')
        query[4] = data.query('Q2 > 5.00 and Q2 <= 7.00')
        query[3] = data.query('Q2 > 4.00 and Q2 <= 5.00')
        query[2] = data.query('Q2 > 3.00 and Q2 <= 4.00')
        query[1] = data.query('Q2 > 2.00 and Q2 <= 3.00')
        query[0] = data.query('Q2 > 1.00 and Q2 <= 2.00')
    if kind == 'p Apa EG1b':
        query[8] = data.query('Q2 > 4.50')
        query[7] = data.query('Q2 > 4.00 and Q2 <= 4.50')
        query[6] = data.query('Q2 > 3.50 and Q2 <= 4.00')
        query[5] = data.query('Q2 > 3.00 and Q2 <= 3.50')
        query[4] = data.query('Q2 > 2.50 and Q2 <= 3.00')
        query[3] = data.query('Q2 > 2.20 and Q2 <= 2.50')
        query[2] = data.query('Q2 > 2.00 and Q2 <= 2.20')
        query[1] = data.query('Q2 > 1.80 and Q2 <= 2.00')
        query[0] = data.query('Q2 > 1.00 and Q2 <= 1.80')
    if kind == 'p Apa DVCS':
        query[6] = data.query('Q2 > 4.50')
        query[5] = data.query('Q2 > 3.50 and Q2 <= 4.50')
        query[4] = data.query('Q2 > 3.00 and Q2 <= 3.50')
        query[3] = data.query('Q2 > 2.50 and Q2 <= 3.00')
        query[2] = data.query('Q2 > 2.20 and Q2 <= 2.50')
        query[1] = data.query('Q2 > 2.00 and Q2 <= 2.20')
        query[0] = data.query('Q2 > 1.00 and Q2 <= 2.00')
    if kind == 'd A1':
        query[4] = data.query('Q2 > 40.0')
        query[3] = data.query('Q2 > 20.0 and Q2 <= 40.0')
        query[2] = data.query('Q2 > 10.0 and Q2 <= 20.0')
        query[1] = data.query('Q2 > 5.00 and Q2 <= 10.0')
        query[0] = data.query('Q2 > 1.00 and Q2 <= 5.00')
    if kind == 'd Apa':
        query[8] = data.query('Q2 > 20.0')
        query[7] = data.query('Q2 > 15.0 and Q2 <= 20.0')
        query[6] = data.query('Q2 > 10.0 and Q2 <= 15.0')
        query[5] = data.query('Q2 > 7.00 and Q2 <= 10.0')
        query[4] = data.query('Q2 > 5.00 and Q2 <= 7.00')
        query[3] = data.query('Q2 > 4.00 and Q2 <= 5.00')
        query[2] = data.query('Q2 > 3.00 and Q2 <= 4.00')
        query[1] = data.query('Q2 > 2.00 and Q2 <= 3.00')
        query[0] = data.query('Q2 > 1.00 and Q2 <= 2.00')
    if kind == 'd Apa EG1b':
        query[8] = data.query('Q2 > 4.50')
        query[7] = data.query('Q2 > 4.00 and Q2 <= 4.50')
        query[6] = data.query('Q2 > 3.50 and Q2 <= 4.00')
        query[5] = data.query('Q2 > 3.00 and Q2 <= 3.50')
        query[4] = data.query('Q2 > 2.50 and Q2 <= 3.00')
        query[3] = data.query('Q2 > 2.20 and Q2 <= 2.50')
        query[2] = data.query('Q2 > 2.00 and Q2 <= 2.20')
        query[1] = data.query('Q2 > 1.80 and Q2 <= 2.00')
        query[0] = data.query('Q2 > 1.60 and Q2 <= 1.80')
    if kind == 'd Apa DVCS':
        query[5] = data.query('Q2 > 3.50')
        query[4] = data.query('Q2 > 3.00 and Q2 <= 3.50')
        query[3] = data.query('Q2 > 2.50 and Q2 <= 3.00')
        query[2] = data.query('Q2 > 2.20 and Q2 <= 2.50')
        query[1] = data.query('Q2 > 2.00 and Q2 <= 2.20')
        query[0] = data.query('Q2 > 1.60 and Q2 <= 2.00')
    if kind == 'p A2':
        query[2] = data.query('Q2 > 5.00')
        query[1] = data.query('Q2 > 2.00 and Q2 <= 5.00')
        query[0] = data.query('Q2 > 1.00 and Q2 <= 2.00')
    if kind == 'p Ape':
        query[8] = data.query('Q2 > 20.0')
        query[7] = data.query('Q2 > 15.0 and Q2 <= 20.0')
        query[6] = data.query('Q2 > 10.0 and Q2 <= 15.0')
        query[5] = data.query('Q2 > 7.00 and Q2 <= 10.0')
        query[4] = data.query('Q2 > 5.00 and Q2 <= 7.00')
        query[3] = data.query('Q2 > 4.00 and Q2 <= 5.00')
        query[2] = data.query('Q2 > 3.00 and Q2 <= 4.00')
        query[1] = data.query('Q2 > 2.00 and Q2 <= 3.00')
        query[0] = data.query('Q2 > 1.00 and Q2 <= 2.00')
    if kind == 'p Atpe':
        query[7] = data.query('Q2 > 15.0')
        query[6] = data.query('Q2 > 10.0 and Q2 <= 15.0')
        query[5] = data.query('Q2 > 7.00 and Q2 <= 10.0')
        query[4] = data.query('Q2 > 5.00 and Q2 <= 7.00')
        query[3] = data.query('Q2 > 4.00 and Q2 <= 5.00')
        query[2] = data.query('Q2 > 3.00 and Q2 <= 4.00')
        query[1] = data.query('Q2 > 2.00 and Q2 <= 3.00')
        query[0] = data.query('Q2 > 1.00 and Q2 <= 2.00')
    if kind == 'd Ape':
        query[8] = data.query('Q2 > 20.0')
        query[7] = data.query('Q2 > 15.0 and Q2 <= 20.0')
        query[6] = data.query('Q2 > 10.0 and Q2 <= 15.0')
        query[5] = data.query('Q2 > 7.00 and Q2 <= 10.0')
        query[4] = data.query('Q2 > 5.00 and Q2 <= 7.00')
        query[3] = data.query('Q2 > 4.00 and Q2 <= 5.00')
        query[2] = data.query('Q2 > 3.00 and Q2 <= 4.00')
        query[1] = data.query('Q2 > 2.00 and Q2 <= 3.00')
        query[0] = data.query('Q2 > 1.00 and Q2 <= 2.00')
    if kind == 'd Atpe':
        query[7] = data.query('Q2 > 15.0')
        query[6] = data.query('Q2 > 10.0 and Q2 <= 15.0')
        query[5] = data.query('Q2 > 7.00 and Q2 <= 10.0')
        query[4] = data.query('Q2 > 5.00 and Q2 <= 7.00')
        query[3] = data.query('Q2 > 4.00 and Q2 <= 5.00')
        query[2] = data.query('Q2 > 3.00 and Q2 <= 4.00')
        query[1] = data.query('Q2 > 2.00 and Q2 <= 3.00')
        query[0] = data.query('Q2 > 1.00 and Q2 <= 2.00')
    if kind == 'h A1':
        query[0] = data.query('Q2 > 1.00')
    if kind == 'h Apa':
        query[3] = data.query('Q2 > 10.0')
        query[2] = data.query('Q2 > 5.00 and Q2 <= 10.0')
        query[1] = data.query('Q2 > 3.00 and Q2 <= 5.00')
        query[0] = data.query('Q2 > 1.00 and Q2 <= 3.00')
    if kind == 'h A2':
        query[0] = data.query('Q2 > 1.00')
    if kind == 'h Ape':
        query[3] = data.query('Q2 > 10.0')
        query[2] = data.query('Q2 > 5.00 and Q2 <= 10.0')
        query[1] = data.query('Q2 > 3.00 and Q2 <= 5.00')
        query[0] = data.query('Q2 > 1.00 and Q2 <= 3.00')
    return query

# Adapted from thesis/pidis.py:get_plot, original lines 175-200; SHA256 c6447a2b83bfa2d4b5008b0a926c14cde97718d4e7f25b5c45ae59e148c5ee72.
def thesis_pidis_get_plot(query, cluster_i=0):
    plot = {_: {} for _ in ['theory', 'X', 'Q2', 'value', 'alpha', 'std_lower', 'std_upper']}
    for key in query:
        theory = query[key]['thy-%d' % cluster_i]
        std_lower = query[key]['dthy_lower-%d' % cluster_i]
        std_upper = query[key]['dthy_upper-%d' % cluster_i]
        X = query[key]['X']
        Q2 = query[key]['Q2']
        value = query[key]['value']
        alpha = query[key]['alpha']
        zx = sorted(zip(Q2, X), key=lambda row: row[0])
        zt = sorted(zip(Q2, theory), key=lambda row: row[0])
        zv = sorted(zip(Q2, value), key=lambda row: row[0])
        za = sorted(zip(Q2, alpha), key=lambda row: row[0])
        zs_lower = sorted(zip(Q2, std_lower), key=lambda row: row[0])
        zs_upper = sorted(zip(Q2, std_upper), key=lambda row: row[0])
        plot['X'][key] = np.array([zx[i][1] for i in range(len(zx))])
        plot['theory'][key] = np.array([zt[i][1] for i in range(len(zt))])
        plot['value'][key] = np.array([zv[i][1] for i in range(len(zv))])
        plot['alpha'][key] = np.array([za[i][1] for i in range(len(za))])
        plot['std_lower'][key] = np.array([zs_lower[i][1] for i in range(len(zs_lower))])
        plot['std_upper'][key] = np.array([zs_upper[i][1] for i in range(len(zs_upper))])
        plot['Q2'][key] = np.array(sorted(Q2))
    return plot

# Adapted from thesis/pidis.py:get_theory, original lines 202-244; SHA256 c6447a2b83bfa2d4b5008b0a926c14cde97718d4e7f25b5c45ae59e148c5ee72.
def thesis_pidis_get_theory(PLOT, nbins, loop=True, funcQ2=True, funcX=False):
    theory = {}
    if funcQ2:
        svar = 'Q2'
    if funcX:
        svar = 'X'
    theory = {_: {} for _ in [svar, 'value', 'std_lower', 'std_upper']}
    for key in range(nbins):
        var = []
        thy = []
        std_lower = []
        std_upper = []
        if loop:
            for exp in PLOT:
                var.extend(PLOT[exp][svar][key])
                thy.extend(PLOT[exp]['theory'][key])
                std_lower.extend(PLOT[exp]['std_lower'][key])
                std_upper.extend(PLOT[exp]['std_upper'][key])
        else:
            var.extend(PLOT[svar][key])
            thy.extend(PLOT['theory'][key])
            std_lower.extend(PLOT['std_lower'][key])
            std_upper.extend(PLOT['std_upper'][key])
        if len(var) == 0:
            continue
        vmin = np.min(var)
        vmax = np.max(var)
        theory[svar][key] = np.geomspace(vmin, vmax, 100)
        if len(var) > 1:
            theory['value'][key] = griddata(np.array(var), np.array(thy), theory[svar][key], method='linear')
            theory['std_lower'][key] = griddata(np.array(var), np.array(std_lower), theory[svar][key], method='linear')
            theory['std_upper'][key] = griddata(np.array(var), np.array(std_upper), theory[svar][key], method='linear')
        else:
            theory['value'][key] = np.ones(100) * thy
            theory['std_lower'][key] = np.ones(100) * std_lower
            theory['std_upper'][key] = np.ones(100) * std_upper
    return theory

# Adapted from thesis/pidis.py:get_details, original lines 246-277; SHA256 c6447a2b83bfa2d4b5008b0a926c14cde97718d4e7f25b5c45ae59e148c5ee72.
def thesis_pidis_get_details(exp):
    if exp == 'COMPASS1':
        (color, marker, ms) = ('firebrick', '*', 6)
    if exp == 'COMPASS2':
        (color, marker, ms) = ('firebrick', '*', 6)
    if exp == 'EMC':
        (color, marker, ms) = ('darkgreen', '^', 6)
    if exp == 'SMC1':
        (color, marker, ms) = ('blue', 'o', 6)
    if exp == 'SMC2':
        (color, marker, ms) = ('blue', 'o', 6)
    if exp == 'HERMES':
        (color, marker, ms) = ('firebrick', '*', 6)
    if exp == 'SLACE143':
        (color, marker, ms) = ('darkgreen', '^', 6)
    if exp == 'SLACE155':
        (color, marker, ms) = ('blue', 'o', 6)
    if exp == 'SLACE80E130':
        (color, marker, ms) = ('black', 'o', 6)
    if exp == 'EG1b4.2':
        (color, marker, ms) = ('firebrick', '*', 6)
    if exp == 'EG1b5.7':
        (color, marker, ms) = ('blue', 'o', 6)
    if exp == 'DVCS4.8':
        (color, marker, ms) = ('firebrick', '*', 6)
    if exp == 'DVCS6.0':
        (color, marker, ms) = ('blue', '^', 6)
    if exp == 'DVCS5.7':
        (color, marker, ms) = ('firebrick', '*', 6)
    if exp == 'SLAC E155x':
        (color, marker, ms) = ('firebrick', '*', 6)
    if exp == 'SLACE142':
        (color, marker, ms) = ('darkgreen', '^', 6)
    if exp == 'SLACE154':
        (color, marker, ms) = ('firebrick', '*', 6)
    if exp == 'JLabE06E014':
        (color, marker, ms) = ('darkgreen', '^', 6)
    if exp == 'JLabE99E117':
        (color, marker, ms) = ('blue', 'o', 6)
    return (color, marker, ms)

# Adapted from thesis/pidis.py:plot_proton_A1_Apa, original lines 279-496; SHA256 c6447a2b83bfa2d4b5008b0a926c14cde97718d4e7f25b5c45ae59e148c5ee72.
def thesis_pidis_plot_proton_A1_Apa(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['pidis']
    tabs = data
    (nrows, ncols) = (1, 2)
    py.figure(figsize=(ncols * 12.0, nrows * 14.0))
    ax11 = py.subplot(nrows, ncols, 1)
    ax12 = py.subplot(nrows, ncols, 2)
    if any((i in data for i in [10002, 10003, 10004, 10035, 10036])):
        DATA = {}
        if 10002 in data:
            DATA['COMPASS1'] = pd.DataFrame(data[10002])
        if 10003 in data:
            DATA['COMPASS2'] = pd.DataFrame(data[10003])
        if 10004 in data:
            DATA['EMC'] = pd.DataFrame(data[10004])
        if 10035 in data:
            DATA['SMC1'] = pd.DataFrame(data[10035])
        if 10036 in data:
            DATA['SMC2'] = pd.DataFrame(data[10036])
        PLOT = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_pidis_get_Q2bins(DATA[exp], 'p A1')
                PLOT[exp] = thesis_pidis_get_plot(query)
            nbins = len(query)
            theory = thesis_pidis_get_theory(PLOT, nbins, funcX=True, funcQ2=False)
        N = 1.0
        hand = {}
        for exp in PLOT:
            for key in range(nbins):
                X = PLOT[exp]['X'][key]
                val = PLOT[exp]['value'][key] + N * float(key)
                alpha = PLOT[exp]['alpha'][key]
                (color, marker, ms) = thesis_pidis_get_details(exp)
                hand[exp] = ax11.errorbar(X, val, alpha, marker=marker, color=color, ms=ms, capsize=3, linestyle='none')
        for key in theory['value']:
            X = theory['X'][key]
            mean = theory['value'][key] + N * float(key)
            std_lower = theory['std_lower'][key]
            std_upper = theory['std_upper'][key]
            down = mean - std_lower
            up = mean + std_upper
            (thy_plot,) = ax11.plot(X, mean, linestyle='solid', color='black')
            thy_band = ax11.fill_between(X, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(X))
            ax11.plot(np.linspace(np.min(X), np.max(X), 10), np.ones(10) * float(N * key), color='purple', ls=':')
        if any((len(v['value'].get(6, [])) for v in PLOT.values())):
            ax11.text(0.52, 0.89, '$Q^2 > 60~{\\rm GeV^2} \\, (i=6)$', transform=ax11.transAxes, fontsize=25)
        if any((len(v['value'].get(5, [])) for v in PLOT.values())):
            ax11.text(0.51, 0.75, '$40 < Q^2 < 60$', transform=ax11.transAxes, fontsize=25)
        if any((len(v['value'].get(4, [])) for v in PLOT.values())):
            ax11.text(0.44, 0.59, '$20 < Q^2 < 40$', transform=ax11.transAxes, fontsize=25)
        if any((len(v['value'].get(3, [])) for v in PLOT.values())):
            ax11.text(0.27, 0.44, '$10 < Q^2 < 20$', transform=ax11.transAxes, fontsize=25)
        if any((len(v['value'].get(2, [])) for v in PLOT.values())):
            ax11.text(0.68, 0.32, '$5 < Q^2 < 10$', transform=ax11.transAxes, fontsize=25)
        if any((len(v['value'].get(1, [])) for v in PLOT.values())):
            ax11.text(0.57, 0.17, '$2 < Q^2 < 5$', transform=ax11.transAxes, fontsize=25)
        if any((len(v['value'].get(0, [])) for v in PLOT.values())):
            ax11.text(0.27, 0.03, '$1 < Q^2 < 2\\, (i=0)$', transform=ax11.transAxes, fontsize=25)
        ax11.semilogx()
        ax11.set_xlim(0.005, 0.7)
        ax11.set_ylim(-0.2, 7.2)
        ax11.tick_params(axis='both', labelsize=30)
        ax11.yaxis.set_tick_params(which='major', length=10)
        ax11.yaxis.set_tick_params(which='minor', length=5)
        ax11.xaxis.set_tick_params(which='major', length=10)
        ax11.xaxis.set_tick_params(which='minor', length=5)
        ax11.set_xticks([0.01, 0.1])
        ax11.set_xticklabels(['$0.01$', '$0.1$'])
        ax11.text(0.05, 0.65, '\\boldmath$A_1^p$', transform=ax11.transAxes, size=60)
        ax11.text(0.16, 0.68, '$(+\\, i)$', transform=ax11.transAxes, size=40)
        ax11.set_xlabel('\\boldmath$x_{\\rm bj}$', size=40)
        ax11.yaxis.set_ticks_position('both')
        ax11.xaxis.set_ticks_position('both')
        ax11.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax11.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        majorLocator = MultipleLocator(2.0)
        minorLocator = MultipleLocator(0.4)
        ax11.yaxis.set_major_locator(majorLocator)
        ax11.yaxis.set_minor_locator(minorLocator)
        handles = [hand.get('COMPASS1'), hand.get('EMC'), hand.get('SMC1'), (thy_band, thy_plot)]
        label1 = '\\textbf{\\textrm{COMPASS}}'
        label2 = '\\textbf{\\textrm{EMC}}'
        label3 = '\\textbf{\\textrm{SMC}}'
        label4 = '\\textbf{\\textrm{JAM}}'
        labels = [label1, label2, label3, label4]
        thesis_legend(ax11, handles, labels, loc='upper left', fontsize=35, frameon=0, handletextpad=0.3, handlelength=1.0)
    if any((i in data for i in [10007, 10022, 10029, 10032])):
        DATA = {}
        if 10007 in data:
            DATA['HERMES'] = pd.DataFrame(data[10007])
        if 10022 in data:
            DATA['SLACE143'] = pd.DataFrame(data[10022])
        if 10029 in data:
            DATA['SLACE155'] = pd.DataFrame(data[10029])
        if 10032 in data:
            DATA['SLACE80E130'] = pd.DataFrame(data[10032])
        PLOT = {}
        theory = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_pidis_get_Q2bins(DATA[exp], 'p Apa')
                PLOT[exp] = thesis_pidis_get_plot(query)
                nbins = len(query)
                theory[exp] = thesis_pidis_get_theory(PLOT[exp], nbins, funcX=True, funcQ2=False, loop=False)
        N = 1.0
        hand = {}
        x = {}
        for exp in PLOT:
            for key in range(nbins):
                X = PLOT[exp]['X'][key]
                val = PLOT[exp]['value'][key] + N * float(key)
                alpha = PLOT[exp]['alpha'][key]
                (color, marker, ms) = thesis_pidis_get_details(exp)
                hand[exp] = ax12.errorbar(X, val, alpha, marker=marker, color=color, ms=ms, capsize=3, linestyle='none')
            for key in theory[exp]['value']:
                X = theory[exp]['X'][key]
                mean = theory[exp]['value'][key] + N * float(key)
                std_lower = theory[exp]['std_lower'][key]
                std_upper = theory[exp]['std_upper'][key]
                down = mean - std_lower
                up = mean + std_upper
                (thy_plot,) = ax12.plot(X, mean, linestyle='solid', color=color)
                thy_band = ax12.fill_between(X, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(X))
                if key not in x:
                    x[key] = []
                x[key].extend(X)
        for key in x:
            ax12.plot(np.linspace(np.min(x[key]), np.max(x[key]), 10), np.ones(10) * float(N * key), color='purple', ls=':')
        if any((len(v['value'].get(8, [])) for v in PLOT.values())):
            ax12.text(0.46, 0.92, '$Q^2 > 20~{\\rm GeV^2} \\, (i=9)$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(7, [])) for v in PLOT.values())):
            ax12.text(0.46, 0.81, '$15 < Q^2 < 20$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(6, [])) for v in PLOT.values())):
            ax12.text(0.36, 0.69, '$10 < Q^2 < 15$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(5, [])) for v in PLOT.values())):
            ax12.text(0.36, 0.57, '$7 < Q^2 < 10$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(4, [])) for v in PLOT.values())):
            ax12.text(0.28, 0.47, '$5 < Q^2 < 7$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(3, [])) for v in PLOT.values())):
            ax12.text(0.19, 0.36, '$4 < Q^2 < 5$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(2, [])) for v in PLOT.values())):
            ax12.text(0.26, 0.25, '$3 < Q^2 < 4$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(1, [])) for v in PLOT.values())):
            ax12.text(0.1, 0.13, '$2 < Q^2 < 3$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(0, [])) for v in PLOT.values())):
            ax12.text(0.6, 0.03, '$1 < Q^2 < 2\\, (i=0)$', transform=ax12.transAxes, fontsize=25)
        ax12.semilogx()
        ax12.set_xlim(0.01, 0.9)
        ax12.set_ylim(-0.2, 9.0)
        ax12.set_xlabel('\\boldmath$x_{\\rm bj}$', size=40)
        ax12.tick_params(axis='both', labelsize=30)
        ax12.yaxis.set_tick_params(which='major', length=10)
        ax12.yaxis.set_tick_params(which='minor', length=5)
        ax12.xaxis.set_tick_params(which='major', length=10)
        ax12.xaxis.set_tick_params(which='minor', length=5)
        ax12.set_xticks([0.02, 0.1, 0.5])
        ax12.set_xticklabels(['$0.02$', '$0.1$', '$0.5$'])
        ax12.text(0.05, 0.65, '\\boldmath$A_{\\parallel}^p$', transform=ax12.transAxes, size=60)
        ax12.text(0.16, 0.68, '$(+\\, i)$', transform=ax12.transAxes, size=40)
        ax12.yaxis.set_ticks_position('both')
        ax12.xaxis.set_ticks_position('both')
        ax12.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax12.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        majorLocator = MultipleLocator(2.0)
        minorLocator = MultipleLocator(0.4)
        ax12.yaxis.set_major_locator(majorLocator)
        ax12.yaxis.set_minor_locator(minorLocator)
        handles = [hand.get('HERMES'), hand.get('SLACE143'), hand.get('SLACE155'), hand.get('SLACE80E130')]
        label1 = '\\textbf{\\textrm{HERMES}}'
        label2 = '\\textbf{\\textrm{SLAC E143}}'
        label3 = '\\textbf{\\textrm{SLAC E155}}'
        label4 = '\\textbf{\\textrm{SLAC E80E130}}'
        labels = [label1, label2, label3, label4]
        thesis_legend(ax12, handles, labels, loc='upper left', fontsize=35, frameon=0, handletextpad=0.3, handlelength=1.0)
    py.tight_layout()
    filename = output_file
    ax11.set_rasterized(True)
    ax12.set_rasterized(True)
    thesis_finish_panels(py.gcf(), 'thesis_pidis_plot_proton_A1_Apa', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/pidis.py:plot_deuteron_A1_Apa, original lines 498-708; SHA256 c6447a2b83bfa2d4b5008b0a926c14cde97718d4e7f25b5c45ae59e148c5ee72.
def thesis_pidis_plot_deuteron_A1_Apa(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['pidis']
    tabs = data
    (nrows, ncols) = (1, 2)
    py.figure(figsize=(ncols * 12.0, nrows * 14.0))
    ax11 = py.subplot(nrows, ncols, 1)
    ax12 = py.subplot(nrows, ncols, 2)
    if any((i in data for i in [10001, 10033, 10034])):
        DATA = {}
        if 10001 in data:
            DATA['COMPASS1'] = pd.DataFrame(data[10001])
        if 10033 in data:
            DATA['SMC1'] = pd.DataFrame(data[10033])
        if 10034 in data:
            DATA['SMC2'] = pd.DataFrame(data[10034])
        PLOT = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_pidis_get_Q2bins(DATA[exp], 'd A1')
                PLOT[exp] = thesis_pidis_get_plot(query)
            nbins = len(query)
            theory = thesis_pidis_get_theory(PLOT, nbins, funcX=True, funcQ2=False)
        N = 1.0
        hand = {}
        for exp in PLOT:
            for key in range(nbins):
                X = PLOT[exp]['X'][key]
                val = PLOT[exp]['value'][key] + N * float(key)
                alpha = PLOT[exp]['alpha'][key]
                (color, marker, ms) = thesis_pidis_get_details(exp)
                hand[exp] = ax11.errorbar(X, val, alpha, marker=marker, color=color, ms=ms, capsize=3, linestyle='none')
        for key in theory['value']:
            X = theory['X'][key]
            mean = theory['value'][key] + N * float(key)
            std_lower = theory['std_lower'][key]
            std_upper = theory['std_upper'][key]
            down = mean - std_lower
            up = mean + std_upper
            (thy_plot,) = ax11.plot(X, mean, linestyle='solid', color='black')
            thy_band = ax11.fill_between(X, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(X))
            ax11.plot(np.linspace(np.min(X), np.max(X), 10), np.ones(10) * float(N * key), color='purple', ls=':')
        if any((len(v['value'].get(4, [])) for v in PLOT.values())):
            ax11.text(0.56, 0.86, '$Q^2 > 40~{\\rm GeV^2} \\, (i=4)$', transform=ax11.transAxes, fontsize=25)
        if any((len(v['value'].get(3, [])) for v in PLOT.values())):
            ax11.text(0.44, 0.62, '$20 < Q^2 < 40$', transform=ax11.transAxes, fontsize=25)
        if any((len(v['value'].get(2, [])) for v in PLOT.values())):
            ax11.text(0.35, 0.43, '$10 < Q^2 < 20$', transform=ax11.transAxes, fontsize=25)
        if any((len(v['value'].get(1, [])) for v in PLOT.values())):
            ax11.text(0.57, 0.25, '$5 < Q^2 < 10$', transform=ax11.transAxes, fontsize=25)
        if any((len(v['value'].get(0, [])) for v in PLOT.values())):
            ax11.text(0.41, 0.03, '$m_c^2 < Q^2 < 5\\, (i=0)$', transform=ax11.transAxes, fontsize=25)
        ax11.semilogx()
        ax11.set_xlim(0.005, 0.7)
        ax11.set_ylim(-0.2, 4.8)
        ax11.tick_params(axis='both', labelsize=30)
        ax11.yaxis.set_tick_params(which='major', length=10)
        ax11.yaxis.set_tick_params(which='minor', length=5)
        ax11.xaxis.set_tick_params(which='major', length=10)
        ax11.xaxis.set_tick_params(which='minor', length=5)
        ax11.set_xticks([0.01, 0.1])
        ax11.set_xticklabels(['$0.01$', '$0.1$'])
        ax11.text(0.05, 0.65, '\\boldmath$A_1^D$', transform=ax11.transAxes, size=60)
        ax11.text(0.16, 0.68, '$(+\\, i)$', transform=ax11.transAxes, size=40)
        ax11.set_xlabel('\\boldmath$x_{\\rm bj}$', size=40)
        ax11.yaxis.set_ticks_position('both')
        ax11.xaxis.set_ticks_position('both')
        ax11.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax11.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        majorLocator = MultipleLocator(1.0)
        minorLocator = MultipleLocator(0.25)
        ax11.yaxis.set_major_locator(majorLocator)
        ax11.yaxis.set_minor_locator(minorLocator)
        handles = [hand.get('COMPASS1'), hand.get('SMC1'), (thy_band, thy_plot)]
        label1 = '\\textbf{\\textrm{COMPASS}}'
        label2 = '\\textbf{\\textrm{SMC}}'
        label3 = '\\textbf{\\textrm{JAM}}'
        labels = [label1, label2, label3]
        thesis_legend(ax11, handles, labels, loc='upper left', fontsize=35, frameon=0, handletextpad=0.3, handlelength=1.0)
    if any((i in data for i in [10006, 10021, 10027])):
        DATA = {}
        if 10006 in data:
            DATA['HERMES'] = pd.DataFrame(data[10006])
        if 10021 in data:
            DATA['SLACE143'] = pd.DataFrame(data[10021])
        if 10027 in data:
            DATA['SLACE155'] = pd.DataFrame(data[10027])
        PLOT = {}
        theory = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_pidis_get_Q2bins(DATA[exp], 'd Apa')
                PLOT[exp] = thesis_pidis_get_plot(query)
                nbins = len(query)
                theory[exp] = thesis_pidis_get_theory(PLOT[exp], nbins, funcX=True, funcQ2=False, loop=False)
        N = 1.0
        hand = {}
        x = {}
        for exp in PLOT:
            for key in range(nbins):
                X = PLOT[exp]['X'][key]
                val = PLOT[exp]['value'][key] + N * float(key)
                alpha = PLOT[exp]['alpha'][key]
                (color, marker, ms) = thesis_pidis_get_details(exp)
                hand[exp] = ax12.errorbar(X, val, alpha, marker=marker, color=color, ms=ms, capsize=3, linestyle='none')
            for key in theory[exp]['value']:
                X = theory[exp]['X'][key]
                mean = theory[exp]['value'][key] + N * float(key)
                std_lower = theory[exp]['std_lower'][key]
                std_upper = theory[exp]['std_upper'][key]
                down = mean - std_lower
                up = mean + std_upper
                (thy_plot,) = ax12.plot(X, mean, linestyle='solid', color=color)
                thy_band = ax12.fill_between(X, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(X))
                if key not in x:
                    x[key] = []
                x[key].extend(X)
        for key in x:
            ax12.plot(np.linspace(np.min(x[key]), np.max(x[key]), 10), np.ones(10) * float(N * key), color='purple', ls=':')
        if any((len(v['value'].get(8, [])) for v in PLOT.values())):
            ax12.text(0.46, 0.92, '$Q^2 > 20~{\\rm GeV^2} \\, (i=9)$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(7, [])) for v in PLOT.values())):
            ax12.text(0.46, 0.8, '$15 < Q^2 < 20$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(6, [])) for v in PLOT.values())):
            ax12.text(0.36, 0.68, '$10 < Q^2 < 15$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(5, [])) for v in PLOT.values())):
            ax12.text(0.36, 0.57, '$7 < Q^2 < 10$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(4, [])) for v in PLOT.values())):
            ax12.text(0.29, 0.46, '$5 < Q^2 < 7$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(3, [])) for v in PLOT.values())):
            ax12.text(0.22, 0.35, '$4 < Q^2 < 5$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(2, [])) for v in PLOT.values())):
            ax12.text(0.23, 0.24, '$3 < Q^2 < 4$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(1, [])) for v in PLOT.values())):
            ax12.text(0.1, 0.13, '$2 < Q^2 < 3$', transform=ax12.transAxes, fontsize=25)
        if any((len(v['value'].get(0, [])) for v in PLOT.values())):
            ax12.text(0.6, 0.03, '$m_c^2 < Q^2 < 2\\, (i=0)$', transform=ax12.transAxes, fontsize=25)
        ax12.semilogx()
        ax12.set_xlim(0.01, 0.9)
        ax12.set_ylim(-0.2, 9.0)
        ax12.set_xlabel('\\boldmath$x_{\\rm bj}$', size=40)
        ax12.tick_params(axis='both', labelsize=30)
        ax12.yaxis.set_tick_params(which='major', length=10)
        ax12.yaxis.set_tick_params(which='minor', length=5)
        ax12.xaxis.set_tick_params(which='major', length=10)
        ax12.xaxis.set_tick_params(which='minor', length=5)
        ax12.set_xticks([0.02, 0.1, 0.5])
        ax12.set_xticklabels(['$0.02$', '$0.1$', '$0.5$'])
        ax12.text(0.05, 0.65, '\\boldmath$A_{\\parallel}^D$', transform=ax12.transAxes, size=60)
        ax12.text(0.16, 0.68, '$(+\\, i)$', transform=ax12.transAxes, size=40)
        ax12.yaxis.set_ticks_position('both')
        ax12.xaxis.set_ticks_position('both')
        ax12.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax12.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        majorLocator = MultipleLocator(2.0)
        minorLocator = MultipleLocator(0.4)
        ax12.yaxis.set_major_locator(majorLocator)
        ax12.yaxis.set_minor_locator(minorLocator)
        handles = [hand.get('HERMES'), hand.get('SLACE143'), hand.get('SLACE155')]
        label1 = '\\textbf{\\textrm{HERMES}}'
        label2 = '\\textbf{\\textrm{SLAC E143}}'
        label3 = '\\textbf{\\textrm{SLAC E155}}'
        labels = [label1, label2, label3]
        thesis_legend(ax12, handles, labels, loc='upper left', fontsize=35, frameon=0, handletextpad=0.3, handlelength=1.0)
    py.tight_layout()
    filename = output_file
    ax11.set_rasterized(True)
    ax12.set_rasterized(True)
    thesis_finish_panels(py.gcf(), 'thesis_pidis_plot_deuteron_A1_Apa', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/pidis.py:plot_helium_neutron, original lines 710-912; SHA256 c6447a2b83bfa2d4b5008b0a926c14cde97718d4e7f25b5c45ae59e148c5ee72.
def thesis_pidis_plot_helium_neutron(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['pidis']
    tabs = data
    (nrows, ncols) = (1, 2)
    py.figure(figsize=(ncols * 6.0, nrows * 7.0))
    ax11 = py.subplot(nrows, ncols, 1)
    ax12 = py.subplot(nrows, ncols, 2)
    if any((i in data for i in [10005, 10018])):
        DATA = {}
        if 10018 in data:
            DATA['SLACE142'] = pd.DataFrame(data[10018])
        if 10005 in data:
            DATA['HERMES'] = pd.DataFrame(data[10005])
        Q2 = np.concatenate([frame['Q2'] for frame in DATA.values()])
        (Q2min, Q2max) = (np.round(np.min(Q2), 1), np.round(np.max(Q2), 1))
        theory = {}
        PLOT = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_pidis_get_Q2bins(DATA[exp], 'h A1')
                PLOT[exp] = thesis_pidis_get_plot(query)
                nbins = len(query)
                theory[exp] = thesis_pidis_get_theory(PLOT[exp], nbins, funcX=True, funcQ2=False, loop=False)
        N = 1.0
        hand = {}
        x = {}
        for exp in PLOT:
            for key in range(nbins):
                X = PLOT[exp]['X'][key]
                val = PLOT[exp]['value'][key] + N * float(key)
                alpha = PLOT[exp]['alpha'][key]
                (color, marker, ms) = thesis_pidis_get_details(exp)
                hand[exp] = ax11.errorbar(X, val, alpha, marker=marker, color=color, ms=ms, capsize=3, linestyle='none')
            for key in theory[exp]['value']:
                X = theory[exp]['X'][key]
                mean = theory[exp]['value'][key] + N * float(key)
                std_lower = theory[exp]['std_lower'][key]
                std_upper = theory[exp]['std_upper'][key]
                down = mean - std_lower
                up = mean + std_upper
                (thy_plot,) = ax11.plot(X, mean, linestyle='solid', color=color)
                thy_band = ax11.fill_between(X, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(X))
                if key not in x:
                    x[key] = []
                x[key].extend(X)
        for key in x:
            ax11.plot(np.linspace(np.min(x[key]), np.max(x[key]), 10), np.ones(10) * float(N * key), color='purple', ls=':')
        if any((len(v['value'].get(0, [])) for v in PLOT.values())):
            ax11.text(0.05, 0.05, '$m_c^2 < Q^2 < 5.5 ~ {\\rm GeV}^2$', transform=ax11.transAxes, fontsize=30)
        ax11.semilogx()
        ax11.set_xlim(0.05, 0.6)
        ax11.set_ylim(-0.5, 0.5)
        ax11.tick_params(axis='both', labelsize=20)
        ax11.yaxis.set_tick_params(which='major', length=5)
        ax11.yaxis.set_tick_params(which='minor', length=3)
        ax11.xaxis.set_tick_params(which='major', length=5)
        ax11.xaxis.set_tick_params(which='minor', length=3)
        ax11.set_xticks([0.1, 0.4])
        ax11.set_xticklabels(['$0.1$', '$0.4$'])
        ax11.text(0.05, 0.65, '\\boldmath$A_1$', transform=ax11.transAxes, size=40)
        ax11.set_xlabel('\\boldmath$x_{\\rm bj}$', size=30)
        ax11.yaxis.set_ticks_position('both')
        ax11.xaxis.set_ticks_position('both')
        ax11.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax11.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        majorLocator = MultipleLocator(0.2)
        minorLocator = MultipleLocator(0.05)
        ax11.yaxis.set_major_locator(majorLocator)
        ax11.yaxis.set_minor_locator(minorLocator)
        (thy_plot,) = ax11.plot(0, 0, linestyle='solid', color='black')
        handles = [hand.get('HERMES'), hand.get('SLACE142'), (thy_band, thy_plot)]
        label1 = '\\textbf{\\textrm{HERMES (``\\boldmath$n$")}}'
        label2 = '\\textbf{\\textrm{SLAC E142 (\\boldmath$^3$He)}}'
        label3 = '\\textbf{\\textrm{JAM}}'
        labels = [label1, label2, label3]
        thesis_legend(ax11, handles, labels, loc='upper left', fontsize=20, frameon=0, handletextpad=0.3, handlelength=1.0)
    if any((i in data for i in [10025])):
        DATA = {}
        if 10025 in data:
            DATA['SLACE154'] = pd.DataFrame(data[10025])
        PLOT = {}
        theory = {}
        for cluster_i in range(kc.nc[istep]):
            if cluster_i != 0:
                continue
            for exp in DATA:
                query = thesis_pidis_get_Q2bins(DATA[exp], 'h Apa')
                PLOT[exp] = thesis_pidis_get_plot(query)
                nbins = len(query)
                theory[exp] = thesis_pidis_get_theory(PLOT[exp], nbins, funcX=True, funcQ2=False, loop=False)
        N = 1.0
        hand = {}
        x = {}
        for exp in PLOT:
            for key in range(nbins):
                X = PLOT[exp]['X'][key]
                val = PLOT[exp]['value'][key] + N * float(key)
                alpha = PLOT[exp]['alpha'][key]
                (color, marker, ms) = thesis_pidis_get_details(exp)
                hand[exp] = ax12.errorbar(X, val, alpha, marker=marker, color=color, ms=ms, capsize=3, linestyle='none')
            for key in theory[exp]['value']:
                X = theory[exp]['X'][key]
                mean = theory[exp]['value'][key] + N * float(key)
                std_lower = theory[exp]['std_lower'][key]
                std_upper = theory[exp]['std_upper'][key]
                down = mean - std_lower
                up = mean + std_upper
                (thy_plot,) = ax12.plot(X, mean, linestyle='solid', color=color)
                thy_band = ax12.fill_between(X, np.minimum(down, up), np.maximum(down, up), color='gold', alpha=1.0, where=thesis_band_mask(X))
                if key not in x:
                    x[key] = []
                x[key].extend(X)
        for key in x:
            ax12.plot(np.linspace(np.min(x[key]), np.max(x[key]), 10), np.ones(10) * float(N * key), color='purple', ls=':')
        if any((len(v['value'].get(3, [])) for v in PLOT.values())):
            ax12.text(0.55, 0.87, '$Q^2 > 10~{\\rm GeV^2} \\, (i=3)$', transform=ax12.transAxes, fontsize=15)
        if any((len(v['value'].get(2, [])) for v in PLOT.values())):
            ax12.text(0.05, 0.57, '$5 < Q^2 < 10$', transform=ax12.transAxes, fontsize=15)
        if any((len(v['value'].get(1, [])) for v in PLOT.values())):
            ax12.text(0.55, 0.3, '$3 < Q^2 < 5$', transform=ax12.transAxes, fontsize=15)
        if any((len(v['value'].get(0, [])) for v in PLOT.values())):
            ax12.text(0.2, 0.04, '$m_c^2 < Q^2 < 3\\, (i=0)$', transform=ax12.transAxes, fontsize=15)
        ax12.semilogx()
        ax12.set_xlim(0.03, 0.9)
        ax12.set_ylim(-0.2, 3.6)
        ax12.set_xlabel('\\boldmath$x_{\\rm bj}$', size=30)
        ax12.tick_params(axis='both', labelsize=20)
        ax12.yaxis.set_tick_params(which='major', length=5)
        ax12.yaxis.set_tick_params(which='minor', length=3)
        ax12.xaxis.set_tick_params(which='major', length=5)
        ax12.xaxis.set_tick_params(which='minor', length=3)
        ax12.set_xticks([0.1, 0.5])
        ax12.set_xticklabels(['$0.1$', '$0.5$'])
        ax12.text(0.05, 0.7, '\\boldmath$A_{\\parallel}^{^3{\\rm He}}$', transform=ax12.transAxes, size=40)
        ax12.text(0.27, 0.73, '$(+\\, i)$', transform=ax12.transAxes, size=30)
        ax12.yaxis.set_ticks_position('both')
        ax12.xaxis.set_ticks_position('both')
        ax12.tick_params(axis='y', which='both', labelleft=True, labelright=False, direction='in')
        ax12.tick_params(axis='x', which='both', labeltop=False, labelbottom=True, direction='in')
        majorLocator = MultipleLocator(1.0)
        minorLocator = MultipleLocator(0.5)
        ax12.yaxis.set_major_locator(majorLocator)
        ax12.yaxis.set_minor_locator(minorLocator)
        handles = [hand.get('SLACE154')]
        label1 = '\\textbf{\\textrm{SLAC E154}}'
        labels = [label1]
        thesis_legend(ax12, handles, labels, loc='upper left', fontsize=20, frameon=0, handletextpad=0.3, handlelength=1.0)
    py.tight_layout()
    filename = output_file
    ax11.set_rasterized(True)
    ax12.set_rasterized(True)
    thesis_finish_panels(py.gcf(), 'thesis_pidis_plot_helium_neutron', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/pjets.py:plot_pjets, original lines 40-283; SHA256 7334930a2eeb0be34ed427b33c30f5fb418d68a66bd80381389c0fb88f85b747.
def thesis_pjets_plot_pjets(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['pjet']
    tabs = data
    (nrows, ncols) = (3, 3)
    fig = py.figure(figsize=(ncols * 7, nrows * 4))
    ax11 = py.subplot(nrows, ncols, 1)
    ax12 = py.subplot(nrows, ncols, 2)
    ax13 = py.subplot(nrows, ncols, 3)
    ax21 = py.subplot(nrows, ncols, 4)
    ax22 = py.subplot(nrows, ncols, 5)
    ax23 = py.subplot(nrows, ncols, 6)
    ax31 = py.subplot(nrows, ncols, 7)
    ax32 = py.subplot(nrows, ncols, 8)
    ax33 = py.subplot(nrows, ncols, 9)
    tabs = datasets['pjet']
    data = predictions['reactions']['pjet']
    hand = {}
    thy_band = {}
    for idx in tabs:
        pT = data[idx]['pT']
        values = data[idx]['value']
        alpha = data[idx]['alpha']
        if idx == 20001:
            ax = ax11
        if idx == 20002:
            ax = ax12
        if idx == 20003:
            ax = ax13
        if idx == 20005:
            continue
        if idx == 20006:
            ax = ax23
        if idx == 20008:
            ax = ax31
        if idx in [20005]:
            color = 'darkblue'
        else:
            color = 'black'
        ts = 30
        if idx in [20004]:
            idx1 = [i for i in range(len(pT)) if data[idx]['eta-abs-min'][i] == 0.0]
            idx2 = [i for i in range(len(pT)) if data[idx]['eta-abs-min'][i] == 0.5]
            hand[idx] = ax21.errorbar(pT[idx1], values[idx1], yerr=alpha[idx1], color=color, fmt='o', ms=2, capsize=3.0)
            hand[idx] = ax22.errorbar(pT[idx2], values[idx2], yerr=alpha[idx2], color=color, fmt='o', ms=2, capsize=3.0)
        elif idx in [20007]:
            idx1 = [i for i in range(len(pT)) if data[idx]['eta-abs-min'][i] == 0.0]
            idx2 = [i for i in range(len(pT)) if data[idx]['eta-abs-min'][i] == 0.5]
            hand[idx] = ax32.errorbar(pT[idx1], values[idx1], yerr=alpha[idx1], color=color, fmt='o', ms=2, capsize=3.0)
            hand[idx] = ax33.errorbar(pT[idx2], values[idx2], yerr=alpha[idx2], color=color, fmt='o', ms=2, capsize=3.0)
        else:
            hand[idx] = ax.errorbar(pT, values, yerr=alpha, color=color, fmt='o', ms=2, capsize=3.0)
        xpos = 0.55
        if idx in [20004]:
            etamin = data[idx]['eta-abs-min'][idx1][0]
            etamax = data[idx]['eta-abs-max'][idx1][0]
            ax21.text(xpos, 0.05, '$|\\eta_{\\rm jet}| \\in [%2.1f,%2.1f]$' % (etamin, etamax), transform=ax21.transAxes, size=ts)
            etamin = data[idx]['eta-abs-min'][idx2][0]
            etamax = data[idx]['eta-abs-max'][idx2][0]
            ax22.text(xpos, 0.05, '$|\\eta_{\\rm jet}| \\in [%2.1f,%2.1f]$' % (etamin, etamax), transform=ax22.transAxes, size=ts)
        elif idx in [20007]:
            etamin = data[idx]['eta-abs-min'][idx1][0]
            etamax = data[idx]['eta-abs-max'][idx1][0]
            ax32.text(xpos, 0.05, '$|\\eta_{\\rm jet}| \\in [%2.1f,%2.1f]$' % (etamin, etamax), transform=ax32.transAxes, size=ts)
            etamin = data[idx]['eta-abs-min'][idx2][0]
            etamax = data[idx]['eta-abs-max'][idx2][0]
            ax33.text(xpos, 0.05, '$|\\eta_{\\rm jet}| \\in [%2.1f,%2.1f]$' % (etamin, etamax), transform=ax33.transAxes, size=ts)
        else:
            if 'eta-abs-min' in data[idx]:
                etamin = data[idx]['eta-abs-min'][0]
                etamax = data[idx]['eta-abs-max'][0]
                ax.text(xpos, 0.05, '$|\\eta_{\\rm jet}| \\in [%2.1f,%2.1f]$' % (etamin, etamax), transform=ax.transAxes, size=ts)
            if 'eta-min' in data[idx]:
                etamin = data[idx]['eta-min'][0]
                etamax = data[idx]['eta-max'][0]
                ax.text(xpos, 0.05, '$\\eta_{\\rm jet}   \\in [%2.1f,%2.1f]$' % (etamin, etamax), transform=ax.transAxes, size=ts)
        for ic in range(nc):
            if nc > 1:
                color = colors[cluster[ic]]
            thy = data[idx]['thy-%d' % ic]
            std_lower = data[idx]['dthy_lower-%d' % ic]
            std_upper = data[idx]['dthy_upper-%d' % ic]
            down = thy - std_lower
            up = thy + std_upper
            if idx in [20005]:
                color = 'blue'
            else:
                color = 'red'
            if idx in [20004]:
                idx1 = [i for i in range(len(pT)) if data[idx]['eta-abs-min'][i] == 0.0]
                idx2 = [i for i in range(len(pT)) if data[idx]['eta-abs-min'][i] == 0.5]
                thy_band[idx] = ax21.fill_between(pT[idx1], np.minimum(down[idx1], up[idx1]), np.maximum(down[idx1], up[idx1]), color=color, alpha=0.8, where=thesis_band_mask(pT[idx1]))
                ax21.plot(pT[idx1], thy[idx1], color=color)
                thy_band[idx] = ax22.fill_between(pT[idx2], np.minimum(down[idx2], up[idx2]), np.maximum(down[idx2], up[idx2]), color=color, alpha=0.8, where=thesis_band_mask(pT[idx2]))
                ax22.plot(pT[idx2], thy[idx2], color=color)
            elif idx in [20007]:
                idx1 = [i for i in range(len(pT)) if data[idx]['eta-abs-min'][i] == 0.0]
                idx2 = [i for i in range(len(pT)) if data[idx]['eta-abs-min'][i] == 0.5]
                thy_band[idx] = ax32.fill_between(pT[idx1], np.minimum(down[idx1], up[idx1]), np.maximum(down[idx1], up[idx1]), color=color, alpha=0.8, where=thesis_band_mask(pT[idx1]))
                ax32.plot(pT[idx1], thy[idx1], color=color)
                thy_band[idx] = ax33.fill_between(pT[idx2], np.minimum(down[idx2], up[idx2]), np.maximum(down[idx2], up[idx2]), color=color, alpha=0.8, where=thesis_band_mask(pT[idx2]))
                ax33.plot(pT[idx2], thy[idx2], color=color)
            else:
                thy_band[idx] = ax.fill_between(pT, np.minimum(down, up), np.maximum(down, up), color=color, alpha=0.8, where=thesis_band_mask(pT))
                ax.plot(pT, thy, color=color)
    for ax in [ax11, ax12, ax13, ax21, ax22, ax23]:
        ax.tick_params(axis='both', which='both', top=True, right=True, labelbottom=False, direction='in', labelsize=30)
    for ax in [ax31, ax32, ax33]:
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=30)
    for ax in [ax11, ax12, ax13, ax21, ax22, ax23, ax31, ax32, ax33]:
        ax.set_xlim(5, 65)
        ax.set_xticks([10, 20, 30, 40, 50, 60])
        minorLocator = MultipleLocator(2)
        majorLocator = MultipleLocator(10)
        ax.xaxis.set_minor_locator(minorLocator)
        ax.xaxis.set_major_locator(majorLocator)
        ax.xaxis.set_tick_params(which='major', length=6)
        ax.xaxis.set_tick_params(which='minor', length=3)
        ax.yaxis.set_tick_params(which='major', length=6)
        ax.yaxis.set_tick_params(which='minor', length=3)
        ax.axhline(0, 0, 1, color='black', alpha=0.2)
    for ax in [ax31, ax32, ax33]:
        ax.set_xlabel('\\boldmath$p_T^{\\rm jet}~({\\rm GeV})$', size=35)
    for ax in [ax12, ax13, ax22, ax23, ax32, ax33]:
        ax.tick_params(axis='both', which='both', labelleft=False)
    for ax in [ax11, ax12, ax13]:
        ax.set_ylim(-0.1, 0.33)
        minorLocator = MultipleLocator(0.05)
        majorLocator = MultipleLocator(0.1)
        ax.yaxis.set_minor_locator(minorLocator)
        ax.yaxis.set_major_locator(majorLocator)
        ax.set_yticks([0, 0.1, 0.2, 0.3])
    for ax in [ax21, ax22, ax23]:
        ax.set_ylim(-0.05, 0.08)
        minorLocator = MultipleLocator(0.01)
        majorLocator = MultipleLocator(0.02)
        ax.yaxis.set_minor_locator(minorLocator)
        ax.yaxis.set_major_locator(majorLocator)
        ax.set_yticks([-0.04, -0.02, 0, 0.02, 0.04, 0.06])
    for ax in [ax31, ax32, ax33]:
        ax.set_ylim(-0.03, 0.08)
        minorLocator = MultipleLocator(0.01)
        majorLocator = MultipleLocator(0.02)
        ax.yaxis.set_minor_locator(minorLocator)
        ax.yaxis.set_major_locator(majorLocator)
        ax.set_yticks([-0.02, 0, 0.02, 0.04, 0.06])
    ax13.text(0.05, 0.7, '\\boldmath$A_{LL}^{\\rm jet}$', transform=ax13.transAxes, size=50)
    ax11.text(0.8, 0.85, '\\textrm{\\textbf{2003}}', transform=ax11.transAxes, size=40)
    ax12.text(0.8, 0.85, '\\textrm{\\textbf{2005}}', transform=ax12.transAxes, size=40)
    ax13.text(0.8, 0.85, '\\textrm{\\textbf{2006}}', transform=ax13.transAxes, size=40)
    ax21.text(0.8, 0.85, '\\textrm{\\textbf{2009}}', transform=ax21.transAxes, size=40)
    ax22.text(0.8, 0.85, '\\textrm{\\textbf{2009}}', transform=ax22.transAxes, size=40)
    ax23.text(0.8, 0.85, '\\textrm{\\textbf{2012}}', transform=ax23.transAxes, size=40)
    ax31.text(0.8, 0.85, '\\textrm{\\textbf{2013}}', transform=ax31.transAxes, size=40)
    ax32.text(0.8, 0.85, '\\textrm{\\textbf{2015}}', transform=ax32.transAxes, size=40)
    ax33.text(0.8, 0.85, '\\textrm{\\textbf{2015}}', transform=ax33.transAxes, size=40)
    (handles, labels) = ([], [])
    handles.append(hand.get(20001))
    handles.append(thy_band.get(20001))
    labels.append('\\textbf{\\textrm{STAR}}')
    labels.append('\\textbf{\\textrm{JAM}}')
    thesis_legend(ax11, handles, labels, frameon=False, fontsize=30, loc='upper left', handletextpad=0.5, handlelength=1.0)
    py.tight_layout()
    py.subplots_adjust(hspace=0.02, wspace=0.02)
    filename = output_file
    thesis_finish_panels(py.gcf(), 'thesis_pjets_plot_pjets', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/polWlep.py:plot_main, original lines 35-226; SHA256 77d5facf64909124b521966f4fd5cd057eba18b622678ff85a1d6a160f5922a9.
def thesis_polWlep_plot_main(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['wzrv']
    tabs = data
    (nrows, ncols) = (1, 2)
    fig = py.figure(figsize=(ncols * 7, nrows * 6))
    ax11 = py.subplot(nrows, ncols, 1)
    ax12 = py.subplot(nrows, ncols, 2)
    tabs = datasets['wzrv']
    hand = {}
    for idx in tabs:
        if idx == 1000:
            (ax, color) = (ax11, 'black')
        elif idx == 1020:
            (ax, color) = (ax12, 'blue')
        elif idx == 1021:
            (ax, color) = (ax12, 'purple')
        values = tabs[idx]['value']
        alpha = np.sqrt(np.array(tabs[idx]['stat_u']) ** 2 + np.array(tabs[idx]['syst_u']) ** 2)
        if 'eta' in tabs[idx]:
            eta = tabs[idx]['eta']
        else:
            eta = (tabs[idx]['eta_min'] + tabs[idx]['eta_max']) / 2.0
            eta_min = tabs[idx]['eta_min']
            eta_max = tabs[idx]['eta_max']
        n = int(len(eta) / 2)
        if idx == 1000:
            X = 0
        elif idx == 1020:
            X = 0.08
        elif idx == 1021:
            X = 0.08
        if idx in [1000]:
            hand[idx] = ax.errorbar(eta[:n] + X, values[:n], yerr=alpha[:n], color=color, fmt='o', ms=5.0, capsize=3.0)
            ax.errorbar(eta[n:] - X, values[n:], yerr=alpha[n:], color=color, fmt='o', ms=5.0, capsize=3.0)
        elif idx in [1020, 1021]:
            hand[idx] = {}
            hand[idx]['+'] = ax.errorbar(eta[:n] + X, values[:n], yerr=alpha[:n], color=color, fmt='o', ms=5.0, capsize=3.0)
            hand[idx]['-'] = ax.errorbar(eta[n:] - X, values[n:], yerr=alpha[n:], color=color, fmt='s', ms=6.0, capsize=3.0)
    j = 0
    for data in [datasets['wzrv']]:
        for idx in tabs:
            if idx == 1000:
                (ax, color) = (ax11, 'black')
            elif idx == 1020:
                (ax, color) = (ax12, 'blue')
            elif idx == 1021:
                (ax, color) = (ax12, 'purple')
            if 'eta' in tabs[idx]:
                eta = tabs[idx]['eta']
            else:
                eta = (tabs[idx]['eta_min'] + tabs[idx]['eta_max']) / 2.0
                eta_min = tabs[idx]['eta_min']
                eta_max = tabs[idx]['eta_max']
            n = int(len(eta) / 2)
            thy = data[idx]['thy-0']
            dthy_lower = data[idx]['dthy_lower-0']
            dthy_upper = data[idx]['dthy_upper-0']
            up = thy + dthy_upper
            down = thy - dthy_lower
            if j == 0:
                if idx in [1000]:
                    (thy_plot0,) = ax.plot(eta[:n], thy[:n], color='red', alpha=1.0, lw=2)
                    ax.plot(eta[n:], thy[n:], color='red', alpha=1.0, lw=2)
                    thy_band0 = ax.fill_between(eta[:n], np.minimum(down[:n], up[:n]), np.maximum(down[:n], up[:n]), color='red', alpha=0.2, where=thesis_band_mask(eta[:n]))
                    ax.fill_between(eta[n:], np.minimum(down[n:], up[n:]), np.maximum(down[n:], up[n:]), color='red', alpha=0.2, where=thesis_band_mask(eta[n:]))
                elif idx in [1020, 1021]:
                    if idx == 1020:
                        X = 0
                    elif idx == 1021:
                        X = 0.12
                    thy_point0 = ax.errorbar(eta[:n] + X, thy[:n], yerr=thesis_error_extents(dthy_lower[:n], dthy_upper[:n]), color='red', fmt='o', ms=5.0, capsize=3.0)
                    ax.errorbar(eta[n:] - X, thy[n:], yerr=thesis_error_extents(dthy_lower[n:], dthy_upper[n:]), color='red', fmt='s', ms=6.0, capsize=3.0)
            if j == 1:
                if idx in [1000]:
                    (thy_plot1,) = ax.plot(eta[:n], thy[:n], color='green', alpha=1.0, ls='--', lw=2)
                    ax.plot(eta[n:], thy[n:], color='green', alpha=1.0, ls='--', lw=2)
        j += 1
    for ax in [ax11, ax12]:
        ax.set_xlim(-1.9, 1.9)
        minorLocator = MultipleLocator(0.25)
        majorLocator = MultipleLocator(1.0)
        ax.xaxis.set_minor_locator(minorLocator)
        ax.xaxis.set_major_locator(majorLocator)
        ax.xaxis.set_tick_params(which='major', length=6)
        ax.xaxis.set_tick_params(which='minor', length=3)
        minorLocator = MultipleLocator(0.1)
        majorLocator = MultipleLocator(0.5)
        ax.yaxis.set_minor_locator(minorLocator)
        ax.yaxis.set_major_locator(majorLocator)
        ax.yaxis.set_tick_params(which='major', length=6)
        ax.yaxis.set_tick_params(which='minor', length=3)
        ax.axhline(0, 0, 1, ls='-', color='black', alpha=0.2, lw=1.0)
        ax.set_xlabel('\\boldmath$\\eta_\\ell$', size=40)
        ax.set_xticks([-1.0, 0.0, 1.0])
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=30)
        ax.set_ylim(-0.9, 0.9)
        ax.set_yticks([-0.5, 0, 0.5])
        ax.set_yticklabels(['$-0.5$', '$0$', '$0.5$'])
    ax12.tick_params(labelleft=False)
    ax11.text(0.08, 0.11, '\\boldmath$A_L^{W}$', transform=ax11.transAxes, size=50)
    ax11.text(0.25, 0.16, '\\textrm{\\textbf{(STAR)}}', transform=ax11.transAxes, size=30)
    ax11.text(0.65, 0.43, '$\\sqrt{s}=510~$' + '\\textrm{GeV}', transform=ax11.transAxes, size=20)
    ax11.text(0.67, 0.34, '$p_T^\\ell > 25~$' + '\\hspace{0.00cm}\\textrm{GeV}', transform=ax11.transAxes, size=20)
    ax11.text(0.04, 0.61, '$W^-$', transform=ax11.transAxes, size=25)
    ax11.text(0.04, 0.3, '$W^+$', transform=ax11.transAxes, size=25)
    ax12.text(0.3, 0.8, '\\boldmath$A_L^{W/Z}$', transform=ax12.transAxes, size=50)
    ax12.text(0.57, 0.85, '\\textrm{\\textbf{(PHENIX)}}', transform=ax12.transAxes, size=30)
    (handles, labels) = ([], [])
    handles.append((thy_band0, thy_plot0))
    labels.append('\\textrm{\\textbf{JAM}}')
    thesis_legend(ax11, handles, labels, frameon=False, fontsize=25, loc='upper left', handletextpad=0.3, handlelength=1.2, ncol=1, columnspacing=2.3)
    (handles, labels) = ([], [])
    handles.append(hand.get(1020, {}).get('+'))
    handles.append(hand.get(1020, {}).get('-'))
    handles.append(hand.get(1021, {}).get('+'))
    handles.append(hand.get(1021, {}).get('-'))
    labels.append('\\textrm{$W^+, p_T^{\\ell} > 16$}')
    labels.append('\\textrm{$W^-, p_T^{\\ell} > 16$}')
    labels.append('\\textrm{$W^+, p_T^{\\ell} > 30$}')
    labels.append('\\textrm{$W^-, p_T^{\\ell} > 30$}')
    thesis_legend(ax12, handles, labels, frameon=False, fontsize=20, loc='lower left', handletextpad=0.3, handlelength=1.2, ncol=2, columnspacing=2.3, handleheight=1.5)
    if 1020 in tabs or 1021 in tabs:
        ax12.errorbar(-1.68, -0.4, yerr=0.06, color='red', fmt='o', ms=5.0, capsize=3.0)
    if 1020 in tabs or 1021 in tabs:
        ax12.errorbar(-1.61, -0.4, yerr=0.06, color='red', fmt='s', ms=6.0, capsize=3.0)
    ax12.text(0.1, 0.25, '\\textrm{\\textbf{JAM}}', transform=ax12.transAxes, size=25)
    py.tight_layout()
    py.subplots_adjust(hspace=0, wspace=0.02, top=0.99, right=0.99)
    filename = output_file
    ax11.set_rasterized(True)
    thesis_finish_panels(py.gcf(), 'thesis_polWlep_plot_main', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/sidis.py:plot_pion, original lines 39-168; SHA256 bbf6969bd9db5719e86594f1a733e14be49ad78d72116e40058a0841b72d868e.
def thesis_sidis_plot_pion(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['sidis']
    tabs = data
    if 'sidis' not in predictions['reactions']:
        return
    data = predictions['reactions']['sidis']
    X = [[0.004, 0.01], [0.01, 0.02], [0.02, 0.03], [0.03, 0.04], [0.04, 0.06], [0.06, 0.1], [0.1, 0.14], [0.14, 0.18], [0.18, 0.4]]
    Y = [[0.1, 0.15], [0.15, 0.2], [0.2, 0.3], [0.3, 0.5], [0.5, 0.7]]
    (nrows, ncols) = (2, 4)
    fig = py.figure(figsize=(ncols * 4, nrows * 5))
    cnt = 0
    ax1 = None
    H = {}
    for tab in data.values():
        if np.any((tab['X'] > 0.004) & (tab['X'] < 0.01)):
            raise ValueError('Selected SIDIS points occupy the source-unrepresented first x window; eight-panel design retained without silently cutting those points.')
    for (slot, xrange) in enumerate(X[1:], 1):
        (xmin, xmax) = xrange
        frames = [pd.DataFrame(data[idx]).query('X>%f and X<%f' % (xmin, xmax)) for idx in [1005, 1006] if idx in data]
        if not any((len(frame) for frame in frames)):
            continue
        cnt = slot
        ax = py.subplot(nrows, ncols, cnt)
        alpha = -1
        for yrange in Y:
            alpha += 1
            (ymin, ymax) = yrange
            for D in frames:
                d = D.query('Y>%f  and Y<%f' % (ymin, ymax))
                if len(d) <= 1:
                    continue
                f = 2 ** alpha
                if ymin == 0.1:
                    color = 'r'
                elif ymin == 0.15:
                    color = 'darkorange'
                elif ymin == 0.2:
                    color = 'g'
                elif ymin == 0.3:
                    color = 'c'
                elif ymin == 0.5:
                    color = 'm'
                else:
                    color = 'k'
                ax.fill_between(d.Z, np.minimum((d.thy - d.dthy_lower) * f, (d.thy + d.dthy_upper) * f), np.maximum((d.thy - d.dthy_lower) * f, (d.thy + d.dthy_upper) * f), color=color, where=thesis_band_mask(d.Z))
                if '+' in d['hadron'].values[0]:
                    ax.plot(d.Z, d.thy * f, color=color, ls='--')
                if '-' in d['hadron'].values[0]:
                    ax.plot(d.Z, d.thy * f, color=color, ls=':')
                p = ax.errorbar(d.Z, d.value * f, d.alpha * f, fmt='.', color=color)
                H[ymin] = p
        ax.set_ylim(0.03, 50.5)
        ax.set_xlim(0.1, 0.95)
        ax.semilogy()
        minorLocator = MultipleLocator(0.05)
        majorLocator = MultipleLocator(0.2)
        ax.xaxis.set_minor_locator(minorLocator)
        ax.xaxis.set_major_locator(majorLocator)
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=20)
        if any([cnt == _ for _ in [2, 3, 4, 6, 7, 8]]):
            ax.set_yticklabels([])
        if any([cnt == _ for _ in [1, 2, 3, 4]]):
            ax.set_xticklabels([])
        if cnt in [5, 6, 7, 8]:
            ax.set_xlabel('\\boldmath$z_H$', size=30)
            ax.set_xticks([0.2, 0.4, 0.6, 0.8])
            ax.set_xticklabels(['$0.2$', '$0.4$', '$0.6$', '$0.8$'])
        if cnt == 1:
            ax.set_ylabel('\\boldmath${\\rm d} M^{h}/{\\rm d} z_H\\times 2^i$', size=30)
            ax.yaxis.set_label_coords(-0.15, -0.05)
        if cnt == 1 or cnt == 5:
            ax.set_yticks([0.1, 1, 10])
            ax.set_yticklabels(['$0.1$', '$1$', '$10$'])
        if cnt == 1:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.01,0.02]$}'
        if cnt == 2:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.02,0.03]$}'
        if cnt == 3:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.03,0.04]$}'
        if cnt == 4:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.04,0.06]$}'
        if cnt == 5:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.06,0.10]$}'
        if cnt == 6:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.10,0.14]$}'
        if cnt == 7:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.14,0.18]$}'
        if cnt == 8:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.18,0.40]$}'
        ax.text(0.3, 0.9, msg, transform=ax.transAxes, size=20)
        if cnt == 1:
            ax1 = ax
        if cnt == 2:
            from matplotlib.lines import Line2D
            h = [Line2D([0], [0], color='k', lw=5, ls='--'), Line2D([0], [0], color='k', lw=5, ls=':')]
            l = ['\\boldmath{$\\pi^+$}', '\\boldmath{$\\pi^-$}']
            h = [handle for (idx, handle) in zip([1005, 1006], h) if idx in data]
            l = [label for (idx, label) in zip([1005, 1006], l) if idx in data]
            thesis_legend(ax, h, l, fontsize=30, loc=3, frameon=0, handlelength=2.5, handletextpad=0.4)
    H = [H.get(_) for _ in [0.1, 0.15, 0.2, 0.3, 0.5]]
    L = ['$0.1~<y<0.15~(i=0)$', '$0.15<y<0.2~~(i=1)$', '$0.2~<y<0.3~~(i=2)$', '$0.3~<y<0.5~~(i=3)$', '$0.5~<y<0.7~~(i=4)$']
    if ax1 is not None:
        thesis_legend(ax1, H, L, loc=3, fontsize=20, frameon=0, handletextpad=0)
    py.subplots_adjust(left=0.08, bottom=0.05, right=0.98, top=0.98, wspace=0, hspace=0)
    filename = output_file
    thesis_save_labels(py.gcf(), filename)

# Adapted from thesis/sidis.py:plot_kaon, original lines 170-299; SHA256 bbf6969bd9db5719e86594f1a733e14be49ad78d72116e40058a0841b72d868e.
def thesis_sidis_plot_kaon(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['sidis']
    tabs = data
    if 'sidis' not in predictions['reactions']:
        return
    data = predictions['reactions']['sidis']
    X = [[0.004, 0.01], [0.01, 0.02], [0.02, 0.03], [0.03, 0.04], [0.04, 0.06], [0.06, 0.1], [0.1, 0.14], [0.14, 0.18], [0.18, 0.4]]
    Y = [[0.1, 0.15], [0.15, 0.2], [0.2, 0.3], [0.3, 0.5], [0.5, 0.7]]
    (nrows, ncols) = (2, 4)
    fig = py.figure(figsize=(ncols * 4, nrows * 5))
    cnt = 0
    ax1 = None
    H = {}
    for tab in data.values():
        if np.any((tab['X'] > 0.004) & (tab['X'] < 0.01)):
            raise ValueError('Selected SIDIS points occupy the source-unrepresented first x window; eight-panel design retained without silently cutting those points.')
    for (slot, xrange) in enumerate(X[1:], 1):
        (xmin, xmax) = xrange
        frames = [pd.DataFrame(data[idx]).query('X>%f and X<%f' % (xmin, xmax)) for idx in [2005, 2006] if idx in data]
        if not any((len(frame) for frame in frames)):
            continue
        cnt = slot
        ax = py.subplot(nrows, ncols, cnt)
        alpha = -1
        for yrange in Y:
            alpha += 1
            (ymin, ymax) = yrange
            for D in frames:
                d = D.query('Y>%f  and Y<%f' % (ymin, ymax))
                if len(d) <= 1:
                    continue
                f = 2 ** alpha
                if ymin == 0.1:
                    color = 'r'
                elif ymin == 0.15:
                    color = 'darkorange'
                elif ymin == 0.2:
                    color = 'g'
                elif ymin == 0.3:
                    color = 'c'
                elif ymin == 0.5:
                    color = 'm'
                else:
                    color = 'k'
                ax.fill_between(d.Z, np.minimum((d.thy - d.dthy_lower) * f, (d.thy + d.dthy_upper) * f), np.maximum((d.thy - d.dthy_lower) * f, (d.thy + d.dthy_upper) * f), color=color, where=thesis_band_mask(d.Z))
                if '+' in d['hadron'].values[0]:
                    ax.plot(d.Z, d.thy * f, color=color, ls='--')
                if '-' in d['hadron'].values[0]:
                    ax.plot(d.Z, d.thy * f, color=color, ls=':')
                p = ax.errorbar(d.Z, d.value * f, d.alpha * f, fmt='.', color=color)
                H[ymin] = p
        ax.set_ylim(0.002, 20)
        ax.set_xlim(0.1, 0.95)
        ax.semilogy()
        minorLocator = MultipleLocator(0.05)
        majorLocator = MultipleLocator(0.2)
        ax.xaxis.set_minor_locator(minorLocator)
        ax.xaxis.set_major_locator(majorLocator)
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=20)
        if any([cnt == _ for _ in [2, 3, 4, 6, 7, 8]]):
            ax.set_yticklabels([])
        if any([cnt == _ for _ in [1, 2, 3, 4]]):
            ax.set_xticklabels([])
        if cnt in [5, 6, 7, 8]:
            ax.set_xlabel('\\boldmath$z_H$', size=30)
            ax.set_xticks([0.2, 0.4, 0.6, 0.8])
            ax.set_xticklabels(['$0.2$', '$0.4$', '$0.6$', '$0.8$'])
        if cnt == 1:
            ax.set_ylabel('\\boldmath${\\rm d} M^{h}/{\\rm d} z_H\\times 2^i$', size=30)
            ax.yaxis.set_label_coords(-0.2, -0.05)
        if cnt == 1 or cnt == 5:
            ax.set_yticks([0.01, 0.1, 1, 10])
            ax.set_yticklabels(['$0.01$', '$0.1$', '$1$', '$10$'])
        if cnt == 1:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.01,0.02]$}'
        if cnt == 2:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.02,0.03]$}'
        if cnt == 3:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.03,0.04]$}'
        if cnt == 4:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.04,0.06]$}'
        if cnt == 5:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.06,0.10]$}'
        if cnt == 6:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.10,0.14]$}'
        if cnt == 7:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.14,0.18]$}'
        if cnt == 8:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.18,0.40]$}'
        ax.text(0.3, 0.9, msg, transform=ax.transAxes, size=20)
        if cnt == 1:
            ax1 = ax
        if cnt == 2:
            from matplotlib.lines import Line2D
            h = [Line2D([0], [0], color='k', lw=5, ls='--'), Line2D([0], [0], color='k', lw=5, ls=':')]
            l = ['\\boldmath{$K^+$}', '\\boldmath{$K^-$}']
            h = [handle for (idx, handle) in zip([2005, 2006], h) if idx in data]
            l = [label for (idx, label) in zip([2005, 2006], l) if idx in data]
            thesis_legend(ax, h, l, fontsize=30, loc=3, frameon=0, handlelength=2.5, handletextpad=0.4)
    H = [H.get(_) for _ in [0.1, 0.15, 0.2, 0.3, 0.5]]
    L = ['$0.1~<y<0.15~(i=0)$', '$0.15<y<0.2~~(i=1)$', '$0.2~<y<0.3~~(i=2)$', '$0.3~<y<0.5~~(i=3)$', '$0.5~<y<0.7~~(i=4)$']
    if ax1 is not None:
        thesis_legend(ax1, H, L, loc=3, fontsize=20, frameon=0, handletextpad=0)
    py.subplots_adjust(left=0.08, bottom=0.05, right=0.98, top=0.98, wspace=0, hspace=0)
    filename = output_file
    thesis_save_labels(py.gcf(), filename)

# Adapted from thesis/sidis.py:plot_hadron, original lines 301-430; SHA256 bbf6969bd9db5719e86594f1a733e14be49ad78d72116e40058a0841b72d868e.
def thesis_sidis_plot_hadron(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['sidis']
    tabs = data
    if 'sidis' not in predictions['reactions']:
        return
    data = predictions['reactions']['sidis']
    X = [[0.004, 0.01], [0.01, 0.02], [0.02, 0.03], [0.03, 0.04], [0.04, 0.06], [0.06, 0.1], [0.1, 0.14], [0.14, 0.18], [0.18, 0.4]]
    Y = [[0.1, 0.15], [0.15, 0.2], [0.2, 0.3], [0.3, 0.5], [0.5, 0.7]]
    (nrows, ncols) = (2, 4)
    fig = py.figure(figsize=(ncols * 4, nrows * 5))
    cnt = 0
    ax1 = None
    H = {}
    for tab in data.values():
        if np.any((tab['X'] > 0.004) & (tab['X'] < 0.01)):
            raise ValueError('Selected SIDIS points occupy the source-unrepresented first x window; eight-panel design retained without silently cutting those points.')
    for (slot, xrange) in enumerate(X[1:], 1):
        (xmin, xmax) = xrange
        frames = [pd.DataFrame(data[idx]).query('X>%f and X<%f' % (xmin, xmax)) for idx in [3000, 3001] if idx in data]
        if not any((len(frame) for frame in frames)):
            continue
        cnt = slot
        ax = py.subplot(nrows, ncols, cnt)
        alpha = -1
        for yrange in Y:
            alpha += 1
            (ymin, ymax) = yrange
            for D in frames:
                d = D.query('Y>%f  and Y<%f' % (ymin, ymax))
                if len(d) <= 1:
                    continue
                f = 2 ** alpha
                if ymin == 0.1:
                    color = 'r'
                elif ymin == 0.15:
                    color = 'darkorange'
                elif ymin == 0.2:
                    color = 'g'
                elif ymin == 0.3:
                    color = 'c'
                elif ymin == 0.5:
                    color = 'm'
                else:
                    color = 'k'
                ax.fill_between(d.Z, np.minimum((d.thy - d.dthy_lower) * f, (d.thy + d.dthy_upper) * f), np.maximum((d.thy - d.dthy_lower) * f, (d.thy + d.dthy_upper) * f), color=color, where=thesis_band_mask(d.Z))
                if '+' in d['hadron'].values[0]:
                    ax.plot(d.Z, d.thy * f, color=color, ls='--')
                if '-' in d['hadron'].values[0]:
                    ax.plot(d.Z, d.thy * f, color=color, ls=':')
                p = ax.errorbar(d.Z, d.value * f, d.alpha * f, fmt='.', color=color)
                H[ymin] = p
        ax.set_ylim(0.03, 50.5)
        ax.set_xlim(0.1, 0.95)
        ax.semilogy()
        minorLocator = MultipleLocator(0.05)
        majorLocator = MultipleLocator(0.2)
        ax.xaxis.set_minor_locator(minorLocator)
        ax.xaxis.set_major_locator(majorLocator)
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=20)
        if any([cnt == _ for _ in [2, 3, 4, 6, 7, 8]]):
            ax.set_yticklabels([])
        if any([cnt == _ for _ in [1, 2, 3, 4]]):
            ax.set_xticklabels([])
        if cnt in [5, 6, 7, 8]:
            ax.set_xlabel('\\boldmath$z_H$', size=30)
            ax.set_xticks([0.2, 0.4, 0.6, 0.8])
            ax.set_xticklabels(['$0.2$', '$0.4$', '$0.6$', '$0.8$'])
        if cnt == 1:
            ax.set_ylabel('\\boldmath${\\rm d} M^{h}/{\\rm d} z_H\\times 2^i$', size=30)
            ax.yaxis.set_label_coords(-0.15, -0.05)
        if cnt == 1 or cnt == 5:
            ax.set_yticks([0.1, 1, 10])
            ax.set_yticklabels(['$0.1$', '$1$', '$10$'])
        if cnt == 1:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.01,0.02]$}'
        if cnt == 2:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.02,0.03]$}'
        if cnt == 3:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.03,0.04]$}'
        if cnt == 4:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.04,0.06]$}'
        if cnt == 5:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.06,0.10]$}'
        if cnt == 6:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.10,0.14]$}'
        if cnt == 7:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.14,0.18]$}'
        if cnt == 8:
            msg = '\\boldmath{$x_{\\rm Bj}\\in[0.18,0.40]$}'
        ax.text(0.3, 0.9, msg, transform=ax.transAxes, size=20)
        if cnt == 1:
            ax1 = ax
        if cnt == 2:
            from matplotlib.lines import Line2D
            h = [Line2D([0], [0], color='k', lw=5, ls='--'), Line2D([0], [0], color='k', lw=5, ls=':')]
            l = ['\\boldmath{$h^+$}', '\\boldmath{$h^-$}']
            h = [handle for (idx, handle) in zip([3000, 3001], h) if idx in data]
            l = [label for (idx, label) in zip([3000, 3001], l) if idx in data]
            thesis_legend(ax, h, l, fontsize=30, loc=3, frameon=0, handlelength=2.5, handletextpad=0.4)
    H = [H.get(_) for _ in [0.1, 0.15, 0.2, 0.3, 0.5]]
    L = ['$0.1~<y<0.15~(i=0)$', '$0.15<y<0.2~~(i=1)$', '$0.2~<y<0.3~~(i=2)$', '$0.3~<y<0.5~~(i=3)$', '$0.5~<y<0.7~~(i=4)$']
    if ax1 is not None:
        thesis_legend(ax1, H, L, loc=3, fontsize=20, frameon=0, handletextpad=0)
    py.subplots_adjust(left=0.08, bottom=0.05, right=0.98, top=0.98, wspace=0, hspace=0)
    filename = output_file
    thesis_save_labels(py.gcf(), filename)

# Adapted from thesis/psidis.py:plot_pion, original lines 40-209; SHA256 f16cd20d12dcbb2cf3d50686398736cb571cf36c6a884d6a78120d8cec104d96.
def thesis_psidis_plot_pion(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['psidis']
    tabs = data
    (nrows, ncols) = (2, 2)
    fig = py.figure(figsize=(ncols * 7, nrows * 4))
    ax11 = py.subplot(nrows, ncols, 1)
    ax12 = py.subplot(nrows, ncols, 2)
    ax21 = py.subplot(nrows, ncols, 3)
    ax22 = py.subplot(nrows, ncols, 4)
    tabs = datasets['psidis']
    data = predictions['reactions']['psidis']
    hand = {}
    thy_band = {}
    for idx in tabs:
        X = tabs[idx]['X']
        values = tabs[idx]['value']
        alpha = data[idx]['alpha']
        if idx == 20004:
            (ax, color) = (ax11, 'firebrick')
        if idx == 20005:
            (ax, color) = (ax12, 'firebrick')
        if idx == 20017:
            (ax, color) = (ax11, 'darkgreen')
        if idx == 20018:
            (ax, color) = (ax12, 'darkgreen')
        if idx == 20008:
            (ax, color) = (ax21, 'firebrick')
        if idx == 20009:
            (ax, color) = (ax22, 'firebrick')
        if idx == 20021:
            (ax, color) = (ax21, 'darkgreen')
        if idx == 20022:
            (ax, color) = (ax22, 'darkgreen')
        hand[idx] = ax.errorbar(X, values, yerr=alpha, color=color, fmt='o', ms=2, capsize=3.0)
        for ic in range(nc):
            if nc > 1:
                color = colors[cluster[ic]]
            thy = data[idx]['thy-%d' % ic]
            std_lower = data[idx]['dthy_lower-%d' % ic]
            std_upper = data[idx]['dthy_upper-%d' % ic]
            down = thy - std_lower
            up = thy + std_upper
            (thy_plot,) = ax.plot(X, thy, color=color)
            thy_band[idx] = ax.fill_between(X, np.minimum(down, up), np.maximum(down, up), color=color, alpha=0.5, where=thesis_band_mask(X))
    for ax in [ax11, ax12]:
        ax.tick_params(axis='both', which='both', top=True, right=True, labelbottom=False, direction='in', labelsize=30)
        ax.set_yticks([0.25, 0.5, 0.75])
    for ax in [ax21, ax22]:
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=30)
    for ax in [ax11, ax12, ax21, ax22]:
        ax.semilogx()
        ax.set_xlim(0.004, 0.6)
        ax.set_xticks([0.01, 0.1])
        ax.set_xticklabels(['$0.01$', '$0.1$'])
        ax.xaxis.set_tick_params(which='major', length=6)
        ax.xaxis.set_tick_params(which='minor', length=3)
        ax.yaxis.set_tick_params(which='major', length=6)
        ax.yaxis.set_tick_params(which='minor', length=3)
        ax.axhline(0, 0, 1, color='black', alpha=0.2)
    for ax in [ax21, ax22]:
        ax.set_xlabel('\\boldmath$x_{\\rm bj}$', size=40)
    for ax in [ax12, ax22]:
        ax.tick_params(axis='both', which='both', labelleft=False)
    ax11.set_ylim(-0.1, 1.0)
    ax12.set_ylim(-0.1, 1.0)
    ax21.set_ylim(-0.1, 0.5)
    ax22.set_ylim(-0.1, 0.5)
    minorLocator = MultipleLocator(0.1)
    majorLocator = MultipleLocator(0.2)
    ax11.yaxis.set_minor_locator(minorLocator)
    ax11.yaxis.set_major_locator(majorLocator)
    ax12.yaxis.set_minor_locator(minorLocator)
    ax12.yaxis.set_major_locator(majorLocator)
    minorLocator = MultipleLocator(0.1)
    majorLocator = MultipleLocator(0.2)
    ax21.yaxis.set_minor_locator(minorLocator)
    ax21.yaxis.set_major_locator(majorLocator)
    ax22.yaxis.set_minor_locator(minorLocator)
    ax22.yaxis.set_major_locator(majorLocator)
    ax11.text(0.05, 0.75, '\\boldmath$A^{p,\\pi^+}_{1}$', transform=ax11.transAxes, size=40)
    ax12.text(0.05, 0.75, '\\boldmath$A^{p,\\pi^-}_{1}$', transform=ax12.transAxes, size=40)
    ax21.text(0.05, 0.75, '\\boldmath$A^{D,\\pi^+}_{1}$', transform=ax21.transAxes, size=40)
    ax22.text(0.05, 0.75, '\\boldmath$A^{D,\\pi^-}_{1}$', transform=ax22.transAxes, size=40)
    (handles, labels) = ([], [])
    handles.append(hand.get(20004))
    handles.append(hand.get(20017))
    labels.append('\\textbf{\\textrm{HERMES}}')
    labels.append('\\textbf{\\textrm{COMPASS}}')
    thesis_legend(ax11, handles, labels, frameon=False, fontsize=22, loc=(0.0, 0.35), handletextpad=0.5, handlelength=1.0)
    (handles, labels) = ([], [])
    handles.append(thy_band.get(20004))
    handles.append(thy_band.get(20017))
    labels.append('\\textbf{\\textrm{JAM (HERMES)}}')
    labels.append('\\textbf{\\textrm{JAM (COMPASS)}}')
    thesis_legend(ax12, handles, labels, frameon=False, fontsize=22, loc=(0.0, 0.35), handletextpad=0.5, handlelength=1.0)
    py.tight_layout()
    py.subplots_adjust(hspace=0, wspace=0)
    filename = output_file
    thesis_finish_panels(py.gcf(), 'thesis_psidis_plot_pion', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/psidis.py:plot_kaon, original lines 211-382; SHA256 f16cd20d12dcbb2cf3d50686398736cb571cf36c6a884d6a78120d8cec104d96.
def thesis_psidis_plot_kaon(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['psidis']
    tabs = data
    (nrows, ncols) = (2, 3)
    fig = py.figure(figsize=(ncols * 7, nrows * 4))
    ax11 = py.subplot(nrows, ncols, 1)
    ax12 = py.subplot(nrows, ncols, 2)
    ax21 = py.subplot(nrows, ncols, 4)
    ax22 = py.subplot(nrows, ncols, 5)
    ax23 = py.subplot(nrows, ncols, 6)
    tabs = datasets['psidis']
    data = predictions['reactions']['psidis']
    hand = {}
    thy_band = {}
    for idx in tabs:
        X = tabs[idx]['X']
        values = tabs[idx]['value']
        alpha = data[idx]['alpha']
        if idx == 20012:
            (ax, color) = (ax21, 'firebrick')
        if idx == 20013:
            (ax, color) = (ax22, 'firebrick')
        if idx == 20014:
            (ax, color) = (ax23, 'firebrick')
        if idx == 20019:
            (ax, color) = (ax11, 'darkgreen')
        if idx == 20020:
            (ax, color) = (ax12, 'darkgreen')
        if idx == 20025:
            (ax, color) = (ax21, 'darkgreen')
        if idx == 20026:
            (ax, color) = (ax22, 'darkgreen')
        hand[idx] = ax.errorbar(X, values, yerr=alpha, color=color, fmt='o', ms=2, capsize=3.0)
        for ic in range(nc):
            if nc > 1:
                color = colors[cluster[ic]]
            thy = data[idx]['thy-%d' % ic]
            std_lower = data[idx]['dthy_lower-%d' % ic]
            std_upper = data[idx]['dthy_upper-%d' % ic]
            down = thy - std_lower
            up = thy + std_upper
            (thy_plot,) = ax.plot(X, thy, color=color)
            thy_band[idx] = ax.fill_between(X, np.minimum(down, up), np.maximum(down, up), color=color, alpha=0.5, where=thesis_band_mask(X))
    for ax in [ax11, ax12]:
        ax.tick_params(axis='both', which='both', top=True, right=True, labelbottom=False, direction='in', labelsize=30)
        ax.set_yticks([0.25, 0.5, 0.75])
    for ax in [ax21, ax22, ax23]:
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=30)
    for ax in [ax11, ax12, ax21, ax22, ax23]:
        ax.semilogx()
        ax.set_xlim(0.004, 0.6)
        ax.set_xticks([0.01, 0.1])
        ax.set_xticklabels(['$0.01$', '$0.1$'])
        ax.xaxis.set_tick_params(which='major', length=6)
        ax.xaxis.set_tick_params(which='minor', length=3)
        ax.yaxis.set_tick_params(which='major', length=6)
        ax.yaxis.set_tick_params(which='minor', length=3)
        ax.axhline(0, 0, 1, color='black', alpha=0.2)
    for ax in [ax21, ax22, ax23]:
        ax.set_xlabel('\\boldmath$x_{\\rm bj}$', size=40)
    for ax in [ax12, ax22, ax23]:
        ax.tick_params(axis='both', which='both', labelleft=False)
    ax11.set_ylim(-0.18, 1.0)
    ax12.set_ylim(-0.18, 1.0)
    ax21.set_ylim(-0.18, 0.9)
    ax22.set_ylim(-0.18, 0.9)
    ax23.set_ylim(-0.18, 0.9)
    minorLocator = MultipleLocator(0.1)
    majorLocator = MultipleLocator(0.2)
    ax11.yaxis.set_minor_locator(minorLocator)
    ax11.yaxis.set_major_locator(majorLocator)
    ax12.yaxis.set_minor_locator(minorLocator)
    ax12.yaxis.set_major_locator(majorLocator)
    minorLocator = MultipleLocator(0.1)
    majorLocator = MultipleLocator(0.2)
    ax21.yaxis.set_minor_locator(minorLocator)
    ax21.yaxis.set_major_locator(majorLocator)
    ax22.yaxis.set_minor_locator(minorLocator)
    ax22.yaxis.set_major_locator(majorLocator)
    ax11.text(0.05, 0.75, '\\boldmath$A^{p,K^+}_{1}$', transform=ax11.transAxes, size=40)
    ax12.text(0.05, 0.75, '\\boldmath$A^{p,K^-}_{1}$', transform=ax12.transAxes, size=40)
    ax21.text(0.05, 0.75, '\\boldmath$A^{D,K^+}_{1}$', transform=ax21.transAxes, size=40)
    ax22.text(0.05, 0.75, '\\boldmath$A^{D,K^-}_{1}$', transform=ax22.transAxes, size=40)
    ax23.text(0.05, 0.75, '\\boldmath$A^{D,K^{\\pm}}_{1}$', transform=ax23.transAxes, size=40)
    (handles, labels) = ([], [])
    handles.append(hand.get(20012))
    handles.append(hand.get(20019))
    labels.append('\\textbf{\\textrm{HERMES}}')
    labels.append('\\textbf{\\textrm{COMPASS}}')
    thesis_legend(ax11, handles, labels, frameon=False, fontsize=22, loc=(0.0, 0.35), handletextpad=0.5, handlelength=1.0)
    (handles, labels) = ([], [])
    handles.append(thy_band.get(20012))
    handles.append(thy_band.get(20019))
    labels.append('\\textbf{\\textrm{JAM (HERMES)}}')
    labels.append('\\textbf{\\textrm{JAM (COMPASS)}}')
    thesis_legend(ax12, handles, labels, frameon=False, fontsize=22, loc=(0.0, 0.35), handletextpad=0.5, handlelength=1.0)
    py.tight_layout()
    py.subplots_adjust(hspace=0, wspace=0)
    filename = output_file
    thesis_finish_panels(py.gcf(), 'thesis_psidis_plot_kaon', _selected_keys)
    py.savefig(filename)

# Adapted from thesis/psidis.py:plot_hadron, original lines 384-577; SHA256 f16cd20d12dcbb2cf3d50686398736cb571cf36c6a884d6a78120d8cec104d96.
def thesis_psidis_plot_hadron(datasets, output_file, interval):
    _selected_keys = {r + ':' + str(i) for (r, tables) in datasets.items() for i in tables}
    thy_plot = thy_band = thy_plot0 = thy_band0 = thy_point0 = None
    istep = 0
    kc = SimpleNamespace(nc={0: 1})
    nc = 1
    predictions = {'reactions': datasets}
    data = datasets['psidis']
    tabs = data
    (nrows, ncols) = (3, 2)
    fig = py.figure(figsize=(ncols * 7, nrows * 4))
    ax11 = py.subplot(nrows, ncols, 1)
    ax12 = py.subplot(nrows, ncols, 2)
    ax21 = py.subplot(nrows, ncols, 3)
    ax22 = py.subplot(nrows, ncols, 4)
    ax31 = py.subplot(nrows, ncols, 5)
    ax32 = py.subplot(nrows, ncols, 6)
    tabs = datasets['psidis']
    data = predictions['reactions']['psidis']
    hand = {}
    thy_band = {}
    for idx in tabs:
        X = tabs[idx]['X']
        values = tabs[idx]['value']
        alpha = data[idx]['alpha']
        if idx == 20000:
            (ax, color) = (ax11, 'blue')
        if idx == 20001:
            (ax, color) = (ax12, 'blue')
        if idx == 20002:
            (ax, color) = (ax21, 'blue')
        if idx == 20003:
            (ax, color) = (ax22, 'blue')
        if idx == 20006:
            (ax, color) = (ax11, 'firebrick')
        if idx == 20007:
            (ax, color) = (ax12, 'firebrick')
        if idx == 20010:
            (ax, color) = (ax21, 'firebrick')
        if idx == 20011:
            (ax, color) = (ax22, 'firebrick')
        if idx == 20015:
            (ax, color) = (ax31, 'firebrick')
        if idx == 20016:
            (ax, color) = (ax32, 'firebrick')
        if idx == 20023:
            (ax, color) = (ax21, 'darkgreen')
        if idx == 20024:
            (ax, color) = (ax22, 'darkgreen')
        hand[idx] = ax.errorbar(X, values, yerr=alpha, color=color, fmt='o', ms=2, capsize=3.0)
        for ic in range(nc):
            if nc > 1:
                color = colors[cluster[ic]]
            thy = data[idx]['thy-%d' % ic]
            std_lower = data[idx]['dthy_lower-%d' % ic]
            std_upper = data[idx]['dthy_upper-%d' % ic]
            down = thy - std_lower
            up = thy + std_upper
            (thy_plot,) = ax.plot(X, thy, color=color)
            thy_band[idx] = ax.fill_between(X, np.minimum(down, up), np.maximum(down, up), color=color, alpha=0.5, where=thesis_band_mask(X))
    for ax in [ax11, ax12, ax21, ax22]:
        ax.tick_params(axis='both', which='both', top=True, right=True, labelbottom=False, direction='in', labelsize=30)
        ax.set_yticks([0.25, 0.5, 0.75])
    for ax in [ax31, ax32]:
        ax.tick_params(axis='both', which='both', top=True, right=True, direction='in', labelsize=30)
    for ax in [ax11, ax12, ax21, ax22, ax31, ax32]:
        ax.semilogx()
        ax.set_xlim(0.004, 0.6)
        ax.set_xticks([0.01, 0.1])
        ax.set_xticklabels(['$0.01$', '$0.1$'])
        ax.xaxis.set_tick_params(which='major', length=6)
        ax.xaxis.set_tick_params(which='minor', length=3)
        ax.yaxis.set_tick_params(which='major', length=6)
        ax.yaxis.set_tick_params(which='minor', length=3)
        ax.axhline(0, 0, 1, color='black', alpha=0.2)
    for ax in [ax31, ax32]:
        ax.set_xlabel('\\boldmath$x_{\\rm bj}$', size=40)
    for ax in [ax12, ax22, ax32]:
        ax.tick_params(axis='both', which='both', labelleft=False)
    ax11.set_ylim(-0.1, 0.99)
    ax12.set_ylim(-0.1, 0.99)
    ax21.set_ylim(-0.1, 0.5)
    ax22.set_ylim(-0.1, 0.5)
    ax31.set_ylim(-0.5, 0.5)
    ax32.set_ylim(-0.5, 0.5)
    minorLocator = MultipleLocator(0.1)
    majorLocator = MultipleLocator(0.2)
    ax11.yaxis.set_minor_locator(minorLocator)
    ax11.yaxis.set_major_locator(majorLocator)
    ax12.yaxis.set_minor_locator(minorLocator)
    ax12.yaxis.set_major_locator(majorLocator)
    minorLocator = MultipleLocator(0.1)
    majorLocator = MultipleLocator(0.2)
    ax21.yaxis.set_minor_locator(minorLocator)
    ax21.yaxis.set_major_locator(majorLocator)
    ax22.yaxis.set_minor_locator(minorLocator)
    ax22.yaxis.set_major_locator(majorLocator)
    minorLocator = MultipleLocator(0.1)
    majorLocator = MultipleLocator(0.2)
    ax31.yaxis.set_minor_locator(minorLocator)
    ax31.yaxis.set_major_locator(majorLocator)
    ax32.yaxis.set_minor_locator(minorLocator)
    ax32.yaxis.set_major_locator(majorLocator)
    ax11.text(0.05, 0.75, '\\boldmath$A^{p,h^+}_{1}$', transform=ax11.transAxes, size=40)
    ax12.text(0.05, 0.75, '\\boldmath$A^{p,h^-}_{1}$', transform=ax12.transAxes, size=40)
    ax21.text(0.05, 0.75, '\\boldmath$A^{D,h^+}_{1}$', transform=ax21.transAxes, size=40)
    ax22.text(0.05, 0.75, '\\boldmath$A^{D,h^-}_{1}$', transform=ax22.transAxes, size=40)
    ax31.text(0.05, 0.75, '\\boldmath$A^{^3{\\rm He},h^+}_{1}$', transform=ax31.transAxes, size=40)
    ax32.text(0.05, 0.75, '\\boldmath$A^{^3{\\rm He},h^-}_{1}$', transform=ax32.transAxes, size=40)
    (handles, labels) = ([], [])
    handles.append(hand.get(20006))
    handles.append(hand.get(20023))
    handles.append(hand.get(20000))
    labels.append('\\textbf{\\textrm{HERMES}}')
    labels.append('\\textbf{\\textrm{COMPASS}}')
    labels.append('\\textbf{\\textrm{SMC}}')
    thesis_legend(ax11, handles, labels, frameon=False, fontsize=22, loc=(0.0, 0.35), handletextpad=0.5, handlelength=1.0)
    (handles, labels) = ([], [])
    handles.append(thy_band.get(20006))
    handles.append(thy_band.get(20023))
    handles.append(thy_band.get(20000))
    labels.append('\\textbf{\\textrm{JAM (HERMES)}}')
    labels.append('\\textbf{\\textrm{JAM (COMPASS)}}')
    labels.append('\\textbf{\\textrm{JAM (SMC)}}')
    thesis_legend(ax12, handles, labels, frameon=False, fontsize=22, loc=(0.0, 0.35), handletextpad=0.5, handlelength=1.0)
    py.tight_layout()
    py.subplots_adjust(hspace=0, wspace=0)
    filename = output_file
    thesis_finish_panels(py.gcf(), 'thesis_psidis_plot_hadron', _selected_keys)
    py.savefig(filename)




_THESIS_FIGURES = [{'source': 'dy.py:plot_ratios', 'function': 'thesis_dy_plot_ratios', 'filename': 'DY-ratios.png', 'datasets': ['dy:20001', 'dy:20002'], 'lines': [42, 212], 'notes': [], 'arithmetic_corrections': [], 'panels': [['dy:20001', 'dy:20002'], ['dy:20001', 'dy:20002']], 'output_directory': ''}, {'source': 'dy.py:plot_xsec', 'function': 'thesis_dy_plot_xsec', 'filename': 'DY-xsec.png', 'datasets': ['dy:10001'], 'lines': [214, 360], 'notes': [], 'arithmetic_corrections': [], 'panels': [['dy:10001']], 'output_directory': ''}, {'source': 'idis.py:plot_proton', 'function': 'thesis_idis_plot_proton', 'filename': 'DIS-proton.png', 'datasets': ['idis:10003', 'idis:10010', 'idis:10016', 'idis:10020', 'idis:10026', 'idis:10027', 'idis:10028', 'idis:10029', 'idis:10030'], 'lines': [301, 777], 'notes': [], 'arithmetic_corrections': [], 'panels': [['idis:10010', 'idis:10016', 'idis:10020'], ['idis:10003'], ['idis:10026', 'idis:10030'], ['idis:10027', 'idis:10028', 'idis:10029']], 'output_directory': ''}, {'source': 'idis.py:plot_deuteron', 'function': 'thesis_idis_plot_deuteron', 'filename': 'DIS-deuteron.png', 'datasets': ['idis:10002', 'idis:10011', 'idis:10017', 'idis:10021', 'idis:10033', 'idis:10050'], 'lines': [779, 1186], 'notes': [], 'arithmetic_corrections': [], 'panels': [['idis:10011', 'idis:10017'], ['idis:10002'], ['idis:10021', 'idis:10050'], ['idis:10033']], 'output_directory': ''}, {'source': 'idis.py:plot_CC', 'function': 'thesis_idis_plot_CC', 'filename': 'DIS-CC.png', 'datasets': ['idis:10031', 'idis:10032'], 'lines': [1188, 1301], 'notes': [], 'arithmetic_corrections': [], 'panels': [['idis:10031', 'idis:10032']], 'output_directory': ''}, {'source': 'idis.py:plot_A3', 'function': 'thesis_idis_plot_A3', 'filename': 'DIS-A3.png', 'datasets': ['idis:10041', 'idis:10051'], 'lines': [1303, 1468], 'notes': [], 'arithmetic_corrections': [], 'panels': [['idis:10041'], ['idis:10051']], 'output_directory': ''}, {'source': 'marathon.py:__main__', 'function': 'thesis_marathon_plot_main', 'filename': 'marathon.png', 'datasets': ['idis:10050', 'idis:10051'], 'lines': [138, 383], 'notes': ['Only selected campaign supplied; historical alternate-fit overlay absent.'], 'arithmetic_corrections': [], 'panels': [['idis:10050'], ['idis:10051']], 'output_directory': ''}, {'source': 'jets.py:plot_data', 'function': 'thesis_jets_plot_data', 'filename': 'jets.png', 'datasets': ['jet:10001', 'jet:10002', 'jet:10003', 'jet:10004'], 'lines': [136, 362], 'notes': [], 'arithmetic_corrections': ["RHIC ratio panels use each dataset's own eta-bin row bounds, not the preceding dataset's."], 'panels': [['jet:10001'], ['jet:10002'], ['jet:10003', 'jet:10004'], ['jet:10003'], ['jet:10004']], 'output_directory': 'jets'}, {'source': 'lepton.py:plot_asym', 'function': 'thesis_lepton_plot_asym', 'filename': 'lepton-asym.png', 'datasets': ['wzrv:2010', 'wzrv:2011', 'wzrv:2012', 'wzrv:2013', 'wzrv:2014', 'wzrv:2016', 'wzrv:2017'], 'lines': [40, 319], 'notes': [], 'arithmetic_corrections': ['Experimental data/theory errors divide by the fixed mean theory, not measured data.'], 'panels': [['wzrv:2010', 'wzrv:2011', 'wzrv:2012', 'wzrv:2013', 'wzrv:2014'], ['wzrv:2016', 'wzrv:2017'], ['wzrv:2011', 'wzrv:2013', 'wzrv:2014'], ['wzrv:2016'], ['wzrv:2010'], ['wzrv:2017'], ['wzrv:2012'], []], 'output_directory': ''}, {'source': 'lepton.py:plot_star', 'function': 'thesis_lepton_plot_star', 'filename': 'lepton-STAR.png', 'datasets': ['wzrv:2020'], 'lines': [321, 469], 'notes': [], 'arithmetic_corrections': [], 'panels': [['wzrv:2020'], ['wzrv:2020']], 'output_directory': ''}, {'source': 'wz.py:__main__', 'function': 'thesis_wz_plot_main', 'filename': 'WZboson.png', 'datasets': ['zrap:1000', 'zrap:1001', 'wasym:1000', 'wasym:1001'], 'lines': [57, 376], 'notes': [], 'arithmetic_corrections': ["Removed source's extra normalization in the Z data/theory uncertainty.", "Z normalization uses the current dataset's measured values, not a stale loop variable."], 'panels': [['zrap:1000', 'zrap:1001'], ['zrap:1000'], ['zrap:1001'], ['wasym:1000', 'wasym:1001'], ['wasym:1000'], ['wasym:1001']], 'output_directory': ''}, {'source': 'pidis.py:plot_proton_A1_Apa', 'function': 'thesis_pidis_plot_proton_A1_Apa', 'filename': 'PDIS-Apa-proton.png', 'datasets': ['pidis:10002', 'pidis:10003', 'pidis:10004', 'pidis:10007', 'pidis:10022', 'pidis:10029', 'pidis:10032', 'pidis:10035', 'pidis:10036'], 'lines': [279, 496], 'notes': [], 'arithmetic_corrections': [], 'panels': [['pidis:10002', 'pidis:10003', 'pidis:10004', 'pidis:10035', 'pidis:10036'], ['pidis:10007', 'pidis:10022', 'pidis:10029', 'pidis:10032']], 'output_directory': ''}, {'source': 'pidis.py:plot_deuteron_A1_Apa', 'function': 'thesis_pidis_plot_deuteron_A1_Apa', 'filename': 'PDIS-Apa-deuteron.png', 'datasets': ['pidis:10001', 'pidis:10006', 'pidis:10021', 'pidis:10027', 'pidis:10033', 'pidis:10034'], 'lines': [498, 708], 'notes': [], 'arithmetic_corrections': [], 'panels': [['pidis:10001', 'pidis:10033', 'pidis:10034'], ['pidis:10006', 'pidis:10021', 'pidis:10027']], 'output_directory': ''}, {'source': 'pidis.py:plot_helium_neutron', 'function': 'thesis_pidis_plot_helium_neutron', 'filename': 'PDIS-Apa-neutron-helium.png', 'datasets': ['pidis:10005', 'pidis:10018', 'pidis:10025'], 'lines': [710, 912], 'notes': [], 'arithmetic_corrections': [], 'panels': [['pidis:10005', 'pidis:10018'], ['pidis:10025']], 'output_directory': ''}, {'source': 'pjets.py:plot_pjets', 'function': 'thesis_pjets_plot_pjets', 'filename': 'pjets.png', 'datasets': ['pjet:20001', 'pjet:20002', 'pjet:20003', 'pjet:20004', 'pjet:20006', 'pjet:20007', 'pjet:20008'], 'lines': [40, 283], 'notes': [], 'arithmetic_corrections': ['Original pjets has only a band: added its mean curve in the existing theory color; no panels or legends changed.'], 'panels': [['pjet:20001'], ['pjet:20002'], ['pjet:20003'], ['pjet:20004'], ['pjet:20004'], ['pjet:20006'], ['pjet:20008'], ['pjet:20007'], ['pjet:20007']], 'output_directory': 'pjets'}, {'source': 'polWlep.py:__main__', 'function': 'thesis_polWlep_plot_main', 'filename': 'polWlep.png', 'datasets': ['wzrv:1000', 'wzrv:1020', 'wzrv:1021'], 'lines': [35, 226], 'notes': ['Only selected campaign supplied; historical alternate-fit overlay absent.'], 'arithmetic_corrections': [], 'panels': [['wzrv:1000'], ['wzrv:1020', 'wzrv:1021']], 'output_directory': ''}, {'source': 'sidis.py:plot_pion', 'function': 'thesis_sidis_plot_pion', 'filename': 'SIDIS-pion.png', 'datasets': ['sidis:1005', 'sidis:1006'], 'lines': [39, 168], 'notes': [], 'arithmetic_corrections': [], 'panels': [['sidis:1005', 'sidis:1006'], ['sidis:1005', 'sidis:1006'], ['sidis:1005', 'sidis:1006'], ['sidis:1005', 'sidis:1006'], ['sidis:1005', 'sidis:1006'], ['sidis:1005', 'sidis:1006'], ['sidis:1005', 'sidis:1006'], ['sidis:1005', 'sidis:1006']], 'output_directory': ''}, {'source': 'sidis.py:plot_kaon', 'function': 'thesis_sidis_plot_kaon', 'filename': 'SIDIS-kaon.png', 'datasets': ['sidis:2005', 'sidis:2006'], 'lines': [170, 299], 'notes': [], 'arithmetic_corrections': [], 'panels': [['sidis:2005', 'sidis:2006'], ['sidis:2005', 'sidis:2006'], ['sidis:2005', 'sidis:2006'], ['sidis:2005', 'sidis:2006'], ['sidis:2005', 'sidis:2006'], ['sidis:2005', 'sidis:2006'], ['sidis:2005', 'sidis:2006'], ['sidis:2005', 'sidis:2006']], 'output_directory': ''}, {'source': 'sidis.py:plot_hadron', 'function': 'thesis_sidis_plot_hadron', 'filename': 'SIDIS-hadron.png', 'datasets': ['sidis:3000', 'sidis:3001'], 'lines': [301, 430], 'notes': [], 'arithmetic_corrections': [], 'panels': [['sidis:3000', 'sidis:3001'], ['sidis:3000', 'sidis:3001'], ['sidis:3000', 'sidis:3001'], ['sidis:3000', 'sidis:3001'], ['sidis:3000', 'sidis:3001'], ['sidis:3000', 'sidis:3001'], ['sidis:3000', 'sidis:3001'], ['sidis:3000', 'sidis:3001']], 'output_directory': ''}, {'source': 'psidis.py:plot_pion', 'function': 'thesis_psidis_plot_pion', 'filename': 'PSIDIS-pion.png', 'datasets': ['psidis:20004', 'psidis:20005', 'psidis:20017', 'psidis:20018', 'psidis:20008', 'psidis:20009', 'psidis:20021', 'psidis:20022'], 'lines': [40, 209], 'notes': [], 'arithmetic_corrections': [], 'panels': [['psidis:20004', 'psidis:20017'], ['psidis:20005', 'psidis:20018'], ['psidis:20008', 'psidis:20021'], ['psidis:20009', 'psidis:20022']], 'output_directory': ''}, {'source': 'psidis.py:plot_kaon', 'function': 'thesis_psidis_plot_kaon', 'filename': 'PSIDIS-kaon.png', 'datasets': ['psidis:20012', 'psidis:20013', 'psidis:20014', 'psidis:20019', 'psidis:20020', 'psidis:20025', 'psidis:20026'], 'lines': [211, 382], 'notes': [], 'arithmetic_corrections': [], 'panels': [['psidis:20019'], ['psidis:20020'], ['psidis:20012', 'psidis:20025'], ['psidis:20013', 'psidis:20026'], ['psidis:20014']], 'output_directory': ''}, {'source': 'psidis.py:plot_hadron', 'function': 'thesis_psidis_plot_hadron', 'filename': 'PSIDIS-hadron.png', 'datasets': ['psidis:20000', 'psidis:20001', 'psidis:20002', 'psidis:20003', 'psidis:20006', 'psidis:20007', 'psidis:20010', 'psidis:20011', 'psidis:20015', 'psidis:20016', 'psidis:20023', 'psidis:20024'], 'lines': [384, 577], 'notes': [], 'arithmetic_corrections': [], 'panels': [['psidis:20000', 'psidis:20006'], ['psidis:20001', 'psidis:20007'], ['psidis:20002', 'psidis:20010', 'psidis:20023'], ['psidis:20003', 'psidis:20011', 'psidis:20024'], ['psidis:20015'], ['psidis:20016']], 'output_directory': ''}, {'source': 'corrected-sia.py:plot_sia:pion', 'function': 'thesis_sia_plot_pion', 'filename': 'SIA-pion.png', 'datasets': ['sia:1030', 'sia:1028', 'sia:1029', 'sia:1001', 'sia:1002', 'sia:1003', 'sia:1005', 'sia:1006', 'sia:1010', 'sia:1011', 'sia:1013', 'sia:1018', 'sia:1025', 'sia:1026', 'sia:1027', 'sia:1019', 'sia:1014', 'sia:1015', 'sia:1016', 'sia:1017'], 'lines': [1, 246], 'notes': [], 'arithmetic_corrections': ['Selected 0.1 split; pointwise low/mean and high/mean; stable dataset identity; no arbitrary mean replacement.'], 'panels': [['sia:1030'], ['sia:1028'], ['sia:1029'], ['sia:1001', 'sia:1002', 'sia:1003', 'sia:1005', 'sia:1006'], ['sia:1010'], ['sia:1011'], ['sia:1013'], ['sia:1018'], ['sia:1025'], ['sia:1026'], ['sia:1027'], ['sia:1019'], ['sia:1014'], ['sia:1015'], ['sia:1016'], ['sia:1017']], 'output_directory': ''}, {'source': 'corrected-sia.py:plot_sia:kaon', 'function': 'thesis_sia_plot_kaon', 'filename': 'SIA-kaon.png', 'datasets': ['sia:2030', 'sia:2028', 'sia:2029', 'sia:2001', 'sia:2002', 'sia:2003', 'sia:2005', 'sia:2008', 'sia:2013', 'sia:2018', 'sia:2025', 'sia:2026', 'sia:2027', 'sia:2019', 'sia:2014', 'sia:2015', 'sia:2016', 'sia:2017'], 'lines': [1, 246], 'notes': [], 'arithmetic_corrections': ['Selected 0.1 split; pointwise low/mean and high/mean; stable dataset identity; no arbitrary mean replacement.'], 'panels': [['sia:2030'], ['sia:2028'], ['sia:2029'], ['sia:2001', 'sia:2002', 'sia:2003', 'sia:2005'], ['sia:2008'], ['sia:2013'], ['sia:2018'], ['sia:2025'], ['sia:2026'], ['sia:2027'], ['sia:2019'], ['sia:2014'], ['sia:2015'], ['sia:2016'], ['sia:2017']], 'output_directory': ''}, {'source': 'corrected-sia.py:plot_sia:hadron', 'function': 'thesis_sia_plot_hadron', 'filename': 'SIA-hadron.png', 'datasets': ['sia:4003', 'sia:4008', 'sia:4009', 'sia:4010', 'sia:4012', 'sia:4004', 'sia:4000', 'sia:4001', 'sia:4017', 'sia:4013', 'sia:4007', 'sia:4002', 'sia:4016', 'sia:4014', 'sia:4015'], 'lines': [1, 246], 'notes': [], 'arithmetic_corrections': ['Selected 0.1 split; pointwise low/mean and high/mean; stable dataset identity; no arbitrary mean replacement.'], 'panels': [['sia:4003', 'sia:4008', 'sia:4009', 'sia:4010', 'sia:4012'], ['sia:4004'], ['sia:4000'], ['sia:4001'], ['sia:4017'], ['sia:4013'], ['sia:4007'], ['sia:4002'], ['sia:4016'], ['sia:4014'], ['sia:4015']], 'output_directory': ''}, {'source': 'corrected-wc.py:plot_wc', 'function': 'thesis_wc_plot', 'filename': 'wcharm.png', 'datasets': ['wc:20001', 'wc:10001', 'wc:20002', 'wc:10002', 'wc:20003'], 'lines': [1, 175], 'notes': [], 'arithmetic_corrections': ['Single red JAM series; pointwise percentiles; no historical comparisons.'], 'panels': [['wc:20001'], ['wc:10001'], ['wc:20002'], ['wc:10002'], ['wc:20003']], 'output_directory': 'wc'}]
_THESIS_PANEL_MAP = {'thesis_dy_plot_ratios': {'panels': [['dy:20001', 'dy:20002'], ['dy:20001', 'dy:20002']], 'annotations': [{'source_line': 139, 'axis': 'ax11', 'label': "'\\\\boldmath$\\\\sigma_{pD}^{\\\\rm DY}/2\\\\sigma_{pp}^{\\\\rm DY}$'", 'selection': 'panel-selected datasets'}, {'source_line': 140, 'axis': 'ax21', 'label': "'\\\\textbf{\\\\textrm{data/theory}}'", 'selection': 'panel-selected datasets'}, {'source_line': 141, 'axis': 'ax11', 'label': "'$0.48 < x_1 < 0.69$'", 'selection': '20002 in data'}, {'source_line': 142, 'axis': 'ax11', 'label': "'$0.36 < x_1 < 0.56$'", 'selection': '20001 in data'}]}, 'thesis_dy_plot_xsec': {'panels': [['dy:10001']], 'annotations': [{'source_line': 255, 'axis': 'ax11', 'label': "'$Q^2 \\\\, \\\\in \\\\,[15,20]\\\\,\\\\mathrm{GeV^2}$'", 'selection': 'panel-selected datasets'}, {'source_line': 256, 'axis': 'ax11', 'label': "'$Q^2 \\\\, \\\\in \\\\,[20,24]$'", 'selection': 'panel-selected datasets'}, {'source_line': 257, 'axis': 'ax11', 'label': "'$Q^2 \\\\, \\\\in \\\\,[24,28]$'", 'selection': 'panel-selected datasets'}, {'source_line': 258, 'axis': 'ax11', 'label': "'$Q^2 \\\\, \\\\in \\\\,[28,35]$'", 'selection': 'panel-selected datasets'}, {'source_line': 259, 'axis': 'ax11', 'label': "'$Q^2 \\\\, \\\\in \\\\,[35,40]$'", 'selection': 'panel-selected datasets'}, {'source_line': 260, 'axis': 'ax11', 'label': "'$Q^2 \\\\, \\\\in \\\\,[40,45]$'", 'selection': 'panel-selected datasets'}, {'source_line': 261, 'axis': 'ax11', 'label': "'$Q^2 \\\\, \\\\in \\\\,[45,52]$'", 'selection': 'panel-selected datasets'}, {'source_line': 262, 'axis': 'ax11', 'label': "'$Q^2 \\\\, \\\\in \\\\,[52,60]$'", 'selection': 'panel-selected datasets'}, {'source_line': 263, 'axis': 'ax11', 'label': "'$Q^2 \\\\, \\\\in \\\\,[60,68]$'", 'selection': 'panel-selected datasets'}, {'source_line': 264, 'axis': 'ax11', 'label': "'$Q^2 \\\\, \\\\in \\\\,[68,75]$'", 'selection': 'panel-selected datasets'}, {'source_line': 265, 'axis': 'ax11', 'label': "'$Q^2 \\\\, \\\\in \\\\,[100,140]$'", 'selection': 'panel-selected datasets'}, {'source_line': 266, 'axis': 'ax11', 'label': "'$Q^2 \\\\, \\\\in \\\\,[140,160]\\\\, (i=11)$'", 'selection': 'panel-selected datasets'}, {'source_line': 267, 'axis': 'ax11', 'label': "'$(i=0)$'", 'selection': 'panel-selected datasets'}, {'source_line': 268, 'axis': 'ax11', 'label': "'\\\\boldmath$M_{\\\\ell \\\\ell}^3 ~ \\\\frac{{\\\\rm d}^2 \\\\sigma_{pp}^{\\\\rm DY}}{{\\\\rm d} M_{\\\\ell \\\\ell} {\\\\rm d} x_F}$'", 'selection': 'panel-selected datasets'}, {'source_line': 269, 'axis': 'ax11', 'label': "'$(\\\\times\\\\, 10^{\\\\, i})$'", 'selection': 'panel-selected datasets'}]}, 'thesis_idis_plot_proton': {'panels': [['idis:10010', 'idis:10016', 'idis:10020'], ['idis:10003'], ['idis:10026', 'idis:10030'], ['idis:10027', 'idis:10028', 'idis:10029']], 'annotations': [{'source_line': 597, 'axis': 'ax11', 'label': "'$x=0.005\\\\, (i=24)$'", 'selection': "any(len(v['value'].get(26, [])) for v in PLOT.values())"}, {'source_line': 598, 'axis': 'ax11', 'label': "'$x=0.008$'", 'selection': "any(len(v['value'].get(25, [])) for v in PLOT.values())"}, {'source_line': 599, 'axis': 'ax11', 'label': "'$x=0.013$'", 'selection': "any(len(v['value'].get(24, [])) for v in PLOT.values())"}, {'source_line': 600, 'axis': 'ax11', 'label': "'$x=0.018$'", 'selection': "any(len(v['value'].get(23, [])) for v in PLOT.values())"}, {'source_line': 601, 'axis': 'ax11', 'label': "'$x=0.026$'", 'selection': "any(len(v['value'].get(22, [])) for v in PLOT.values())"}, {'source_line': 602, 'axis': 'ax11', 'label': "'$x=0.036$'", 'selection': "any(len(v['value'].get(21, [])) for v in PLOT.values())"}, {'source_line': 603, 'axis': 'ax11', 'label': "'$x=0.05$'", 'selection': "any(len(v['value'].get(20, [])) for v in PLOT.values())"}, {'source_line': 604, 'axis': 'ax11', 'label': "'$x=0.07$'", 'selection': "any(len(v['value'].get(19, [])) for v in PLOT.values())"}, {'source_line': 605, 'axis': 'ax11', 'label': "'$x=0.09$'", 'selection': "any(len(v['value'].get(18, [])) for v in PLOT.values())"}, {'source_line': 606, 'axis': 'ax11', 'label': "'$x=0.10$'", 'selection': "any(len(v['value'].get(17, [])) for v in PLOT.values())"}, {'source_line': 607, 'axis': 'ax11', 'label': "'$x=0.11$'", 'selection': "any(len(v['value'].get(16, [])) for v in PLOT.values())"}, {'source_line': 608, 'axis': 'ax11', 'label': "'$x=0.14$'", 'selection': "any(len(v['value'].get(15, [])) for v in PLOT.values())"}, {'source_line': 609, 'axis': 'ax11', 'label': "'$x=0.18$'", 'selection': "any(len(v['value'].get(14, [])) for v in PLOT.values())"}, {'source_line': 610, 'axis': 'ax11', 'label': "'$x=0.23$'", 'selection': "any(len(v['value'].get(13, [])) for v in PLOT.values())"}, {'source_line': 611, 'axis': 'ax11', 'label': "'$x=0.28$'", 'selection': "any(len(v['value'].get(12, [])) for v in PLOT.values())"}, {'source_line': 612, 'axis': 'ax11', 'label': "'$x=0.35$'", 'selection': "any(len(v['value'].get(11, [])) for v in PLOT.values())"}, {'source_line': 613, 'axis': 'ax11', 'label': "'$x=0.43$'", 'selection': "any(len(v['value'].get(10, [])) for v in PLOT.values())"}, {'source_line': 614, 'axis': 'ax11', 'label': "'$x=0.46$'", 'selection': "any(len(v['value'].get(9, [])) for v in PLOT.values())"}, {'source_line': 615, 'axis': 'ax11', 'label': "'$x=0.50$'", 'selection': "any(len(v['value'].get(8, [])) for v in PLOT.values())"}, {'source_line': 616, 'axis': 'ax11', 'label': "'$x=0.53$'", 'selection': "any(len(v['value'].get(7, [])) for v in PLOT.values())"}, {'source_line': 617, 'axis': 'ax11', 'label': "'$x=0.56$'", 'selection': "any(len(v['value'].get(6, [])) for v in PLOT.values())"}, {'source_line': 618, 'axis': 'ax11', 'label': "'$x=0.59$'", 'selection': "any(len(v['value'].get(5, [])) for v in PLOT.values())"}, {'source_line': 619, 'axis': 'ax11', 'label': "'$x=0.61$'", 'selection': "any(len(v['value'].get(4, [])) for v in PLOT.values())"}, {'source_line': 620, 'axis': 'ax11', 'label': "'$x=0.65$'", 'selection': "any(len(v['value'].get(3, [])) for v in PLOT.values())"}, {'source_line': 621, 'axis': 'ax11', 'label': "'$x=0.75$'", 'selection': "any(len(v['value'].get(2, [])) for v in PLOT.values())"}, {'source_line': 622, 'axis': 'ax11', 'label': "'$x=0.85$'", 'selection': "any(len(v['value'].get(1, [])) for v in PLOT.values())"}, {'source_line': 623, 'axis': 'ax11', 'label': "'$x=0.88\\\\, (i=0)$'", 'selection': "any(len(v['value'].get(0, [])) for v in PLOT.values())"}, {'source_line': 641, 'axis': 'ax11', 'label': "'\\\\boldmath$F_2^p$'", 'selection': 'panel-selected datasets'}, {'source_line': 642, 'axis': 'ax11', 'label': "'$(\\\\times\\\\, 2^{\\\\, i})$'", 'selection': 'panel-selected datasets'}, {'source_line': 689, 'axis': 'ax12', 'label': "'$\\\\theta=70^{\\\\circ} \\\\, (i=5)$'", 'selection': "any(len(v['value'].get(5, [])) for v in PLOT.values())"}, {'source_line': 690, 'axis': 'ax12', 'label': "'$\\\\theta=60^{\\\\circ} $'", 'selection': "any(len(v['value'].get(4, [])) for v in PLOT.values())"}, {'source_line': 691, 'axis': 'ax12', 'label': "'$\\\\theta=55^{\\\\circ} $'", 'selection': "any(len(v['value'].get(3, [])) for v in PLOT.values())"}, {'source_line': 692, 'axis': 'ax12', 'label': "'$\\\\theta=45^{\\\\circ} $'", 'selection': "any(len(v['value'].get(2, [])) for v in PLOT.values())"}, {'source_line': 693, 'axis': 'ax12', 'label': "'$\\\\theta=41^{\\\\circ} $'", 'selection': "any(len(v['value'].get(1, [])) for v in PLOT.values())"}, {'source_line': 694, 'axis': 'ax12', 'label': "'$\\\\theta=38^{\\\\circ}  \\\\, (i=0)$'", 'selection': "any(len(v['value'].get(0, [])) for v in PLOT.values())"}, {'source_line': 708, 'axis': 'ax12', 'label': "'\\\\boldmath$F_2^p$'", 'selection': 'panel-selected datasets'}, {'source_line': 709, 'axis': 'ax12', 'label': "'$(\\\\, \\\\times\\\\, 2^{\\\\, i})$'", 'selection': 'panel-selected datasets'}, {'source_line': 756, 'axis': 'ax21', 'label': "'$x=2.8\\\\cdot 10^{-5}\\\\, (i=24)$'", 'selection': "any(len(v['value'].get(24, [])) for v in PLOT.values())"}, {'source_line': 757, 'axis': 'ax21', 'label': "'$x=4.6\\\\cdot 10^{-5}$'", 'selection': "any(len(v['value'].get(23, [])) for v in PLOT.values())"}, {'source_line': 758, 'axis': 'ax21', 'label': "'$x=7.3\\\\cdot 10^{-5}$'", 'selection': "any(len(v['value'].get(22, [])) for v in PLOT.values())"}, {'source_line': 759, 'axis': 'ax21', 'label': "'$x=1.2\\\\cdot 10^{-4}$'", 'selection': "any(len(v['value'].get(21, [])) for v in PLOT.values())"}, {'source_line': 760, 'axis': 'ax21', 'label': "'$x=1.9\\\\cdot 10^{-4}$'", 'selection': "any(len(v['value'].get(20, [])) for v in PLOT.values())"}, {'source_line': 761, 'axis': 'ax21', 'label': "'$x=3.1\\\\cdot 10^{-4}$'", 'selection': "any(len(v['value'].get(19, [])) for v in PLOT.values())"}, {'source_line': 762, 'axis': 'ax21', 'label': "'$x=4.4\\\\cdot 10^{-4}$'", 'selection': "any(len(v['value'].get(18, [])) for v in PLOT.values())"}, {'source_line': 763, 'axis': 'ax21', 'label': "'$x=6.2\\\\cdot 10^{-4}$'", 'selection': "any(len(v['value'].get(17, [])) for v in PLOT.values())"}, {'source_line': 764, 'axis': 'ax21', 'label': "'$x=7.7\\\\cdot 10^{-4}$'", 'selection': "any(len(v['value'].get(16, [])) for v in PLOT.values())"}, {'source_line': 765, 'axis': 'ax21', 'label': "'$x=9.1 \\\\cdot 10^{-4}$'", 'selection': "any(len(v['value'].get(15, [])) for v in PLOT.values())"}, {'source_line': 766, 'axis': 'ax21', 'label': "'$x=0.0014$'", 'selection': "any(len(v['value'].get(14, [])) for v in PLOT.values())"}, {'source_line': 767, 'axis': 'ax21', 'label': "'$x=0.0024$'", 'selection': "any(len(v['value'].get(13, [])) for v in PLOT.values())"}, {'source_line': 768, 'axis': 'ax21', 'label': "'$x=0.0035$'", 'selection': "any(len(v['value'].get(12, [])) for v in PLOT.values())"}, {'source_line': 769, 'axis': 'ax21', 'label': "'$x=0.0056$'", 'selection': "any(len(v['value'].get(11, [])) for v in PLOT.values())"}, {'source_line': 770, 'axis': 'ax21', 'label': "'$x=0.0083$'", 'selection': "any(len(v['value'].get(10, [])) for v in PLOT.values())"}, {'source_line': 771, 'axis': 'ax21', 'label': "'$x=0.013$'", 'selection': "any(len(v['value'].get(9, [])) for v in PLOT.values())"}, {'source_line': 772, 'axis': 'ax21', 'label': "'$x=0.021$'", 'selection': "any(len(v['value'].get(8, [])) for v in PLOT.values())"}, {'source_line': 773, 'axis': 'ax21', 'label': "'$x=0.032$'", 'selection': "any(len(v['value'].get(7, [])) for v in PLOT.values())"}, {'source_line': 774, 'axis': 'ax21', 'label': "'$x=0.052$'", 'selection': "any(len(v['value'].get(6, [])) for v in PLOT.values())"}, {'source_line': 775, 'axis': 'ax21', 'label': "'$x=0.084$'", 'selection': "any(len(v['value'].get(5, [])) for v in PLOT.values())"}, {'source_line': 776, 'axis': 'ax21', 'label': "'$x=0.13$'", 'selection': "any(len(v['value'].get(4, [])) for v in PLOT.values())"}, {'source_line': 777, 'axis': 'ax21', 'label': "'$x=0.18$'", 'selection': "any(len(v['value'].get(3, [])) for v in PLOT.values())"}, {'source_line': 778, 'axis': 'ax21', 'label': "'$x=0.25$'", 'selection': "any(len(v['value'].get(2, [])) for v in PLOT.values())"}, {'source_line': 779, 'axis': 'ax21', 'label': "'$x=0.40$'", 'selection': "any(len(v['value'].get(1, [])) for v in PLOT.values())"}, {'source_line': 780, 'axis': 'ax21', 'label': "'$x=0.65 \\\\, (i=0)$'", 'selection': "any(len(v['value'].get(0, [])) for v in PLOT.values())"}, {'source_line': 795, 'axis': 'ax21', 'label': "'\\\\textrm{\\\\textbf{HERA}}'", 'selection': 'panel-selected datasets'}, {'source_line': 796, 'axis': 'ax21', 'label': "'\\\\boldmath$\\\\sigma_{\\\\rm red}^{\\\\rm NC}$'", 'selection': 'panel-selected datasets'}, {'source_line': 797, 'axis': 'ax21', 'label': "'$(\\\\, \\\\times\\\\, 2^{\\\\, i})$'", 'selection': 'panel-selected datasets'}, {'source_line': 798, 'axis': 'ax21', 'label': "'$\\\\sqrt{s}=318 \\\\,\\\\rm{GeV}$'", 'selection': 'panel-selected datasets'}, {'source_line': 846, 'axis': 'ax22', 'label': "'$x=3.3 \\\\cdot 10^{-5}\\\\, (i=29)$'", 'selection': "any(len(v['value'].get(29, [])) for v in PLOT.values())"}, {'source_line': 847, 'axis': 'ax22', 'label': "'$x=5.9 \\\\cdot 10^{-5}$'", 'selection': "any(len(v['value'].get(28, [])) for v in PLOT.values())"}, {'source_line': 848, 'axis': 'ax22', 'label': "'$x=1.6 \\\\cdot 10^{-4}$'", 'selection': "any(len(v['value'].get(27, [])) for v in PLOT.values())"}, {'source_line': 849, 'axis': 'ax22', 'label': "'$x=3.1 \\\\cdot 10^{-4}$'", 'selection': "any(len(v['value'].get(26, [])) for v in PLOT.values())"}, {'source_line': 850, 'axis': 'ax22', 'label': "'$x=4.5 \\\\cdot 10^{-4}$'", 'selection': "any(len(v['value'].get(25, [])) for v in PLOT.values())"}, {'source_line': 851, 'axis': 'ax22', 'label': "'$x=6.2 \\\\cdot 10^{-4}$'", 'selection': "any(len(v['value'].get(24, [])) for v in PLOT.values())"}, {'source_line': 852, 'axis': 'ax22', 'label': "'$x=7.8 \\\\cdot 10^{-4}$'", 'selection': "any(len(v['value'].get(23, [])) for v in PLOT.values())"}, {'source_line': 853, 'axis': 'ax22', 'label': "'$x=9.1 \\\\cdot 10^{-4}$'", 'selection': "any(len(v['value'].get(22, [])) for v in PLOT.values())"}, {'source_line': 854, 'axis': 'ax22', 'label': "'$x=0.0011$'", 'selection': "any(len(v['value'].get(21, [])) for v in PLOT.values())"}, {'source_line': 855, 'axis': 'ax22', 'label': "'$x=0.0015$'", 'selection': "any(len(v['value'].get(20, [])) for v in PLOT.values())"}, {'source_line': 856, 'axis': 'ax22', 'label': "'$x=0.0022$'", 'selection': "any(len(v['value'].get(19, [])) for v in PLOT.values())"}, {'source_line': 857, 'axis': 'ax22', 'label': "'$x=0.0034$'", 'selection': "any(len(v['value'].get(18, [])) for v in PLOT.values())"}, {'source_line': 858, 'axis': 'ax22', 'label': "'$x=0.0046$'", 'selection': "any(len(v['value'].get(17, [])) for v in PLOT.values())"}, {'source_line': 859, 'axis': 'ax22', 'label': "'$x=0.0064$'", 'selection': "any(len(v['value'].get(16, [])) for v in PLOT.values())"}, {'source_line': 860, 'axis': 'ax22', 'label': "'$x=0.0085$'", 'selection': "any(len(v['value'].get(15, [])) for v in PLOT.values())"}, {'source_line': 861, 'axis': 'ax22', 'label': "'$x=0.0094$'", 'selection': "any(len(v['value'].get(14, [])) for v in PLOT.values())"}, {'source_line': 862, 'axis': 'ax22', 'label': "'$x=0.011$'", 'selection': "any(len(v['value'].get(13, [])) for v in PLOT.values())"}, {'source_line': 863, 'axis': 'ax22', 'label': "'$x=0.0138$'", 'selection': "any(len(v['value'].get(12, [])) for v in PLOT.values())"}, {'source_line': 864, 'axis': 'ax22', 'label': "'$x=0.0156$'", 'selection': "any(len(v['value'].get(11, [])) for v in PLOT.values())"}, {'source_line': 865, 'axis': 'ax22', 'label': "'$x=0.0183$'", 'selection': "any(len(v['value'].get(10, [])) for v in PLOT.values())"}, {'source_line': 866, 'axis': 'ax22', 'label': "'$x=0.020$'", 'selection': "any(len(v['value'].get(9, [])) for v in PLOT.values())"}, {'source_line': 867, 'axis': 'ax22', 'label': "'$x=0.025$'", 'selection': "any(len(v['value'].get(8, [])) for v in PLOT.values())"}, {'source_line': 868, 'axis': 'ax22', 'label': "'$x=0.032$'", 'selection': "any(len(v['value'].get(7, [])) for v in PLOT.values())"}, {'source_line': 869, 'axis': 'ax22', 'label': "'$x=0.05$'", 'selection': "any(len(v['value'].get(6, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10028', 'HERA 10029']))"}, {'source_line': 870, 'axis': 'ax22', 'label': "'$x=0.08$'", 'selection': "any(len(v['value'].get(5, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10028', 'HERA 10029']))"}, {'source_line': 871, 'axis': 'ax22', 'label': "'$x=0.13$'", 'selection': "any(len(v['value'].get(4, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10028', 'HERA 10029']))"}, {'source_line': 872, 'axis': 'ax22', 'label': "'$x=0.18$'", 'selection': "any(len(v['value'].get(3, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10028', 'HERA 10029']))"}, {'source_line': 873, 'axis': 'ax22', 'label': "'$x=0.25$'", 'selection': "any(len(v['value'].get(2, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10028', 'HERA 10029']))"}, {'source_line': 874, 'axis': 'ax22', 'label': "'$x=0.40$'", 'selection': "any(len(v['value'].get(1, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10028', 'HERA 10029']))"}, {'source_line': 875, 'axis': 'ax22', 'label': "'$x=0.65 \\\\, (i=0)$'", 'selection': "any(len(v['value'].get(0, [])) for v in PLOT.values())"}, {'source_line': 876, 'axis': 'ax22', 'label': "'$x=0.05$'", 'selection': "any(len(v['value'].get(6, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10027']))"}, {'source_line': 877, 'axis': 'ax22', 'label': "'$x=0.08$'", 'selection': "any(len(v['value'].get(5, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10027']))"}, {'source_line': 878, 'axis': 'ax22', 'label': "'$x=0.13$'", 'selection': "any(len(v['value'].get(4, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10027']))"}, {'source_line': 879, 'axis': 'ax22', 'label': "'$x=0.18$'", 'selection': "any(len(v['value'].get(3, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10027']))"}, {'source_line': 880, 'axis': 'ax22', 'label': "'$x=0.25$'", 'selection': "any(len(v['value'].get(2, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10027']))"}, {'source_line': 881, 'axis': 'ax22', 'label': "'$x=0.40\\\\, (i=1)$'", 'selection': "any(len(v['value'].get(1, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10027']))"}, {'source_line': 887, 'axis': 'ax22', 'label': "'\\\\textrm{\\\\textbf{HERA}}' + ' ' + '\\\\boldmath$e^-p$'", 'selection': 'panel-selected datasets'}, {'source_line': 888, 'axis': 'ax22', 'label': "'\\\\boldmath$\\\\sigma_{\\\\rm red}^{\\\\rm NC}$'", 'selection': 'panel-selected datasets'}, {'source_line': 889, 'axis': 'ax22', 'label': "'$(\\\\, \\\\times\\\\, 2^{\\\\, i})$'", 'selection': 'panel-selected datasets'}]}, 'thesis_idis_plot_deuteron': {'panels': [['idis:10011', 'idis:10017'], ['idis:10002'], ['idis:10021', 'idis:10050'], ['idis:10033']], 'annotations': [{'source_line': 971, 'axis': 'ax11', 'label': "'$x=0.07 \\\\,(i=15)$'", 'selection': "any(len(v['value'].get(18, [])) for v in PLOT.values())"}, {'source_line': 972, 'axis': 'ax11', 'label': "'$x=0.09$'", 'selection': "any(len(v['value'].get(17, [])) for v in PLOT.values())"}, {'source_line': 973, 'axis': 'ax11', 'label': "'$x=0.10$'", 'selection': "any(len(v['value'].get(16, [])) for v in PLOT.values())"}, {'source_line': 974, 'axis': 'ax11', 'label': "'$x=0.14$'", 'selection': "any(len(v['value'].get(15, [])) for v in PLOT.values())"}, {'source_line': 975, 'axis': 'ax11', 'label': "'$x=0.18$'", 'selection': "any(len(v['value'].get(14, [])) for v in PLOT.values())"}, {'source_line': 976, 'axis': 'ax11', 'label': "'$x=0.23$'", 'selection': "any(len(v['value'].get(13, [])) for v in PLOT.values())"}, {'source_line': 977, 'axis': 'ax11', 'label': "'$x=0.28$'", 'selection': "any(len(v['value'].get(12, [])) for v in PLOT.values())"}, {'source_line': 978, 'axis': 'ax11', 'label': "'$x=0.35$'", 'selection': "any(len(v['value'].get(11, [])) for v in PLOT.values())"}, {'source_line': 979, 'axis': 'ax11', 'label': "'$x=0.43$'", 'selection': "any(len(v['value'].get(10, [])) for v in PLOT.values())"}, {'source_line': 980, 'axis': 'ax11', 'label': "'$x=0.46$'", 'selection': "any(len(v['value'].get(9, [])) for v in PLOT.values())"}, {'source_line': 981, 'axis': 'ax11', 'label': "'$x=0.50$'", 'selection': "any(len(v['value'].get(8, [])) for v in PLOT.values())"}, {'source_line': 982, 'axis': 'ax11', 'label': "'$x=0.53$'", 'selection': "any(len(v['value'].get(7, [])) for v in PLOT.values())"}, {'source_line': 983, 'axis': 'ax11', 'label': "'$x=0.56$'", 'selection': "any(len(v['value'].get(6, [])) for v in PLOT.values())"}, {'source_line': 984, 'axis': 'ax11', 'label': "'$x=0.59$'", 'selection': "any(len(v['value'].get(5, [])) for v in PLOT.values())"}, {'source_line': 985, 'axis': 'ax11', 'label': "'$x=0.61$'", 'selection': "any(len(v['value'].get(4, [])) for v in PLOT.values())"}, {'source_line': 986, 'axis': 'ax11', 'label': "'$x=0.65$'", 'selection': "any(len(v['value'].get(3, [])) for v in PLOT.values())"}, {'source_line': 987, 'axis': 'ax11', 'label': "'$x=0.75$'", 'selection': "any(len(v['value'].get(2, [])) for v in PLOT.values())"}, {'source_line': 988, 'axis': 'ax11', 'label': "'$x=0.85$'", 'selection': "any(len(v['value'].get(1, [])) for v in PLOT.values())"}, {'source_line': 989, 'axis': 'ax11', 'label': "'$x=0.88 \\\\,(i=0)$'", 'selection': "any(len(v['value'].get(0, [])) for v in PLOT.values())"}, {'source_line': 1009, 'axis': 'ax11', 'label': "'\\\\boldmath$F_2^D$'", 'selection': 'panel-selected datasets'}, {'source_line': 1010, 'axis': 'ax11', 'label': "'$(\\\\times\\\\, 2^{\\\\, i})$'", 'selection': 'panel-selected datasets'}, {'source_line': 1054, 'axis': 'ax12', 'label': "'$\\\\theta=70^{\\\\circ} \\\\, (i=5)$'", 'selection': "any(len(v['value'].get(5, [])) for v in PLOT.values())"}, {'source_line': 1055, 'axis': 'ax12', 'label': "'$\\\\theta=60^{\\\\circ} $'", 'selection': "any(len(v['value'].get(4, [])) for v in PLOT.values())"}, {'source_line': 1056, 'axis': 'ax12', 'label': "'$\\\\theta=55^{\\\\circ} $'", 'selection': "any(len(v['value'].get(3, [])) for v in PLOT.values())"}, {'source_line': 1057, 'axis': 'ax12', 'label': "'$\\\\theta=45^{\\\\circ} $'", 'selection': "any(len(v['value'].get(2, [])) for v in PLOT.values())"}, {'source_line': 1058, 'axis': 'ax12', 'label': "'$\\\\theta=41^{\\\\circ} $'", 'selection': "any(len(v['value'].get(1, [])) for v in PLOT.values())"}, {'source_line': 1059, 'axis': 'ax12', 'label': "'$\\\\theta=38^{\\\\circ}  \\\\, (i=0)$'", 'selection': "any(len(v['value'].get(0, [])) for v in PLOT.values())"}, {'source_line': 1073, 'axis': 'ax12', 'label': "'\\\\boldmath$F_2^D$'", 'selection': 'panel-selected datasets'}, {'source_line': 1074, 'axis': 'ax12', 'label': "'$(\\\\, \\\\times\\\\, 2^{\\\\, i})$'", 'selection': 'panel-selected datasets'}, {'source_line': 1121, 'axis': 'ax21', 'label': "'$x=0.005 \\\\, (i=17)$'", 'selection': "any(len(v['value'].get(17, [])) for v in PLOT.values())"}, {'source_line': 1122, 'axis': 'ax21', 'label': "'$x=0.008$'", 'selection': "any(len(v['value'].get(16, [])) for v in PLOT.values())"}, {'source_line': 1123, 'axis': 'ax21', 'label': "'$x=0.013$'", 'selection': "any(len(v['value'].get(15, [])) for v in PLOT.values())"}, {'source_line': 1124, 'axis': 'ax21', 'label': "'$x=0.018$'", 'selection': "any(len(v['value'].get(14, [])) for v in PLOT.values())"}, {'source_line': 1125, 'axis': 'ax21', 'label': "'$x=0.025$'", 'selection': "any(len(v['value'].get(13, [])) for v in PLOT.values())"}, {'source_line': 1126, 'axis': 'ax21', 'label': "'$x=0.035$'", 'selection': "any(len(v['value'].get(12, [])) for v in PLOT.values())"}, {'source_line': 1127, 'axis': 'ax21', 'label': "'$x=0.05$'", 'selection': "any(len(v['value'].get(11, [])) for v in PLOT.values())"}, {'source_line': 1128, 'axis': 'ax21', 'label': "'$x=0.07$'", 'selection': "any(len(v['value'].get(10, [])) for v in PLOT.values())"}, {'source_line': 1129, 'axis': 'ax21', 'label': "'$x=0.09$'", 'selection': "any(len(v['value'].get(9, [])) for v in PLOT.values())"}, {'source_line': 1130, 'axis': 'ax21', 'label': "'$x=0.11$'", 'selection': "any(len(v['value'].get(8, [])) for v in PLOT.values())"}, {'source_line': 1131, 'axis': 'ax21', 'label': "'$x=0.14$'", 'selection': "any(len(v['value'].get(7, [])) for v in PLOT.values())"}, {'source_line': 1132, 'axis': 'ax21', 'label': "'$x=0.18$'", 'selection': "any(len(v['value'].get(6, [])) for v in PLOT.values())"}, {'source_line': 1133, 'axis': 'ax21', 'label': "'$x=0.23$'", 'selection': "any(len(v['value'].get(5, [])) for v in PLOT.values())"}, {'source_line': 1134, 'axis': 'ax21', 'label': "'$x=0.28$'", 'selection': "any(len(v['value'].get(4, [])) for v in PLOT.values())"}, {'source_line': 1135, 'axis': 'ax21', 'label': "'$x=0.35$'", 'selection': "any(len(v['value'].get(3, [])) for v in PLOT.values())"}, {'source_line': 1136, 'axis': 'ax21', 'label': "'$x=0.45$'", 'selection': "any(len(v['value'].get(2, [])) for v in PLOT.values())"}, {'source_line': 1137, 'axis': 'ax21', 'label': "'$x=0.55$'", 'selection': "any(len(v['value'].get(1, [])) for v in PLOT.values())"}, {'source_line': 1138, 'axis': 'ax21', 'label': "'$x=0.68 \\\\,(i=0)$'", 'selection': "any(len(v['value'].get(0, [])) for v in PLOT.values())"}, {'source_line': 1158, 'axis': 'ax21', 'label': "'\\\\boldmath$F_2^D/F_2^p$'", 'selection': 'panel-selected datasets'}, {'source_line': 1159, 'axis': 'ax21', 'label': "'$(\\\\, \\\\times\\\\, 2^{\\\\, i})$'", 'selection': 'panel-selected datasets'}, {'source_line': 1199, 'axis': 'ax22', 'label': "'$Q^2 = 4.0 $' + '$\\\\, (i=5)$'", 'selection': "any(len(v['value'].get(5, [])) for v in PLOT.values())"}, {'source_line': 1200, 'axis': 'ax22', 'label': "'$Q^2 = 3.4 $'", 'selection': "any(len(v['value'].get(4, [])) for v in PLOT.values())"}, {'source_line': 1201, 'axis': 'ax22', 'label': "'$Q^2 = 2.9 $'", 'selection': "any(len(v['value'].get(3, [])) for v in PLOT.values())"}, {'source_line': 1202, 'axis': 'ax22', 'label': "'$Q^2 = 2.4 $'", 'selection': "any(len(v['value'].get(2, [])) for v in PLOT.values())"}, {'source_line': 1203, 'axis': 'ax22', 'label': "'$Q^2 = 2.0 $'", 'selection': "any(len(v['value'].get(1, [])) for v in PLOT.values())"}, {'source_line': 1204, 'axis': 'ax22', 'label': "'$Q^2 = 1.7 \\\\: \\\\rm{GeV^2}$' + '$\\\\, (i=0)$'", 'selection': "any(len(v['value'].get(0, [])) for v in PLOT.values())"}, {'source_line': 1214, 'axis': 'ax22', 'label': "'\\\\boldmath$F_2^n/F_2^D$'", 'selection': 'panel-selected datasets'}, {'source_line': 1215, 'axis': 'ax22', 'label': "'$(\\\\, \\\\times\\\\, 4^{\\\\, i})$'", 'selection': 'panel-selected datasets'}]}, 'thesis_idis_plot_CC': {'panels': [['idis:10031', 'idis:10032']], 'annotations': [{'source_line': 1282, 'axis': 'ax', 'label': "'$x=0.008 \\\\, (i=7)$'", 'selection': "any(len(v['value'].get(7, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10032']))"}, {'source_line': 1283, 'axis': 'ax', 'label': "'$x=0.013$'", 'selection': "any(len(v['value'].get(6, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10032']))"}, {'source_line': 1284, 'axis': 'ax', 'label': "'$x=0.032$'", 'selection': "any(len(v['value'].get(5, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10032']))"}, {'source_line': 1285, 'axis': 'ax', 'label': "'$x=0.08$'", 'selection': "any(len(v['value'].get(4, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10032']))"}, {'source_line': 1286, 'axis': 'ax', 'label': "'$x=0.13$'", 'selection': "any(len(v['value'].get(3, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10032']))"}, {'source_line': 1287, 'axis': 'ax', 'label': "'$x=0.25$'", 'selection': "any(len(v['value'].get(2, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10032']))"}, {'source_line': 1288, 'axis': 'ax', 'label': "'$x=0.40$'", 'selection': "any(len(v['value'].get(1, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10032']))"}, {'source_line': 1289, 'axis': 'ax', 'label': "'$x=0.65 \\\\, (i=0) $'", 'selection': "any(len(v['value'].get(0, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10032']))"}, {'source_line': 1290, 'axis': 'ax', 'label': "'$x=0.008 \\\\, (i=7)$'", 'selection': "any(len(v['value'].get(7, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10031']))"}, {'source_line': 1291, 'axis': 'ax', 'label': "'$x=0.013$'", 'selection': "any(len(v['value'].get(6, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10031']))"}, {'source_line': 1292, 'axis': 'ax', 'label': "'$x=0.032$'", 'selection': "any(len(v['value'].get(5, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10031']))"}, {'source_line': 1293, 'axis': 'ax', 'label': "'$x=0.08$'", 'selection': "any(len(v['value'].get(4, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10031']))"}, {'source_line': 1294, 'axis': 'ax', 'label': "'$x=0.13$'", 'selection': "any(len(v['value'].get(3, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10031']))"}, {'source_line': 1295, 'axis': 'ax', 'label': "'$x=0.25$'", 'selection': "any(len(v['value'].get(2, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10031']))"}, {'source_line': 1296, 'axis': 'ax', 'label': "'$x=0.40 \\\\, (i=1)$'", 'selection': "any(len(v['value'].get(1, [])) for v in (v for name, v in PLOT.items() if name in ['HERA 10031']))"}, {'source_line': 1308, 'axis': 'ax', 'label': "'\\\\boldmath$\\\\sigma_{\\\\rm red}^{\\\\rm CC}$'", 'selection': 'panel-selected datasets'}, {'source_line': 1309, 'axis': 'ax', 'label': "'$(\\\\, \\\\times\\\\, 5^{\\\\, i})$'", 'selection': 'panel-selected datasets'}]}, 'thesis_idis_plot_A3': {'panels': [['idis:10041'], ['idis:10051']], 'annotations': [{'source_line': 1380, 'axis': 'ax11', 'label': "'\\\\boldmath$F_2^{^3\\\\rm{He}}/F_2^{D}$'", 'selection': 'panel-selected datasets'}, {'source_line': 1436, 'axis': 'ax12', 'label': "'\\\\boldmath$F_2^{^3\\\\rm{He}}/F_2^{^3\\\\rm{H}}$'", 'selection': 'panel-selected datasets'}]}, 'thesis_marathon_plot_main': {'panels': [['idis:10050'], ['idis:10051']], 'annotations': [{'source_line': 1669, 'axis': 'ax12', 'label': "'$Q^2 = 14 x_{\\\\rm bj} ~ {\\\\rm GeV^2}$'", 'selection': 'panel-selected datasets'}, {'source_line': 1679, 'axis': 'ax11', 'label': "'\\\\boldmath$F_2^D/F_2^p$'", 'selection': 'panel-selected datasets'}, {'source_line': 1680, 'axis': 'ax12', 'label': "'\\\\boldmath$F_2^{^3\\\\rm{He}}/F_2^{^3\\\\rm{H}}$'", 'selection': 'panel-selected datasets'}]}, 'thesis_jets_plot_data': {'panels': [['jet:10001'], ['jet:10002'], ['jet:10003', 'jet:10004'], ['jet:10003'], ['jet:10004']], 'annotations': [{'source_line': 1895, 'axis': 'axs[i]', 'label': "'$%s$' % eta_range", 'selection': 'panel-selected datasets'}, {'source_line': 1905, 'axis': 'axs[i]', 'label': "'\\\\boldmath{$\\\\mathrm{%s}$}' % text", 'selection': 'panel-selected datasets'}, {'source_line': 1929, 'axis': 'axs[3]', 'label': "'\\\\boldmath{$\\\\mathrm{data/theory}$}'", 'selection': 'panel-selected datasets'}, {'source_line': 1930, 'axis': 'axs[4]', 'label': "'\\\\boldmath{$\\\\mathrm{data/theory}$}'", 'selection': 'panel-selected datasets'}, {'source_line': 1931, 'axis': 'axs[3]', 'label': "'\\\\boldmath{$\\\\mathrm{STAR~2003}$}'", 'selection': 'panel-selected datasets'}, {'source_line': 1932, 'axis': 'axs[4]', 'label': "'\\\\boldmath{$\\\\mathrm{STAR~2004}$}'", 'selection': 'panel-selected datasets'}, {'source_line': 1937, 'axis': 'axs[0]', 'label': "'$%s$' % eta_name", 'selection': 'panel-selected datasets'}, {'source_line': 1938, 'axis': 'axs[0]', 'label': "'$%s$' % factor", 'selection': 'panel-selected datasets'}, {'source_line': 1940, 'axis': 'axs[0]', 'label': "'$%s$' % factor", 'selection': 'panel-selected datasets'}, {'source_line': 1942, 'axis': 'axs[0]', 'label': "'$%s$' % factor", 'selection': 'panel-selected datasets'}, {'source_line': 1944, 'axis': 'axs[0]', 'label': "'$%s$' % factor", 'selection': 'panel-selected datasets'}, {'source_line': 1946, 'axis': 'axs[0]', 'label': "'$%s$' % factor", 'selection': 'panel-selected datasets'}, {'source_line': 1948, 'axis': 'axs[0]', 'label': "'$%s$' % factor", 'selection': 'panel-selected datasets'}, {'source_line': 1953, 'axis': 'axs[1]', 'label': "'$%s$' % eta_name", 'selection': 'panel-selected datasets'}, {'source_line': 1954, 'axis': 'axs[1]', 'label': "'$%s$' % factor", 'selection': 'panel-selected datasets'}, {'source_line': 1956, 'axis': 'axs[1]', 'label': "'$%s$' % factor", 'selection': 'panel-selected datasets'}, {'source_line': 1958, 'axis': 'axs[1]', 'label': "'$%s$' % factor", 'selection': 'panel-selected datasets'}, {'source_line': 1960, 'axis': 'axs[1]', 'label': "'$%s$' % factor", 'selection': 'panel-selected datasets'}, {'source_line': 1962, 'axis': 'axs[1]', 'label': "'$%s$' % factor", 'selection': 'panel-selected datasets'}]}, 'thesis_lepton_plot_asym': {'panels': [['wzrv:2010', 'wzrv:2011', 'wzrv:2012', 'wzrv:2013', 'wzrv:2014'], ['wzrv:2016', 'wzrv:2017'], ['wzrv:2011', 'wzrv:2013', 'wzrv:2014'], ['wzrv:2016'], ['wzrv:2010'], ['wzrv:2017'], ['wzrv:2012'], []], 'annotations': [{'source_line': 2063, 'axis': 'ax11', 'label': "'\\\\boldmath$A_l$'", 'selection': 'panel-selected datasets'}, {'source_line': 2064, 'axis': 'ax11', 'label': "'\\\\textrm{\\\\textbf{CMS}}'", 'selection': 'panel-selected datasets'}, {'source_line': 2065, 'axis': 'ax12', 'label': "'\\\\textrm{\\\\textbf{LHCb}}'", 'selection': 'panel-selected datasets'}, {'source_line': 2189, 'axis': 'ax21', 'label': "'\\\\textrm{\\\\textbf{data/theory}}'", 'selection': 'panel-selected datasets'}, {'source_line': 2190, 'axis': 'ax31', 'label': "'\\\\textrm{\\\\textbf{data/theory}}'", 'selection': 'panel-selected datasets'}, {'source_line': 2191, 'axis': 'ax41', 'label': "'\\\\textrm{\\\\textbf{data/theory}}'", 'selection': 'panel-selected datasets'}, {'source_line': 2192, 'axis': 'ax22', 'label': "'\\\\textrm{\\\\textbf{data-theory}}'", 'selection': 'panel-selected datasets'}, {'source_line': 2193, 'axis': 'ax32', 'label': "'\\\\textrm{\\\\textbf{data-theory}}'", 'selection': 'panel-selected datasets'}]}, 'thesis_lepton_plot_star': {'panels': [['wzrv:2020'], ['wzrv:2020']], 'annotations': [{'source_line': 2270, 'axis': 'ax11', 'label': "'\\\\boldmath$\\\\sigma_{pp}^{W^+}/\\\\sigma_{pp}^{W^-}$'", 'selection': 'panel-selected datasets'}, {'source_line': 2271, 'axis': 'ax11', 'label': "'$\\\\sqrt{s} = 510$' + ' ' + '\\\\textrm{GeV}'", 'selection': 'panel-selected datasets'}, {'source_line': 2272, 'axis': 'ax11', 'label': "'\\\\rm{$p_T^{\\\\ell}$ $>$ 25 GeV}'", 'selection': 'panel-selected datasets'}, {'source_line': 2273, 'axis': 'ax21', 'label': "'\\\\textbf{\\\\textrm{data/theory}}'", 'selection': 'panel-selected datasets'}]}, 'thesis_wz_plot_main': {'panels': [['zrap:1000', 'zrap:1001'], ['zrap:1000'], ['zrap:1001'], ['wasym:1000', 'wasym:1001'], ['wasym:1000'], ['wasym:1001']], 'annotations': [{'source_line': 2378, 'axis': 'ax', 'label': 'label', 'selection': 'panel-selected datasets'}, {'source_line': 2418, 'axis': 'ax11', 'label': "'\\\\boldmath$\\\\frac{1}{\\\\sigma} \\\\frac{d\\\\sigma(Z/\\\\gamma^*)}{dy}$'", 'selection': 'panel-selected datasets'}, {'source_line': 2419, 'axis': 'ax21', 'label': "'\\\\textbf{\\\\textrm{data/theory}}'", 'selection': 'panel-selected datasets'}, {'source_line': 2420, 'axis': 'ax11', 'label': "'$\\\\sqrt{s} = 1.96$' + ' ' + '\\\\textrm{TeV}'", 'selection': 'panel-selected datasets'}, {'source_line': 2474, 'axis': 'ax', 'label': 'label', 'selection': 'panel-selected datasets'}, {'source_line': 2476, 'axis': 'ax', 'label': 'label', 'selection': 'panel-selected datasets'}, {'source_line': 2516, 'axis': 'ax11', 'label': "'\\\\boldmath$A_W$'", 'selection': 'panel-selected datasets'}, {'source_line': 2517, 'axis': 'ax21', 'label': "'\\\\textbf{\\\\textrm{data/theory}}'", 'selection': 'panel-selected datasets'}, {'source_line': 2518, 'axis': 'ax11', 'label': "'$\\\\sqrt{s} = 1.96$' + ' ' + '\\\\textrm{TeV}'", 'selection': 'panel-selected datasets'}]}, 'thesis_pidis_plot_proton_A1_Apa': {'panels': [['pidis:10002', 'pidis:10003', 'pidis:10004', 'pidis:10035', 'pidis:10036'], ['pidis:10007', 'pidis:10022', 'pidis:10029', 'pidis:10032']], 'annotations': [{'source_line': 2821, 'axis': 'ax11', 'label': "'$Q^2 > 60~{\\\\rm GeV^2} \\\\, (i=6)$'", 'selection': "any(len(v['value'].get(6, [])) for v in PLOT.values())"}, {'source_line': 2822, 'axis': 'ax11', 'label': "'$40 < Q^2 < 60$'", 'selection': "any(len(v['value'].get(5, [])) for v in PLOT.values())"}, {'source_line': 2823, 'axis': 'ax11', 'label': "'$20 < Q^2 < 40$'", 'selection': "any(len(v['value'].get(4, [])) for v in PLOT.values())"}, {'source_line': 2824, 'axis': 'ax11', 'label': "'$10 < Q^2 < 20$'", 'selection': "any(len(v['value'].get(3, [])) for v in PLOT.values())"}, {'source_line': 2825, 'axis': 'ax11', 'label': "'$5 < Q^2 < 10$'", 'selection': "any(len(v['value'].get(2, [])) for v in PLOT.values())"}, {'source_line': 2826, 'axis': 'ax11', 'label': "'$2 < Q^2 < 5$'", 'selection': "any(len(v['value'].get(1, [])) for v in PLOT.values())"}, {'source_line': 2827, 'axis': 'ax11', 'label': "'$1 < Q^2 < 2\\\\, (i=0)$'", 'selection': "any(len(v['value'].get(0, [])) for v in PLOT.values())"}, {'source_line': 2838, 'axis': 'ax11', 'label': "'\\\\boldmath$A_1^p$'", 'selection': 'panel-selected datasets'}, {'source_line': 2839, 'axis': 'ax11', 'label': "'$(+\\\\, i)$'", 'selection': 'panel-selected datasets'}, {'source_line': 2900, 'axis': 'ax12', 'label': "'$Q^2 > 20~{\\\\rm GeV^2} \\\\, (i=9)$'", 'selection': "any(len(v['value'].get(8, [])) for v in PLOT.values())"}, {'source_line': 2901, 'axis': 'ax12', 'label': "'$15 < Q^2 < 20$'", 'selection': "any(len(v['value'].get(7, [])) for v in PLOT.values())"}, {'source_line': 2902, 'axis': 'ax12', 'label': "'$10 < Q^2 < 15$'", 'selection': "any(len(v['value'].get(6, [])) for v in PLOT.values())"}, {'source_line': 2903, 'axis': 'ax12', 'label': "'$7 < Q^2 < 10$'", 'selection': "any(len(v['value'].get(5, [])) for v in PLOT.values())"}, {'source_line': 2904, 'axis': 'ax12', 'label': "'$5 < Q^2 < 7$'", 'selection': "any(len(v['value'].get(4, [])) for v in PLOT.values())"}, {'source_line': 2905, 'axis': 'ax12', 'label': "'$4 < Q^2 < 5$'", 'selection': "any(len(v['value'].get(3, [])) for v in PLOT.values())"}, {'source_line': 2906, 'axis': 'ax12', 'label': "'$3 < Q^2 < 4$'", 'selection': "any(len(v['value'].get(2, [])) for v in PLOT.values())"}, {'source_line': 2907, 'axis': 'ax12', 'label': "'$2 < Q^2 < 3$'", 'selection': "any(len(v['value'].get(1, [])) for v in PLOT.values())"}, {'source_line': 2908, 'axis': 'ax12', 'label': "'$1 < Q^2 < 2\\\\, (i=0)$'", 'selection': "any(len(v['value'].get(0, [])) for v in PLOT.values())"}, {'source_line': 2921, 'axis': 'ax12', 'label': "'\\\\boldmath$A_{\\\\parallel}^p$'", 'selection': 'panel-selected datasets'}, {'source_line': 2922, 'axis': 'ax12', 'label': "'$(+\\\\, i)$'", 'selection': 'panel-selected datasets'}]}, 'thesis_pidis_plot_deuteron_A1_Apa': {'panels': [['pidis:10001', 'pidis:10033', 'pidis:10034'], ['pidis:10006', 'pidis:10021', 'pidis:10027']], 'annotations': [{'source_line': 2991, 'axis': 'ax11', 'label': "'$Q^2 > 40~{\\\\rm GeV^2} \\\\, (i=4)$'", 'selection': "any(len(v['value'].get(4, [])) for v in PLOT.values())"}, {'source_line': 2992, 'axis': 'ax11', 'label': "'$20 < Q^2 < 40$'", 'selection': "any(len(v['value'].get(3, [])) for v in PLOT.values())"}, {'source_line': 2993, 'axis': 'ax11', 'label': "'$10 < Q^2 < 20$'", 'selection': "any(len(v['value'].get(2, [])) for v in PLOT.values())"}, {'source_line': 2994, 'axis': 'ax11', 'label': "'$5 < Q^2 < 10$'", 'selection': "any(len(v['value'].get(1, [])) for v in PLOT.values())"}, {'source_line': 2995, 'axis': 'ax11', 'label': "'$m_c^2 < Q^2 < 5\\\\, (i=0)$'", 'selection': "any(len(v['value'].get(0, [])) for v in PLOT.values())"}, {'source_line': 3006, 'axis': 'ax11', 'label': "'\\\\boldmath$A_1^D$'", 'selection': 'panel-selected datasets'}, {'source_line': 3007, 'axis': 'ax11', 'label': "'$(+\\\\, i)$'", 'selection': 'panel-selected datasets'}, {'source_line': 3065, 'axis': 'ax12', 'label': "'$Q^2 > 20~{\\\\rm GeV^2} \\\\, (i=9)$'", 'selection': "any(len(v['value'].get(8, [])) for v in PLOT.values())"}, {'source_line': 3066, 'axis': 'ax12', 'label': "'$15 < Q^2 < 20$'", 'selection': "any(len(v['value'].get(7, [])) for v in PLOT.values())"}, {'source_line': 3067, 'axis': 'ax12', 'label': "'$10 < Q^2 < 15$'", 'selection': "any(len(v['value'].get(6, [])) for v in PLOT.values())"}, {'source_line': 3068, 'axis': 'ax12', 'label': "'$7 < Q^2 < 10$'", 'selection': "any(len(v['value'].get(5, [])) for v in PLOT.values())"}, {'source_line': 3069, 'axis': 'ax12', 'label': "'$5 < Q^2 < 7$'", 'selection': "any(len(v['value'].get(4, [])) for v in PLOT.values())"}, {'source_line': 3070, 'axis': 'ax12', 'label': "'$4 < Q^2 < 5$'", 'selection': "any(len(v['value'].get(3, [])) for v in PLOT.values())"}, {'source_line': 3071, 'axis': 'ax12', 'label': "'$3 < Q^2 < 4$'", 'selection': "any(len(v['value'].get(2, [])) for v in PLOT.values())"}, {'source_line': 3072, 'axis': 'ax12', 'label': "'$2 < Q^2 < 3$'", 'selection': "any(len(v['value'].get(1, [])) for v in PLOT.values())"}, {'source_line': 3073, 'axis': 'ax12', 'label': "'$m_c^2 < Q^2 < 2\\\\, (i=0)$'", 'selection': "any(len(v['value'].get(0, [])) for v in PLOT.values())"}, {'source_line': 3086, 'axis': 'ax12', 'label': "'\\\\boldmath$A_{\\\\parallel}^D$'", 'selection': 'panel-selected datasets'}, {'source_line': 3087, 'axis': 'ax12', 'label': "'$(+\\\\, i)$'", 'selection': 'panel-selected datasets'}]}, 'thesis_pidis_plot_helium_neutron': {'panels': [['pidis:10005', 'pidis:10018'], ['pidis:10025']], 'annotations': [{'source_line': 3161, 'axis': 'ax11', 'label': "'$m_c^2 < Q^2 < 5.5 ~ {\\\\rm GeV}^2$'", 'selection': "any(len(v['value'].get(0, [])) for v in PLOT.values())"}, {'source_line': 3172, 'axis': 'ax11', 'label': "'\\\\boldmath$A_1$'", 'selection': 'panel-selected datasets'}, {'source_line': 3227, 'axis': 'ax12', 'label': "'$Q^2 > 10~{\\\\rm GeV^2} \\\\, (i=3)$'", 'selection': "any(len(v['value'].get(3, [])) for v in PLOT.values())"}, {'source_line': 3228, 'axis': 'ax12', 'label': "'$5 < Q^2 < 10$'", 'selection': "any(len(v['value'].get(2, [])) for v in PLOT.values())"}, {'source_line': 3229, 'axis': 'ax12', 'label': "'$3 < Q^2 < 5$'", 'selection': "any(len(v['value'].get(1, [])) for v in PLOT.values())"}, {'source_line': 3230, 'axis': 'ax12', 'label': "'$m_c^2 < Q^2 < 3\\\\, (i=0)$'", 'selection': "any(len(v['value'].get(0, [])) for v in PLOT.values())"}, {'source_line': 3243, 'axis': 'ax12', 'label': "'\\\\boldmath$A_{\\\\parallel}^{^3{\\\\rm He}}$'", 'selection': 'panel-selected datasets'}, {'source_line': 3244, 'axis': 'ax12', 'label': "'$(+\\\\, i)$'", 'selection': 'panel-selected datasets'}]}, 'thesis_pjets_plot_pjets': {'panels': [['pjet:20001'], ['pjet:20002'], ['pjet:20003'], ['pjet:20004'], ['pjet:20004'], ['pjet:20006'], ['pjet:20008'], ['pjet:20007'], ['pjet:20007']], 'annotations': [{'source_line': 3323, 'axis': 'ax21', 'label': "'$|\\\\eta_{\\\\rm jet}| \\\\in [%2.1f,%2.1f]$' % (etamin, etamax)", 'selection': 'panel-selected datasets'}, {'source_line': 3326, 'axis': 'ax22', 'label': "'$|\\\\eta_{\\\\rm jet}| \\\\in [%2.1f,%2.1f]$' % (etamin, etamax)", 'selection': 'panel-selected datasets'}, {'source_line': 3330, 'axis': 'ax32', 'label': "'$|\\\\eta_{\\\\rm jet}| \\\\in [%2.1f,%2.1f]$' % (etamin, etamax)", 'selection': 'panel-selected datasets'}, {'source_line': 3333, 'axis': 'ax33', 'label': "'$|\\\\eta_{\\\\rm jet}| \\\\in [%2.1f,%2.1f]$' % (etamin, etamax)", 'selection': 'panel-selected datasets'}, {'source_line': 3338, 'axis': 'ax', 'label': "'$|\\\\eta_{\\\\rm jet}| \\\\in [%2.1f,%2.1f]$' % (etamin, etamax)", 'selection': 'panel-selected datasets'}, {'source_line': 3342, 'axis': 'ax', 'label': "'$\\\\eta_{\\\\rm jet}   \\\\in [%2.1f,%2.1f]$' % (etamin, etamax)", 'selection': 'panel-selected datasets'}, {'source_line': 3413, 'axis': 'ax13', 'label': "'\\\\boldmath$A_{LL}^{\\\\rm jet}$'", 'selection': 'panel-selected datasets'}, {'source_line': 3414, 'axis': 'ax11', 'label': "'\\\\textrm{\\\\textbf{2003}}'", 'selection': 'panel-selected datasets'}, {'source_line': 3415, 'axis': 'ax12', 'label': "'\\\\textrm{\\\\textbf{2005}}'", 'selection': 'panel-selected datasets'}, {'source_line': 3416, 'axis': 'ax13', 'label': "'\\\\textrm{\\\\textbf{2006}}'", 'selection': 'panel-selected datasets'}, {'source_line': 3417, 'axis': 'ax21', 'label': "'\\\\textrm{\\\\textbf{2009}}'", 'selection': 'panel-selected datasets'}, {'source_line': 3418, 'axis': 'ax22', 'label': "'\\\\textrm{\\\\textbf{2009}}'", 'selection': 'panel-selected datasets'}, {'source_line': 3419, 'axis': 'ax23', 'label': "'\\\\textrm{\\\\textbf{2012}}'", 'selection': 'panel-selected datasets'}, {'source_line': 3420, 'axis': 'ax31', 'label': "'\\\\textrm{\\\\textbf{2013}}'", 'selection': 'panel-selected datasets'}, {'source_line': 3421, 'axis': 'ax32', 'label': "'\\\\textrm{\\\\textbf{2015}}'", 'selection': 'panel-selected datasets'}, {'source_line': 3422, 'axis': 'ax33', 'label': "'\\\\textrm{\\\\textbf{2015}}'", 'selection': 'panel-selected datasets'}]}, 'thesis_polWlep_plot_main': {'panels': [['wzrv:1000'], ['wzrv:1020', 'wzrv:1021']], 'annotations': [{'source_line': 3539, 'axis': 'ax11', 'label': "'\\\\boldmath$A_L^{W}$'", 'selection': 'panel-selected datasets'}, {'source_line': 3540, 'axis': 'ax11', 'label': "'\\\\textrm{\\\\textbf{(STAR)}}'", 'selection': 'panel-selected datasets'}, {'source_line': 3541, 'axis': 'ax11', 'label': "'$\\\\sqrt{s}=510~$' + '\\\\textrm{GeV}'", 'selection': 'panel-selected datasets'}, {'source_line': 3542, 'axis': 'ax11', 'label': "'$p_T^\\\\ell > 25~$' + '\\\\hspace{0.00cm}\\\\textrm{GeV}'", 'selection': 'panel-selected datasets'}, {'source_line': 3543, 'axis': 'ax11', 'label': "'$W^-$'", 'selection': 'panel-selected datasets'}, {'source_line': 3544, 'axis': 'ax11', 'label': "'$W^+$'", 'selection': 'panel-selected datasets'}, {'source_line': 3545, 'axis': 'ax12', 'label': "'\\\\boldmath$A_L^{W/Z}$'", 'selection': 'panel-selected datasets'}, {'source_line': 3546, 'axis': 'ax12', 'label': "'\\\\textrm{\\\\textbf{(PHENIX)}}'", 'selection': 'panel-selected datasets'}, {'source_line': 3563, 'axis': 'ax12', 'label': "'\\\\textrm{\\\\textbf{JAM}}'", 'selection': 'panel-selected datasets'}]}, 'thesis_sidis_plot_pion': {'panels': [['sidis:1005', 'sidis:1006'], ['sidis:1005', 'sidis:1006'], ['sidis:1005', 'sidis:1006'], ['sidis:1005', 'sidis:1006'], ['sidis:1005', 'sidis:1006'], ['sidis:1005', 'sidis:1006'], ['sidis:1005', 'sidis:1006'], ['sidis:1005', 'sidis:1006']], 'annotations': [{'source_line': 95, 'axis': 'ax', 'label': 'msg', 'selection': 'panel-selected datasets'}]}, 'thesis_sidis_plot_kaon': {'panels': [['sidis:2005', 'sidis:2006'], ['sidis:2005', 'sidis:2006'], ['sidis:2005', 'sidis:2006'], ['sidis:2005', 'sidis:2006'], ['sidis:2005', 'sidis:2006'], ['sidis:2005', 'sidis:2006'], ['sidis:2005', 'sidis:2006'], ['sidis:2005', 'sidis:2006']], 'annotations': [{'source_line': 95, 'axis': 'ax', 'label': 'msg', 'selection': 'panel-selected datasets'}]}, 'thesis_sidis_plot_hadron': {'panels': [['sidis:3000', 'sidis:3001'], ['sidis:3000', 'sidis:3001'], ['sidis:3000', 'sidis:3001'], ['sidis:3000', 'sidis:3001'], ['sidis:3000', 'sidis:3001'], ['sidis:3000', 'sidis:3001'], ['sidis:3000', 'sidis:3001'], ['sidis:3000', 'sidis:3001']], 'annotations': [{'source_line': 95, 'axis': 'ax', 'label': 'msg', 'selection': 'panel-selected datasets'}]}, 'thesis_psidis_plot_pion': {'panels': [['psidis:20004', 'psidis:20017'], ['psidis:20005', 'psidis:20018'], ['psidis:20008', 'psidis:20021'], ['psidis:20009', 'psidis:20022']], 'annotations': [{'source_line': 3988, 'axis': 'ax11', 'label': "'\\\\boldmath$A^{p,\\\\pi^+}_{1}$'", 'selection': 'panel-selected datasets'}, {'source_line': 3989, 'axis': 'ax12', 'label': "'\\\\boldmath$A^{p,\\\\pi^-}_{1}$'", 'selection': 'panel-selected datasets'}, {'source_line': 3990, 'axis': 'ax21', 'label': "'\\\\boldmath$A^{D,\\\\pi^+}_{1}$'", 'selection': 'panel-selected datasets'}, {'source_line': 3991, 'axis': 'ax22', 'label': "'\\\\boldmath$A^{D,\\\\pi^-}_{1}$'", 'selection': 'panel-selected datasets'}]}, 'thesis_psidis_plot_kaon': {'panels': [['psidis:20019'], ['psidis:20020'], ['psidis:20012', 'psidis:20025'], ['psidis:20013', 'psidis:20026'], ['psidis:20014']], 'annotations': [{'source_line': 4094, 'axis': 'ax11', 'label': "'\\\\boldmath$A^{p,K^+}_{1}$'", 'selection': 'panel-selected datasets'}, {'source_line': 4095, 'axis': 'ax12', 'label': "'\\\\boldmath$A^{p,K^-}_{1}$'", 'selection': 'panel-selected datasets'}, {'source_line': 4096, 'axis': 'ax21', 'label': "'\\\\boldmath$A^{D,K^+}_{1}$'", 'selection': 'panel-selected datasets'}, {'source_line': 4097, 'axis': 'ax22', 'label': "'\\\\boldmath$A^{D,K^-}_{1}$'", 'selection': 'panel-selected datasets'}, {'source_line': 4098, 'axis': 'ax23', 'label': "'\\\\boldmath$A^{D,K^{\\\\pm}}_{1}$'", 'selection': 'panel-selected datasets'}]}, 'thesis_psidis_plot_hadron': {'panels': [['psidis:20000', 'psidis:20006'], ['psidis:20001', 'psidis:20007'], ['psidis:20002', 'psidis:20010', 'psidis:20023'], ['psidis:20003', 'psidis:20011', 'psidis:20024'], ['psidis:20015'], ['psidis:20016']], 'annotations': [{'source_line': 4219, 'axis': 'ax11', 'label': "'\\\\boldmath$A^{p,h^+}_{1}$'", 'selection': 'panel-selected datasets'}, {'source_line': 4220, 'axis': 'ax12', 'label': "'\\\\boldmath$A^{p,h^-}_{1}$'", 'selection': 'panel-selected datasets'}, {'source_line': 4221, 'axis': 'ax21', 'label': "'\\\\boldmath$A^{D,h^+}_{1}$'", 'selection': 'panel-selected datasets'}, {'source_line': 4222, 'axis': 'ax22', 'label': "'\\\\boldmath$A^{D,h^-}_{1}$'", 'selection': 'panel-selected datasets'}, {'source_line': 4223, 'axis': 'ax31', 'label': "'\\\\boldmath$A^{^3{\\\\rm He},h^+}_{1}$'", 'selection': 'panel-selected datasets'}, {'source_line': 4224, 'axis': 'ax32', 'label': "'\\\\boldmath$A^{^3{\\\\rm He},h^-}_{1}$'", 'selection': 'panel-selected datasets'}]}, 'thesis_sia_plot_pion': {'panels': [['sia:1030'], ['sia:1028'], ['sia:1029'], ['sia:1001', 'sia:1002', 'sia:1003', 'sia:1005', 'sia:1006'], ['sia:1010'], ['sia:1011'], ['sia:1013'], ['sia:1018'], ['sia:1025'], ['sia:1026'], ['sia:1027'], ['sia:1019'], ['sia:1014'], ['sia:1015'], ['sia:1016'], ['sia:1017']], 'groups': {'ARGUS': [1030], 'BABAR': [1028], 'BELLE': [1029], 'TASSO': [1001, 1002, 1003, 1005, 1006], 'TPC(c)': [1010], 'TPC(b)': [1011], 'TOPAZ': [1013], 'ALEPH': [1018], 'DELPHI': [1025], 'DELPHI(uds)': [1026], 'DELPHI(b)': [1027], 'OPAL': [1019], 'SLD': [1014], 'SLD(uds)': [1015], 'SLD(c)': [1016], 'SLD(b)': [1017]}, 'annotations': 'Selected experiment/energy; fixed source grid slot'}, 'thesis_sia_plot_kaon': {'panels': [['sia:2030'], ['sia:2028'], ['sia:2029'], ['sia:2001', 'sia:2002', 'sia:2003', 'sia:2005'], ['sia:2008'], ['sia:2013'], ['sia:2018'], ['sia:2025'], ['sia:2026'], ['sia:2027'], ['sia:2019'], ['sia:2014'], ['sia:2015'], ['sia:2016'], ['sia:2017']], 'groups': {'ARGUS': [2030], 'BABAR': [2028], 'BELLE': [2029], 'TASSO': [2001, 2002, 2003, 2005], 'TPC': [2008], 'TOPAZ': [2013], 'ALEPH': [2018], 'DELPHI': [2025], 'DELPHI(uds)': [2026], 'DELPHI(b)': [2027], 'OPAL': [2019], 'SLD': [2014], 'SLD(uds)': [2015], 'SLD(c)': [2016], 'SLD(b)': [2017]}, 'annotations': 'Selected experiment/energy; fixed source grid slot'}, 'thesis_sia_plot_hadron': {'panels': [['sia:4003', 'sia:4008', 'sia:4009', 'sia:4010', 'sia:4012'], ['sia:4004'], ['sia:4000'], ['sia:4001'], ['sia:4017'], ['sia:4013'], ['sia:4007'], ['sia:4002'], ['sia:4016'], ['sia:4014'], ['sia:4015']], 'groups': {'TASSO': [4003, 4008, 4009, 4010, 4012], 'TPC': [4004], 'ALEPH': [4000], 'DELPHI': [4001], 'DELPHI(uds)': [4017], 'DELPHI(b)': [4013], 'OPAL': [4007], 'SLD': [4002], 'SLD(uds)': [4016], 'SLD(c)': [4014], 'SLD(b)': [4015]}, 'annotations': 'Selected experiment/energy; fixed source grid slot'}, 'thesis_wc_plot': {'panels': [['wc:20001'], ['wc:10001'], ['wc:20002'], ['wc:10002'], ['wc:20003']], 'annotations': 'Selected dataset only; supplied data_info by fixed grid slot'}}
_SIA_GROUPS = {'pion': {'ARGUS': [1030], 'BABAR': [1028], 'BELLE': [1029], 'TASSO': [1001, 1002, 1003, 1005, 1006], 'TPC(c)': [1010], 'TPC(b)': [1011], 'TOPAZ': [1013], 'ALEPH': [1018], 'DELPHI': [1025], 'DELPHI(uds)': [1026], 'DELPHI(b)': [1027], 'OPAL': [1019], 'SLD': [1014], 'SLD(uds)': [1015], 'SLD(c)': [1016], 'SLD(b)': [1017]}, 'kaon': {'ARGUS': [2030], 'BABAR': [2028], 'BELLE': [2029], 'TASSO': [2001, 2002, 2003, 2005], 'TPC': [2008], 'TOPAZ': [2013], 'ALEPH': [2018], 'DELPHI': [2025], 'DELPHI(uds)': [2026], 'DELPHI(b)': [2027], 'OPAL': [2019], 'SLD': [2014], 'SLD(uds)': [2015], 'SLD(c)': [2016], 'SLD(b)': [2017]}, 'hadron': {'TASSO': [4003, 4008, 4009, 4010, 4012], 'TPC': [4004], 'ALEPH': [4000], 'DELPHI': [4001], 'DELPHI(uds)': [4017], 'DELPHI(b)': [4013], 'OPAL': [4007], 'SLD': [4002], 'SLD(uds)': [4016], 'SLD(c)': [4014], 'SLD(b)': [4015]}}
_SIA_HAD_LAB = {'pion': '\\pi^{\\pm}', 'kaon': 'K^{\\pm}', 'hadron': 'h^{\\pm}'}
_SIA_HAD_COLORS = ['red', 'dodgerblue', 'xkcd:grassy green']
_SIA_TASSO_COLORS = {'pion': ['xkcd:vibrant purple', 'k', 'xkcd:grassy green', 'xkcd:bright orange', 'dodgerblue'], 'kaon': ['red', 'k', 'xkcd:grassy green', 'xkcd:vibrant purple'], 'hadron': ['dodgerblue', 'red', 'k', 'xkcd:vibrant purple', 'xkcd:bright orange']}
_SIA_TASSO_NAMES = {'pion': ['12', '14', '22', '32', '44'], 'kaon': ['12', '14', '22', '32'], 'hadron': ['12', '14', '22', '30', '44']}
_WC_DATA_INFO = {10001: {'name': 'ATLAS~7~TeV', 'ylabel': '\\boldmath $\\frac{d\\sigma(W^{-}+c)}{d|\\eta_{\\ell}|}$', 'yticks': [0, 10000.0, 20000.0, 30000.0], 'yticklabels': ['$0$', '$10$', '$20$', '$30$'], 'ymm': [2000.0, 10000.0], 'xlim': [0.0, 2.49], 'ylim': [4000.0, 24900.0], 'txt-loc-pT': [0.08, 0.22], 'pT': 20, 'txt-loc-rs': [0.09, 0.1], 'rs': 7}, 10002: {'name': 'ATLAS~7~TeV', 'ylabel': '\\boldmath $\\frac{d\\sigma(W^{+}+\\bar{c})}{d|\\eta_{\\ell}|}$', 'yticks': [0, 10000.0, 20000.0, 30000.0], 'yticklabels': ['$0$', '$10$', '$20$', '$30$'], 'ymm': [2500.0, 10000.0], 'xlim': [0.0, 2.49], 'ylim': [4000.0, 24900.0], 'txt-loc-pT': [0.08, 0.22], 'pT': 20, 'txt-loc-rs': [0.09, 0.1], 'rs': 7}, 20001: {'name': 'CMS~7~TeV', 'ylabel': '\\boldmath $\\frac{d\\sigma(W + c)}{d|\\eta_{\\ell}|}$', 'yticks': [40000.0, 50000.0, 60000.0, 70000.0], 'yticklabels': ['$40$', '$50$', '$60$', '$70$'], 'ymm': [5000.0, 10000.0], 'xlim': [0.0, 1.99], 'ylim': [30100.0, 80000.0], 'txt-loc-pT': [0.08, 0.22], 'pT': 25, 'txt-loc-rs': [0.09, 0.1], 'rs': 7}, 20002: {'name': 'CMS~7~TeV', 'ylabel': '\\boldmath $\\frac{\\sigma(W^{+} + \\bar{c})}{\\sigma(W^{-} + c)}$', 'yticks': [0.6, 0.8, 1.0, 1.2, 1.4], 'yticklabels': ['$0.6$', '$0.8$', '$1.0$', '$1.2$', '$1.4$'], 'ymm': [0.1, 0.2], 'xlim': [0.0, 1.99], 'ylim': [0.5, 1.5], 'txt-loc-pT': [0.08, 0.22], 'pT': 25, 'txt-loc-rs': [0.09, 0.1], 'rs': 7}, 20003: {'name': 'CMS~13~TeV', 'ylabel': '\\boldmath $\\frac{d\\sigma(W + c)}{d|\\eta_{\\ell}|}$', 'yticks': [100000.0, 200000.0, 300000.0, 400000.0, 500000.0, 600000.0], 'yticklabels': ['$100$', '$200$', '$300$', '$400$', '$500$', '$600$'], 'ymm': [50000.0, 100000.0], 'xlim': [0.0, 2.25], 'ylim': [201000.0, 699000.0], 'txt-loc-pT': [0.08, 0.22], 'pT': 26, 'txt-loc-rs': [0.09, 0.1], 'rs': 13}}
def thesis_legend(axis, handles, labels, **kwargs):
    def present(handle):
        return all(present(part) for part in handle) if type(handle) is tuple else handle is not None
    entries = [(h, label) for h, label in zip(handles, labels) if present(h)]
    if not entries:
        return None
    return axis.legend([e[0] for e in entries], [e[1] for e in entries], **kwargs)


def thesis_finish_panels(fig, function, selected):
    panel_map = _THESIS_PANEL_MAP[function]['panels']
    for index, axis in enumerate(fig.axes):
        required = panel_map[index] if index < len(panel_map) else []
        if not set(required) & selected:
            axis.set_visible(False)
    fig._thesis_selected = sorted(selected)

def thesis_save_labels(fig, path, **kwargs):
    # Extend the export canvas, not source panel positions, for default-positioned labels.
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    boxes = [fig.bbox_inches]
    boxes.extend(axis.xaxis.label.get_window_extent(renderer).transformed(fig.dpi_scale_trans.inverted())
                 for axis in fig.axes if axis.get_visible() and axis.get_xlabel())
    boxes.extend(label.get_window_extent(renderer).transformed(fig.dpi_scale_trans.inverted())
                 for label in fig.texts if label.get_visible())
    bounds = matplotlib.transforms.Bbox.union(boxes).padded(.01)
    fig._thesis_export_bounds = bounds
    fig.savefig(path, bbox_inches=bounds, **kwargs)


def thesis_sia_ratios(tab, dataset):
    mean = np.asarray(tab['thy-0'])
    invalid = np.flatnonzero(~np.isfinite(mean) | (mean <= 0))
    if len(invalid):
        raise ValueError('sia:%d has nonpositive/nonfinite mean cross section at original rows %s; '
                         'the source data/theory ratio is incompatible and no positive replacement was made.' %
                         (dataset, invalid.tolist()))
    low = thesis_ratio(mean - tab['dthy_lower-0'], mean)
    high = thesis_ratio(mean + tab['dthy_upper-0'], mean)
    return (thesis_ratio(tab['value'], mean), thesis_ratio(tab['alpha'], mean, error=True),
            np.minimum(low, high), np.maximum(low, high))

# Adapted from the user-supplied corrected SIA source; SHA256 50d27ce3566bb931bdd9ef53686945dab3f69c27ad84e84a0d9c656a9c123063.
from mpl_toolkits.axes_grid1 import make_axes_locatable

def thesis_sia_panel(had_idx, num, had, name, z, val, val_err, thy, thy_dn, thy_up, idx_list, identities):
    ax = py.subplot(5, 4, num + 1)
    divider = make_axes_locatable(ax)
    axL = divider.append_axes('right', size='120%', pad=0, sharey=ax)
    axL.set_xlim(0.2, 0.95)
    axL.spines['left'].set_visible(False)
    axL.yaxis.set_ticks_position('right')
    py.setp(axL.get_xticklabels(), visible=True)
    ax.spines['right'].set_visible(False)
    one = np.ones(len(z))
    if name == 'TASSO':
        for (i, idx) in enumerate(_SIA_GROUPS[had]['TASSO']):
            if idx not in idx_list:
                continue
            mask = identities == idx
            z_i = z[mask]
            val_i = val[mask]
            err_i = val_err[mask]
            for ax_ in [ax, axL]:
                ax_.errorbar(z_i, val_i, err_i, fmt='.', color=_SIA_TASSO_COLORS[had][i], capsize=None)
                ax_.scatter(z_i, val_i, color=_SIA_TASSO_COLORS[had][i], s=10)
    else:
        for ax_ in [ax, axL]:
            ax_.errorbar(z, val, val_err, fmt='k.', capsize=None)
            ax_.scatter(z, val, color='k', s=20)
    for ax_ in [ax, axL]:
        ax_.fill_between(z, thy_dn, thy_up, color=_SIA_HAD_COLORS[had_idx], alpha=0.8, where=thesis_band_mask(z))
        ax_.axhline(y=1, linestyle='--', color='k')
        ax_.set_ylim(bottom=0.5, top=1.5)
    ax.set_xscale('log')
    ax.set_xlim(0.015, 0.2)
    axL.set_xlim(0.2, 0.95)
    (x, y) = (0.13, 0.85)
    if had == 'kaon':
        if name == 'DELPHI':
            (x, y) = (0.27, 0.85)
        elif name == 'DELPHI(uds)':
            (x, y) = (0.27, 0.85)
        elif name == 'DELPHI(b)':
            (x, y) = (0.4, 0.85)
        elif 'ALEPH' in name:
            (x, y) = (0.19, 0.85)
    ax.text(x, y, '\\boldmath$\\mathrm{%s}$' % name, transform=ax.transAxes, size=30)
    if any((experiment in name for experiment in ['ALEPH', 'DELPHI', 'OPAL', 'SLD'])):
        ax.text(0.13, 0.08, '\\boldmath$\\sqrt{s} = M_Z$', transform=ax.transAxes, size=30)
    if any((experiment in name for experiment in ['ARGUS', 'BELLE', 'BABAR'])):
        ax.text(0.13, 0.08, '\\boldmath$\\sqrt{s} \\approx 10~\\mathrm{GeV}$', transform=ax.transAxes, size=30)
    if 'TPC' in name:
        ax.text(0.13, 0.08, '\\boldmath$\\sqrt{s} = 29~\\mathrm{GeV}$', transform=ax.transAxes, size=30)
    if 'TOPAZ' in name:
        ax.text(0.13, 0.08, '\\boldmath$\\sqrt{s} = 58~\\mathrm{GeV}$', transform=ax.transAxes, size=30)
    if num == 1:
        axL.text(0.55, 0.76, '\\boldmath$%s$' % _SIA_HAD_LAB[had], transform=axL.transAxes, color=_SIA_HAD_COLORS[had_idx], size=60)
    if num >= len(_SIA_GROUPS[had].keys()) - 4:
        axL.set_xlabel('\\boldmath$z_h$', size=45, x=1.0 / 12.0)
        (labelbottom, labeltop, labelleft, labelright, bottom, top, left, right) = (True, False, False, False, True, True, True, False)
        (labelbottomL, labeltopL, labelleftL, labelrightL, bottomL, topL, leftL, rightL) = (True, False, False, False, True, True, False, True)
        if num % 4 == 0:
            labelleft = True
    elif num % 4 == 0:
        (labelbottom, labeltop, labelleft, labelright, bottom, top, left, right) = (False, False, True, False, True, True, True, False)
        (labelbottomL, labeltopL, labelleftL, labelrightL, bottomL, topL, leftL, rightL) = (False, False, False, False, True, True, False, True)
    else:
        (labelbottom, labeltop, labelleft, labelright, bottom, top, left, right) = (False, False, False, False, True, True, True, False)
        (labelbottomL, labeltopL, labelleftL, labelrightL, bottomL, topL, leftL, rightL) = (False, False, False, False, True, True, False, True)
    ax.tick_params(axis='both', which='both', labelleft=labelleft, labelbottom=labelbottom, labelright=labelright, bottom=bottom, top=top, left=left, right=right, direction='in', labelsize=30, length=8, pad=10)
    axL.tick_params(axis='both', which='both', labelleft=labelleftL, labelbottom=labelbottomL, labelright=labelrightL, bottom=bottomL, top=topL, left=leftL, right=rightL, direction='in', labelsize=30, length=8, pad=10)
    for ax_ in [ax, axL]:
        ax_.set_yticks([0.6, 0.8, 1.0, 1.2, 1.4])
        ax_.set_yticklabels(['$0.6$', '$0.8$', '$1.0$', '$1.2$', '$1.4$'])
        ax_.set_facecolor('none')
    ax.set_xticks([0.03, 0.1])
    ax.set_xticklabels(['$0.03$', '$0.1$'], ha='center')
    axL.set_xticks([0.2, 0.4, 0.6, 0.8])
    axL.set_xticklabels(['$0.2$', '$0.4$', '$0.6$', '$0.8$'])
    ax.xaxis.set_minor_locator(matplotlib.ticker.FixedLocator([0.02, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09]))
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    axL.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    axL.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.set_xlim(0.015, 0.2)
    axL.set_xlim(0.2, 0.95)
    ax.set_rasterized(True)
    ax.spines['top'].set_linewidth(2)
    ax.spines['bottom'].set_linewidth(2)
    ax.spines['left'].set_linewidth(2)
    axL.set_rasterized(True)
    axL.spines['top'].set_linewidth(2)
    axL.spines['right'].set_linewidth(2)
    axL.spines['bottom'].set_linewidth(2)
    for spine in ax.spines.values():
        spine.set_zorder(10)
    for spine in axL.spines.values():
        spine.set_zorder(10)
    if name == 'TASSO':
        handles = []
        labels = []
        for (i, idx) in enumerate(_SIA_GROUPS[had]['TASSO']):
            if idx not in idx_list:
                continue
            handle = ax.errorbar([], [], fmt='o', color=_SIA_TASSO_COLORS[had][i], capsize=None, markersize=6, linestyle='None')
            handles.append(handle)
            labels.append('\\boldmath$%s~\\mathrm{GeV}$' % _SIA_TASSO_NAMES[had][i])
        if had == 'pion':
            y = 0.53
        if had == 'kaon':
            y = 0.49
        if had == 'hadron':
            y = 0.53
        ax.legend(handles, labels, frameon=False, fontsize=20, loc='upper left', handletextpad=-0.2, labelspacing=0.4, bbox_to_anchor=(-0.04, y))
    num += 1
    return num


def thesis_sia_plot_species(datasets, output_file, interval, had):
    data = datasets['sia']
    had_idx = ['pion', 'kaon', 'hadron'].index(had)
    fig = py.figure(figsize=(4 * 7, 5 * 4))
    for num, (name, group) in enumerate(_SIA_GROUPS[had].items()):
        selected = [idx for idx in group if idx in data]
        if not selected:
            continue
        values, errors, lower, upper, coordinates, identities = [], [], [], [], [], []
        for idx in selected:
            tab = data[idx]
            val, err, low, high = thesis_sia_ratios(tab, idx)
            values.extend(val)
            errors.extend(err)
            lower.extend(low)
            upper.extend(high)
            coordinates.extend(tab['z'])
            identities.extend([idx] * len(val))
        z = np.asarray(coordinates)
        order = np.argsort(z, kind='stable')
        arrays = [np.asarray(a)[order] for a in (values, errors, lower, upper, identities)]
        val, err, low, high, identities = arrays
        thesis_sia_panel(had_idx, num, had, name, z[order], val, err, np.ones(len(z)),
                         low, high, selected, identities)
    y = .698 if had == 'hadron' else .60
    fig.text(-.017, y, r'\boldmath$\mathrm{data/theory}$', rotation=90, va='center', ha='center', fontsize=55)
    py.tight_layout()
    py.subplots_adjust(hspace=0, wspace=.04)
    if any(not any(idx in data for idx in group) for group in _SIA_GROUPS[had].values()):
        thesis_save_labels(fig, output_file, dpi=100)
    else:
        fig.savefig(output_file, bbox_inches='tight', dpi=100)


def thesis_sia_plot_pion(datasets, output_file, interval):
    thesis_sia_plot_species(datasets, output_file, interval, 'pion')


def thesis_sia_plot_kaon(datasets, output_file, interval):
    thesis_sia_plot_species(datasets, output_file, interval, 'kaon')


def thesis_sia_plot_hadron(datasets, output_file, interval):
    thesis_sia_plot_species(datasets, output_file, interval, 'hadron')


# Adapted from the user-supplied corrected W+charm source; SHA256 c16a55145de507ccf5f300de3114cb6fce2a986f149622e2574a2d42146f5ef8.
def thesis_wc_draw(ax, idx, tab):
    i_d = 1
    err_type = 1
    (hand, thy_plot, thy_band) = ({}, {}, {})
    names = {1: 'JAM'}
    colors_err = colors_line = {1: 'red'}
    data_info = _WC_DATA_INFO
    (x, val, val_err) = (tab['y'], tab['value'], tab['alpha'])
    thy = tab['thy-0']
    thy_CI = {'dn': thy - tab['dthy_lower-0'], 'up': thy + tab['dthy_upper-0']}
    info = data_info[idx]
    hand[idx] = ax.errorbar(x, val, val_err, color='blue', fmt='.', capsize=None, ms=15, linewidth=2)
    if i_d == 0:
        ls = '-'
        alpha_err = 0.4
        alpha_line = 0.5
    else:
        ls = '-'
        alpha_line = 1
        alpha_err = 0.6
    (thy_plot[i_d],) = ax.plot(x, thy, color=colors_line[i_d], lw=1.5, ls=ls, label='{\\rm \\textbf{%s}}' % names[i_d], zorder=i_d - 2, alpha=alpha_line)
    if err_type == 1:
        thy_band[i_d] = ax.fill_between(x, thy_CI['dn'], thy_CI['up'], color=colors_err[i_d], alpha=alpha_err, lw=1, label='{\\rm \\textbf{%s}}' % names[i_d], zorder=i_d - 2, where=thesis_band_mask(x))
    ax.text(info['txt-loc-pT'][0], info['txt-loc-pT'][1], '$p^{l}_{T} > %g~\\textrm{GeV}$' % info['pT'], transform=ax.transAxes, size=25)
    ax.text(info['txt-loc-rs'][0], info['txt-loc-rs'][1], '$\\sqrt{s} = %g~\\textrm{TeV}$' % info['rs'], transform=ax.transAxes, size=25)
    ax.yaxis.set_minor_locator(MultipleLocator(info['ymm'][0]))
    ax.yaxis.set_major_locator(MultipleLocator(info['ymm'][1]))
    ax.text(0.94, 0.75, info['ylabel'], ha='right', transform=ax.transAxes, size=50)
    ax.set_yticks(info['yticks'])
    ax.set_yticklabels(info['yticklabels'])
    ax.set_ylim(bottom=info['ylim'][0], top=info['ylim'][1])
    if idx not in [20002]:
        ax.set_ylabel('\\textrm{[pb]}', fontsize=30, rotation='vertical')
        ax.yaxis.set_label_coords(-0.025, 0.93)
    ax.xaxis.set_minor_locator(MultipleLocator(0.1))
    ax.xaxis.set_major_locator(MultipleLocator(0.5))
    ax.set_xlabel('\\boldmath $|\\eta_{\\ell}|$', fontsize=40)
    ax.set_xlim(left=0.001, right=2.49)
    ax.tick_params(axis='both', which='major', top=True, right=True, direction='in', labelsize=30, length=12, pad=10)
    ax.tick_params(axis='both', which='minor', top=True, right=True, direction='in', labelsize=30, length=6, pad=10)
    return (hand[idx], thy_plot[1], thy_band.get(1))


def thesis_wc_plot(datasets, output_file, interval):
    data = datasets['wc']
    fig = py.figure(figsize=(2 * 8, 3 * 5))
    for slot, idx in enumerate([20001, 10001, 20002, 10002, 20003]):
        if idx not in data:
            continue
        ax = py.subplot(3, 2, slot + 1)
        _, line, band = thesis_wc_draw(ax, idx, data[idx])
        if idx == 20002:
            handles = [(band, line)] if _THESIS_BANDS else [line]
            ax.legend(handles, [r'\textbf{\textrm{JAM}}'], loc='upper left', fontsize=30,
                      frameon=0, handletextpad=.3, handlelength=1.)
        if idx == 20001:
            py.title(label=r'$\textbf{\textrm{CMS}}$', loc='center', fontsize=40, pad=20)
        if idx == 10001:
            py.title(label=r'$\textbf{\textrm{ATLAS}}$', loc='center', fontsize=40, pad=20)
        for side in ['top', 'right', 'bottom', 'left']:
            ax.spines[side].set_linewidth(2)
        ax.set_rasterized(True)
    py.tight_layout()
    py.subplots_adjust(hspace=0, wspace=.125)
    fig.savefig(output_file)


import base64
import copy
import os
from types import SimpleNamespace
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as py
from matplotlib import pyplot, gridspec
from matplotlib.lines import Line2D
from matplotlib.collections import PolyCollection
from matplotlib.container import ErrorbarContainer
from matplotlib.ticker import MultipleLocator, FormatStrFormatter, ScalarFormatter
import matplotlib.colors as mcolors
import pandas as pd
import scipy as sp
from scipy import interpolate
from scipy.interpolate import griddata
from scipy.integrate import fixed_quad

_THESIS_BANDS = True


def thesis_band_mask(x):
    return np.full(np.asarray(x).shape, _THESIS_BANDS, dtype=bool)


def thesis_ratio(value, mean, error=False):
    mean = np.asarray(mean)
    if np.any(~np.isfinite(mean)) or np.any(mean == 0):
        raise ValueError('Original data/theory display has a zero or nonfinite mean denominator.')
    return np.asarray(value) / (np.abs(mean) if error else mean)


def thesis_deviation(curves, interval, side):
    mean, bounds = theory_statistics(curves, interval)
    if bounds is None:
        return np.zeros_like(mean)
    return mean - bounds[0] if side == 'lower' else bounds[1] - mean


def thesis_error_extents(lower, upper):
    if not _THESIS_BANDS:
        return None
    if np.any(lower < 0) or np.any(upper < 0):
        raise ValueError('Pointwise interval does not enclose the mean; the original theory-errorbar representation is incompatible.')
    return np.array([lower, upper])


def thesis_dataset(point, interval):
    n = len(point['value'])
    data = {k: np.asarray(v).copy() for k, v in point['tab'].items()
            if np.asarray(v).ndim == 1 and len(v) == n}
    mean, bounds = theory_statistics(point['theory'], interval)
    data.update(value=point['value'].copy(), alpha=point['alpha'].copy())
    data['thy-0'] = mean
    data['dthy_lower-0'] = mean - bounds[0] if bounds is not None else np.zeros(n)
    data['dthy_upper-0'] = bounds[1] - mean if bounds is not None else np.zeros(n)
    data['thy'] = mean
    data['dthy_lower'] = data['dthy_lower-0']
    data['dthy_upper'] = data['dthy_upper-0']
    for name in ('X', 'Y', 'Z'):
        lo, hi = name.lower() + 'min', name.lower() + 'max'
        if name not in data and lo in data and hi in data:
            data[name] = (data[lo] + data[hi]) / 2
    if 'X' in data:
        data['x'] = data['X']
        data['rx'] = np.around(data['X'], decimals=2)
    if 'Q2' in data:
        data['rQ2'] = np.around(data['Q2'], decimals=0)
    if 'shifts' in point:
        data['shifts'] = np.mean(point['shifts'], axis=0)
    if point['reaction'] == 'jet':
        data['thy-rep'] = {'all': np.asarray(point['theory'])}
        data['plot-factor'] = theory_plot_factors(data)
        names = {'d2sigma/dy-dpt': 'd2_sigma_over_d_y_d_pt',
                 'd2sigma/2-pi-dy-dpt': 'd2_sigma_over_2_pi_d_y_d_pt'}
        if 'obs' in data:
            data['obs'] = np.array([names.get(str(x), str(x)) for x in data['obs']])
        if 'units' not in data and 'unit' in data:
            data['units'] = data['unit']
    return data


def thesis_log_clipping(fig):
    def count_coordinates(paths, dimension):
        count = 0
        for path in paths:
            vertices = path.vertices
            if path.codes is not None:
                vertices = vertices[path.codes != path.CLOSEPOLY]
            invalid = np.all(np.isfinite(vertices), axis=1) & (vertices[:, dimension] <= 0)
            count += len(np.unique(vertices[invalid], axis=0))
        return count

    clipping = []
    for index, axis in enumerate(fig.axes):
        for coordinate, dimension in (('x', 0), ('y', 1)):
            if getattr(axis, 'get_' + coordinate + 'scale')() != 'log':
                continue
            # Inspect emitted polygons after source bin selection and display transforms.
            bands = sum(count_coordinates(collection.get_paths(), dimension)
                        for collection in axis.collections
                        if isinstance(collection, PolyCollection) and collection.get_visible()
                        and collection.get_transform() == axis.transData)
            errors = sum(count_coordinates(collection.get_paths(), dimension)
                         for container in axis.containers if isinstance(container, ErrorbarContainer)
                         for collection in container.lines[2] if collection.get_visible())
            for kind, count in (('theory_band', bands), ('experimental_errorbar', errors)):
                if count:
                    clipping.append({'kind': kind, 'axis': index, 'coordinate': coordinate,
                                     'nonpositive_boundary_coordinates': count})
    return clipping


def render_theory_plots(points, interval, output_dir, metadata):
    global _THESIS_BANDS
    interval = validate_credible_interval(interval)
    _THESIS_BANDS = metadata['n_replicas'] > 1
    images, limitations, accounted = [], [], set()
    selected = {(p['reaction'], p['dataset_id']): p for p in points}
    for case in _THESIS_FIGURES:
        ordered = [(k.split(':')[0], int(k.split(':')[1])) for k in case['datasets']]
        present = set(ordered) & set(selected)
        if not present:
            continue
        accounted |= present
        folder = case.get('output_directory', '')
        base = output_dir
        if folder in {'jets', 'pjets'} and os.path.basename(base) in {'jet', 'pjet'}:
            base = os.path.join(os.path.dirname(base), folder)
        destination = base if not folder or os.path.basename(base) == folder else os.path.join(base, folder)
        os.makedirs(destination, exist_ok=True)
        path = os.path.join(destination, case['filename'])
        if os.path.isfile(path):
            os.remove(path)
        datasets = {}
        prior = set(py.get_fignums())
        try:
            for key in ordered:
                if key in selected:
                    datasets.setdefault(key[0], {})[key[1]] = thesis_dataset(selected[key], interval)
            with matplotlib.rc_context({'text.usetex': True, 'text.latex.preamble': r'\usepackage{amsmath}'}):
                globals()[case['function']](datasets, path, interval)
                if not os.path.isfile(path):
                    raise ValueError('Original plotting function did not produce a figure for the selected points.')
                fig = py.gcf()
                notes = list(case['notes'])
                clipping = thesis_log_clipping(fig)
                band_issues = []
                for item in clipping:
                    location = 'original logarithmic %s axis in subplot %d' % (item['coordinate'], item['axis'] + 1)
                    if item['kind'] == 'theory_band':
                        reason = ('%s: %d nonpositive theory credible-band boundary coordinates cannot be represented on the %s; '
                                  'full theory-band display is UNVALIDATED. Source-clipped image retained; no points, replicas, or bounds changed.' %
                                  (case['filename'][:-4], item['nonpositive_boundary_coordinates'], location))
                        band_issues.append(reason)
                        limitations.append({'source_function': case['source'], **item,
                                            'band_display_status': 'unvalidated', 'reason': reason})
                    else:
                        reason = ('%d experimental errorbar boundary coordinates are clipped by the %s; '
                                  'this source-consistent clipping does not itself invalidate the theory band.' %
                                  (item['nonpositive_boundary_coordinates'], location))
                    notes.append(reason)
                for note in case['notes']:
                    limitations.append({'source_function': case['source'], 'reason': note})
                label = ('%g%% pointwise credible interval' % interval if _THESIS_BANDS else
                         'mean only; credible interval requires at least two fitted replicas')
                if _THESIS_BANDS and interval == 100:
                    label += ' (available fitted-replica envelope, not universal physical coverage)'
                if band_issues:
                    label += '; ' + '; '.join(band_issues)
                selected_names = ['%s:%s' % key for key in ordered if key in present]
                omitted = [name for name in case['datasets'] if name not in selected_names]
                with open(path, 'rb') as handle:
                    png = base64.b64encode(handle.read()).decode()
                axes = []
                for axis in fig.axes:
                    spec = axis.get_subplotspec()
                    axes.append({'xscale': axis.get_xscale(), 'yscale': axis.get_yscale(),
                                 'xlim': list(axis.get_xlim()), 'ylim': list(axis.get_ylim()),
                                 'position': list(axis.get_position().bounds), 'visible': axis.get_visible(),
                                 'grid_position': None if spec is None else
                                 [spec.rowspan.start, spec.rowspan.stop, spec.colspan.start, spec.colspan.stop]})
                images.append({'path': path, 'family': case['filename'][:-4], 'panels': len(fig.axes),
                               'source_function': case['source'], 'datasets': selected_names,
                               'omitted_datasets': omitted, 'source_panels': case['panels'],
                               'credible_interval': interval, 'n_replicas': metadata['n_replicas'],
                               'caption': case['filename'][:-4] + '; mean fitted profiled theory; ' + label,
                               'plot_status': 'partial' if case['notes'] or band_issues else 'ok',
                               'band_display_status': ('unvalidated' if band_issues else
                                                       'compatible' if _THESIS_BANDS else 'not_drawn'),
                               'log_clipping': clipping, 'display_notes': notes,
                               'arithmetic_corrections': case['arithmetic_corrections'],
                               'axes': axes, 'png_b64': png})
        except (ValueError, KeyError, TypeError, RuntimeError, OSError, IndexError, UnboundLocalError) as exc:
            if os.path.isfile(path):
                os.remove(path)
            limitations.append({'source_function': case['source'], 'reason': str(exc)})
        finally:
            for number in set(py.get_fignums()) - prior:
                py.close(number)
    for key in selected:
        if key not in accounted:
            limitations.append({'dataset': '%s:%s' % key,
                                'reason': ('Original polarized-jet figure explicitly omits PHENIX 20005; no panel added.'
                                           if key == ('pjet', 20005) else
                                           'No experimental-data figure for this dataset in the supplied source functions; no generic figure substituted.')})
    metadata['rendering_errors'] = limitations
    metadata['band_drawn'] = bool(images and metadata['n_replicas'] > 1)
    metadata['band_display_status'] = ('unvalidated' if any(i['band_display_status'] == 'unvalidated' for i in images)
                                       else 'compatible' if metadata['band_drawn'] else 'not_drawn')
    if limitations:
        metadata['note'] = (metadata.get('note', '') + ' Thesis plotting limitations: ' +
                            '; '.join(item['reason'] for item in limitations)).strip()
        metadata['plot_status'] = 'partial' if images else 'error'
    return images



'''


# Data vs theory runs on a per-campaign cache (jamai/analysis_cache.py): each
# fitted replica's prediction for each dataset is computed once, in the
# container (_PREDICTIONS), and read back by plain python3 in seconds
# (_PREDICTIONS_VIEW). Only the datasets asked for are computed, and FitPack
# loads only those: the others are dropped from the campaign's conf before
# RESMAN reads anything.
_PREDICTIONS = _REPLICA_STATE + r'''
import json, os, time
import numpy as np
stage = __STAGE__; cache = __CACHE__; want_ds = __DATASETS__; want_exp = __EXPERIMENTS__
started = time.time()
records, excluded = fitted_replicas(stage, replica_state)
if not records:
    print(json.dumps({'status': 'missing_inputs', 'excluded_replicas': excluded,
                      'message': 'No fitted replicas with .msr files; nothing to compare with the data.'}))
    raise SystemExit(0)
sig = signature(records)
from tools.config import conf, load_config
conf.clear(); load_config(os.path.join(stage, 'input.py')); conf['bootstrap'] = False
ids = {key_of(r, i): (r, i) for r, spec in conf['datasets'].items() for i in (spec.get('xlsx') or {})}
meta = read_json(os.path.join(cache, 'meta.json')) or {}
info = meta.get('info') if meta.get('version') == VERSION else None
if not info or set(info) != set(ids):
    import pandas as pd
    info = {k: v for k, v in (info or {}).items() if k in ids}
    for k, (r, i) in ids.items():
        if k in info: continue
        item = {'reaction': r, 'dataset_id': str(i), 'experiment': '', 'observable': '', 'target': '', 'npts_raw': None}
        fname = conf['datasets'][r]['xlsx'][i]
        try:
            t = pd.read_excel(fname if fname.startswith('./') else os.path.join(os.environ['FITPACK'], 'database', fname))
            item['npts_raw'] = int(len(t.index))
            for name, column in (('experiment', 'col'), ('observable', 'obs'), ('target', 'target')):
                if column in t.columns and len(t.index) and pd.notna(t.iloc[0][column]):
                    item[name] = str(t.iloc[0][column])
        except Exception as exc:
            item['note'] = str(exc)[:200]
        info[k] = item
    info = {k: info[k] for k in ids}
    write_json(os.path.join(cache, 'meta.json'), {'version': VERSION, 'info': info})
selected, unknown, unmatched = select(info, want_ds, want_exp)
if not selected:
    print(json.dumps({'status': 'no_match', 'unknown_datasets': unknown, 'unmatched_experiments': unmatched,
                      'available': catalog(info)}))
    raise SystemExit(0)
missing = [k for k in selected if (lambda d: d.get('signature') != sig or d.get('version') != VERSION)(read_json(file_of(cache, k)) or {})]
import hashlib
# Columns FitPack rewrites for each replica; sigtot is SIA's inclusive theory
# cross section. Every other column (data, kinematics, uncertainties) must be
# identical for every replica.
DYNAMIC = {'thy', 'prediction', 'shift', 'Shift', 'N', 'residuals', 'r-residuals', 'n-residuals', 'sigtot'}
def point_identity(tab):
    original = {str(k): np.asarray(v).tolist() for k, v in tab.items() if k not in DYNAMIC}
    return hashlib.sha256(json.dumps(original, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
if missing:
    from fitlib.resman import RESMAN
    from tools.tools import load
    need = {}
    for k in missing:
        need.setdefault(ids[k][0], set()).add(ids[k][1])
    # A normalization tied to another reaction's ('reaction id') needs that reaction in conf.
    for r in list(need):
        for n in (conf['datasets'][r].get('norm') or {}).values():
            ref = n.get('fixed')
            if isinstance(ref, str) and len(ref.split()) == 2 and ref.split()[0] not in need:
                rk = ref.split()[0]
                if rk in conf['datasets'] and conf['datasets'][rk].get('xlsx'):
                    first = sorted(conf['datasets'][rk]['xlsx'], key=str)[0]
                    need[rk] = {first}; missing.append(key_of(rk, first))
    for r in list(conf['datasets']):
        if r not in need: del conf['datasets'][r]
        else: conf['datasets'][r]['xlsx'] = {i: p for i, p in conf['datasets'][r]['xlsx'].items() if i in need[r]}
    resman = None; context = 'loading the selected datasets'
    try:
        resman = RESMAN(nworkers=1, parallel=False, datasets=True)
        residuals = {}
        for r in need:
            attr = {'dy-pion': 'dy_pion_res', 'pion_qT': 'pion_qTres'}.get(r, r + '_res')
            res = getattr(resman, attr, None)
            if res is None or not isinstance(getattr(res, 'tabs', None), dict):
                raise ValueError('No FitPack residual tables for reaction ' + r)
            residuals[r] = res
        fitted_ids = {tuple(x) for x in resman.parman.order if x[0] == 1}
        acc = {k: {'theory': [], 'shift': [], 'chi2': [], 'chi2_cor': [], 'chi2_norm': [], 'norm': [], 'columns': None}
               for k in missing if ids[k][1] in residuals[ids[k][0]].tabs}
        for s in records:
            context = s['identity']
            m = load(s['msr_path'])
            order, params = m['order'][0], np.asarray(m['params'][0], dtype=float)
            if params.ndim != 1 or len(order) != len(params) or not np.isfinite(params).all():
                raise ValueError('Invalid fitted parameter array/order.')
            if {tuple(x) for x in order if x[0] == 1} != fitted_ids:
                raise ValueError('Fitted parameter identities do not match the campaign input.')
            resman.parman.order = order
            resman.get_residuals(params, initial=True)
            for k, a in acc.items():
                r, i = ids[k]
                context = '%s; %s' % (s['identity'], k)
                tab = residuals[r].tabs[i]
                n = len(tab['value'])
                identity = point_identity(tab)
                if a.setdefault('identity', identity) != identity:
                    raise ValueError('Experimental point ordering, kinematics or uncertainties changed between replicas.')
                pred = np.asarray(tab['prediction'], dtype=float)
                if pred.shape != (n,) or not np.isfinite(pred).all():
                    raise ValueError('Missing or nonfinite prediction.')
                a['theory'].append(pred)
                a['shift'].append(np.asarray(tab.get('shift', np.zeros(n)), dtype=float))
                a['chi2'].append(float(np.sum(np.asarray(tab['residuals'], dtype=float) ** 2)))
                rres = np.asarray(tab.get('r-residuals', []), dtype=float)
                a['chi2_cor'].append(float(np.sum(rres ** 2))); a['ncor'] = int(rres.size)
                spec = (conf['datasets'][r].get('norm') or {}).get(i)
                a['norm_fitted'] = bool(spec) and spec.get('fixed') is not True
                a['norm'].append(float(spec['value']) if spec else None)
                nres = np.asarray(tab.get('n-residuals', [0.0]), dtype=float)
                a['chi2_norm'].append(float(nres[0] ** 2) if spec and spec.get('fixed') is False and nres.size else 0.0)
                if a['columns'] is None:
                    cols = {}
                    for name, v in tab.items():
                        arr = np.asarray(v)
                        # sigtot: SIA's inclusive theory cross section, not a data column.
                        if arr.ndim != 1 or len(arr) != n or name in ('residuals', 'r-residuals', 'n-residuals', 'thy',
                                                                       'prediction', 'shift', 'Shift', 'N', 'sigtot'):
                            continue
                        if arr.dtype.kind in 'iuf':
                            cols[str(name)] = [None if not np.isfinite(x) else float('%.8g' % x) for x in arr.astype(float)]
                        else:
                            cols[str(name)] = [str(x) for x in arr]
                    a['columns'] = cols
    except (OSError, KeyError, TypeError, ValueError, ArithmeticError, SystemExit) as exc:
        print(json.dumps({'status': 'error', 'message': 'Cannot evaluate ' + context + ': ' + str(exc) +
                          '. Nothing was cached for these datasets.'}))
        raise SystemExit(0)
    finally:
        if resman is not None: resman.shutdown()
    rnd = lambda rows: [[float('%.8g' % x) for x in row] for row in rows]
    for k in missing:
        r, i = ids[k]
        doc = {'version': VERSION, 'key': k, 'reaction': r, 'dataset_id': str(i), 'signature': sig,
               'replicas': [s['identity'] for s in records],
               **{f: info[k].get(f) for f in ('experiment', 'observable', 'target', 'npts_raw')}}
        a = acc.get(k)
        if a is None:
            doc['unavailable'] = 'No experimental points survived the campaign reader/cuts.'
        else:
            doc.update(npts=len(a['columns']['value']), ncor=a['ncor'], norm_fitted=a['norm_fitted'],
                       point_order_sha256=a['identity'],
                       columns=a['columns'], theory=rnd(a['theory']), shift=rnd(a['shift']),
                       stats={f: a[f] for f in ('chi2', 'chi2_cor', 'chi2_norm', 'norm')})
        write_json(file_of(cache, k), doc)
print(json.dumps({'status': 'success', 'computed': missing, 'selected': selected,
                  'seconds': round(time.time() - started, 1)}))
'''

_PREDICTIONS_VIEW = _REPLICA_STATE + r'''
import json, os
stage = __STAGE__; cache = __CACHE__; want_ds = __DATASETS__; want_exp = __EXPERIMENTS__
interval = __INTERVAL__; charts = __CHARTS__; selection = __SELECTION__
records, excluded = fitted_replicas(stage, replica_state)
if not records:
    print(json.dumps({'status': 'missing_inputs', 'excluded_replicas': excluded,
                      'message': 'No fitted replicas with .msr files; nothing to compare with the data.'}))
    raise SystemExit(0)
sig = signature(records)
meta = read_json(os.path.join(cache, 'meta.json')) or {}
if meta.get('version') != VERSION:
    print(json.dumps({'status': 'need_build'})); raise SystemExit(0)
selected, unknown, unmatched = select(meta['info'], want_ds, want_exp)
if not selected:
    print(json.dumps({'status': 'no_match', 'unknown_datasets': unknown, 'unmatched_experiments': unmatched,
                      'available': catalog(meta['info'])}))
    raise SystemExit(0)
docs = {k: read_json(file_of(cache, k)) or {} for k in selected}
stale = [k for k, d in docs.items() if d.get('signature') != sig or d.get('version') != VERSION]
if stale:
    print(json.dumps({'status': 'need_build', 'stale': stale})); raise SystemExit(0)
kept, dropped = select_replicas(records, selection)
if not kept:
    print(json.dumps({'status': 'error', 'message': 'The replica selection leaves no fitted replica.',
                      'replica_selection': selection_report(selection, kept, len(records), dropped)}))
    raise SystemExit(0)
keep = {s['identity'] for s in kept}
best = min(kept, key=lambda s: float(s.get('chi2_total') if s.get('chi2_total') is not None else 'inf'))
rows, plots, unavailable = [], [], []
for k, d in docs.items():
    if d.get('unavailable'):
        unavailable.append({'dataset': k, 'reason': d['unavailable']}); continue
    d = subset_doc(d, keep)
    rows.append(table_row(d, best['identity']))
    if charts: plots.append(chart_data(d, interval))
print(json.dumps({'status': 'success', 'n_replicas': len(kept), 'excluded_replicas': excluded,
                  'replica_selection': selection_report(selection, kept, len(records), dropped) if selection else None,
                  'replica_ids': [s['identity'] for s in kept], 'table': rows, 'charts': plots,
                  'best_replica': best['identity'], 'best_msr': best['msr_path'], 'experiments': by_experiment(rows),
                  'unavailable_datasets': unavailable, 'unknown_datasets': unknown,
                  'unmatched_experiments': unmatched}))
'''

# The thesis-style PNGs, from the cache: the original plotting functions are
# fed the cached points, so FitPack is not run again. LaTeX makes them slow:
# only on request (export_png).
_THEORY_PNG = _REPLICA_STATE + ("\nimport base64, zlib\n"
    "exec(zlib.decompress(base64.b64decode(%r)))\n" %
    base64.b64encode(zlib.compress(_THEORY_PLOTTING.encode(), 9)).decode()) + r'''
import json, os
stage = __STAGE__; cache = __CACHE__; keys = __KEYS__; selection = __SELECTION__
interval = validate_credible_interval(__INTERVAL__)
keep = {s['identity'] for s in select_replicas(fitted_replicas(stage, replica_state)[0], selection)[0]}
points = []
for k in keys:
    d = read_json(file_of(cache, k)) or {}
    if d.get('unavailable') or 'theory' not in d: continue
    d = subset_doc(d, keep)
    n = d['npts']
    tab = {name: np.asarray(v) if all(isinstance(x, str) for x in v) else np.asarray(v, dtype=float)
           for name, v in d['columns'].items()}
    points.append({'reaction': d['reaction'],
                   'dataset_id': int(d['dataset_id']) if str(d['dataset_id']).isdigit() else d['dataset_id'],
                   'experiment': d.get('experiment', ''), 'tab': tab,
                   'value': np.asarray(d['columns']['value'], dtype=float),
                   'alpha': np.asarray(d['columns']['alpha'], dtype=float),
                   'theory': np.asarray(d['theory'], dtype=float), 'shifts': np.asarray(d['shift'], dtype=float)})
metadata = {'n_replicas': len(points[0]['theory']) if points else 0, 'note': ''}
try:
    images = render_theory_plots(points, interval, os.path.join(os.path.dirname(os.path.dirname(stage)),
                                 'analysis', 'by_experiment'), metadata)
except (OSError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
    print(json.dumps({'status': 'error', 'message': 'Theory plotting failed: ' + str(exc)})); raise SystemExit(0)
print(json.dumps({'status': 'success', 'images': images, 'rendering_errors': metadata.get('rendering_errors', []),
                  'note': metadata.get('note', '')}))
'''


def _analysis_code(template, **values):
    """A cluster script: the cache helpers, then the template filled in."""
    return inspect.getsource(analysis_cache) + "\n" + _fill(template, **values)


def _last_json(r):
    try:
        return json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        return None


def _cache_dir(u, e):
    return f"{os.path.dirname(os.path.dirname(_stage_root(u, e)))}/analysis/cache"


REPLICAS_ARG = {
    "description": ("Use only some fitted replicas for the statistics, as JAM analyses do (nothing is refit; the "
                    "cache keeps every replica). Object with any of: best (keep the n lowest total chi2), chi2_max "
                    "(drop replicas whose total chi2 is above), sigma (drop outliers above median + k*1.4826*MAD of "
                    "the total chi2), exclude (replica numbers), only (replica numbers); applied in the order only, "
                    "exclude, chi2_max, sigma, best. A string works too: 'best:50', 'chi2_max:1200', 'sigma:3', "
                    "'exclude:3,7', 'only:1-20', several joined by ';'. Omit for every fitted replica. The result's "
                    "replica_selection says which replicas were kept and why the others were left out."),
    "anyOf": [{"type": "object", "properties": {
        "best": {"type": "integer", "minimum": 2}, "chi2_max": {"type": "number", "exclusiveMinimum": 0},
        "sigma": {"type": "number", "exclusiveMinimum": 0},
        "exclude": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "only": {"type": "array", "items": {"type": "integer", "minimum": 1}}}, "additionalProperties": False},
        {"type": "string"}]}


def _numbers(text):
    """'3,7, 10-12' or [3, 7] -> [3, 7, 10, 11, 12]."""
    if isinstance(text, (list, tuple)):
        return [int(x) for x in text]
    out = []
    for part in re.split(r"[,\s]+", str(text).strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if b < a or b - a > 5000:
                raise ValueError(f"bad replica range {part!r}")
            out.extend(range(a, b + 1))
        else:
            out.append(int(part))
    return out


def _replica_selection(arg):
    """A replica selection as the tools accept it (object, JSON or 'best:50;
    sigma:3' text) -> a validated dict, or None for every replica. Raises
    ValueError with a message for the user."""
    if arg in (None, "", {}, "all"):
        return None
    if isinstance(arg, str):
        text = arg.strip()
        if text.startswith("{"):
            arg = json.loads(text)
        else:
            arg = {}
            for part in filter(None, (x.strip() for x in text.split(";"))):
                key, _, value = part.partition(":")
                arg[key.strip().lower().replace("-", "_")] = value.strip()
    if not isinstance(arg, dict):
        raise ValueError("replicas: an object or text such as 'best:50' or 'sigma:3'")
    unknown = set(arg) - set(analysis_cache.SELECTION_KEYS)
    if unknown:
        raise ValueError(f"replicas: unknown rule(s) {sorted(unknown)}; use {list(analysis_cache.SELECTION_KEYS)}")
    out = {}
    try:
        if arg.get("only") not in (None, "", []):
            out["only"] = sorted(set(_numbers(arg["only"])))
        if arg.get("exclude") not in (None, "", []):
            out["exclude"] = sorted(set(_numbers(arg["exclude"])))
        if arg.get("chi2_max") not in (None, ""):
            out["chi2_max"] = float(arg["chi2_max"])
            assert math.isfinite(out["chi2_max"]) and out["chi2_max"] > 0
        if arg.get("sigma") not in (None, ""):
            out["sigma"] = float(arg["sigma"])
            assert math.isfinite(out["sigma"]) and out["sigma"] > 0
        if arg.get("best") not in (None, ""):
            out["best"] = int(arg["best"])
            assert out["best"] >= 2
    except (TypeError, ValueError, AssertionError):
        raise ValueError("replicas: best is an integer of at least 2; chi2_max and sigma are positive numbers; "
                         "only and exclude are replica numbers such as 3,7 or 1-20")
    return out or None


def _names(text):
    """'a, b; c' or a list -> ['a', 'b', 'c']."""
    if isinstance(text, (list, tuple)):
        text = ",".join(str(x) for x in text)
    return [x.strip() for x in re.split(r"[,;]", str(text or "")) if x.strip()]


def _theory_results(u, e, datasets, experiments, interval, charts=True, export_png=False, selection=None):
    """Data vs theory for the datasets asked for: the per-dataset table, the
    chart data and, on request, the thesis PNGs. Computes into the cache only
    what is missing or stale."""
    stage, cache = _stage_root(u, e), _cache_dir(u, e)
    ws = _workspace(u)
    args = dict(STAGE=stage, CACHE=cache, DATASETS=datasets, EXPERIMENTS=experiments)
    view = lambda: _last_json(_run_ifarm_py(u, _analysis_code(_PREDICTIONS_VIEW, INTERVAL=interval, CHARTS=charts,
                                                               SELECTION=selection, **args), timeout=300, cap=60000000))
    d, built = view(), None
    if d and d.get("status") == "need_build":
        r = _run_ifarm_py(u, _analysis_code(_PREDICTIONS, **args), apptainer=True, timeout=3600, cap=200000,
                          env={"MPLCONFIGDIR": f"{ws}/{CAMPAIGNS_DIR}/.matplotlib"})
        built = _last_json(r)
        if not built:
            return {"status": "error", "message": "computing the replica predictions failed",
                    "detail": (r.get("err") or r.get("out") or "")[-1500:]}
        if built.get("status") != "success":
            return built
        d = view()
    if not d:
        return {"status": "error", "message": "reading the cached predictions failed"}
    if d.get("status") != "success":
        return d
    out = {"status": "ok", "collection_id": e["collection_id"], "campaign_name": e.get("campaign_name"),
           "credible_interval": interval, "n_replicas": d["n_replicas"], "excluded_replicas": d["excluded_replicas"],
           "percentiles": [(100 - interval) / 2, 100 - (100 - interval) / 2],
           "interval_kind": "pointwise_equal_tailed", "central_estimator": "mean",
           "theory_convention": "FitPack prediction: normalized theory plus profiled correlated shifts",
           "chi2_estimator": ("table: mean over the fitted replicas, each against the central data; "
                              "experiments: best fitted replica against the central data (not ensemble-mean chi2)"),
           "replica_ids": d["replica_ids"], "best_replica": d["best_replica"], "best_msr": d["best_msr"],
           "replica_selection": d.get("replica_selection"),
           "band_drawn": bool(charts and d["n_replicas"] > 1),
           "table": d["table"], "experiments": d["experiments"], "unavailable_datasets": d["unavailable_datasets"],
           "computed_now": (built or {}).get("computed", []), "compute_seconds": (built or {}).get("seconds")}
    for k in ("unknown_datasets", "unmatched_experiments"):
        if d.get(k):
            out[k] = d[k]
    notes = ["chi2 terms are means over the fitted replicas, each replica against the central data; "
             "fitted norm is mean(std) over the replicas."]
    sel = d.get("replica_selection")
    if sel:
        notes.append(f"Replica selection {sel['applied']}: {sel['kept']} of {sel['of']} fitted replicas used; "
                     f"{len(sel['dropped'])} left out (see replica_selection).")
    if d["n_replicas"] < 2:
        notes.append("Only one fitted replica: no credible band.")
    if out.get("unknown_datasets") or out.get("unmatched_experiments"):
        notes.append("Some names matched no dataset of this campaign: "
                     + ", ".join(out.get("unknown_datasets", []) + out.get("unmatched_experiments", [])) + ".")
    ui = {"view": "theory", "credible_interval": interval, "charts": d.get("charts") or []}
    if export_png:
        keys = [row["dataset"] for row in d["table"]]
        r = _run_ifarm_py(u, inspect.getsource(object_registry) + "\n" +
                          _analysis_code(_THEORY_PNG, STAGE=stage, CACHE=cache, KEYS=keys, INTERVAL=interval,
                                         SELECTION=selection),
                          apptainer=True, timeout=1200, cap=60000000,
                          env={"MPLCONFIGDIR": f"{ws}/{CAMPAIGNS_DIR}/.matplotlib"})
        p = _last_json(r) or {"status": "error", "message": "thesis plotting failed",
                              "detail": (r.get("err") or "")[-600:]}
        if p.get("status") == "success":
            imgs = p.get("images") or []
            out["plots"] = [{k: v for k, v in i.items() if k != "png_b64"} for i in imgs]
            out["_images"] = [{"data": i["png_b64"], "mimeType": "image/png"} for i in imgs]
            ui["panel_plots"] = [{**pl, "data_url": "data:image/png;base64," + i["png_b64"]}
                                 for pl, i in zip(out["plots"], imgs)]
            if p.get("rendering_errors"):
                out["rendering_errors"] = p["rendering_errors"]
        else:
            notes.append("Thesis PNGs: " + (p.get("message") or "failed") + ".")
    out["note"] = " ".join(notes)
    out["_ui"] = ui
    return out


def analyze_by_experiment(collection_id="", experiments="", plot=True, username="", credible_interval=95,
                          datasets="", export_png=False, replicas=None):
    """Per-dataset table (JAM paper columns) and data vs mean fitted theory, for
    the datasets or experiments named (empty: all)."""
    try:
        credible_interval = object_registry.validate_credible_interval(credible_interval)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    u, err = _need_user(username)
    if err:
        return err
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}."}
    collection_id = e["collection_id"]      # a campaign name resolves to its id
    try:
        selection = _replica_selection(replicas)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    ds, ex = _names(datasets), _names(experiments)
    return _background(u, "analyze_by_experiment",
                       {"collection_id": e["collection_id"], "datasets": ds, "experiments": ex,
                        "interval": credible_interval, "plot": bool(plot), "png": bool(export_png),
                        "replicas": selection},
                       lambda: _theory_results(u, e, ds, ex, credible_interval, charts=bool(plot),
                                               export_png=bool(export_png), selection=selection))


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
    # The latest job that ran replica i: a range (first-last) or, for a rerun, a list.
    found = None
    for j in jobs:
        try:
            if (i in j['replicas']) if j.get('replicas') else int(j['first']) <= i <= int(j['last']):
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
    collection_id = e["collection_id"]      # a campaign name resolves to its id
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

# The analysis tools render in the panel's chat widget, which calls
# analysis_result itself while a job runs.
WIDGET_META = {"ui": {"resourceUri": PANEL_URI}, "openai/outputTemplate": PANEL_URI_SKYBRIDGE,
               "openai/widgetAccessible": True}
COLLECTION_ARG = "The collection id, or the campaign name as the user says it (its newest submission)."
ASYNC_NOTE = ("A call answers within about 40 s: when the work takes longer it returns status running with "
              "a job_id; then call analysis_result(job_id) until it is done.")

def _analysis_catalog_store():
    # Only the repository/deployed application catalog is readable by these tools.
    return analysis_catalog.Catalog(analysis_catalog.catalog_root(HERE))


def _catalog_request(operation, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("catalog arguments must be an object")
    allowed = {"list": {"cursor", "limit"},
               "get": {"analysis_id", "section", "object_key", "cursor", "limit", "detail", "group", "status", "dataset_id", "setting_id"},
               "requirements": {"analysis_id", "cursor", "limit", "detail", "requirement_id"}}[operation]
    # Shared-gateway identity is already checked by the existing MCP dispatcher.
    kwargs = {k: v for k, v in arguments.items() if k != "username"}
    if set(kwargs) - allowed:
        raise ValueError("unsupported catalog argument(s): " + ", ".join(sorted(set(kwargs) - allowed)))
    if operation != "list" and "analysis_id" not in kwargs:
        raise ValueError("analysis_id is required; discover IDs with list_analysis_catalog")
    return getattr(_analysis_catalog_store(), operation)(**kwargs)


def _analysis_results_store():
    return analysis_results.Results(analysis_results.results_root(HERE))


def _results_request(operation, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("results arguments must be an object")
    args = {k: v for k, v in arguments.items() if k != "username"}
    store = _analysis_results_store()
    if operation == "list":
        if set(args) - {"cursor", "limit"}:
            raise ValueError("Unsupported results discovery arguments")
        return store.list(**args)
    analysis_results.validate_request(operation, args)
    manifest = store.resolve(args["analysis_id"])
    if any(_outside_read_roots(path) for path in analysis_results.archive_directories(manifest)):
        raise ValueError("Registered results archive is outside the site's authorized read roots")
    user, error = _resolve_user("")
    if error:
        raise ValueError("Authorized JLab connection required for reference results")
    # Source and JSON travel as data through the existing base64 SSH helper.
    # No client-controlled paths or commands become shell fragments.
    request = {"manifest": manifest, "operation": operation, "arguments": args}
    with open(analysis_results.__file__, encoding="utf-8") as handle:
        source = handle.read()
    code = source + "\nprint(encoded(remote_request(json_read(" + repr(analysis_results.encoded(request)) + "))))\n"
    reply = _run_ifarm_py(user, code, apptainer=False, timeout=60, cap=16000)
    if reply.get("rc") != 0 or reply.get("timeout"):
        raise ValueError("JLab reference-results storage is unavailable; reconnect to HPC and retry")
    try:
        answer = analysis_results.json_read(reply.get("out") or "")
    except (ValueError, TypeError):
        raise ValueError("JLab reference-results reader returned an invalid or truncated response") from None
    if not answer.get("ok"):
        raise ValueError(answer.get("error", "Reference results unavailable"))
    return answer["result"]


TOOLS = {
    "list_analysis_results": {
        "compact": True,
        "desc": "Read-only compact discovery of archived numerical reference results. Returns stable analysis_id and revision-pinned results_ref. No arrays or scientific configuration are transmitted. Follow next_cursor.",
        "schema": {"type": "object", "additionalProperties": False, "properties": {
            "cursor": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "fn": lambda a: _results_request("list", a)},
    "get_analysis_fit_statistics": {
        "compact": True,
        "desc": "Read precomputed authoritative numerical fit statistics, without recalculation. Defaults to dataset records; scope=overall or reactions gives aggregates. Filter by reaction and reaction-qualified dataset_id. Reuse results_ref as analysis_id. JLab connection required; follow next_cursor with identical selectors. Missing values remain null with reasons.",
        "schema": {"type": "object", "additionalProperties": False, "required": ["analysis_id"], "properties": {
            "analysis_id": {"type": "string"}, "scope": {"type": "string", "enum": ["datasets", "reactions", "overall"], "default": "datasets"},
            "reaction": {"type": "string"}, "dataset_id": {"type": "string"}, "cursor": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "fn": lambda a: _results_request("statistics", a)},
    "get_analysis_theory_predictions": {
        "compact": True,
        "desc": "Read saved replica-averaged predictions and aligned experimental points for one reaction-qualified dataset. Summary only by default; detail=points explicitly requests paginated arrays as ordered point records. fields selects at most 12 names from available_fields (including exp.* kinematics/uncertainties). No full replica arrays, theory evaluation, or new credible intervals. Reuse results_ref; JLab connection required.",
        "schema": {"type": "object", "additionalProperties": False, "required": ["analysis_id", "reaction", "dataset_id"], "properties": {
            "analysis_id": {"type": "string"}, "reaction": {"type": "string"}, "dataset_id": {"type": "string"},
            "detail": {"type": "string", "enum": ["summary", "points"], "default": "summary"},
            "fields": {"type": "array", "maxItems": 12, "items": {"type": "string"}}, "cursor": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "fn": lambda a: _results_request("predictions", a)},

    "list_analysis_catalog": {
        "compact": True,
        "desc": "Compact read-only discovery of published analyses: publication, objects, readiness and reusable catalog_ref. Scientific configuration stays server-side; no HPC connection is needed. Follow next_cursor if present.",
        "schema": {"type": "object", "additionalProperties": False, "properties": {
            "cursor": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "fn": lambda a: _catalog_request("list", a)},
    "get_analysis_catalog": {
        "compact": True,
        "desc": "Read-only catalog inspection. Defaults to compact summaries; detail=full explicitly requests definitions/evidence. Filter parameters by object_key/group/status, datasets by reaction object_key/dataset_id, or qcd/statistics by setting_id. Reuse catalog_ref as analysis_id; do not download/retransmit full configuration for orchestration. Explicit document sections provide lossless exports. Follow next_cursor with identical selectors. No campaign creation or fitting.",
        "schema": {"type": "object", "additionalProperties": False, "required": ["analysis_id"], "properties": {
            "analysis_id": {"type": "string", "description": "Stable analysis ID, alias or revision-pinned catalog_ref from discovery."}, "section": {"type": "string", "enum": ["overview", "parametrization", "parameters", "datasets", "qcd", "statistics", "provenance", "analysis_config", "paper_findings", "unresolved_settings", "documents"], "default": "overview"},
            "object_key": {"type": "string", "default": ""}, "cursor": {"type": "string"},
            "detail": {"type": "string", "enum": ["summary", "full"], "default": "summary"},
            "group": {"type": "string", "description": "Exact parameter shape prefix, e.g. g1 or u1."},
            "status": {"type": "string", "enum": ["free", "fixed", "linked"]},
            "dataset_id": {"type": "string", "description": "Exact ID, qualified by reaction object_key."},
            "setting_id": {"type": "string", "description": "Exact setting key or scientific ID, e.g. positivity, Q20 or theory.order."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "fn": lambda a: _catalog_request("get", a)},
    "get_analysis_requirements": {
        "compact": True,
        "desc": "Read-only current reproduction blockers with priority, rationale and evidence. Defaults to active blockers only; requirement_id targets one, detail=full also includes historical/conditional notes. Resolved issues stay resolved. Accepts ID or catalog_ref; follow next_cursor.",
        "schema": {"type": "object", "additionalProperties": False, "required": ["analysis_id"], "properties": {
            "analysis_id": {"type": "string"}, "cursor": {"type": "string"},
            "detail": {"type": "string", "enum": ["summary", "full"], "default": "summary"},
            "requirement_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "fn": lambda a: _catalog_request("requirements", a)},
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
        "desc": "The fit composer's choices: objects, proton parameters from examples/input02.py, "
                "one-shape pion and LN parameters from the trusted final-fit input.npy reference "
                "(block, parameter, FIXED/FREE/tied, value, min, max; source names the file), the "
                "reactions, default cuts, QCD and job settings with their allowed values, and the "
                "Slurm partitions. Pion metadata also supplies its QCD/job defaults. Temporary "
                "compatibility selects DIS 0 for fitted pion (including combined), otherwise 1. "
                "Needs the cluster chain and a workspace. Call it first and walk "
                "the user through each step.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: campaign_options()},
    "campaign_datasets": {
        "desc": "The datasets of one available reaction from campaign_options, grouped by experiment, with labels "
                "(experiment, observable, target). Optional search text. Nothing is preselected. "
                + ("SIDIS (PDFs + FFs) and SIA (FFs) are fittable reactions." if FF_EXECUTION_ENABLED else
                   "SIDIS and SIA are selectable for dataset configuration only; FF fitting is unavailable."),
        "schema": {"type": "object", "properties": {"reaction": {"type": "string", "enum": sorted(CATALOG_REACTIONS)},
                                                    "search": {"type": "string"}},
                   "required": ["reaction"]},
        "fn": lambda a: campaign_datasets(a.get("reaction", "idis"), a.get("search", ""))},
    "use_ai_branch": {
        "desc": "Switch the workspace to the FitPack ai branch (fitlib/ai.py runs campaigns). "
                "Refuses if tracked files have uncommitted changes.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: use_ai_branch()},
    "validate_campaign": {
        "desc": ("SIDIS needs the proton PDF plus the FFs of its hadrons; SIA needs FFs only. "
                 if FF_EXECUTION_ENABLED else
                 "SIDIS/SIA selections return a dataset-only input fragment with configuration_valid and "
                 "fit_ready=false, never a runnable fit or submission confirmation. ") +
                "Create Input Card: validate the composer card and return the summary (free/fixed "
                "parameters per object. Additive card.objects=[{type:'pdf',parameters:{}},"
                "{type:'pdf-pion',parameters:{}}] selects one or both; card.companions={'p->pi,n':"
                "{parameters:{...}}} edits the fitted leading-neutron companion. Legacy card.object "
                "remains accepted. Parameter names are scoped to their object; pion second shapes "
                "are excluded. Each reaction's cuts can be a list or a dataset-ID mapping of lists "
                "(LN reference defaults: 1000 y<0.3, 2000 y<0.2). The five added proton reactions "
                "use their approved cut policies: jet has only pT>8; wzrv, wasym, zrap and wc "
                "have no additional filters. The qcd card accepts four independent boolean options: "
                "pdfPositivity, ffpionPositivity, ffkaonPositivity, ffhadronPositivity "
                "(default true; false is preserved). Summary reports "
                "parameters (including optional equal_to links between PDF parameters), the ones changed from the library, datasets, cuts, QCD, job, and "
                "points_after_cuts per dataset), the Slurm array script, the confirmation phrase and "
                "warnings (a dataset with no points left after the cuts, a data file without a column "
                "FitPack's status report reads); warnings never block submission. " + 'card = {"object": {"type": "pdf", "parameters": {"g1 a": {"value", "min", "max", "fixed": true|false, "equal_to": "uv1 a"}, ...}}, "reactions": ["idis", "dy"], "datasets": {"idis": ["10010", ...]}, "normalizations": {"idis": {"10010": {"value", "min", "max", "fixed": true|false|"<id of another selected dataset whose normalization this one equals>"}}} (only for datasets campaign_datasets lists with a normalization), "cuts": {"idis": [{"variable": "Q2", "op": ">", "value": 1.69, "enabled": true}]}, "qcd": {"order", "Q20", "tmc": "off"|"AOT", "higherTwist", "dglapMode", "asMode", "pdfPositivity": true|false, "ffpionPositivity": true|false, "ffkaonPositivity": true|false, "ffhadronPositivity": true|false}, "parameters": {"initialization": "default"|"random_within_bounds", "randomSeed", "perReplicaSeed"}, "job": {"campaignName", "replicas", "maxIterations", "tolerance", "optimizer", "bootstrap"}, "slurm": {"partition" ("" = auto), "time" (per replica, HH:MM:SS), "mem", "cpus"}}. Only parameters the user changed need to be listed; the rest keep the library values, and only changed rows are written into the card. A parameter switched to FREE needs a min and a max.',
        "schema": {"type": "object", "properties": {"card": {"type": "object"}}, "required": ["card"]},
        "fn": lambda a: validate_campaign(a.get("card"))},
    "submit_campaign": {
        "desc": "Submit Jobs: materialize the card and submit one Slurm array task per replica. "
                + ("" if FF_EXECUTION_ENABLED else "Rejects SIDIS/SIA configuration-only cards before uploads or job creation. ") +
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
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string", "description": COLLECTION_ARG}},
                   "required": ["collection_id"]},
        "fn": lambda a: campaign_status(a.get("collection_id", ""))},
    "postprocess_campaign": {
        "desc": "Postprocess: aggregate the finished replicas into the campaign fit summary. Only the "
                "replicas that really fitted enter the statistics; the ones that did not fit (only chi2 "
                "at the starting values) are counted apart (not_fitted_replicas).",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string", "description": COLLECTION_ARG}},
                   "required": ["collection_id"]},
        "fn": lambda a: postprocess_campaign(a.get("collection_id", ""))},
    "campaign_summary": {
        "desc": "Load Fit Summary: replicas that fitted (completed_replicas), did not fit "
                "(not_fitted_replicas) and failed, chi2 statistics over the fitted ones, the best "
                "replica, and chi2/N per dataset (worst first). note says when replicas did not fit "
                "or the summary is out of date. With replicas (a selection), the statistics are recomputed "
                "over the selected fitted replicas (deselected_replicas counts the rest); the stored summary "
                "is not changed.",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string", "description": COLLECTION_ARG},
                                                    "replicas": REPLICAS_ARG},
                   "required": ["collection_id"]},
        "fn": lambda a: campaign_summary(a.get("collection_id", ""), replicas=a.get("replicas"))},
    "campaign_pdf_plots": {
        "desc": "View PDF Plots: x f(x, Q2) of the fitted replicas at q2 (GeV^2). Proton (object_key pdf) flavors, "
                "in the requested order: u,d,g,s,ub,db,sb,uv,dv,d/u,db+ub,db-ub,sp,sm,Rs,up,dp; sums are xPDFs, "
                "d/u and Rs are dimensionless; each fitted replica is transformed before the intervals. "
                "The result is drawn twice: an interactive chart in the panel and the chat widget (zoom, hover "
                "values, flavor toggles, interval and log/linear switches) and the figure (proton: one "
                "two-column band figure without replica lines, display x_min=0.01, x_max=1, band_color=red, "
                "band_alpha=0.95; pion: the replica plot). export_png=false skips the figure. mean_overlay "
                "adds a mean line to the figure (off by default for proton, on for pion). The result gives "
                "mean_at_x, a few points of each mean curve. The equal-tailed pointwise credible interval "
                "defaults to 95%; greater than 0, at most 100 (100 = full envelope of the fitted replicas, "
                "not the whole posterior); at least two fitted replicas for a band. object_key selects pdf "
                "or pdf-pion; pion defaults valence (ub-u), sea (u), g in the native pi- convention. Curves "
                "are cached per Q2 and flavor: a repeat is fast. " + ASYNC_NOTE,
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string", "description": COLLECTION_ARG},
                                                    "flavors": {"type": "string"},
                                                    "object_key": {"type": "string", "enum": list(SELECTABLE_OBJECTS)},
                                                    "q2": {"type": "number"},
                                                    "credible_interval": {"type": "number", "default": 95,
                                                                          "exclusiveMinimum": 0, "maximum": 100,
                                                                          "description": "Equal-tailed pointwise credible interval (%)."},
                                                    "mean_overlay": {"type": "boolean"},
                                                    "x_min": {"type": "number", "exclusiveMinimum": 0, "default": 0.01},
                                                    "x_max": {"type": "number", "maximum": 1, "default": 1},
                                                    "band_color": {"type": "string", "default": "red"},
                                                    "band_alpha": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.95},
                                                    "export_png": {"type": "boolean", "default": True},
                                                    "replicas": REPLICAS_ARG},
                   "required": ["collection_id"]},
        "meta": WIDGET_META,
        "fn": lambda a: campaign_pdf_plots(a.get("collection_id", ""), a.get("flavors", ""),
                                           a.get("q2", 10.0), a.get("mean_overlay"),
                                           object_key=a.get("object_key", ""),
                                           credible_interval=a.get("credible_interval", 95),
                                           x_min=a.get("x_min", 0.01), x_max=a.get("x_max", 1),
                                           band_color=a.get("band_color", "red"), band_alpha=a.get("band_alpha", 0.95),
                                           export_png=a.get("export_png", True), replicas=a.get("replicas"))},
    "campaign_ff_plots": {
        "desc": "Native positive-hadron zD bands in three columns. Modes combined/individual may both be selected. "
                "Hadrons pion,kaon,rhadron(residual),hadron(total) require actual components. "
                "Flavors g,u,d,s,ub,db,sb,c,b,up,dp,sp; combinations are formed per replica. "
                "Defaults Q2=100, z=.02 to .9, 95% pointwise equal-tailed intervals. No replica lines. "
                "configured=true is an explicit deterministic starting-value preview without a band. "
                "The bands are also drawn as an interactive chart in the panel and the chat widget (by flavor "
                "with hadrons overlaid, or by hadron; hover values, zoom, interval and log/linear switches); "
                "export_png=false skips the figures. Replica curves are cached per Q2, z range and flavor: a "
                "redraw or another interval or hadron choice is fast. " + ASYNC_NOTE,
        "schema": {"type":"object", "properties": {
            "collection_id":{"type":"string","description":COLLECTION_ARG}, "flavors":{"type":"string"},
            "q2":{"type":"number","exclusiveMinimum":0,"default":100},
            "credible_interval":{"type":"number","exclusiveMinimum":0,"maximum":100,"default":95},
            "configured":{"type":"boolean"}, "mean_overlay":{"type":"boolean","description":"Legacy argument; fitted FF outputs are bands only."},
            "modes":{"type":"string","default":"combined"}, "hadrons":{"type":"string"},
            "z_min":{"type":"number","exclusiveMinimum":0,"default":.02},
            "z_max":{"type":"number","exclusiveMaximum":1,"default":.9},
            "band_color":{"type":"string","default":"red"},
            "band_alpha":{"type":"number","minimum":0,"maximum":1,"default":.9},
            "export_png":{"type":"boolean","default":True}, "replicas": REPLICAS_ARG},"required":["collection_id"]},
        "meta": WIDGET_META,
        "fn": lambda a: campaign_ff_plots(a.get("collection_id",""),a.get("flavors",""),a.get("q2",100),
                                          a.get("mean_overlay",False),credible_interval=a.get("credible_interval",95),
                                          configured=a.get("configured",False),modes=a.get("modes","combined"),
                                          hadrons=a.get("hadrons",""),z_min=a.get("z_min",.02),z_max=a.get("z_max",.9),
                                          band_color=a.get("band_color","red"),band_alpha=a.get("band_alpha",.9),
                                          export_png=a.get("export_png",True),replicas=a.get("replicas"))},
    "analyze_by_experiment": {
        "desc": "Data vs theory and the per-dataset table, for the datasets and/or experiments the user "
                "names, passed as they said them: datasets 'idis:10010, jet 10001' or bare ids; experiments "
                "'D0, CDF, STAR' (case, accents and separators do not matter; a prefix matches). Both empty "
                "= every dataset of the campaign. table has, per dataset (as in the JAM papers): npts / "
                "npts_raw (after / before cuts), ncor (correlated systematics), chi2_red with its z_score, "
                "chi2_cor, chi2_norm and the fitted norm as norm(norm_std); chi2 terms are means over the "
                "fitted replicas. experiments: chi2/N of the best replica grouped by experiment (SLAC, BCDMS, "
                "NMC, E866 ...), a separate best-replica diagnostic, not chi2 of the ensemble mean. The panel and the chat widget draw interactive data vs mean-theory charts "
                "(axis and grouping choices, ratio and pull views, credible-interval switch, zoom, hover). "
                "plot=false: the table only. export_png=true also renders the thesis-style PNGs (slow: "
                "LaTeX). Names that match nothing come back as unknown_datasets / unmatched_experiments; "
                "if nothing matches, status no_match lists what the campaign has (available): choose from "
                "it, no other tool is needed. Predictions are cached per dataset: only the first request "
                "for a dataset evaluates the replicas. " + ASYNC_NOTE,
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string", "description": COLLECTION_ARG},
                                                    "datasets": {"type": "string",
                                                                 "description": "Comma list of reaction:id (or ids)."},
                                                    "experiments": {"type": "string",
                                                                    "description": "Comma list of experiment names."},
                                                    "plot": {"type": "boolean", "default": True},
                                                    "export_png": {"type": "boolean", "default": False},
                                                    "replicas": REPLICAS_ARG,
                                                    "credible_interval": {"type": "number", "default": 95,
                                                                          "exclusiveMinimum": 0, "maximum": 100,
                                                                          "description": "Equal-tailed pointwise credible interval (%)."}},
                   "required": ["collection_id"]},
        "meta": WIDGET_META,
        "fn": lambda a: analyze_by_experiment(a.get("collection_id", ""), a.get("experiments", ""),
                                              a.get("plot", True), credible_interval=a.get("credible_interval", 95),
                                              datasets=a.get("datasets", ""), export_png=a.get("export_png", False),
                                              replicas=a.get("replicas"))},
    "analysis_result": {
        "desc": "Wait for a long analysis that answered status running (analyze_by_experiment, "
                "campaign_pdf_plots): returns its result when done, or running again after about 40 s. "
                "Call it again with the same job_id until it is done; never restart the analysis.",
        "schema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
        "meta": WIDGET_META,
        "fn": lambda a: analysis_result(a.get("job_id", ""))},
    "rerun_replicas": {
        "desc": "Run replicas of a campaign again under their own numbers: replicas='failed' (default) "
                "takes every replica that failed, did not fit or left no output; or give numbers such as "
                "'3,7,12' or '10-12'. Replicas still queued or running are never resubmitted, and earlier "
                "results move to previous/ (kept, not deleted). Without confirm it returns the plan and a "
                "confirmation phrase; show the user the plan and submit with confirm=<phrase> only after they "
                "agree. Afterwards postprocess_campaign updates the summary. (append_replicas adds new "
                "replica numbers instead.)",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string", "description": COLLECTION_ARG},
                                                    "replicas": {"type": "string", "default": "failed"},
                                                    "confirm": {"type": "string"}},
                   "required": ["collection_id"]},
        "fn": lambda a: rerun_replicas(a.get("collection_id", ""), a.get("replicas", "failed"), a.get("confirm", ""))},
    "campaign_logs": {
        "desc": "Logs of a campaign: each replica's state (fitted, not fitted, failed, running, no "
                "output) and, for a replica that did not fit, the Python error and the end of its "
                "Slurm log. Use it whenever replicas fail or did not fit, and explain the error to the "
                "user.",
        "schema": {"type": "object", "properties": {
            "collection_id": {"type": "string", "description": COLLECTION_ARG},
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
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string", "description": COLLECTION_ARG},
                                                    "count": {"type": "integer"},
                                                    "confirm": {"type": "string"}},
                   "required": ["collection_id", "count"]},
        "fn": lambda a: append_replicas(a.get("collection_id", ""), a.get("count", 0), a.get("confirm", ""))},
    "cancel_campaign": {
        "desc": "Stop a campaign: cancel its queued and running Slurm tasks. The first call returns "
                "the plan and a confirmation phrase; nothing is cancelled until the user confirms "
                "and the call is repeated with confirm = the phrase.",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string", "description": COLLECTION_ARG},
                                                    "confirm": {"type": "string"}},
                   "required": ["collection_id"]},
        "fn": lambda a: cancel_campaign(a.get("collection_id", ""), a.get("confirm", ""))},
    "delete_campaign": {
        "desc": "Remove a campaign from Collections (delete_folder also removes its folder and "
                "results). Refused while any of its tasks are in the Slurm queue: stop it first "
                "with cancel_campaign. Needs the confirmation phrase from a first call.",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string", "description": COLLECTION_ARG},
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
    "Published-analysis catalog questions use list_analysis_catalog, get_analysis_catalog and "
    "get_analysis_requirements. These are read-only, need no HPC/workspace, and do not start "
    "a fit. Prefer summaries and targeted selectors; keep numerical configuration server-side and "
    "reuse catalog_ref instead of retrieving/retransmitting all parameters. Full definitions/export "
    "are opt-in. Follow pagination; use current requirements rather than historical unresolved notes. "
    "Only follow the campaign workflow below when the user requests a fitting campaign.\n"
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
    + ("3. Non-perturbative objects and parameters: Unpolarized PDF, Pion PDF and the fragmentation functions "
       "(pion, kaon, total hadron), alone or combined, as campaign_options lists them. "
       if FF_EXECUTION_ENABLED else
       "3. Non-perturbative objects and parameters: Unpolarized PDF, Pion PDF, or both. ") +
    "campaign_options returns each object's library: FREE/FIXED/tied, value, min, max. "
    "Pion follows its final-fit reference with strictly one shape; never add second-shape pion "
    "parameters. Fitted pion temporarily selects DIS 0, including combined campaigns; otherwise 1. "
    "Use objects:[{type,parameters},...] (legacy object is still accepted). Leading neutron adds "
    "the fitted p->pi,n companion and a fixed proton pdf unless pdf is selected. Ask which to change; "
    "only changed rows are written into the card. A parameter switched to FREE needs a min "
    "and max from the user. A parameter can also be set equal to another parameter in the same object "
    "(equal_to): it then follows that parameter's value and is not fitted on its own.\n"
    "4. Reactions: offer the enabled, compatible choices from campaign_options for the selected "
    "objects; ask the user which to include. Never preselect reactions or datasets.\n"
    "5. Datasets and cuts: campaign_datasets per reaction, grouped by experiment; ask which "
    "ids. A dataset listed with a 'normalization' has a normalization parameter in the library: "
    "offer to keep it, change its value or range, set it FREE or FIXED, or make it equal to "
    "another selected dataset's normalization (normalizations in the card). Show the "
    "reaction's cuts (e.g. Q2>1.69, W2>10) and ask whether to keep them. "
    "Preserve dataset-specific cut mappings, including distinct H1 and ZEUS thresholds.\n"
    "6. QCD settings: order, Q20, TMC, higher twist, DGLAP mode, alphaS mode.\n"
    "7. Job config: campaign name, replicas, parameter initialization (library values or "
    "random within bounds), inference (ensemble of pseudo-data replicas or central values), "
    "tolerance, max iterations, optimizer — and the Slurm resources per replica (time, "
    "memory, cpus, partition or auto), using slurm_partitions to explain the choice.\n"
    "Every setting shows its library value; the user keeps or changes it. Reactions and "
    "datasets are never preselected; never pick a value for the user.\n"
    + ("8. Review: SIDIS (PDFs plus FFs) and SIA (FFs only) campaigns are fitted like the others. For every fit, "
       if FF_EXECUTION_ENABLED else
       "8. Review: SIDIS/SIA datasets are configuration-only: validate_campaign returns an input "
       "fragment, configuration_valid and fit_ready=false. Do not attempt submission or claim FF fits "
       "are implemented. SIDIS needs PDFs plus FFs; SIA needs FFs only. For supported fits, ") +
    "validate_campaign; show the summary, its warnings (they never block "
    "submission) and the Slurm array script exactly as returned (not shortened); submit with "
    "submit_campaign(card, confirm=<phrase>) only after the user agrees.\n"
    "9. Collections and analysis: list_campaigns (it also returns the user's Slurm queue), "
    "campaign_status, postprocess_campaign, campaign_summary (fit summary), campaign_pdf_plots "
    "(PDF curves as an interactive chart), analyze_by_experiment (per-dataset chi2 table and data vs "
    "theory charts), campaign_logs (each replica's state and log), append_replicas (always "
    "confirmed with its phrase), cancel_campaign (stops its queued and running tasks) and "
    "delete_campaign (only when nothing of it is queued) — both also confirmed with their "
    "phrase. rerun_replicas runs failed or unfitted replicas again under their own numbers (also "
    "confirmed with its phrase). The analysis tools (campaign_summary, analyze_by_experiment, "
    "campaign_pdf_plots, campaign_ff_plots) take replicas, a selection of the fitted replicas "
    "(best N, chi2_max, sigma outlier cut, exclude, only), when the user wants statistics over some "
    "replicas only; say how many were kept and why the others were left out. "
    "A replica counts as fitted only when its optimization ran: succeeded counts those, "
    "not_fitted counts replicas whose optimization failed (FitPack then only computed chi2 at "
    "the starting values). When replicas did not fit or failed, call campaign_logs and explain "
    "the error to the user in plain words. The panel's Collections and Analysis tabs show the "
    "same (Analysis has a Logs button).\n"
    "Results and plots: when the user names a campaign, datasets or experiments, call the analysis "
    "tool directly with those names as said (collection_id takes the campaign name; datasets and "
    "experiments take the user's words) — do not look them up first with list_campaigns or "
    "campaign_summary. A result with status running carries a job_id: call analysis_result(job_id) "
    "until it is done; never start the same analysis again. status no_match lists what the campaign "
    "has: pick from it. Charts are interactive in the panel and the chat widget; ask for PNGs "
    "(export_png) only when the user wants files or the client shows no widget.\n"
    "10. Any other cluster job: draft_slurm_job with the resources agreed with the user (it "
    "runs sbatch --test-only), show the returned script exactly as written, ask the user, "
    "then submit_slurm_job with the phrase.")

# ── plugins ─────────────────────────────────────────────────────────────────
# Other physics tasks (plugins/<name>/, see plugins/__init__.py) add their own
# tools, resources and instructions here. The JAM tools above are not touched;
# a plugin that fails to load is left out and the gateway runs without it.

PLUGIN_RESOURCES = {}
PLUGIN_PAGES = {}                   # plugin name -> its browser page (GET ?app=<name>)
PLUGIN_STATUS = {"loaded": [], "errors": {}}


def _page_url(app, query):
    q = urllib.parse.urlencode({"app": app, **{k: v for k, v in query.items() if v}})
    return (PUBLIC_URL or f"{PROXY}/cloud/{MCP_NAME}/mcp") + "?" + q


_PLUGINS_INIT = os.path.join(HERE, "plugins", "__init__.py")
if os.path.isfile(_PLUGINS_INIT):
    try:
        _spec = importlib.util.spec_from_file_location("jam_plugins", _PLUGINS_INIT)
        _plugins = importlib.util.module_from_spec(_spec)
        sys.modules["jam_plugins"] = _plugins
        _spec.loader.exec_module(_plugins)
        PLUGIN_RESOURCES, PLUGIN_PAGES, _routing, _plugin_text, PLUGIN_STATUS = _plugins.attach(
            TOOLS, {PANEL_URI, PANEL_URI_SKYBRIDGE}, _page_url)
        if _routing:
            # Models choose tools by what they read first: with several tasks on
            # one gateway, say up front which questions go where.
            INSTRUCTIONS = (
                "This gateway serves independent physics tasks. Decide the task from the user's "
                "question, then use only that task's tools; never start one task's steps for "
                "the other's question:\n" + "\n".join(_routing) + "\n"
                "- JAM (every other tool): global QCD analysis with FitPack on ifarm: proton and "
                "pion PDFs, fragmentation functions, reactions, datasets and cuts, fitting "
                "campaigns, their results and plots, other Slurm jobs. Follow the JAM steps below.\n"
                "If the question could belong to either task, ask the user which one they mean.\n\n"
                + INSTRUCTIONS + _plugin_text)
    except Exception as e:
        PLUGIN_STATUS = {"loaded": [], "errors": {"plugins": f"{type(e).__name__}: {e}"}}

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


CHARTS_FILE = os.path.join(HERE, "jamai", "static", "jamcharts.js")
CHARTS_MARK = "/*@jamcharts*/"


def inline_charts(html):
    """A page with the shared chart code (jamai/static/jamcharts.js) in place of
    its /*@jamcharts*/ mark: the JAM panel here, the SBI dashboard in its plugin."""
    if CHARTS_MARK not in html:
        return html
    with open(CHARTS_FILE, encoding="utf-8") as fh:
        return html.replace(CHARTS_MARK, fh.read(), 1)


def _panel_html():
    try:
        with open(PANEL_FILE, encoding="utf-8") as fh:
            return inline_charts(fh.read())
    except OSError:
        return _FALLBACK_PANEL


def _tool_entry(name, spec):
    entry = {"name": name, "description": spec["desc"], "inputSchema": spec["schema"]}
    if spec.get("meta"):
        entry["_meta"] = spec["meta"]
    if spec.get("annotations"):
        entry["annotations"] = spec["annotations"]
    return entry


def _tool_result(out, is_error=False, compact=False):
    images, ui = [], None
    if isinstance(out, dict):
        # Plots travel as MCP image content (the model sees them; MCP Apps
        # hosts and the panel render them). "_ui" is for the panel and the chat
        # widget only (chart data, PNG data URLs): it goes in _meta, which
        # hosts hand to the widget and keep out of the model's context.
        out = dict(out)
        images = out.pop("_images", None) or []
        ui = out.pop("_ui", None)
        if "_panel_plots" in out:           # the older spelling of _ui's panel_plots
            ui = dict(ui or {}, panel_plots=out.pop("_panel_plots"))
        text_view = out
    else:
        text_view = out
    text = (json.dumps(text_view, separators=(",", ":")) if compact else json.dumps(text_view, indent=2)) if isinstance(text_view, (dict, list)) else str(text_view)
    res = {"content": [{"type": "text", "text": text}] +
                      [{"type": "image", "data": i["data"], "mimeType": i.get("mimeType", "image/png")}
                       for i in images],
           "isError": bool(is_error)}
    if isinstance(out, dict):
        res["structuredContent"] = out
    if ui is not None:
        res["_meta"] = {"jam/ui": ui}
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
            # ?app=<plugin> is that plugin's page; every other page request gets the JAM panel.
            query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
            page = PLUGIN_PAGES.get(query.get("app", ""))
            try:
                # A page(query) sees the URL's query (?app=<name>&view=...); page() does not.
                takes_query = page is not None and len(inspect.signature(page).parameters) > 0
                body = (page(query) if takes_query else page() if page else _panel_html()).encode()
            except Exception:
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
                 "description": "FemtoMind operations panel (ChatGPT widget)"}] + [
                {"uri": uri, "name": r.get("name", uri), "mimeType": r["mimeType"],
                 "description": r.get("description", "")} for uri, r in PLUGIN_RESOURCES.items()]}
        elif method == "resources/read":
            uri = params.get("uri", "")
            mime = {PANEL_URI: PANEL_MIME,
                    PANEL_URI_SKYBRIDGE: "text/html+skybridge"}.get(uri)
            plugin_res = PLUGIN_RESOURCES.get(uri) if isinstance(uri, str) else None
            if plugin_res:
                try:
                    result = {"contents": [{"uri": uri, "mimeType": plugin_res["mimeType"],
                                            "text": plugin_res["text"](),
                                            "_meta": plugin_res.get("meta") or {}}]}
                except Exception as e:
                    error = {"code": -32603, "message": f"resource unavailable: {uri} ({type(e).__name__})"}
            elif not mime:
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
                            result = _tool_result(spec["fn"](args), compact=spec.get("compact", False))
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
    if PLUGIN_STATUS["loaded"] or PLUGIN_STATUS["errors"]:
        print(f"plugins loaded: {', '.join(PLUGIN_STATUS['loaded']) or 'none'}"
              + "".join(f"; {n} left out ({why})" for n, why in PLUGIN_STATUS["errors"].items()),
              flush=True)
    if os.environ.get("SLURM_JOB_ID"):
        register(port)
    server.serve_forever()
