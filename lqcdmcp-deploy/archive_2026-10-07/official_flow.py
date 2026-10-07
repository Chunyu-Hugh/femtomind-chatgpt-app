#!/usr/bin/env python3
"""Official LQCDMCP flow: connect to the proxy's own MCP at /jlab, launch a
backend via launch_mcp_server_using_script, then use connect_mcp_server +
list_backend_tools + call_backend_tool — the documented path from the
dashboard's Config Exporter."""
import base64
import json
import re
import ssl
import sys
import time
import urllib.request

T = sys.argv[1] if len(sys.argv) > 1 else ""
B = "https://lqcdmcp.jlab.org:8000/jlab/"
ctx = ssl.create_default_context()
sid = None
rid = 0


def call(method, params=None, timeout=180):
    global sid, rid
    rid += 1
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    hdrs = {"Authorization": "Bearer " + T, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}
    if sid:
        hdrs["Mcp-Session-Id"] = sid
    req = urllib.request.Request(B, data=json.dumps(body).encode(),
                                 headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        sid = r.headers.get("Mcp-Session-Id") or sid
        raw = r.read().decode()
    # SSE: take the LAST data event (the final result)
    events = re.findall(r"data: (\{.*?\})\n\n", raw, re.S) or re.findall(r"data: (\{.*\})", raw, re.S)
    if events:
        try:
            return json.loads(events[-1])
        except json.JSONDecodeError:
            for e in reversed(events):
                try:
                    return json.loads(e)
                except json.JSONDecodeError:
                    pass
    return {"raw": raw[:400]}


def tool_call(name, args, timeout=180):
    r = call("tools/call", {"name": name, "arguments": args}, timeout)
    res = r.get("result", {})
    if isinstance(res, dict) and "content" in res:
        return " ".join(c.get("text", "") for c in res["content"]
                        if isinstance(c, dict) and c.get("type") == "text")[:2000]
    return json.dumps(res)[:2000]


FM_SERVER = r'''
import json, os, random, socket, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

TOOLS = {
    "ping": {"desc": "Reply pong.", "schema": {"type": "object", "properties": {}},
             "fn": lambda a: "pong from JLab node"},
    "generate_random_number": {"desc": "Return a random integer between 1 and 100.",
             "schema": {"type": "object", "properties": {}},
             "fn": lambda a: random.randint(1, 100)},
    "sum": {"desc": "Simple addition of two integers.",
             "schema": {"type": "object",
                        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                        "required": ["a", "b"]},
             "fn": lambda a: a["a"] + a["b"]},
}

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"fm-demo MCP server"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            req = json.loads(self.rfile.read(n))
        except Exception:
            self.send_response(400); self.end_headers(); return
        method = req.get("method"); rid = req.get("id")
        if method == "initialize":
            result = {"protocolVersion": (req.get("params") or {}).get("protocolVersion", "2025-03-26"),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fm-demo", "version": "1.0.0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": k, "description": v["desc"], "inputSchema": v["schema"]}
                                for k, v in TOOLS.items()]}
        elif method == "tools/call":
            name = (req.get("params") or {}).get("name", "")
            args = (req.get("params") or {}).get("arguments") or {}
            if name not in TOOLS:
                result = {"content": [{"type": "text", "text": "unknown tool"}], "isError": True}
            else:
                try:
                    out = TOOLS[name]["fn"](args)
                    result = {"content": [{"type": "text", "text": str(out)}], "isError": False}
                except Exception as e:
                    result = {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
        else:
            result = {}
        data = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)
    def log_message(self, *a):
        pass

def register():
    fqdn = socket.getfqdn() or socket.gethostname()
    url = "http://%s:8080/mcp" % fqdn
    job_id = os.environ.get("SLURM_JOB_ID", "0")
    job_name = os.environ.get("SLURM_JOB_NAME", "fm-demo")
    enc = urllib.parse.quote(url)
    target = ("https://lqcdmcp.jlab.org:8000/server?mcp_name=%s&server_url=%s"
              "&owner=chunyu&job_id=%s&job_name=%s") % (job_name, enc, job_id, job_name)
    for i in range(10):
        try:
            with urllib.request.urlopen(urllib.request.Request(target, method="POST", data=b""), timeout=15) as r:
                print("register ok:", r.read().decode()[:100], flush=True)
                return
        except Exception as e:
            print("register retry:", e, flush=True)
            time.sleep(5)

print("fm-demo listening 8080", flush=True)
if os.environ.get("SLURM_JOB_ID"):
    register()
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
'''


def build_slurm():
    b64 = base64.b64encode(FM_SERVER.encode()).decode()
    return ("#!/bin/bash\n#SBATCH --partition=24s\n#SBATCH --time=06:00:00\n"
            "#SBATCH --nodes=1\n#SBATCH --ntasks=1\n#SBATCH --cpus-per-task=2\n"
            "cd /tmp\n"
            "python3 - <<'PYDEC' > /dev/null\nimport base64\n"
            "open('fm_server.py','wb').write(base64.b64decode('%s'))\nPYDEC\n"
            "python3 fm_server.py\n") % b64


def main():
    call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "official-flow", "version": "1"}})
    print("== idle nodes ==")
    print(tool_call("number_of_idle_nodes", {"unused": ""}))

    print("== launch via official tool ==")
    r = tool_call("launch_mcp_server_using_script",
                  {"mcp_name": "fm-demo", "wait": False,
                   "submission_script": build_slurm(), "base64_content": False},
                  timeout=180)
    print(r[:300])

    for i in range(30):
        st = tool_call("check_mcp_server_status_by_name", {"mcp_name": "fm-demo"})
        print(f"[poll {i+1}] {st[:250]}")
        if "RUNNING" in st and ("http" in st or "url" in st.lower()):
            break
        time.sleep(30)

    print("== connect_mcp_server ==")
    print(tool_call("connect_mcp_server", {"mcp_name": "fm-demo"}))
    time.sleep(5)
    print("== list_backend_tools ==")
    print(tool_call("list_backend_tools", {"mcp_name": "fm-demo"}))
    print("== call_backend_tool x3 ==")
    for i in range(3):
        print(tool_call("call_backend_tool",
                        {"mcp_name": "fm-demo",
                         "tool_name": "generate_random_number", "arguments": {}}))


if __name__ == "__main__":
    main()
