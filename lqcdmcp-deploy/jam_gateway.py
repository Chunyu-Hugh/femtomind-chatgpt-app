"""jam_gateway — one person's MCP gateway for JAM fits on an HPC site.

Launched by its owner as a Slurm job on the LQCDMCP platform (redeploy_jam.py
or the dashboard page), it registers with the proxy as jam-gateway-<user> and
serves, at https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<user>/mcp:
  * MCP tools for AI clients (Claude Code, VS Code, Claude Desktop, ...),
  * the operations panel (jam_panel.html): Connect / Project / Campaign /
    Collections / Analysis, as a browser page and as an MCP Apps resource.
Stdlib only; plain JSON-RPC over HTTP.

Access: every call needs the owner's LQCDMCP dashboard token (Bearer). The
gateway asks the dashboard which MCP servers the token's user launched and
accepts the token when this gateway is among them. The proxy itself checks
nobody, so this is what keeps the owner's open cluster session private.

Cluster: a two-hop SSH chain per user (login host with PIN + one-time code,
then the compute host with the password), opened on the panel's Connect tab;
credentials are never taken in chat. Everything site-specific is in SITE
(ifarm built in; ~/.jam_gateway/site.json overrides).

Fits: a campaign is the JAM app's composer (object and parameter table,
reactions, datasets and cuts, QCD settings, job config, review). jamai/
generates the card from the repository's examples/input02.py plus the
choices, and a Slurm array runs one replica per task through fitlib/ai.py
(FitPack `ai` branch) inside the site's container. Analysis: postprocess,
fit summary, PDF plots, chi2 and data vs theory by experiment, more replicas
(with a confirmation phrase). Any other job: LLM-guided Slurm drafts checked
with sbatch --test-only.

Sections, in order: constants and site · local and remote helpers · access ·
cluster chain · tools · campaigns · tool registry · http · boot.
"""

import base64
import getpass
import hashlib
import hmac
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

PROXY = "https://lqcdmcp.jlab.org:8000"
HERE = os.path.dirname(os.path.abspath(__file__))
CM_DIR = os.path.expanduser("~/fm-cm")          # ControlMaster sockets
FA_FWD_PORT_BASE = 2222                          # + crc32(user) % 400

# One instance per person: the owner is the account the job runs under, and
# the proxy route is the job name (jam-gateway-<user> from redeploy_jam.py).
GATEWAY_OWNER = os.environ.get("JAM_GW_OWNER") or getpass.getuser()
MCP_NAME = (os.environ.get("JAM_GW_NAME") or os.environ.get("SLURM_JOB_NAME")
            or "jam-gateway")
# Several people's gateways can share a node, so 8080 is only a first choice.
PORT = int(os.environ.get("JAM_GW_PORT") or 0)
BIND = os.environ.get("JAM_GW_BIND", "0.0.0.0")   # 127.0.0.1 for a local test run
# Optional second entry on port 443 (e.g. a cloudflared quick tunnel to the
# proxy) for clients or networks that refuse port 8000.
TUNNEL_URL = os.environ.get("JAM_GW_TUNNEL_URL", "").rstrip("/")
# Where users reach this gateway's /mcp endpoint, for the panel links it hands
# out. Defaults to its route on the LQCDMCP proxy; a gateway run elsewhere (a
# local test) sets its own, e.g. http://host:8090/mcp.
PUBLIC_URL = os.environ.get("JAM_GW_PUBLIC_URL", "").rstrip("/")

# Where this instance keeps its tokens, log and site override (mode 700).
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
    # The user's own area: workspaces, the marker file and the LHAPDF link.
    "area": "/w/ceph24/JAM/{user}",
    # fs_list / fs_read may look anywhere under these.
    "read_roots": ["/w/ceph24"],
    "container": {"image": "/u/scigroup/scicomp/jupyterhub/theory/theory-notebook.sif",
                  "binds": ["/w/ceph24"],
                  "pythonpath": "/usr/local/lib/python3.11/site-packages"},
    # <area>/lhapdf_tables is linked here when missing, and these are searched after it.
    "lhapdf_link": "/w/ceph24/JAM/chunyu/lhapdf_tables",
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
    "test_partition": "priority",
    # Shown by cluster_status; measured on the JAM24 reproduction ladder.
    "hints": ["warm start: ~40 min @ 4 cores (2 h budget -> priority)",
              "cold start: budget 10h @ 4 cores (-> production)",
              "reference: step7 warm converges to chi2/N ~ 1.452"],
    # Every card the gateway derives reads its grids from here.
    "grid_root": "/w/ceph24/JAM/share/fitpack_grids",
    # The analysis template, as in RedstarJAM: the user picks reactions, then
    # datasets, and the fit card is generated from this one, keeping only what
    # was picked. It ships with the repository (unpolarized PDF setup).
    "template_card": "examples/input02.py",
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


def _area(user):
    return SITE["area"].format(user=user)

# Card dataset-line pattern: conf['datasets']['<reac>']['xlsx'][<id>] = ...
# Reaction names can carry a hyphen (dy-pion, dy-kaon). v6 matched them with
# \w+, so those datasets never reached the catalog and could not be dropped.
DATASET_LINE_RE = r"conf\['datasets'\]\['([\w-]+)'\]\['xlsx'\]\[(\w+)\]\s*="

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

# Cards are named by their path relative to <workspace>/inputs, at most one
# folder deep: "input_pdf_fit.py" or "jam24/input_jam24_s7_warm.py". A bare
# file name is also found in any folder of inputs/, so older names keep working.
CARD_RE = r"(?:[A-Za-z0-9._-]{1,64}/)?[A-Za-z0-9._-]{1,80}\.py"
DERIVED_DIR = "derived"          # inputs/derived/ holds the cards the gateway writes
# Files the JAM repository does not carry (fit driver, path bootstrap, the
# JAM24 cards), laid out as in a workspace. setup_workspace and use_workspace
# write each one that is missing; a file already there is never overwritten.
WORKSPACE_FILES = os.environ.get("JAM_GW_WORKSPACE_FILES", os.path.join(HERE, "workspace_files"))

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
    Recorded in <area>/.jam_workspace at setup time; the default is the
    conventional clone target."""
    if user not in _WS_CACHE:
        area = _area(user)
        r = _remote(user, f"cat {area}/.jam_workspace 2>/dev/null", timeout=30)
        _WS_CACHE[user] = (r.get("out") or "").strip() or f"{area}/fitpack_legacy"
    return _WS_CACHE[user]


def _paths(user):
    """(workspace root, runs root, logs dir) — cards live under ws/inputs,
    runs live INSIDE the clone at ws/runs."""
    ws = _workspace(user)
    return ws, f"{ws}/runs", f"{ws}/runs/logs"


# Prepended to every snippet that opens a card: card_path(name) finds a card
# by its path under inputs/, or a bare file name in any folder of inputs/.
_CARD_FIND = r'''
import os as _os, glob as _glob
_INPUTS = __INPUTS__
def card_path(name):
    # The repository's own example cards are named from the workspace root.
    if name.startswith('examples/'):
        p = _os.path.join(_os.path.dirname(_INPUTS), name)
        if _os.path.isfile(p):
            return p
        raise SystemExit('CARD-NOT-FOUND ' + name)
    p = _os.path.join(_INPUTS, name)
    if _os.path.isfile(p):
        return p
    if '/' not in name:
        hits = sorted(_glob.glob(_os.path.join(_INPUTS, '*', name)))
        if hits:
            return hits[0]
    raise SystemExit('CARD-NOT-FOUND ' + name)
def card_name(path):
    return _os.path.relpath(path, _INPUTS)
'''


def _card_code(user):
    return _CARD_FIND.replace("__INPUTS__", repr(f"{_paths(user)[0]}/inputs"))


def _card_missing(r, card):
    """A tool result for a card that is not in the workspace, or None."""
    if "CARD-NOT-FOUND" in (r.get("err") or "") + (r.get("out") or ""):
        return {"status": "error", "card": card,
                "message": f"No card named {card} in the workspace's inputs/ folder. "
                           "list_runs shows the cards that are there."}
    return None


def _container_exec(fp, extra_path="", env=None):
    """apptainer exec prefix that runs python3 inside the site's image with the
    workspace (and extra_path, if any) on the path."""
    c = SITE["container"]
    binds = " ".join(f"--bind {b}" for b in c.get("binds", []))
    pypath = fp + (f":{extra_path}" if extra_path else "")
    envs = "".join(f" --env {k}={_shq(str(v))}" for k, v in (env or {}).items())
    return (f"apptainer exec {binds} --env PYTHONPATH={pypath} --env FITPACK={fp}{envs} "
            f"{c['image']} python3")


def _remote(user, cmd, timeout=120, cap=4000):
    """Run a shell command on the compute host through the user's chain."""
    _, fa = _socks(user)
    return _run(["ssh", "-S", fa, "-p", str(_fwd_port(user)), f"{user}@localhost",
                 "bash -lc " + _shq(cmd)], timeout=timeout, cap=cap)


def _run_ifarm_py(user, code, apptainer=False, timeout=300, cap=4000, extra_path="", env=None):
    """Ship a python snippet to the compute host as base64 and run it (avoids
    all shell-quoting issues). apptainer=True runs inside the site's image
    (needed for fitpack/numpy imports)."""
    b64 = base64.b64encode(code.encode()).decode()
    if apptainer:
        run = f"echo {b64} | base64 -d | {_container_exec(_paths(user)[0], extra_path, env)} -"
    else:
        run = f"echo {b64} | base64 -d | python3"
    return _remote(user, run, timeout=timeout, cap=cap)


def _resolve_outdir(user, outdir):
    if outdir.startswith("/"):
        return outdir
    return f"{_paths(user)[1]}/{outdir}"


# ── access ──────────────────────────────────────────────────────────────────
#
# A request carries "Authorization: Bearer <dashboard token>". A token of the
# owner resolves to the owner, and every tool acts as the owner and nobody
# else. A username argument is still accepted when it matches, so clients that
# pass one keep working; one that names somebody else is refused.

_tls = threading.local()
USERNAME_RE = r"[A-Za-z0-9._-]{1,32}"
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
    """Resolve a bearer token to (username, role), or ('', '') if unusable."""
    if not auth_header or auth_header[:7].lower() != "bearer ":
        return "", ""
    token = auth_header[7:].strip()
    if not token:
        return "", ""
    if TEST_TOKEN and hmac.compare_digest(token, TEST_TOKEN):
        return GATEWAY_OWNER, "owner"
    return _dashboard_identify(token, _sha(token))


# ── dashboard tokens ─────────────────────────────────────────────────────────
#
# The LQCDMCP dashboard token (Globus login) opens this gateway: the
# dashboard is asked which MCP servers the token's user launched, and the
# token is the owner's when that list contains this gateway. The proxy itself
# forwards /cloud/<name>/mcp without checking anyone, so this check is what
# keeps other people off a gateway whose cluster chain is open.

