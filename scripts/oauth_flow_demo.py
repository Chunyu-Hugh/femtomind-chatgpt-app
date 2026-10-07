#!/usr/bin/env python3
"""Simulate exactly what ChatGPT does when connecting to the FemtoMind MCP
endpoint: discovery -> consent -> PKCE code -> bearer token -> MCP calls."""
import base64
import hashlib
import json
import os
import secrets
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8010"
INVITE = os.environ.get("INVITE", "FM-8PQL-IVLH-EXM")
REDIRECT = "https://chatgpt.com/connector/oauth"  # ChatGPT's real callback


def get(url, headers=None, allow_redirect=False):
    req = urllib.request.Request(url, headers=headers or {})
    if allow_redirect:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            r = opener.open(req, timeout=15)
            return r.status, r.headers, r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode(errors="replace")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.headers, r.read().decode(errors="replace")


def post(url, data, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        r = opener.open(req, timeout=15)
        return r.status, r.headers, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode(errors="replace")


verifier = secrets.token_urlsafe(48)
challenge = base64.urlsafe_b64encode(
    hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

print("=" * 70)
print("STEP 1  发现: /.well-known/oauth-protected-resource (RFC 9728)")
s, h, b = get(f"{BASE}/.well-known/oauth-protected-resource")
print(f"  {s}  {b.strip()[:200]}")

print("STEP 2  授权服务器元数据 (RFC 8414)")
s, h, b = get(f"{BASE}/.well-known/oauth-authorization-server")
print(f"  {s}  {b.strip()[:300]}")

print("STEP 3  OIDC 配置 (ChatGPT 也会探测这个)")
s, h, b = get(f"{BASE}/.well-known/openid-configuration")
print(f"  {s}  {b.strip()[:200]}")

print("STEP 4  授权页 (浏览器弹出,用户看到的就是它)")
q = urllib.parse.urlencode({
    "response_type": "code", "client_id": "chatgpt-connector",
    "redirect_uri": REDIRECT, "code_challenge": challenge,
    "code_challenge_method": "S256", "state": secrets.token_hex(8),
})
s, h, b = get(f"{BASE}/authorize?{q}")
title = b.split("<title>")[1].split("</title>")[0] if "<title>" in b else "?"
print(f"  {s}  页面标题: {title}  (HTML {len(b)} 字节,含邀请码输入框)")

print("STEP 5  用户输入邀请码并提交 (POST /authorize)")
form = urllib.parse.urlencode({"q": q, "code": INVITE}).encode()
s, h, b = post(f"{BASE}/authorize", form,
               {"Content-Type": "application/x-www-form-urlencoded"})
loc = h.get("Location", "")
print(f"  {s}  → 302 跳回 ChatGPT: {loc[:80]}...")
code = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query)).get("code", "")
print(f"  拿到一次性授权码: {code[:30]}...")

print("STEP 6  换 token (code + PKCE verifier → bearer token)")
body = urllib.parse.urlencode({
    "grant_type": "authorization_code", "code": code,
    "redirect_uri": REDIRECT, "code_verifier": verifier,
}).encode()
s, h, b = post(f"{BASE}/token", body,
               {"Content-Type": "application/x-www-form-urlencoded"})
tok = json.loads(b)
print(f"  {s}  拿到 bearer token: {tok.get('access_token','')[:35]}... "
      f"有效期 {tok.get('expires_in')}s, 用户 {tok.get('user','{}').get('display_name') if isinstance(tok.get('user'), dict) else tok.get('user')}")

print("STEP 7  带 token 调 MCP: initialize → tools/list → jam_gateway_status")
auth = {"Authorization": f"Bearer {tok['access_token']}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"}


def mcp_post(payload, session_id=None):
    h = dict(auth)
    if session_id:
        h["mcp-session-id"] = session_id
    req = urllib.request.Request(f"{BASE}/mcp", data=json.dumps(payload).encode(),
                                 headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        raw = r.read().decode()
    # The server replies in SSE (text/event-stream): pull the data: payloads.
    if raw.lstrip().startswith("event:") or "\ndata: " in raw:
        events = [ln[6:] for ln in raw.splitlines() if ln.startswith("data: ")]
        raw = events[-1] if events else raw
    return sid, raw


rpc = lambda m, p, i: {"jsonrpc": "2.0", "id": i, "method": m, "params": p}
sid, raw = mcp_post(rpc("initialize", {"protocolVersion": "2025-03-26",
                                       "capabilities": {},
                                       "clientInfo": {"name": "sim", "version": "1"}}, 1))
print(f"  initialize → session id: {sid}")
sid, raw = mcp_post(rpc("tools/list", {}, 2), sid)
tools = [t["name"] for t in json.loads(raw)["result"]["tools"]]
jam = [t for t in tools if t.startswith("jam_")]
print(f"  tools/list → {len(tools)} 个工具,其中 jam_* 桥: {jam}")
sid, raw = mcp_post(rpc("tools/call", {"name": "jam_gateway_status",
                                       "arguments": {}}, 3), sid)
res = json.loads(raw)
text = "".join(c.get("text", "") for c in res["result"]["content"]
               if c.get("type") == "text")
print("  jam_gateway_status →", text[:260].replace("\n", " "))
print("=" * 70)
print("这就是 ChatGPT 每次连接时发生的完整流程。")
