"""Where the scripts in this directory find the gateway URL and token.

Each person runs their own gateway, registered with the LQCDMCP proxy as
jam-gateway-<JAM_GW_USER>. JAM_GW_NAME overrides the name (the first shared
gateway was plain "jam-gateway"); JAM_GW_URL overrides the whole URL.

The token is LQCDMCP_TOKEN, the dashboard token from the Globus login: the
gateway accepts it from the person who launched it. JAM_GW_TOKEN overrides it
for a test gateway that runs off the proxy (JAM_GW_TEST_TOKEN on its side).
"""
import hashlib
import os

PROXY = "https://lqcdmcp.jlab.org:8000"
USER = os.environ.get("JAM_GW_USER", "").strip()
NAME = (os.environ.get("JAM_GW_NAME", "").strip()
        or (f"jam-gateway-{USER}" if USER else "jam-gateway"))
URL = os.environ.get("JAM_GW_URL", "").strip() or f"{PROXY}/cloud/{NAME}/mcp"


def token():
    return (os.environ.get("JAM_GW_TOKEN", "").strip()
            or os.environ.get("LQCDMCP_TOKEN", "").strip())


def token_hash(tok=None):
    return hashlib.sha256((tok if tok is not None else token()).encode()).hexdigest()


def auth_headers():
    tok = token()
    return {"Authorization": "Bearer " + tok, "X-Jam-Gateway-Token": tok} if tok else {}