DASHBOARD_URL = os.environ.get("JAM_GW_DASHBOARD_URL", f"{PROXY}/jlab/")
DASHBOARD_AUTH = os.environ.get("JAM_GW_DASHBOARD_AUTH", "1") != "0"
_DASH_CACHE = {}                    # token hash -> (identity tuple, expires at)
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


def _dashboard_text(resp):
    res = (resp or {}).get("result") or {}
    parts = [c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict)]
    if res.get("structuredContent") is not None:
        parts.append(json.dumps(res["structuredContent"]))
    return " ".join(parts)


def _dashboard_owns_gateway(token):
    """True when the dashboard says this token's user launched this gateway."""
    _, sid = _dashboard_call(token, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                   "clientInfo": {"name": MCP_NAME, "version": "1"}})
    # The dashboard expects validate_user before other calls in a session.
    _dashboard_call(token, "tools/call", {"name": "validate_user",
                                          "arguments": {"username": GATEWAY_OWNER}}, sid)
    resp, _ = _dashboard_call(token, "tools/call", {"name": "get_my_mcp_servers",
                                                    "arguments": {"unused": ""}}, sid)
    if (resp.get("result") or {}).get("isError"):
        return False
    return re.search(r"(?<![\w-])" + re.escape(MCP_NAME) + r"(?![\w-])", _dashboard_text(resp)) is not None


def _dashboard_identify(token, h):
    if not DASHBOARD_AUTH or len(token) < 16:
        return "", ""
    now = time.time()
    with _DASH_LOCK:
        hit = _DASH_CACHE.get(h)
        if hit and hit[1] > now:
            return hit[0]
    try:
        ident = (GATEWAY_OWNER, "owner") if _dashboard_owns_gateway(token) else ("", "")
        ttl = 600 if ident[0] else 60
    except Exception as e:
        _hlog(f"dashboard check failed: {type(e).__name__}")
        ident, ttl = ("", ""), 15
    with _DASH_LOCK:
        if len(_DASH_CACHE) > 256:
            _DASH_CACHE.clear()
        _DASH_CACHE[h] = (ident, now + ttl)
    return ident


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


def _chain_alive(u):
    gw, fa = _socks(u)
    return (_master_alive(gw, f"{u}@{SITE['login_host']}")
            and _master_alive(fa, f"{u}@localhost", ["-p", str(_fwd_port(u))]))


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

def connect_hpc(username="", pin_otp="", password=""):
    u, err = _resolve_user(username)
    if err:
        return dict(err, connected=False)
    os.makedirs(CM_DIR, mode=0o700, exist_ok=True)
    gw, fa = _socks(u)
    gwd = f"{u}@{SITE['login_host']}"
    port = str(_fwd_port(u))
    # reuse FIRST — never unlink a live socket
    if _master_alive(gw, gwd) and _master_alive(fa, f"{u}@localhost", ["-p", port]):
        check = _remote(u, "hostname; whoami; date -u", timeout=30)
        return {"status": "reused", "connected": True, "username": u,
                "note": "existing ControlMasters alive — no OTP needed",
                "remote": check["out"][:200], "check": check["rc"]}
    for s in (gw, fa):
        if os.path.exists(s):
            try:
                os.unlink(s)
            except OSError:
                pass
    if not pin_otp or not password:
        return {"status": "error", "connected": False, "username": u,
                "message": f"no live chain for {u}; need pin_otp (PIN+one-time code, "
                           "no space) and password. Alternatively give the user the "
                           "panel page to sign in interactively (see get_console)"}
    hop_opts = ["-o", "ControlPersist=8h", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "NumberOfPasswordPrompts=2", "-o", "PubkeyAuthentication=no",
                "-o", "PreferredAuthentications=keyboard-interactive,password"]
    r1 = _run(["setsid", "ssh", "-M", "-S", gw, *hop_opts, "-fN", gwd],
              timeout=40, secret=pin_otp)
    if not _master_alive(gw, gwd):
        return {"status": "error", "connected": False, "step": "gateway", "username": u,
                "message": f"{SITE['login_host']} failed: {r1['err'][-400:] or r1['out'][-200:]}"}
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
    q = "?ui=panel&tab=" + urllib.parse.quote(tab)
    urls = [{"entry": "direct", "url": (PUBLIC_URL or f"{PROXY}/cloud/{MCP_NAME}/mcp") + q}]
    if TUNNEL_URL:
        urls.append({"entry": "port-443 tunnel (Claude Desktop / restricted networks)",
                     "url": f"{TUNNEL_URL}/cloud/{MCP_NAME}/mcp" + q})
    return urls


def get_console(tab="connect"):
    tab = tab if tab in PANEL_TABS else "connect"
    return {
        "panel_urls": _panel_urls(tab),
        # Older agent prompts ask for these two keys; both now open the panel.
        "console_urls": _panel_urls("connect"),
        "composer_urls": _panel_urls("campaign"),
        "guidance": "Give the user a panel_url to open in a browser. The panel has five "
                    "tabs: Connect (sign in to the cluster), Project (check or create the "
                    "workspace), Campaign (pick datasets and QCD settings, then submit), "
                    "Collections (runs so far) and Analysis (chi2 history, comparison with "
                    "the paper, job log). Cluster credentials are typed only on that page, "
                    "never into chat. The page asks for the user's gateway token once."}


def check_hpc(username=""):
    u, err = _need_user(username)
    if err:
        return err
    out = _remote(u, f"hostname; squeue -u {u} -o '%.10i %.9P %.12j %.2t' | head -8; "
                  f"echo ---; ls {_area(u)} 2>&1 | head -10", timeout=90)
    return {"status": "ok", "username": u, **out}


def submit_test_fit(username=""):
    """Trivial fire-and-forget chain smoke test (kept for first-time checks)."""
    u, err = _need_user(username)
    if err:
        return err
    script = (f"#!/bin/bash\n#SBATCH --partition={SITE['test_partition']}\n"
              "#SBATCH --time=00:05:00\nhostname; date; sleep 30\n")
    b64 = base64.b64encode(script.encode()).decode()
    out = _remote(u, f"mkdir -p ~/.jam_gateway && echo {b64} | base64 -d > ~/.jam_gateway/chain_test.sh && "
                  "nohup bash -c 'sbatch ~/.jam_gateway/chain_test.sh' "
                  ">~/.jam_gateway/chain_test.log 2>&1 </dev/null & echo FIRED", timeout=15)
    return {"status": "fired", "username": u, **out}


def get_submit_result(username=""):
    u, err = _need_user(username)
    if err:
        return err
    out = _remote(u, "sleep 3; echo --- submit log ---; cat ~/.jam_gateway/chain_test.log 2>&1; "
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
        f"echo '--- cards ---'; cd {fp}/inputs 2>&1 && ls *.py */*.py 2>/dev/null | head -40; "
        f"echo '--- run dirs ---'; ls {rr}/001 2>&1 | head -20; "
        f"echo '--- msr files (newest 8) ---'; "
        f"find {rr} -name '*.msr' -printf '%T@ %p\\n' 2>/dev/null | sort -rn | head -8 | cut -d' ' -f2-",
        timeout=60)
    return {"status": "ok", "username": u, **out}


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


