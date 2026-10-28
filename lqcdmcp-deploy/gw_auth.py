"""Where the scripts in this directory find the gateway URL and its tokens.

Each person runs their own gateway, registered with the LQCDMCP proxy as
jam-gateway-<JAM_GW_USER>. Two credentials are involved:

- the LQCDMCP dashboard token (LQCDMCP_TOKEN, from the Globus login) launches,
  cancels and inspects the gateway JOB through the dashboard;
- the gateway token is what the gateway itself accepts. redeploy_jam.py makes
  it once per gateway, keeps it in SECRETS_DIR (mode 600) and puts only its
  sha256 into the job script. The owner exports it as JAM_GW_TOKEN, or lets the
  scripts read the file. An older gateway launched without a token hash still
  accepts the dashboard token (legacy); token() falls back to that.

A second secret, the admin secret, lets hot_redeploy.py replace the running
gateway's code; it travels in the X-JAM-Admin header, never as the bearer.

For a test gateway: JAM_GW_NAME overrides the name, JAM_GW_URL the whole URL
(one that runs off the proxy), and JAM_GW_TOKEN the token (JAM_GW_TEST_TOKEN
on the gateway's side).
"""
import hashlib
import os
import secrets
import stat
import sys

PROXY = "https://lqcdmcp.jlab.org:8000"
USER = os.environ.get("JAM_GW_USER", "").strip()
NAME = os.environ.get("JAM_GW_NAME", "").strip() or (f"jam-gateway-{USER}" if USER else "")
URL = os.environ.get("JAM_GW_URL", "").strip() or (f"{PROXY}/cloud/{NAME}/mcp" if NAME else "")
SECRETS_DIR = os.path.expanduser(os.environ.get("JAM_GW_SECRETS_DIR", "~/.config/jam_gateway"))


def require():
    if not URL:
        sys.exit("set JAM_GW_USER to your JLab username (or JAM_GW_URL for a test gateway)")


def _bare(tok):
    """A token without a leading "Bearer ", in case it was copied with one."""
    tok = tok.strip()
    return tok[7:].strip() if tok[:7].lower() == "bearer " else tok


def dashboard_token():
    """LQCDMCP_TOKEN, for calls to the dashboard itself (launch, cancel, status)."""
    return _bare(os.environ.get("LQCDMCP_TOKEN", ""))


# ── the gateway's own secrets ──────────────────────────────────────────────────

def secret_path(kind):
    """kind: token (the bearer), admin (hot redeploys) or users (the shared
    gateway's {name: sha256} table)."""
    if not NAME:
        require()
    ext = {"token": "token", "admin": "admin", "users": "users.json"}[kind]
    return os.path.join(SECRETS_DIR, f"{NAME}.{ext}")


def read_secret(kind):
    """The stored secret, or '' when there is none. A file that others can
    read is refused, since it would no longer be a secret."""
    path = secret_path(kind)
    try:
        st = os.stat(path)
    except OSError:
        return ""
    if st.st_mode & 0o077:
        sys.exit(f"{path} is readable by others (mode {stat.filemode(st.st_mode)}); run: chmod 600 {path}")
    with open(path) as fh:
        return fh.read().strip()


def ensure_secret(kind):
    """The stored secret, created (mode 600 in a mode 700 folder) when missing."""
    existing = read_secret(kind)
    if existing:
        return existing
    os.makedirs(SECRETS_DIR, mode=0o700, exist_ok=True)
    os.chmod(SECRETS_DIR, 0o700)
    value = secrets.token_urlsafe(32)
    fd = os.open(secret_path(kind), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value + "\n")
    return value


def rotate_secret(kind):
    """Replace the stored secret with a new one (the gateway must be relaunched
    with the new hash before it accepts it)."""
    path = secret_path(kind)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    return ensure_secret(kind)


def sha256(value):
    return hashlib.sha256(value.encode()).hexdigest()


def token():
    """The token for calls to the gateway: JAM_GW_TOKEN, else the stored gateway
    token, else (legacy gateways) the dashboard token."""
    tok = _bare(os.environ.get("JAM_GW_TOKEN", ""))
    if tok:
        return tok
    if NAME:
        tok = read_secret("token")
        if tok:
            return tok
    tok = dashboard_token()
    if tok:
        print("note: no gateway token (JAM_GW_TOKEN or ~/.config/jam_gateway); using the dashboard token, "
              "which only a gateway launched without a token hash accepts", file=sys.stderr)
    return tok


def auth_headers():
    tok = token()
    return {"Authorization": "Bearer " + tok} if tok else {}


def admin_token():
    """The admin secret for hot_redeploy.py: JAM_GW_ADMIN, else the stored one."""
    return _bare(os.environ.get("JAM_GW_ADMIN", "")) or (read_secret("admin") if NAME else "")


def admin_headers():
    tok = admin_token()
    return {"X-JAM-Admin": tok} if tok else {}


def load_users():
    """The shared gateway's {name: sha256}; only hashes are stored."""
    import json
    try:
        with open(secret_path("users")) as fh:
            table = json.load(fh)
    except (OSError, ValueError):
        return {}
    return table if isinstance(table, dict) else {}


def save_users(table):
    import json
    os.makedirs(SECRETS_DIR, mode=0o700, exist_ok=True)
    path = secret_path("users")
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(table, fh, indent=1, sort_keys=True)
    os.replace(path + ".tmp", path)
