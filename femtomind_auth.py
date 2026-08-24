"""Invite-code OAuth 2.1 authorization server for the FemtoMind MCP endpoint.

Why this exists
---------------
The MCP endpoint is on the public internet, so every request has to say who is
making it — otherwise one person's fits, history and JLab account are reachable
by anyone who learns the URL.

ChatGPT's connector speaks OAuth, and its "User-Defined OAuth Client" mode lets
us point it at endpoints we host. So rather than delegating identity to Google
(which would force every collaborator to have an account there), this module IS
the authorization server: the PI hands each collaborator an invite code, the
collaborator pastes it once on the consent screen, and from then on ChatGPT
holds a bearer token that identifies them.

Design notes
------------
* Codes, not passwords. A code identifies a person and can be revoked with one
  UPDATE; nobody has to manage a password they will reuse elsewhere.
* PKCE is required (OAuth 2.1) and verified, so an intercepted authorization
  code is useless on its own.
* Everything lives in one SQLite file next to the app — no extra service to run
  or back up separately.
* Tokens are stored hashed. A leaked database still does not yield usable
  bearer tokens.
"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

AUTH_DB = Path(os.environ.get(
    "FEMTOMIND_AUTH_DB",
    str(Path(__file__).parent / "auth.db"),
))
PUBLIC_HOST = os.environ.get("FEMTOMIND_PUBLIC_HOST", "localhost:8010")
ISSUER = f"https://{PUBLIC_HOST}" if not PUBLIC_HOST.startswith("localhost") else f"http://{PUBLIC_HOST}"

# An authorization code is redeemed within seconds of being issued; anything
# older is far more likely to be a replay than a slow user.
AUTH_CODE_TTL_S = 300
TOKEN_TTL_S = int(os.environ.get("FEMTOMIND_TOKEN_TTL_S", str(90 * 24 * 3600)))

# Per (client-ip, invite-code) failed-attempt ledger for the consent form.
# In-memory on purpose: single-process uvicorn, and a restart clearing it is
# harmless. Behind a TLS proxy all attempts share the proxy IP, which only
# makes the lockout stricter (one bot poisons its own window, real users on
# other networks are unaffected).
_INVITE_FAILS: dict[str, list[int]] = {}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(AUTH_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id       TEXT PRIMARY KEY,
                display_name  TEXT NOT NULL,
                invite_code   TEXT NOT NULL UNIQUE,
                hpc_username  TEXT,
                revoked       INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                last_seen_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS auth_codes (
                code            TEXT PRIMARY KEY,
                user_id         TEXT NOT NULL,
                challenge       TEXT NOT NULL,
                challenge_method TEXT NOT NULL,
                redirect_uri    TEXT NOT NULL,
                issued_at       INTEGER NOT NULL,
                used            INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tokens (
                token_hash  TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                issued_at   INTEGER NOT NULL,
                expires_at  INTEGER NOT NULL,
                revoked     INTEGER NOT NULL DEFAULT 0
            );
            """
        )


# ChatGPT issues a distinct callback path per connector, so the allowlist is a
# set of prefixes rather than exact URLs. Extra entries can be added via
# FEMTOMIND_EXTRA_REDIRECTS (comma-separated) for other MCP clients.
_REDIRECT_PREFIXES = tuple(
    p for p in (
        "https://chatgpt.com/connector/oauth/",
        "https://chat.openai.com/connector/oauth/",
        *[x.strip() for x in os.environ.get("FEMTOMIND_EXTRA_REDIRECTS", "").split(",") if x.strip()],
    )
)


def _redirect_allowed(uri: str) -> bool:
    return uri.startswith(_REDIRECT_PREFIXES)


def _hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def _now() -> int:
    return int(time.time())


# ─── administration ─────────────────────────────────────────────────────────

def mint_invite(display_name: str, hpc_username: str = "") -> dict[str, str]:
    """Create a collaborator and return the code to hand them."""
    init_db()
    user_id = "u_" + secrets.token_hex(6)
    raw = secrets.token_urlsafe(9).upper().replace("_", "").replace("-", "")[:12]
    code = "FM-" + "-".join(raw[i:i + 4] for i in range(0, 12, 4))
    with _db() as c:
        c.execute(
            "INSERT INTO users (user_id, display_name, invite_code, hpc_username, created_at)"
            " VALUES (?,?,?,?,datetime('now'))",
            (user_id, display_name, code, hpc_username or None),
        )
    return {"user_id": user_id, "display_name": display_name, "invite_code": code}