def fs_write(path="", content="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    p = os.path.normpath(path.strip())
    root = _area(u)
    if not p.startswith(root + "/"):
        return {"status": "error", "username": u,
                "message": f"write restricted to your own area under {root}"}
    b64 = base64.b64encode((content or "").encode()).decode()
    out = _remote(u, f"mkdir -p {_shq(os.path.dirname(p))} && "
                  f"echo {b64} | base64 -d > {_shq(p)} && wc -c {_shq(p)}", timeout=60)
    return {"status": "ok", "username": u, "path": p, **out}


def setup_workspace(source="", repo_url="", github_token="",
                     target_dir="", username="", folder_name="", branch=""):
    """git clone the JAM code to target_dir (default <area>/fitpack_legacy),
    record it as THE workspace, create <workspace>/runs and add the missing
    cards — one folder holds code, cards AND runs.

    repo_url defaults to the JAM repository. source=<username> copies that
    user's recorded workspace instead of cloning. folder_name is the panel's
    way of naming target_dir inside the user's own area; branch picks the
    branch to clone."""
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
    # The FitPack `ai` branch carries fitlib/ai.py, which runs campaigns (as
    # the JAM app's "Clone / Sync FitPack" does); another branch on request.
    br = (branch or "").strip() or ("ai" if (repo_url or "").strip() in ("", JAM_REPO_URL) else "")
    if br and not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", br):
        return {"status": "error", "ok": False,
                "message": "Branch name may use letters, numbers, '.', '_', '/' and '-' only."}
    srcu = (source or "").strip()
    if srcu and not re.fullmatch(USERNAME_RE, srcu):
        return {"status": "error", "ok": False, "message": "invalid source username"}
    steps = []
    exists = _remote(u, f"test -d {_shq(target)} && echo YES || echo NO", timeout=30)
    if "YES" in (exists.get("out") or ""):
        steps.append("workspace already present — skipped clone")
    elif srcu:
        # Read the source user's workspace marker through the CALLER's own
        # chain; the source user's chain is theirs, not ours to use.
        src_area = _area(srcu)
        mark = _remote(u, f"cat {src_area}/.jam_workspace 2>/dev/null", timeout=30)
        src_ws = (mark.get("out") or "").strip() or f"{src_area}/fitpack_legacy"
        out = _remote(u, f"set -o pipefail; mkdir -p {_shq(os.path.dirname(target))} && "
                      f"cp -r {_shq(src_ws)} {_shq(target)} 2>&1 | tail -3",
                      timeout=900)
        steps.append(f"cp from {srcu}: rc={out['rc']} {out['err'][-200:]}")
        if out["rc"] != 0:
            return {"status": "error", "ok": False, "username": u, "steps": steps,
                    "message": f"Copy from {srcu} failed.", "detail": out["err"][-300:]}
    else:
        url = repo_url.strip() or JAM_REPO_URL
        tok = (github_token or "").strip()
        auth_url = url.replace("https://", f"https://{tok}@", 1) \
            if tok and url.startswith("https://") else url
        bflag = f"-b {_shq(br)} " if br else ""
        # pipefail: without it the exit status is tail's, and a failed clone
        # was reported as success. GIT_TERMINAL_PROMPT=0 makes a private repo
        # without a token fail at once instead of waiting out the timeout.
        out = _remote(u, f"set -o pipefail; mkdir -p {_shq(os.path.dirname(target))} && "
                      f"GIT_TERMINAL_PROMPT=0 git clone {bflag}{_shq(auth_url)} "
                      f"{_shq(target)} 2>&1 | tail -3", timeout=900)
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
        if tok:
            # The token was only for this clone; keep it out of .git/config.
            _remote(u, f"git -C {_shq(target)} remote set-url origin {_shq(url)}", timeout=30)
    out = _remote(u, f"mkdir -p {_shq(target)}/runs/logs {_shq(target)}/inputs && "
                  f"printf '%s\n' {_shq(target)} > {root}/.jam_workspace && "
                  f"{_lhapdf_link_cmd(root)}; echo DONE", timeout=60)
    steps.append("runs tree + workspace marker: " + (out.get("out") or "").strip()[-60:])
    steps.append(_link_grids(u, target))
    steps.append(_install_workspace_files(u, target))
    _WS_CACHE.pop(u, None)
    return {"status": "ok", "ok": True, "username": u, "workspace": target,
            "steps": steps, "message": f"Workspace ready at {target}.",
            "report": check_project(u)}


def _lhapdf_link_cmd(root):
    """Shell fragment: link <area>/lhapdf_tables to the site's shared tables
    unless something is already there."""
    src = SITE.get("lhapdf_link") or ""
    if not src:
        return "true"
    return (f"{{ [ -e {root}/lhapdf_tables ] || "
            f"ln -s {_shq(src)} {root}/lhapdf_tables; }}")


def _link_grids(u, ws):
    """Link each of the site's grids into <ws>/grids/ unless something is
    already there. Returns a one-line summary for the setup steps."""
    links = SITE.get("grid_links") or {}
    if not links:
        return "grids: no links configured for this site"
    parts = []
    for name, target in sorted(links.items()):
        dest = _shq(f"{ws}/grids/{name}")
        parts.append(f"if [ -e {dest} ] || [ -L {dest} ]; then echo KEPT; "
                     f"elif [ -e {_shq(target)} ]; then mkdir -p $(dirname {dest}) && "
                     f"ln -s {_shq(target)} {dest} && echo LINKED; else echo NOSRC {_shq(name)}; fi")
    r = _remote(u, "; ".join(parts), timeout=60)
    out = r.get("out") or ""
    msg = (f"grids: {out.count('LINKED')} linked, {out.count('KEPT')} already there")
    missing = re.findall(r"NOSRC (\S+)", out)
    if missing:
        msg += f", source missing for: {', '.join(missing)}"
    return msg


def _workspace_files():
    """[(path relative to the workspace, local file)] shipped with the gateway."""
    out = []
    for dirpath, _, names in os.walk(WORKSPACE_FILES):
        for n in names:
            full = os.path.join(dirpath, n)
            rel = os.path.relpath(full, WORKSPACE_FILES).replace(os.sep, "/")
            if re.fullmatch(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+){0,3}", rel) and not n.endswith(".md"):
                out.append((rel, full))
    return sorted(out)


def _install_workspace_files(u, ws):
    """Write each shipped file that <ws> does not have yet. One call per file:
    a single shell argument is capped at 128 KiB."""
    files = _workspace_files()
    if not files:
        return "workspace files: none shipped with this gateway"
    added, kept, failed = [], [], []
    for rel, full in files:
        with open(full, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        p = _shq(f"{ws}/{rel}")
        r = _remote(u, f"if [ -e {p} ]; then echo KEPT; else mkdir -p $(dirname {p}) && "
                       f"echo {b64} | base64 -d > {p} && echo ADDED; fi", timeout=60)
        out = r.get("out") or ""
        (added if "ADDED" in out else kept if "KEPT" in out else failed).append(rel)
    msg = f"workspace files: {len(added)} added"
    if kept:
        msg += f", {len(kept)} already there (left as they are)"
    if failed:
        msg += f", FAILED: {', '.join(failed)}"
    return msg


def _site_public():
    """What the panel needs to label the sign-in steps for this site."""
    return {"id": SITE["id"], "label": SITE["label"],
            "hops": [{"host": SITE["login_host"], "secret": SITE["login_secret"]},
                     {"host": SITE["compute_host"], "secret": SITE["compute_secret"]}]}


def check_hpc_connection(username=""):
    """Cheap connection probe for the panel's Connect tab: is this user's
    two-hop chain open, and which workspace is recorded."""
    u, err = _resolve_user(username)
    if err:
        return {"connected": False, "status": err.get("status"), "note": err["message"]}
    alive = _chain_alive(u)
    account = {"username": u, "area": _area(u)}
    if alive:
        account["workspace"] = _workspace(u)
    return {"connected": alive, "master_open": alive, "cluster_reachable": alive,
            # connect_hpc rebuilds both hops whenever either is down, so the
            # one-time code is always needed again; say so.
            "gateway_open": False,
            "account": account, "site": _site_public(), "sites": [_site_public()],
            "note": ("Ready to run fits." if alive else
                     "Not signed in to the cluster. Sign in on the Connect tab of the "
                     "panel page; the chain stays open until the gateway's daily "
                     "window closes.")}


_PROJECT_ITEMS = [
    ("JAM code (fitpack_legacy)", ""), ("fitlib", "fitlib"), ("qcdlib", "qcdlib"),
    ("obslib", "obslib"), ("experimental data", "database"), ("run directory", "runs"),
    ("interpolation grids", "grids"), ("fit cards", "inputs")]


def check_project(username=""):
    """Workspace report for the Project tab: the recorded workspace, every
    checkout in the user's area, and whether each part is present."""
    u, err = _need_user(username)
    if err:
        return err
    root = _area(u)
    code = f"""
import os, json, glob
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
        if rel == 'inputs':
            # The folder alone is not enough: a fit needs at least one card.
            n = len([c for c in glob.glob(os.path.join(p, '*.py')) + glob.glob(os.path.join(p, '*', '*.py'))
                     if os.path.basename(c) != '__init__.py'])
            items.append({{'name': label, 'detail': p + ' — ' + str(n) + (' card' if n == 1 else ' cards'),
                          'ok': n > 0, 'cards': n}})
            continue
        items.append({{'name': label, 'detail': p, 'ok': os.path.isdir(p)}})
    lh = os.path.join(root, 'lhapdf_tables')
    items.append({{'name': 'LHAPDF tables', 'detail': lh, 'ok': os.path.exists(lh)}})
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
    for it in d["items"]:
        if not it["ok"]:
            if it["name"] == "LHAPDF tables":
                it["action"] = "Re-select the workspace with 'Use selected' to restore the link."
            elif "missing" in it:
                it["action"] = ("Missing: " + ", ".join(it["missing"]) + ". Re-select the workspace "
                                "with 'Use selected' to link the shared grids.")
            elif "cards" in it:
                it["action"] = ("No fit cards yet. Put a card (.py) in inputs/ or a folder inside it; "
                                "the JAM repository itself ships none.")
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
    out = _remote(u, f"test -d {_shq(p)}/fitlib && printf '%s\\n' {_shq(p)} > {root}/.jam_workspace "
                     f"&& mkdir -p {_shq(p)}/runs/logs {_shq(p)}/inputs "
                     f"&& {_lhapdf_link_cmd(root)} && echo USED", timeout=60)
    if "USED" not in (out.get("out") or ""):
        return {"ok": False, "message": f"{p} is not a fitpack checkout (no fitlib/ inside)."}
    _link_grids(u, p)
    _install_workspace_files(u, p)
    _WS_CACHE.pop(u, None)
    return {"ok": True, "workspace": p, "report": check_project(u)}



def _bad_card(*names):
    """Card names are paths under <workspace>/inputs, at most one folder deep."""
    for nm in names:
        if not nm:
            return {"status": "error",
                    "message": "Name a card. list_runs shows the cards in your workspace."}
        if not re.fullmatch(CARD_RE, nm):
            return {"status": "error",
                    "message": "card names are paths under inputs/ such as "
                               f"input_pdf_fit.py or jam24/input_jam24_s7_warm.py; got {nm!r}"}
    return None


def _derived_name(out_card):
    """Cards the gateway writes all go to inputs/derived/."""
    return f"{DERIVED_DIR}/{out_card.rsplit('/', 1)[-1]}"


def get_fit_parameters(card="", username=""):
    """Read the actual fit parameters (block/name/value/min/max/fixed) from a
    card, so UIs and agents always show the card's true values."""
    u, err = _need_user(username)
    if err:
        return err
    bad = _bad_card(card)
    if bad:
        return bad
    code = _card_code(u) + f"""
import re, json, ast
txt = open(card_path({card!r})).read()
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
    missing = _card_missing(r, card)
    if missing:
        return missing
    try:
        rows = json.loads(r["out"].strip().splitlines()[-1])
        return {"status": "ok", "username": u, "card": card,
                "parameters": rows, "count": len(rows)}
    except Exception:
        return {"status": "error", "username": u, "detail": r}


def search_datasets(card="", query="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    bad = _bad_card(card)
    if bad:
        return bad
    q = (query or "").strip().lower()
    code = _card_code(u) + f"""
import re
from collections import Counter
txt = open(card_path({card!r})).read()
rows = [(m.group(1), m.group(2)) for m in re.finditer(r"{DATASET_LINE_RE}", txt)
        if {q!r} and {q!r} in (m.group(1) + m.group(2)).lower() or not {q!r}]
c = Counter(reac for reac, _ in rows)
print('datasets matching:', len(rows))
print('by reaction:', dict(c))
for reac, idx in rows[:40]:
    print(f'  {{reac}} {{idx}}')
"""
    out = _run_ifarm_py(u, code, timeout=60)
    return _card_missing(out, card) or {"status": "ok", "username": u, "card": card, **out}


def get_dataset_catalog(card="", username="", reactions=""):
    u, err = _need_user(username)
    if err:
        return err
    card = card or SITE.get("template_card", "")
    bad = _bad_card(card)
    if bad:
        return bad
    code = _card_code(u) + f"""
import re, json
txt = open(card_path({card!r})).read()
c = {{}}
for m in re.finditer(r"{DATASET_LINE_RE}", txt):
    c.setdefault(m.group(1), {{}})[m.group(2)] = True
out = {{r: [{{'id': i, 'label': str(i)}} for i in sorted(ids)] for r, ids in c.items()}}
print(json.dumps(out))
"""
    r = _run_ifarm_py(u, code, timeout=60, cap=60000)
    missing = _card_missing(r, card)
    if missing:
        return missing
    try:
        catalog = json.loads(r["out"].strip().splitlines()[-1])
        want = [x.strip() for x in (reactions or "").split(",") if x.strip()]
        if want:
            catalog = {k: v for k, v in catalog.items() if k in want}
        return {"status": "ok", "username": u, "card": card, "catalog": catalog}
    except Exception:
        return {"status": "error", "username": u, "detail": r}


def list_reactions(username=""):
    """The reactions the analysis template offers, with how many datasets each
    has — the first thing the user picks."""
    cat = get_dataset_catalog("", username)
    if cat.get("status") != "ok":
        return cat
    return {"status": "ok", "username": cat.get("username"), "template": cat["card"],
            "reactions": [{"reaction": k, "datasets": len(v)} for k, v in cat["catalog"].items()]}


def build_card(base_card="", exclude_ids="", out_card="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    bad = _bad_card(base_card, out_card)
    if bad:
        return bad
    out_card = _derived_name(out_card)
    excl = [x.strip() for x in (exclude_ids or "").split(",") if x.strip()]
    code = _card_code(u) + f"""
import re
txt = open(card_path({base_card!r})).read()
excl = {excl!r}
n = 0
def rep(m):
    global n
    if m.group(2) in excl:
        n += 1
        return '# ' + m.group(0)
    return m.group(0)
txt2 = re.sub(r"{DATASET_LINE_RE}", rep, txt)
_os.makedirs(_os.path.join(_INPUTS, {DERIVED_DIR!r}), exist_ok=True)
open(_os.path.join(_INPUTS, {out_card!r}), 'w').write(txt2)
print('commented out', n, 'dataset lines')
"""
    out = _run_ifarm_py(u, code, timeout=60)
    return _card_missing(out, base_card) or {
        "status": "ok", "username": u, "base_card": base_card,
        "out_card": out_card, "excluded": excl, **out}


def submit_jam_fit(card="", outdir="", prior_msr="", cores=4, seed=160300,
                   hours=10, partition="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    if not outdir:
        return {"status": "error", "message": "need card and outdir"}
    bad = _bad_card(card)
    if bad:
        return bad
    # Resolve the card now, so a missing one is reported instead of failing
    # inside a queued job.
    where = _run_ifarm_py(u, _card_code(u) + f"print(card_path({card!r}))", timeout=60)
    missing = _card_missing(where, card)
    if missing:
        return missing
    card_abs = (where.get("out") or "").strip().splitlines()[-1:] or [""]
    card_abs = card_abs[0]
    if where.get("rc") != 0 or not card_abs.startswith("/"):
        return {"status": "error", "username": u, "message": "could not locate the card",
                "detail": where}
    prior_msr = (prior_msr or "").strip()
    if prior_msr:
        # A warm start continues THIS workspace's own results: the prior must
        # sit under <workspace>/runs (given by path or relative to it). It
        # also goes into the job script inside quotes, hence the plain charset.
        rr = _paths(u)[1]
        pm = os.path.normpath(prior_msr if prior_msr.startswith("/") else f"{rr}/{prior_msr}")
        if not (re.fullmatch(r"[A-Za-z0-9._/+-]{1,400}\.msr", pm) and pm.startswith(rr + "/")):
            return {"status": "error",
                    "message": f"prior_msr must be a .msr result of this workspace (under {rr}); "
                               "list_runs shows them. A workspace without results starts cold."}
        prior_msr = pm
    partition = (partition or "").strip() or _auto_partition(hours)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", partition):
        return {"status": "error", "message": f"invalid partition {partition!r}"}
    fp, _, ld = _paths(u)
    out_abs = _resolve_outdir(u, outdir)
    hhmm = f"{int(hours):02d}:00:00"
    prior_flag = f'-p "{prior_msr}"' if prior_msr else ""
    c = SITE["container"]
    binds = " ".join(f"--bind {b}" for b in c.get("binds", []))
    pypath = "$FP" + (f":{c['pythonpath']}" if c.get("pythonpath") else "")
    lhapdf = ":".join([f"{_area(u)}/lhapdf_tables"] + list(SITE.get("lhapdf_extra") or []))
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
apptainer exec {binds} \\
  --env PYTHONPATH={pypath} \\
  --env FITPACK=$FP \\
  --env LHAPDF_DATA_PATH={lhapdf} \\
  {c['image']} \\
  python3 -m fitlib.maxlike_jam {_shq(card_abs)} "$OUT" -n {cores} -v 10 -s {seed} {prior_flag}
echo "exit=$?"
ls "$OUT"/msr/ 2>/dev/null
"""
    b64 = base64.b64encode(script.encode()).decode()
    # The job script and the sbatch reply live in the user's own logs folder;
    # a fixed name in the compute host's /tmp would clash between users.
    out = _remote(u, f"mkdir -p {ld} && echo {b64} | base64 -d > {ld}/jam_fit_latest.sh && "
                  f"nohup bash -c 'sbatch {ld}/jam_fit_latest.sh' "
                  f">{ld}/jam_fit_latest.submit 2>&1 </dev/null & echo FIRED", timeout=20)
    return {"status": "fired", "username": u, "card": card, "outdir": out_abs,
            "partition": partition, "hours": int(hours), **out}


def _auto_partition(hours):
    """Short jobs to the short partition, the rest to the batch partition."""
    short = SITE.get("short_partition")
    if short and int(hours) <= int(SITE.get("short_hours") or 0):
        return short
    return SITE["partition"]


def submit_fit_from_selection(base_card="", selected_ids="",
                              out_card="agent_selection.py", outdir="", prior_msr="",
                              cores=4, hours=0, partition="", username="",
                              qcd=None, params=None):
    u, err = _need_user(username)
    if err:
        return err
    base_card = base_card or SITE.get("template_card", "")
    bad = _bad_card(base_card, out_card)
    if bad:
        return bad
    out_card = _derived_name(out_card)
    if not selected_ids.strip():
        return {"status": "error", "message": "need selected_ids (comma-separated)"}
    if int(hours) <= 0:
        hours = 2 if prior_msr else 10   # resource rules: warm 2h, cold 10h
    keep = set(x.strip() for x in selected_ids.split(",") if x.strip())
    # "reaction:id" pins a dataset to its family — idis and dy share numeric
    # ids, so a bare id keeps that id in every family. Bare ids still work.
    keep_pairs = sorted(k for k in keep if ":" in k)
    keep_ids = sorted(k for k in keep if ":" not in k)

    # QCD overrides. The card writes tmc as False or 'AOT' and ht as a bool.
    # v6 matched the value with \w+, which never matches 'AOT', so an override
    # of a card that already had TMC on was silently dropped; and "on" was
    # written as True, which is not a TMC scheme.
    qcd = qcd or {}
    qset = {}
    if qcd.get("Q20") not in (None, ""):
        qset["Q20"] = repr(float(qcd["Q20"]))
    truthy = (True, "AOT", "aot", "on", "On", "true", "True")
    if "tmc" in qcd:
        qset["tmc"] = "'AOT'" if qcd["tmc"] in truthy else "False"
    if "ht" in qcd:
        qset["ht"] = "True" if qcd["ht"] in truthy else "False"
    if SITE.get("grid_root"):
        # Derived cards read grids from the site's shared area, never from the
        # folder named in the base card (which may be someone's own).
        qset["grid path"] = repr(SITE["grid_root"])
    qpatch_code = _QCD_PATCH.replace("__QSET__", repr(qset)) if qset else ""

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

    code = _card_code(u) + f"""import re, json
txt = open(card_path({base_card!r})).read()
keep_pairs = set({keep_pairs!r})
keep_ids = set({keep_ids!r})
n = 0
def rep(m):
    global n
    if (m.group(1) + ':' + m.group(2)) in keep_pairs or m.group(2) in keep_ids:
        return m.group(0)
    n += 1
    return '# ' + m.group(0)
txt2 = re.sub(r"{DATASET_LINE_RE}", rep, txt)
{qpatch_code}
{param_code}
_os.makedirs(_os.path.join(_INPUTS, {DERIVED_DIR!r}), exist_ok=True)
open(_os.path.join(_INPUTS, {out_card!r}), 'w').write(txt2)
print('commented out', n, 'of the non-selected datasets')
"""
    gen = _run_ifarm_py(u, code, timeout=60)
    missing = _card_missing(gen, base_card)
    if missing:
        return missing
    if gen.get("rc") != 0:
        return {"status": "error", "username": u, "card_gen": gen}
    sub = submit_jam_fit(card=out_card, outdir=outdir, prior_msr=prior_msr,
                         cores=cores, hours=hours, partition=partition, username=u)
    return {"status": sub.get("status"), "username": u, "out_card": out_card,
            "kept": sorted(keep), "qcd": qset, "hours": hours,
            "card_gen": gen.get("out", "").strip(), "submit": sub}


# Applied to the derived card inside submit_fit_from_selection's generated
# code. Matches the whole value to the end of the line, either quote style,
# and appends the setting when the card has no line for it — an override is
# never silently dropped.
_QCD_PATCH = r'''
qset = __QSET__
applied = []
for k, lit in qset.items():
    pat = re.compile(r"conf\[['\"]" + k + r"['\"]\]\s*=\s*[^\n]+")
    txt2, c = pat.subn(lambda m, k=k, lit=lit: "conf['%s']=%s" % (k, lit), txt2)
    if c == 0:
        txt2 += "\nconf['%s']=%s\n" % (k, lit)
    applied.append('%s=%s' % (k, lit))
print('qcd overrides applied:', ', '.join(applied))
'''


def get_jam_result(outdir="", job_name="jamfit", username=""):
    u, err = _need_user(username)
    if err:
        return err
    _, rr, ld = _paths(u)
    out_abs = _resolve_outdir(u, outdir) if outdir else rr
    out = _remote(u,
        f"echo '--- queue ---'; squeue -u {u} -o '%.10i %.9P %.12j %.2t %.10M' | head -8; "
        f"echo '--- submit log ---'; cat {ld}/jam_fit_latest.submit 2>&1; "
        f"echo '--- progress ---'; ls -t {out_abs}/progress.json 2>/dev/null | head -1 | xargs -r tail -c 1500; "
        f"echo; echo '--- msr ---'; ls {out_abs}/msr 2>&1 | head -5; "
        f"echo '--- newest slurm err tail ---'; ls -t {ld}/jamfit_*.err 2>/dev/null | head -1 | xargs -r tail -12",
        timeout=60)
    return {"status": "ok", "username": u, **out}


# One JSON line: chi2/N per iteration (thinned to ~400 points) and the
# latest per-dataset chi2/N, worst first. Feeds the panel's Analysis chart.
_CHI2_JSON = r'''
import json
d = json.load(open(__PROGRESS__))
series = []
for i, e in enumerate(d):
    t = e.get('chi2')
    n = sum(v.get('npts', 0) for v in e.get('datasets', {}).values())
    if t is not None and n:
        series.append([i, round(t / n, 5)])
if len(series) > 400:
    step = len(series) / 400.0
    series = [series[int(k * step)] for k in range(400)] + [series[-1]]
ds = d[-1].get('datasets', {}) if d else {}
rows = sorted(([k, round(v['chi2'] / max(v['npts'], 1), 4), v['npts']] for k, v in ds.items()),
              key=lambda r: -r[1])
print(json.dumps({'entries': len(d), 'series': series, 'datasets': rows[:80]}))
'''


def read_chi2(outdir="", username="", structured=False):
    u, err = _need_user(username)
    if err:
        return err
    out_abs = _resolve_outdir(u, outdir) if outdir else _paths(u)[1]
    if structured:
        code = _CHI2_JSON.replace("__PROGRESS__", repr(out_abs + "/progress.json"))
        r = _run_ifarm_py(u, code, timeout=120, cap=400000)
        try:
            d = json.loads(r["out"].strip().splitlines()[-1])
        except Exception:
            return {"status": "error", "username": u, "outdir": out_abs,
                    "message": "No readable progress.json in this run yet.",
                    "detail": (r.get("err") or r.get("out") or "")[-300:]}
        return {"status": "ok", "username": u, "outdir": out_abs, **d}
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
        f"echo '--- my queue ---'; squeue -u {u} -o '%.10i %.9P %.12j %.2t %.10M %.6D' | head -10",
        timeout=90)
    return {"status": "ok", "username": u, "site": SITE["id"],
            "partition_rule": f"auto: {SITE.get('short_partition')} for jobs up to "
                              f"{SITE.get('short_hours')} h, {SITE['partition']} otherwise",
            "partitions": SITE.get("partitions", []), "hints": SITE.get("hints", []), **out}


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


# Runs = directories under <workspace>/runs (one or two levels down, e.g.
# 001/step7) that hold fit output; plus the user's Slurm queue, so a run that
# is submitted but not started yet is visible too.
_RUNS_JSON = r"""
import os, json, glob, subprocess
rr = __RUNS__
cards_dir = __CARDS__
template = __TEMPLATE__
user = __USER__
runs = []
for d in glob.glob(os.path.join(rr, '*')) + glob.glob(os.path.join(rr, '*', '*')):
    if not os.path.isdir(d):
        continue
    prog = os.path.join(d, 'progress.json')
    if not (os.path.isdir(os.path.join(d, 'msr')) or os.path.exists(prog)):
        continue
    msr = sorted(glob.glob(os.path.join(d, 'msr', '*.msr')), key=os.path.getmtime)
    times = [os.path.getmtime(d)] + [os.path.getmtime(m) for m in msr[-1:]]
    if os.path.exists(prog):
        times.append(os.path.getmtime(prog))
    runs.append({'name': os.path.relpath(d, rr), 'path': d, 'msr_count': len(msr),
                 'msr': [os.path.basename(m) for m in msr[-5:]],
                 'newest_msr': msr[-1] if msr else '',
                 'has_progress': os.path.exists(prog), 'modified': int(max(times))})
runs.sort(key=lambda r: -r['modified'])
# Cards: .py files in inputs/ and one folder below, named relative to inputs/.
# Those the gateway derived (inputs/derived/) are listed apart from the rest.
cards, derived = [], []
for c in sorted(glob.glob(os.path.join(cards_dir, '*.py')) + glob.glob(os.path.join(cards_dir, '*', '*.py'))):
    rel = os.path.relpath(c, cards_dir)
    if os.path.basename(rel) == '__init__.py':
        continue
    (derived if rel.startswith(__DERIVED__ + '/') else cards).append(rel)
queue = []
try:
    q = subprocess.run(['squeue', '-h', '-u', user, '-o', '%i|%j|%T|%M|%P'],
                       capture_output=True, text=True, timeout=30).stdout
    for line in q.splitlines():
        f = line.split('|')
        if len(f) == 5:
            queue.append({'job_id': f[0], 'name': f[1], 'state': f[2],
                          'elapsed': f[3], 'partition': f[4]})
except Exception:
    pass
print(json.dumps({'runs_root': rr, 'runs': runs[:200], 'cards': cards,
                  'derived_cards': derived, 'queue': queue,
                  'template_exists': bool(template) and os.path.isfile(template)}))
"""


def _template_path(ws):
    """Absolute path of the site's template card inside workspace ws."""
    t = SITE.get("template_card", "")
    if not t:
        return ""
    return f"{ws}/{t}" if t.startswith("examples/") else f"{ws}/inputs/{t}"


def list_runs(username=""):
    """Structured run listing for the panel's Collections tab: every run with
    output, newest first, the cards available to start from, and the queue."""
    u, err = _need_user(username)
    if err:
        return err
    fp, rr, _ = _paths(u)
    code = (_RUNS_JSON.replace("__RUNS__", repr(rr))
            .replace("__CARDS__", repr(f"{fp}/inputs"))
            .replace("__DERIVED__", repr(DERIVED_DIR))
            .replace("__TEMPLATE__", repr(_template_path(fp)))
            .replace("__USER__", repr(u)))
    r = _run_ifarm_py(u, code, timeout=90, cap=400000)
    try:
        d = json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        return {"status": "error", "username": u, "message": "could not list runs",
                "detail": (r.get("err") or r.get("out") or "")[-300:]}
    tpl = SITE.get("template_card", "")
    exists = d.pop("template_exists", False)
    return {"status": "ok", "username": u,
            "template_card": tpl if exists else "",
            "partitions": SITE.get("partitions", []),
            "partition_auto": {"short": SITE.get("short_partition"), "short_hours": SITE.get("short_hours"),
                               "long": SITE["partition"]}, **d}


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
# fit (see jamai/input_materializer.py and jamai/run_replica.py).

JAMAI_DIR = os.environ.get("JAM_GW_JAMAI_DIR", os.path.join(HERE, "jamai"))
JAMAI_REMOTE = ".jamai"                 # <ws>/.jamai/modules/jam/jamai
CAMPAIGNS_DIR = "campaigns"             # <ws>/campaigns/<folder>/...
CAMPAIGN_TEMPLATE = "examples/input02.py"
STAGE = "pdf_stage"

# The unpolarized PDF parameter table of the JAM app (FemtoMindOperationsPanel
# `unpolarizedPdfParameterRows`), the same values as examples/input02.py.
PDF_PARAMETER_ROWS = [
    ("g1", "N", True, 3.87592e-01, None, None), ("g1", "a", False, -6.23068169e-01, -1.9, 1),
    ("g1", "b", False, 9.25741583e0, 0, 20), ("g1", "c", False, 0, -100, 100),
    ("g1", "d", False, 0, -100, 100),
    ("uv1", "N", True, 3.47549e-01, None, None), ("uv1", "a", False, -1.21835956e-01, -0.9, 1),
    ("uv1", "b", False, 3.20766744e0, 0, 10), ("uv1", "c", False, 0, -100, 100),
    ("uv1", "d", False, 0, -100, 100),
    ("dv1", "N", True, 1.52089e-01, None, None), ("dv1", "a", False, -2.39874967e-01, -0.9, 1),
    ("dv1", "b", False, 3.8390262e0, 0, 10), ("dv1", "c", False, 0, -100, 100),
    ("dv1", "d", False, 0, -100, 100),
    ("sea1", "N", False, 5.71081196e-03, 0, 1), ("sea1", "a", False, -1.36329697e0, -1.9, -1),
    ("sea1", "b", False, 4.7472105e0, 0, 100),
    ("sea2", "N", False, 2.0864e-02, 0, 1), ("sea2", "a", False, -1.5e0, -1.9, -1),
    ("sea2", "b", False, 1.0e1, 0, 100),
    ("db1", "N", False, 3.67609928e-02, 0, 1), ("db1", "a", False, -8.41360631e-01, -1, 1),
    ("db1", "b", False, 5.31285539e0, 0, 50), ("db1", "c", False, 0, -100, 100),
    ("db1", "d", False, 0, -100, 100),
    ("ub1", "N", False, 1.95464789e-02, 0, 1), ("ub1", "a", False, -9.93659187e-01, -1, 1),
    ("ub1", "b", False, 8.38905814e0, 0, 20), ("ub1", "c", False, 0, -100, 100),
    ("ub1", "d", False, 0, -100, 100),
    ("s1", "N", True, 0, 0, 1), ("s1", "a", False, 1.34706224e-01, -0.5, 1),
    ("s1", "b", False, 6.00759596e0, 0, 100),
    ("sb1", "N", False, 7.46109845e-07, 0, 1), ("sb1", "a", False, 3.83495317e-01, -0.9, 1),
    ("sb1", "b", False, 4.61209808e0, 0, 10),
]
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
# Launches above this many replicas, and every append, need the user to type
# the confirmation phrase back (the panel's Submit button is that click).
CONFIRM_REPLICAS = 0


def _confirm_phrase(kind, payload):
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:10]
    return f"CONFIRM JAM {kind} {digest}"


