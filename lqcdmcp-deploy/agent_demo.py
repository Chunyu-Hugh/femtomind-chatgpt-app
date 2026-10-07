#!/usr/bin/env python3
"""Agentic JAM demo: an LLM MCP client walks the full expert loop through
the LQCDMCP jam-gateway.

Loop (the "distilled expert decision-making"):
  1. connect (reuses live ControlMasters; OTP only once per 6h boot)
  2. inspect runs / pick prior + card
  3. submit a warm JAM fit
  4. poll chi2/N until converged or timeout
  5. evaluate the .msr against the JAM24 paper band

Usage: python3 agent_demo.py [--outdir 001/step7_agent] [--card ...] [--prior ...]
A pure MCP client: initialize -> tools/list -> tools/call, JSON-RPC 2.0.
"""
import json
import sys
import time
import urllib.request

URL = "https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp"


class Client:
    def __init__(self):
        self.rid = 0

    def rpc(self, method, params):
        self.rid += 1
        body = {"jsonrpc": "2.0", "id": self.rid, "method": method, "params": params}
        req = urllib.request.Request(
            URL, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.loads(r.read().decode())
        return d.get("result", {})

    def call(self, name, args):
        res = self.rpc("tools/call", {"name": name, "arguments": args})
        content = res.get("content", [])
        text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


def main():
    outdir = "001/step7_agent"
    card = "input_jam24_s7_warm.py"
    prior = "/w/ceph24/JAM/chunyu/jam24repro/smoke4/001/step6_v2/msr/8XW0AVCIGLJ8.msr"
    submit = "--submit" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--outdir" and i + 1 < len(sys.argv):
            outdir = sys.argv[i + 1]
        if a == "--card" and i + 1 < len(sys.argv):
            card = sys.argv[i + 1]
        if a == "--prior" and i + 1 < len(sys.argv):
            prior = sys.argv[i + 1]

    c = Client()
    c.rpc("initialize", {"protocolVersion": "2025-03-26",
                         "capabilities": {}, "clientInfo": {"name": "agent-demo", "version": "1"}})
    tools = [t["name"] for t in c.rpc("tools/list", {}).get("tools", [])]
    print("tools:", tools, flush=True)

    # 1. chain up (reuse if alive)
    conn = c.call("connect_hpc", {"pin_otp": "", "password": ""})
    print("connect:", conn.get("status"), "-", conn.get("note", conn.get("chain", "")), flush=True)

    # 2. inspect
    inv = c.call("list_jam_runs", {})
    print("inventory rc:", inv.get("rc"), flush=True)

    # 3. submit (only with --submit)
    if submit:
        r = c.call("submit_jam_fit", {"card": card, "outdir": outdir,
                                      "prior_msr": prior, "cores": 4,
                                      "seed": 160300, "hours": 2,
                                      "partition": "jupyter"})
        print("submit:", r.get("status"), r.get("outdir", ""), flush=True)

        # 4. poll chi2/N while it runs
        print("polling chi2/N...", flush=True)
        for i in range(24):
            chi = c.call("read_chi2", {"outdir": outdir})
            if isinstance(chi, dict):
                lines = [l for l in chi.get("out", "").splitlines() if "chi2/N=" in l]
                if lines:
                    print(f"  t={time.strftime('%H:%M:%S')} {lines[-1]}", flush=True)
            time.sleep(150)

    # 5. evaluate vs paper band
    ev = c.call("evaluate_fit", {"outdir": outdir})
    print(ev.get("out", ev), flush=True)


if __name__ == "__main__":
    main()
