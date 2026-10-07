#!/usr/bin/env python3
"""stdio MCP bridge: Claude Desktop <-> LQCDMCP jam-gateway over HTTPS.

Zero dependencies (stdlib only). Claude speaks newline-delimited JSON-RPC on
stdin/stdout; this forwards every tools/list and tools/call to the gateway's
plain JSON-RPC endpoint and returns the result in MCP shape.
"""
import json
import sys
import urllib.request

GATEWAY = "https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp"


def rpc(method, params, rid=0):
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        GATEWAY, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode())


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        if method == "initialize":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": params.get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "jam-gateway-bridge", "version": "0.1.0"}}}
        elif method == "ping":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {}}
        elif method == "tools/list":
            r = rpc("tools/list", {}, rid)
            resp = {"jsonrpc": "2.0", "id": rid, "result": r.get("result", {})}
        elif method == "tools/call":
            r = rpc("tools/call",
                    {"name": params.get("name"),
                     "arguments": params.get("arguments") or {}}, rid)
            resp = {"jsonrpc": "2.0", "id": rid,
                    "result": r.get("result", {"content": [], "isError": True}),
                    "error": r.get("error")}
            if resp["error"] is None:
                resp.pop("error")
        elif method == "resources/list":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {"resources": []}}
        elif method == "prompts/list":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {"prompts": []}}
        elif method.startswith("notifications/"):
            continue  # notifications have no response
        else:
            resp = {"jsonrpc": "2.0", "id": rid, "result": {}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