def _parse_hms(text):
    m = re.fullmatch(r"(?:(\d+)-)?(\d{1,2}):(\d{2}):(\d{2})", str(text or "").strip())
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def _auto_partition_seconds(seconds):
    return _auto_partition(max(1, -(-seconds // 3600)))


# ---- options and the dataset catalog ---------------------------------------

def campaign_options():
    """Everything the composer offers, with the library values."""
    rows = [{"block": b, "parameter": p, "name": f"{b} {p}", "fixed": fx, "value": v,
             "min": lo, "max": hi} for b, p, fx, v, lo, hi in PDF_PARAMETER_ROWS]
    return {"status": "ok",
            "steps": ["Non-perturbative object", "Reactions", "QCD settings", "Job config", "Review"],
            "objects": CAMPAIGN_OBJECTS,
            "parameters": {"pdf": {"rows": rows, **PDF_DETAILS,
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
        r = _run_ifarm_py(u, _IDIS_CATALOG.replace("__WS__", repr(_paths(u)[0])), timeout=60, cap=200000)
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

def _normalize_card(card):
    """Accept the composer card (also the JAM app's shape) and return
    (normalized card, errors)."""
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
    known = {f"{b} {p}": (fx, v, lo, hi) for b, p, fx, v, lo, hi in PDF_PARAMETER_ROWS}
    overrides = {}
    for key, ov in (obj.get("parameterBounds") or {}).items():
        overrides[key.replace(":", " ")] = dict(ov or {})
    for key, ov in (obj.get("parameters") or {}).items():
        overrides.setdefault(key.replace(":", " "), {}).update(ov or {})
    params = []
    for name, (fx, v, lo, hi) in known.items():
        ov = overrides.get(name, {})
        row = {"name": name, "fixed": bool(ov.get("fixed", fx)), "value": ov.get("value", v),
               "min": ov.get("min", lo), "max": ov.get("max", hi)}
        try:
            row["value"] = float(row["value"])
            if not row["fixed"]:
                row["min"], row["max"] = float(row["min"]), float(row["max"])
                if not row["min"] <= row["value"] <= row["max"]:
                    errors.append(f"Parameter {name}: the value must lie within [min, max].")
        except (TypeError, ValueError):
            errors.append(f"Parameter {name}: value, min and max must be numbers.")
        params.append(row)
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
            "reactions": rx, "datasets": datasets, "cuts": cuts, "qcd": q, "parameters": pi,
            "job": j, "slurm": sl}
    return norm, errors


def _card_summary(norm):
    free = [p["name"] for p in norm["parameters_table"] if not p["fixed"]]
    fixed = [p["name"] for p in norm["parameters_table"] if p["fixed"]]
    return {"object": "Unpolarized PDF", "free_parameters": len(free), "fixed_parameters": len(fixed),
            "fixed": fixed, "reactions": norm["reactions"],
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
    sectors = [{"key": "pdf", "parameters": [
        {"name": p["name"], "value": p["value"], "min": p["min"], "max": p["max"], "fixed": p["fixed"]}
        for p in norm["parameters_table"]]}]
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


def _array_script(u, ws, folder, norm, first, last):
    """The Slurm job array: one replica per task, inside the site's image."""
    sl, c = norm["slurm"], SITE["container"]
    stage = f"{ws}/{CAMPAIGNS_DIR}/{folder}/stages/{STAGE}"
    binds = " ".join(f"--bind {b}" for b in c.get("binds", []))
    lhapdf = ":".join([f"{_area(u)}/lhapdf_tables"] + list(SITE.get("lhapdf_extra") or []))
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
REPLICA_ID="$SLURM_ARRAY_TASK_ID"
cd {_shq(ws)}
apptainer exec {binds} \\
  --env PYTHONPATH={ws}:{ws}/{JAMAI_REMOTE} --env FITPACK={ws} \\
  --env JAMAI_MAX_NFEV={norm["job"]["maxIterations"]} --env JAMAI_METHOD={norm["job"]["optimizer"]} \\
  --env LHAPDF_DATA_PATH={lhapdf} --env MPLCONFIGDIR={stage}/.matplotlib \\
  {c["image"]} \\
  python3 -m modules.jam.jamai.run_replica --manifest {stage}/manifest.resolved.yaml \\
    --output {stage}/current/replica_${{REPLICA_ID}} --replica "${{REPLICA_ID}}" -n {sl["cpus"]}
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
    code = _REGISTRY.replace("__WS__", repr(_paths(u)[0])).replace("__OP__", repr(op))
    r = _run_ifarm_py(u, code, timeout=60, cap=2000000)
    try:
        return json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        raise RuntimeError("could not read campaigns/collections.json: " + (r.get("err") or "")[-200:])


def _collection(u, cid):
    for e in _registry(u, {"kind": "read"}):
        if e.get("collection_id") == cid:
            return e
    return None


def _ai_branch_ready(u):
    r = _remote(u, f"test -f {_shq(_paths(u)[0])}/fitlib/ai.py && echo READY", timeout=30)
    return "READY" in (r.get("out") or "")


def use_ai_branch(username=""):
    """Check out the FitPack `ai` branch (fitlib/ai.py, the campaign runner) in
    the workspace. Refuses when the checkout has uncommitted changes."""
    u, err = _need_user(username)
    if err:
        return err
    ws = _paths(u)[0]
    if _ai_branch_ready(u):
        return {"status": "ok", "workspace": ws, "message": "The workspace already has the ai branch."}
    r = _remote(u, f"cd {_shq(ws)} && git rev-parse --verify -q origin/ai >/dev/null || "
                   f"GIT_TERMINAL_PROMPT=0 git fetch -q origin ai 2>&1; "
                   f"if [ -n \"$(git status --porcelain --untracked-files=no)\" ]; then echo DIRTY; "
                   f"else git checkout -q -B ai origin/ai 2>&1 && echo SWITCHED; fi", timeout=180)
    out = (r.get("out") or "") + (r.get("err") or "")
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

def validate_campaign(card=None, username=""):
    """Validate an input card and show what would run, with the confirmation
    phrase submit_campaign needs."""
    u, err = _need_user(username)
    if err:
        return err
    norm, errors = _normalize_card(card)
    if not errors:
        ws = _paths(u)[0]
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
            errors.append("The workspace is not on the FitPack ai branch (no fitlib/ai.py): run "
                          "use_ai_branch, or create a new workspace.")
    if errors:
        return {"status": "invalid", "valid": False, "errors": errors}
    summary = _card_summary(norm)
    phrase = _confirm_phrase("SUBMIT", norm)
    return {"status": "ok", "valid": True, "summary": summary,
            "slurm_script": _array_script(u, _paths(u)[0], "<campaign-folder>", norm, 1, norm["job"]["replicas"]),
            "confirmation_phrase": phrase,
            "next": "Show the user this summary and the Slurm script. Submit with "
                    "submit_campaign(card, confirm=<the phrase>) only after they confirm."}


def submit_campaign(card=None, confirm="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    v = validate_campaign(card, u)
    if not v.get("valid"):
        return v
    norm, _ = _normalize_card(card)
    if (confirm or "").strip() != v["confirmation_phrase"]:
        return {"status": "needs_confirmation", "confirmation_phrase": v["confirmation_phrase"],
                "summary": v["summary"], "slurm_script": v["slurm_script"],
                "message": "Not submitted. Show the user the summary and Slurm script; when they "
                           "confirm, call again with confirm set to the phrase."}
    ws = _paths(u)[0]
    folder = norm["job"]["campaignName"] + "_" + time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    croot = f"{ws}/{CAMPAIGNS_DIR}/{folder}"
    stage = f"{croot}/stages/{STAGE}"
    n = norm["job"]["replicas"]
    _upload_jamai(u, ws)
    yaml_b64 = base64.b64encode(_campaign_yaml(ws, folder, norm, n).encode()).decode()
    script_b64 = base64.b64encode(_array_script(u, ws, folder, norm, 1, n).encode()).decode()
    mat = (f"mkdir -p {croot} && echo {yaml_b64} | base64 -d > {croot}/campaign.request.json && "
           f"cd {_shq(ws)} && {_container_exec(ws, f'{ws}/{JAMAI_REMOTE}')} -m modules.jam.jamai.campaign_manifest "
           f"{croot}/campaign.request.json --materialize > {croot}/materialize.json 2>&1 && "
           f"{_container_exec(ws)} -m py_compile {stage}/input.py && "
           f"mkdir -p {stage}/logs {stage}/current {stage}/slurm && "
           f"echo {script_b64} | base64 -d > {stage}/slurm/array_1_{n}.sh && "
           f"sbatch {stage}/slurm/array_1_{n}.sh")
    r = _remote(u, mat, timeout=600)
    out = (r.get("out") or "") + (r.get("err") or "")
    m = re.search(r"Submitted batch job (\d+)", out)
    if not m:
        tail = _remote(u, f"tail -c 1500 {croot}/materialize.json 2>/dev/null", timeout=30).get("out")
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
    ws = _paths(u)[0]
    stage = f"{ws}/{CAMPAIGNS_DIR}/{e['campaign_folder']}/stages/{STAGE}"
    sb = base64.b64encode(_array_script(u, ws, e["campaign_folder"], e["input_card"], first, last).encode()).decode()
    r = _remote(u, f"echo {sb} | base64 -d > {stage}/slurm/array_{first}_{last}.sh && "
                   f"sbatch {stage}/slurm/array_{first}_{last}.sh", timeout=60)
    m = re.search(r"Submitted batch job (\d+)", (r.get("out") or "") + (r.get("err") or ""))
    if not m:
        return {"status": "error", "message": "sbatch refused the job.", "detail": (r.get("err") or r.get("out"))[-400:]}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    e["jobs"] = list(e.get("jobs") or []) + [{"job_id": m.group(1), "first": first, "last": last, "submitted_at": now}]
    e.update(replicas=last, updated_at=now, slurm_job_id=m.group(1), status="submitted")
    _registry(u, {"kind": "upsert", "entry": e})
    return {"status": "submitted", "job_id": m.group(1), **plan}


# ---- collections and status ----------------------------------------------------

_STATUS = r'''
import json, glob, os, subprocess
stage = __STAGE__
jobs = __JOBS__
total = __TOTAL__
done = ok = 0
for i in range(1, total + 1):
    p = os.path.join(stage, 'current', 'replica_%d' % i, 'fit_summary.json')
    if os.path.isfile(p):
        done += 1
        try:
            ok += json.load(open(p)).get('status') == 'success'
        except Exception:
            pass
states = {}
ids = ','.join(j['job_id'] for j in jobs)
try:
    q = subprocess.run(['squeue', '-h', '-j', ids, '-o', '%T'], capture_output=True, text=True, timeout=30).stdout.split()
    for s in q: states[s] = states.get(s, 0) + 1
except Exception:
    pass
print(json.dumps({'finished': done, 'succeeded': ok, 'queue': states}))
'''


def _campaign_progress(u, e):
    stage = f"{_paths(u)[0]}/{CAMPAIGNS_DIR}/{e['campaign_folder']}/stages/{STAGE}"
    code = (_STATUS.replace("__STAGE__", repr(stage)).replace("__JOBS__", repr(e.get("jobs") or []))
            .replace("__TOTAL__", str(int(e.get("replicas") or 0))))
    r = _run_ifarm_py(u, code, timeout=60)
    try:
        return json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        return {"finished": None, "succeeded": None, "queue": {}}


def list_campaigns(username=""):
    """Every campaign in this workspace (Collections), newest first, with live
    replica counts."""
    u, err = _need_user(username)
    if err:
        return err
    out = []
    for e in sorted(_registry(u, {"kind": "read"}), key=lambda x: x.get("created_at", ""), reverse=True):
        prog = _campaign_progress(u, e)
        running = sum(prog["queue"].values()) if prog.get("queue") else 0
        state = ("running" if running else "postprocessed" if e.get("status") == "postprocessed"
                 else "completed" if prog.get("finished") else e.get("status", "submitted"))
        out.append({"collection_id": e["collection_id"], "campaign_name": e.get("campaign_name"),
                    "created_at": e.get("created_at"), "replicas": e.get("replicas"),
                    "job_ids": [j["job_id"] for j in e.get("jobs") or []], "status": state,
                    "finished": prog.get("finished"), "succeeded": prog.get("succeeded"),
                    "queue": prog.get("queue"), "reactions": e["input_card"]["reactions"],
                    "datasets": e["input_card"]["datasets"]})
    return {"status": "ok", "campaigns": out, "count": len(out)}


def campaign_status(collection_id="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}."}
    prog = _campaign_progress(u, e)
    return {"status": "ok", "collection_id": collection_id, "replicas": e["replicas"],
            "jobs": e.get("jobs"), **prog, "summary": _card_summary(e["input_card"])}


def delete_campaign(collection_id="", delete_folder=False, confirm="", username=""):
    u, err = _need_user(username)
    if err:
        return err
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}."}
    plan = {"collection_id": collection_id, "delete_folder": bool(delete_folder)}
    phrase = _confirm_phrase("DELETE", plan)
    if (confirm or "").strip() != phrase:
        return {"status": "needs_confirmation", "plan": plan, "confirmation_phrase": phrase}
    _registry(u, {"kind": "delete", "id": collection_id})
    removed = False
    if delete_folder:
        folder = f"{_paths(u)[0]}/{CAMPAIGNS_DIR}/{e['campaign_folder']}"
        if re.fullmatch(r"[A-Za-z0-9._-]+", e["campaign_folder"]):
            removed = "GONE" in (_remote(u, f"rm -rf {_shq(folder)} && echo GONE", timeout=120).get("out") or "")
    return {"status": "ok", "deleted": collection_id, "folder_removed": removed}


# ---- analysis ---------------------------------------------------------------------

_POSTPROCESS = r'''
import glob, json, os, statistics
stage = __STAGE__
total = __TOTAL__
paths = sorted(glob.glob(os.path.join(stage, 'current', 'replica_*', 'fit_summary.json')))
summaries = []
for p in paths:
    try: summaries.append((p, json.load(open(p))))
    except Exception: pass
completed = failed = 0; chi2s = []; best = None; per = {}; cv = []
for p, s in summaries:
    if s.get('status') == 'success' and s.get('chi2_total') is not None:
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
       'missing_replicas': max(total - len(summaries), 0),
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
    return f"{_paths(u)[0]}/{CAMPAIGNS_DIR}/{e['campaign_folder']}/stages/{STAGE}"


def postprocess_campaign(collection_id="", username=""):
    """Aggregate every replica's fit_summary.json into the campaign summary."""
    u, err = _need_user(username)
    if err:
        return err
    e = _collection(u, collection_id)
    if not e:
        return {"status": "error", "message": f"No campaign {collection_id!r}."}
    code = _POSTPROCESS.replace("__STAGE__", repr(_stage_root(u, e))).replace("__TOTAL__", str(int(e["replicas"])))
    r = _run_ifarm_py(u, code, timeout=120, cap=2000000)
    try:
        agg = json.loads(r["out"].strip().splitlines()[-1])
    except Exception:
        return {"status": "error", "message": "postprocess failed", "detail": (r.get("err") or "")[-400:]}
    e.update(status="postprocessed", result_summary={k: agg[k] for k in agg if k != "chi2_per_dataset"},
             updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    _registry(u, {"kind": "upsert", "entry": e})
    return {"status": "ok", "collection_id": collection_id, "message": "Campaign postprocess completed.",
            "summary": {k: agg[k] for k in agg if k != "chi2_per_dataset"}}


def campaign_summary(collection_id="", username=""):
    """The fit summary: replicas completed/failed, chi2 statistics, best replica,
    and chi2 per dataset (Load Fit Summary)."""
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
    # Replicas that finished after the last postprocess are not in it yet.
    prog = _campaign_progress(u, e)
    if (prog.get("succeeded") or 0) > (agg.get("completed_replicas") or 0) or int(e["replicas"]) != agg.get("total_replicas"):
        out["stale"] = True
        out["note"] = (f"This summary aggregates {agg.get('completed_replicas')} replicas, but "
                       f"{prog.get('succeeded')} of {e['replicas']} have finished since: run "
                       "postprocess_campaign to include them.")
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
    """Draw the fitted PDFs of every finished replica at Q2 (View PDF Plots)."""
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
    code = (_PDF_PLOTS.replace("__STAGE__", repr(_stage_root(u, e))).replace("__FLAVORS__", repr(fl))
            .replace("__Q2__", repr(q2)).replace("__MEAN__", repr(bool(mean_overlay))))
    ws = _paths(u)[0]
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
    code = (_BY_EXPERIMENT.replace("__STAGE__", repr(_stage_root(u, e))).replace("__WANT__", repr(want))
            .replace("__PLOT__", repr(bool(plot))))
    ws = _paths(u)[0]
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
    ws, area = _paths(u)[0], _area(u)
    wd = os.path.normpath((workdir or ws).strip())
    if not (wd == area or wd.startswith(area + "/")):
        return {"status": "error", "message": f"workdir must be inside your own area ({area})"}
    sdir = f"{ws}/slurm"
    c = SITE["container"]
    body = command.strip()
    if in_container:
        binds = " ".join(f"--bind {b}" for b in c.get("binds", []))
        body = (f"apptainer exec {binds} --env PYTHONPATH={ws} --env FITPACK={ws} {c['image']} "
                f"bash -lc {_shq(body)}")
    script = "\n".join([
        "#!/bin/bash", f"#SBATCH --job-name={name}", f"#SBATCH --partition={part}",
        f"#SBATCH --time={time_limit}", f"#SBATCH --cpus-per-task={cpus}", f"#SBATCH --mem={mem}",
        *([f"#SBATCH --array={array}"] if array else []),
        f"#SBATCH --output={sdir}/{name}_%A{'_%a' if array else ''}.out",
        "set -euo pipefail", f"cd {_shq(wd)}", body, ""])
    b64 = base64.b64encode(script.encode()).decode()
    r = _remote(u, f"mkdir -p {sdir} && echo {b64} | base64 -d > {sdir}/{name}.sh && "
                   f"sbatch --test-only {sdir}/{name}.sh 2>&1", timeout=60)
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
    path = f"{_paths(u)[0]}/slurm/{name}.sh"
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
                "compute host; see check_hpc_connection for this site's hosts). The username "
                "comes from your gateway token, so it can be omitted. Call it with no "
                "credentials first: status=reused means the chain is live and nothing "
                "else is needed. If it is not live, do NOT ask for the PIN, one-time code "
                "or password in chat — give the user the panel's Connect tab "
                "(get_console tab=connect) and let them sign in there.",
        "schema": {"type": "object",
                   "properties": {"username": {"type": "string"},
                                  "pin_otp": {"type": "string"},
                                  "password": {"type": "string"}}},
        "fn": lambda a: connect_hpc(a.get("username", ""),
                                    a.get("pin_otp") or a.get("gateway_code", ""),
                                    a.get("password") or a.get("ifarm_password", ""))},
    "check_hpc": {"desc": "Verify the chain and show the connected user's queue plus their JAM directory.",
                  "schema": {"type": "object", "properties": {"username": {"type": "string"}}},
                  "fn": lambda a: check_hpc(a.get("username", ""))},
    "submit_test_fit": {"desc": "Submit a trivial test job on the cluster through the chain (fire-and-forget).",
                        "schema": {"type": "object", "properties": {"username": {"type": "string"}}},
                        "fn": lambda a: submit_test_fit(a.get("username", ""))},
    "get_submit_result": {"desc": "Poll the background test sbatch result and the queue.",
                          "schema": {"type": "object", "properties": {"username": {"type": "string"}}},
                          "fn": lambda a: get_submit_result(a.get("username", ""))},
    "node_diag": {"owner": True,
                  "desc": "Run a shell command on the gateway node (diagnostics / hot-redeploy). Owner only.",
                  "schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
                  "fn": lambda a: node_diag(a.get("cmd", "id; hostname"))},
    "remote_exec": {"owner": True,
                    "desc": "Run a shell command on the cluster through the owner's chain. Owner only.",
                    "schema": {"type": "object",
                               "properties": {"cmd": {"type": "string"},
                                              "timeout": {"type": "integer"},
                                              "username": {"type": "string"}}},
                    "fn": lambda a: remote_exec(a.get("cmd", "hostname"),
                                                int(a.get("timeout", 120)),
                                                a.get("username", ""))},
    "list_jam_runs": {"desc": "List the connected user's cards, run dirs, and msr files as text.",
                      "schema": {"type": "object", "properties": {"username": {"type": "string"}}},
                      "fn": lambda a: list_jam_runs(a.get("username", ""))},
    "fs_list": {"desc": "List a directory on the cluster (defaults to the user's own area).",
                "schema": {"type": "object",
                           "properties": {"path": {"type": "string"},
                                          "username": {"type": "string"}}},
                "fn": lambda a: fs_list(a.get("path", ""), a.get("username", ""))},
    "fs_read": {"desc": "Read a text file on the cluster (fit cards, slurm logs, "
                       "progress.json). Path must be under " + " or ".join(SITE["read_roots"]) + ".",
                "schema": {"type": "object",
                           "properties": {"path": {"type": "string"},
                                          "max_lines": {"type": "integer"},
                                          "username": {"type": "string"}}},
                "fn": lambda a: fs_read(a.get("path", ""), int(a.get("max_lines", 200)),
                                        a.get("username", ""))},
    "fs_write": {"desc": "Write a text file on the cluster, restricted to the user's own "
                        "area (" + SITE["area"].format(user="<username>") + ").",
                 "schema": {"type": "object",
                            "properties": {"path": {"type": "string"},
                                           "content": {"type": "string"},
                                           "username": {"type": "string"}},
                            "required": ["path", "content"]},
                 "fn": lambda a: fs_write(a.get("path", ""), a.get("content", ""),
                                          a.get("username", ""))},
    "setup_workspace": {
        "desc": "Create a workspace in your own area (" + SITE["area"].format(user="<you>")
                + "): git-clone the JAM code (repo_url defaults to " + JAM_REPO_URL
                + "; a private repository needs a GitHub token, which the user types on the "
                "panel's Project tab — never ask for it in chat), create the runs "
                "tree, link the shared grids and LHAPDF tables, and add what the repository "
                "lacks (fit driver, JAM24 cards under inputs/jam24/). Existing files are "
                "never overwritten. source=<username> copies that user's workspace "
                "instead of cloning. Run once before the first fit.",
        "schema": {"type": "object",
                   "properties": {"source": {"type": "string"},
                                  "repo_url": {"type": "string"},
                                  "target_dir": {"type": "string"},
                                  "username": {"type": "string"},
                                  "github_token": {"type": "string"},
                                  "folder_name": {"type": "string"},
                                  "branch": {"type": "string"}}},
        "fn": lambda a: setup_workspace(a.get("source", ""),
                                        a.get("repo_url", ""), a.get("github_token", ""),
                                        a.get("target_dir", ""), a.get("username", ""),
                                        a.get("folder_name", ""), a.get("branch", ""))},
    "search_datasets": {
        "desc": "List datasets (reaction + id) inside a JAM card, optionally "
                "filtered by query. Use it to help the user pick which reactions "
                "to include before building a custom card.",
        "schema": {"type": "object",
                   "properties": {"card": {"type": "string"},
                                  "query": {"type": "string"},
                                  "username": {"type": "string"}}},
        "fn": lambda a: search_datasets(a.get("card", ""),
                                        a.get("query", ""), a.get("username", ""))},
    "list_reactions": {
        "desc": "Step 3 of a fit: the reactions the analysis template offers (idis, dy, "
                "jet, ...) with their dataset counts. Show them and ask which to include.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: list_reactions()},
    "get_dataset_catalog": {
        "desc": "Step 4 of a fit: the dataset ids of each reaction, {reaction: [{id, label}]}. "
                "Pass reactions (comma list) to get only the ones the user picked; card "
                "defaults to the analysis template. Nothing is preselected — ask which ids.",
        "schema": {"type": "object",
                   "properties": {"reactions": {"type": "string"},
                                  "card": {"type": "string"},
                                  "username": {"type": "string"}}},
        "fn": lambda a: get_dataset_catalog(a.get("card", ""),
                                            a.get("username", ""), a.get("reactions", ""))},
    "get_fit_parameters": {
        "desc": "Read the ACTUAL fit parameters (block/name/value/min/max/fixed) from "
                "a card. UIs and agents must use these values, never hardcoded ones.",
        "schema": {"type": "object",
                   "properties": {"card": {"type": "string"},
                                  "username": {"type": "string"}}},
        "fn": lambda a: get_fit_parameters(a.get("card", ""),
                                           a.get("username", ""))},
    "build_card": {
        "desc": "Create a modified fit card: copy base_card and comment out the "
                "dataset lines whose ids are in exclude_ids (comma-separated). "
                "base_card is a card path under inputs/; the new card is written to "
                "inputs/derived/<out_card>.",
        "schema": {"type": "object",
                   "properties": {"base_card": {"type": "string"},
                                  "exclude_ids": {"type": "string"},
                                  "out_card": {"type": "string"},
                                  "username": {"type": "string"}},
                   "required": ["base_card", "out_card"]},
        "fn": lambda a: build_card(a.get("base_card", ""), a.get("exclude_ids", ""),
                                   a.get("out_card", ""), a.get("username", ""))},
    "submit_fit_from_selection": {
        "desc": "Step 6 of a fit: generate the card from the analysis template (or "
                "base_card if the user named one), keeping only selected_ids (comma list of "
                "reaction:id), and submit it. Powers the "
                "web fit composer's Submit button. hours=0 (default) auto-sizes: "
                "2h for warm starts, 10h for cold starts; 4 cores. partition empty = "
                "auto (short jobs to the short partition, the rest to the batch one). "
                "The derived card goes to inputs/derived/. "
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
            a.get("base_card", ""),
            a.get("selected_ids", ""), a.get("out_card", "agent_selection.py"),
            a.get("outdir", ""), a.get("prior_msr", ""),
            int(a.get("cores", 4)), int(a.get("hours", 0)),
            a.get("partition", ""), a.get("username", ""),
            a.get("qcd") or None, a.get("params") or None)},
    "submit_jam_fit": {
        "desc": "Submit a REAL JAM fit on the cluster as the connected user "
                "(apptainer maxlike_jam). card = card path under inputs/; outdir = "
                "output dir (relative to <workspace>/runs or absolute); prior_msr = "
                "optional prior .msr file (empty = cold start). RESOURCE RULES: warm "
                "start (prior given) 1-2h; cold start (no prior) hours>=10; 4 cores is "
                "the ladder reference; partition empty = auto (" + str(SITE.get("short_partition"))
                + " up to " + str(SITE.get("short_hours")) + " h, " + SITE["partition"]
                + " otherwise). Check cluster_status first when the queue looks busy.",
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
            int(a.get("hours", 10)), a.get("partition", ""),
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
        "desc": "Parse progress.json on the cluster: chi2/N series + per-dataset chi2/N. "
                "structured=true returns the full series as data instead of text.",
        "schema": {"type": "object",
                   "properties": {"outdir": {"type": "string"},
                                  "username": {"type": "string"},
                                  "structured": {"type": "boolean"}}},
        "fn": lambda a: read_chi2(a.get("outdir", ""), a.get("username", ""),
                                  bool(a.get("structured", False)))},
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
                "workspace is recorded? Cheap; use it before anything that needs the cluster.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: check_hpc_connection()},
    "check_project": {
        "desc": "Report on your workspace: which checkout is in use, every checkout in "
                "your area, and whether code, data, runs and grids are all present.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: check_project()},
    "use_workspace": {
        "desc": "Make an existing checkout in your own area the workspace that fits "
                "run in. path must be inside your own area ("
                + SITE["area"].format(user="<you>") + ") and contain fitlib/.",
        "schema": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"]},
        "fn": lambda a: use_workspace(a.get("path", ""))},
    "list_runs": {
        "desc": "Your fit runs with output (newest first, with their .msr files), the "
                "cards in your workspace (paths under inputs/; cards the gateway derived "
                "are listed apart), and your Slurm queue — as data.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: list_runs()},
    "campaign_options": {
        "desc": "The fit composer's choices, as in the JAM app: objects, the Unpolarized PDF "
                "parameter table (block, parameter, FIXED/FREE, library value, min, max), the "
                "reactions, default cuts, QCD and job settings with their allowed values, and the "
                "Slurm partitions. Call it first and walk the user through each step.",
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
                "parameters, datasets, cuts, QCD, job), the Slurm array script and the confirmation "
                "phrase. " + 'card = {"object": {"type": "pdf", "parameters": {"g1 a": {"value", "min", "max", "fixed"}, ...}}, "reactions": ["idis", "dy"], "datasets": {"idis": ["10010", ...]}, "cuts": {"idis": [{"variable": "Q2", "op": ">", "value": 1.69, "enabled": true}]}, "qcd": {"order", "Q20", "tmc": "off"|"AOT", "higherTwist", "dglapMode", "asMode"}, "parameters": {"initialization": "default"|"random_within_bounds", "randomSeed", "perReplicaSeed"}, "job": {"campaignName", "replicas", "maxIterations", "tolerance", "optimizer", "bootstrap"}, "slurm": {"partition" ("" = auto), "time" (per replica, HH:MM:SS), "mem", "cpus"}}. Only parameters the user changed need to be listed; the rest keep the library values.',
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
        "desc": "Collections: every campaign in this workspace with its status, job ids and how "
                "many replicas have finished.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: list_campaigns()},
    "campaign_status": {
        "desc": "One campaign's Slurm state and replica progress.",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string"}},
                   "required": ["collection_id"]},
        "fn": lambda a: campaign_status(a.get("collection_id", ""))},
    "postprocess_campaign": {
        "desc": "Postprocess: aggregate the finished replicas into the campaign fit summary.",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string"}},
                   "required": ["collection_id"]},
        "fn": lambda a: postprocess_campaign(a.get("collection_id", ""))},
    "campaign_summary": {
        "desc": "Load Fit Summary: completed/failed replicas, chi2 statistics, the best replica, and "
                "chi2/N per dataset (worst first).",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string"}},
                   "required": ["collection_id"]},
        "fn": lambda a: campaign_summary(a.get("collection_id", ""))},
    "campaign_pdf_plots": {
        "desc": "View PDF Plots: draw x f(x, Q2) of every finished replica (flavors: comma list of "
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
    "append_replicas": {
        "desc": "Generate More Replicas: add count replicas to a campaign. The first call returns "
                "the plan and a confirmation phrase; nothing is queued until the user confirms "
                "and the call is repeated with confirm = the phrase.",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string"},
                                                    "count": {"type": "integer"},
                                                    "confirm": {"type": "string"}},
                   "required": ["collection_id", "count"]},
        "fn": lambda a: append_replicas(a.get("collection_id", ""), a.get("count", 0), a.get("confirm", ""))},
    "delete_campaign": {
        "desc": "Remove a campaign from Collections (delete_folder also removes its files). Needs "
                "the confirmation phrase from a first call.",
        "schema": {"type": "object", "properties": {"collection_id": {"type": "string"},
                                                    "delete_folder": {"type": "boolean"},
                                                    "confirm": {"type": "string"}},
                   "required": ["collection_id"]},
        "fn": lambda a: delete_campaign(a.get("collection_id", ""), bool(a.get("delete_folder", False)),
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

OWNER_ONLY = {k for k, v in TOOLS.items() if v.get("owner")}



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
    "campaign, as a conversation. Every call acts as the user who owns the gateway token; "
    "never ask for or pass another username. Go one question at a time, in this order:\n"
    "1. HPC: check_hpc_connection; ask which HPC (its 'sites'). If not connected, the user "
    f"signs in ({SITE['login_secret']} for {SITE['login_host']}, {SITE['compute_secret']} for "
    f"{SITE['compute_host']}) on the panel's Connect tab — give the link (get_console "
    "tab=connect) and wait. Never ask for these in chat.\n"
    "2. Workspace: check_project lists the folders in the user's area; ask which to use "
    "(use_workspace) or create a new one (setup_workspace; it clones the FitPack ai branch). "
    "If a clone needs a GitHub token, send the user to the panel's Project tab (the link in "
    "the result) — never ask for a token in chat. Campaigns need fitlib/ai.py: if "
    "validate_campaign says the workspace is not on the ai branch, offer use_ai_branch.\n"
    "3. Non-perturbative object and parameters: campaign_options. Only Unpolarized PDF is "
    "available. Show the parameter table (FREE/FIXED, library value, min, max) and ask which "
    "to change.\n"
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
    "8. Review: validate_campaign; show the summary and the Slurm array script exactly as "
    "returned (not shortened); submit with "
    "submit_campaign(card, confirm=<phrase>) only after the user agrees.\n"
    "9. Collections and analysis: list_campaigns, campaign_status, postprocess_campaign, "
    "campaign_summary (fit summary), campaign_pdf_plots (PDF plots, returned as images), "
    "analyze_by_experiment (chi2 and data vs theory by experiment), append_replicas (always "
    "confirmed with its phrase). The panel's Collections and Analysis tabs show the same.\n"
    "10. Any other cluster job: draft_slurm_job with the resources agreed with the user (it "
    "runs sbatch --test-only), show the returned script exactly as written, ask the user, "
    "then submit_slurm_job with the phrase.")

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
    """Authorization: Bearer <token>, or X-Jam-Gateway-Token: <token> for paths
    where a proxy in front of the gateway does not pass Authorization through."""
    user, role = _identify(headers.get("Authorization"))
    if not user:
        alt = (headers.get("X-Jam-Gateway-Token") or "").strip()
        if alt:
            user, role = _identify("Bearer " + alt)
    return user, role


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
        _tls.sid = sid
        _tls.user, _tls.role = _request_identity(self.headers)
        try:
            self._dispatch(sid)
        finally:
            _tls.user, _tls.role = "", ""

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
        if not getattr(_tls, "user", ""):
            # Owner tooling also sends the token in params._meta: the body always
            # reaches us, so a proxy that drops headers can never lock the owner out.
            meta = params.get("_meta") if isinstance(params, dict) else None
            tok = meta.get("jam_gateway_token") if isinstance(meta, dict) else None
            if isinstance(tok, str) and tok:
                _tls.user, _tls.role = _identify("Bearer " + tok)
                _tls.via_body = bool(_tls.user)
        role = getattr(_tls, "role", "")
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
            result = {"tools": [_tool_entry(k, v) for k, v in TOOLS.items()
                                if not v.get("owner") or role == "owner"]}
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
            if spec is None or (spec.get("owner") and role != "owner"):
                # Owner tools are not listed to anyone else, so they answer as unknown.
                result = _tool_result("unknown tool", is_error=True)
            elif not getattr(_tls, "user", ""):
                result = _tool_result({"status": "unauthorized", "message": AUTH_HELP},
                                      is_error=True)
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