def list_users() -> list[dict[str, Any]]:
    init_db()
    with _db() as c:
        return [dict(r) for r in c.execute(
            "SELECT user_id, display_name, invite_code, hpc_username, revoked, created_at, last_seen_at"
            " FROM users ORDER BY created_at"
        )]


def revoke_user(user_id: str) -> bool:
    """Revoking must kill live sessions too, not just future sign-ins."""
    with _db() as c:
        n = c.execute("UPDATE users SET revoked=1 WHERE user_id=?", (user_id,)).rowcount
        c.execute("UPDATE tokens SET revoked=1 WHERE user_id=?", (user_id,))
    return n > 0


# ─── token verification (used on every MCP request) ─────────────────────────

def identify(auth_header: Optional[str]) -> Optional[dict[str, Any]]:
    """Resolve a Bearer token to a user, or None if it is not usable."""
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    tok = auth_header.split(None, 1)[1].strip()
    if not tok:
        return None
    init_db()
    with _db() as c:
        row = c.execute(
            "SELECT t.user_id, t.expires_at, t.revoked AS trev,"
            "       u.display_name, u.hpc_username, u.revoked AS urev"
            "  FROM tokens t JOIN users u ON u.user_id = t.user_id"
            " WHERE t.token_hash = ?",
            (_hash_token(tok),),
        ).fetchone()
        if not row or row["trev"] or row["urev"] or row["expires_at"] < _now():
            return None
        c.execute("UPDATE users SET last_seen_at=datetime('now') WHERE user_id=?", (row["user_id"],))
        return {
            "user_id": row["user_id"],
            "display_name": row["display_name"],
            "hpc_username": row["hpc_username"] or "",
        }


# ─── OAuth endpoints ────────────────────────────────────────────────────────

def protected_resource_metadata(_: Request) -> JSONResponse:
    """RFC 9728 — tells the client which authorization server to use."""
    return JSONResponse({
        "resource": f"{ISSUER}/mcp",
        "authorization_servers": [ISSUER],
        "bearer_methods_supported": ["header"],
    })


def authorization_server_metadata(_: Request) -> JSONResponse:
    """RFC 8414 — the endpoints ChatGPT will drive."""
    return JSONResponse({
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
    })


