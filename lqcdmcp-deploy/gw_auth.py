"""Where the scripts in this directory find the gateway URL and token.

Each person runs their own gateway, registered with the LQCDMCP proxy as
jam-gateway-<JAM_GW_USER>. The token is LQCDMCP_TOKEN, the dashboard token
from the Globus login: the gateway accepts it from the person who launched it.

For a test gateway: JAM_GW_NAME overrides the name, JAM_GW_URL the whole URL
(one that runs off the proxy), and JAM_GW_TOKEN the token (JAM_GW_TEST_TOKEN
on the gateway's side).
"""
import os
import sys

PROXY = "https://lqcdmcp.jlab.org:8000"
USER = os.environ.get("JAM_GW_USER", "").strip()
NAME = os.environ.get("JAM_GW_NAME", "").strip() or (f"jam-gateway-{USER}" if USER else "")
URL = os.environ.get("JAM_GW_URL", "").strip() or (f"{PROXY}/cloud/{NAME}/mcp" if NAME else "")


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


def token():
    """The token for calls to the gateway: JAM_GW_TOKEN for a test gateway,
    otherwise the dashboard token."""
    return _bare(os.environ.get("JAM_GW_TOKEN", "")) or dashboard_token()


def auth_headers():
    tok = token()
    return {"Authorization": "Bearer " + tok} if tok else {}