_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>FemtoMind access</title>
<style>
:root{{color-scheme:light dark}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f6f7f9;color:#15202b;
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
@media(prefers-color-scheme:dark){{body{{background:#0f1319;color:#e8ecf2}}}}
.card{{background:#fff;border:1px solid #e0e4ea;border-radius:14px;padding:32px;width:min(420px,92vw)}}
@media(prefers-color-scheme:dark){{.card{{background:#171d25;border-color:#28313d}}}}
h1{{font-size:19px;margin:0 0 6px}} p{{color:#5c6774;font-size:14px;margin:0 0 20px}}
label{{display:block;font-size:13px;font-weight:600;margin-bottom:6px}}
input{{width:100%;font:inherit;padding:11px 13px;border:1px solid #ccd3dc;border-radius:9px;
letter-spacing:.08em;background:transparent;color:inherit}}
button{{width:100%;margin-top:16px;font:inherit;font-weight:600;padding:11px;border:0;border-radius:9px;
background:#1d5fd0;color:#fff;cursor:pointer}}
.err{{color:#c0392b;font-size:13.5px;margin-top:12px}}
.foot{{color:#8a94a1;font-size:12px;margin-top:20px;text-align:center}}
</style></head><body><div class="card">
<h1>Connect to FemtoMind</h1>
<p>Enter the access code you were given. It identifies you so your fits, history and cluster account stay yours.</p>
<form method="post">
<input type="hidden" name="q" value="{q}">
<label for="c">Access code</label>
<input id="c" name="code" placeholder="FM-XXXX-XXXX-XXXX" autocomplete="off" autofocus required>
<button type="submit">Continue</button>
</form>
{err}
<div class="foot">JAM global analysis · Jefferson Lab</div>
</div></body></html>"""


async def authorize(request: Request):
    """Consent screen. GET shows the form; POST validates the code.

    `q` is attacker-influenced query data round-tripped through a hidden form
    field, so it is HTML-escaped on every render — unescaped it breaks out of
    the `value="…"` attribute and is a reflected-XSS vector on the page where
    the collaborator types their invite code.
    """
    if request.method == "GET":
        q = html.escape(request.url.query)
        return HTMLResponse(_PAGE.format(q=q, err=""))

    form = await request.form()
    params = dict(x.split("=", 1) for x in str(form.get("q", "")).split("&") if "=" in x)
    from urllib.parse import unquote_plus
    params = {k: unquote_plus(v) for k, v in params.items()}

    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    challenge = params.get("code_challenge", "")
    method = params.get("code_challenge_method", "S256")
    if not redirect_uri or not challenge:
        return HTMLResponse(_PAGE.format(q=html.escape(str(form.get("q", ""))),
                                         err='<div class="err">Missing PKCE parameters.</div>'), 400)
    # Without this the endpoint is an open redirector: anyone could send a
    # collaborator a link that harvests their authorization code. ChatGPT mints
    # a fresh callback path per connector, so match on the prefix.
    if not _redirect_allowed(redirect_uri):
        return HTMLResponse(_PAGE.format(q=html.escape(str(form.get("q", ""))),
                                         err='<div class="err">That redirect address is not allowed.</div>'), 400)

    supplied = str(form.get("code", "")).strip().upper()
    # Public endpoint: throttle invite-code guessing. The code space is large
    # (~32^12), but a per-key failure lockout makes brute force pointless even
    # against a lucky short key, and caps the page-render abuse surface.
    ip = (request.client.host if request.client else "unknown")
    key = f"{ip}|{supplied[:64]}"
    now = _now()
    _INVITE_FAILS[key] = [t for t in _INVITE_FAILS.get(key, []) if now - t < 900]
    if len(_INVITE_FAILS[key]) >= 5:
        return HTMLResponse(_PAGE.format(q=html.escape(str(form.get("q", ""))),
                                         err='<div class="err">Too many attempts — wait a few minutes.</div>'), 429)
    init_db()
    with _db() as c:
        row = c.execute(
            "SELECT user_id FROM users WHERE invite_code=? AND revoked=0", (supplied,)
        ).fetchone()
    if not row:
        _INVITE_FAILS[key].append(now)
        return HTMLResponse(_PAGE.format(q=html.escape(str(form.get("q", ""))),
                                         err='<div class="err">That code is not valid.</div>'), 401)
    _INVITE_FAILS.pop(key, None)

    auth_code = secrets.token_urlsafe(32)
    with _db() as c:
        c.execute(
            "INSERT INTO auth_codes (code,user_id,challenge,challenge_method,redirect_uri,issued_at)"
            " VALUES (?,?,?,?,?,?)",
            (auth_code, row["user_id"], challenge, method, redirect_uri, _now()),
        )
    sep = "&" if "?" in redirect_uri else "?"
    from urllib.parse import quote
    dest = f"{redirect_uri}{sep}code={quote(auth_code)}"
    if state:
        dest += f"&state={quote(state)}"
    return RedirectResponse(dest, status_code=302)


async def token(request: Request):
    """Exchange a one-time authorization code (plus PKCE verifier) for a token."""
    form = await request.form()
    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, 400)
    code = str(form.get("code", ""))
    verifier = str(form.get("code_verifier", ""))
    init_db()
    with _db() as c:
        row = c.execute("SELECT * FROM auth_codes WHERE code=?", (code,)).fetchone()
        if not row or row["used"] or _now() - row["issued_at"] > AUTH_CODE_TTL_S:
            return JSONResponse({"error": "invalid_grant"}, 400)
        # PKCE: the verifier must hash to the challenge sent at /authorize
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        if row["challenge_method"].upper() == "S256":
            if digest != row["challenge"]:
                return JSONResponse({"error": "invalid_grant"}, 400)
        elif verifier != row["challenge"]:
            return JSONResponse({"error": "invalid_grant"}, 400)

        c.execute("UPDATE auth_codes SET used=1 WHERE code=?", (code,))
        access = secrets.token_urlsafe(40)
        c.execute(
            "INSERT INTO tokens (token_hash,user_id,issued_at,expires_at) VALUES (?,?,?,?)",
            (_hash_token(access), row["user_id"], _now(), _now() + TOKEN_TTL_S),
        )
    return JSONResponse({
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL_S,
    })
