"""Run E1 against a gateway: one official card, one mutant per fault, one
validate_campaign call each.

    python3 -m evaluation.e1.runner --url https://.../mcp --token-file ~/.config/jam_gateway/jam-gateway-chunyu.token --out ~/bench_eval/e1/e1_<stamp>.json

The gateway is asked for the official card first (campaign_card_from_catalog),
so every mutant is one deliberate delta away from a parity-checked card. Each
mutant is validated on its own: `validate_campaign` never queues anything, so
the whole sweep is read-only, and the reply says whether the card was refused,
warned or accepted, with the stable code and the catalog differences.
"""
import argparse
import json
import os
import ssl
import time
import urllib.request

from evaluation.e1.mutants import MUTANTS, mutate


class Client:
    def __init__(self, url, token, timeout=120):
        self.url, self.token, self.timeout = url, token.replace("Bearer ", ""), timeout
        self.ctx = ssl.create_default_context()
        self.sid, self.rid = None, 0

    def call(self, method, params=None, timeout=None):
        self.rid += 1
        body = {"jsonrpc": "2.0", "id": self.rid, "method": method}
        if params is not None:
            body["params"] = params
        hdrs = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}
        if self.sid:
            hdrs["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self.ctx) as r:
            self.sid = r.headers.get("Mcp-Session-Id") or self.sid
            raw = r.read().decode()
        try:
            d = json.loads(raw)
            if isinstance(d, dict) and ("result" in d or "error" in d):
                return d
        except ValueError:
            pass
        for line in reversed(raw.splitlines()):
            if line.startswith("data: "):
                d = json.loads(line[6:])
                if "result" in d or "error" in d:
                    return d
        return {"raw": raw[:400]}

    def tool(self, name, arguments, timeout=None):
        t0 = time.time()
        d = self.call("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        if "error" in d:
            return {"status": "mcp_error", "error": d["error"], "secs": round(time.time() - t0, 1)}
        text = "".join(c.get("text") or "" for c in (d["result"].get("content") or []) if c.get("type") == "text")
        try:
            out = json.loads(text)
        except ValueError:
            out = {"status": "unparsed", "raw": text[:400]}
        out["_secs"] = round(time.time() - t0, 1)
        return out


def _settle(client, reply, tries=12, pause=5):
    """A validate that answers 'running' carries a job id; wait for it."""
    for _ in range(tries):
        if reply.get("status") != "running":
            return reply
        job = reply.get("job_id")
        if not job:
            return reply
        time.sleep(pause)
        reply = client.tool("analysis_result", {"job_id": job}, timeout=60)
    return reply


def outcome(reply):
    """refused / warned / accepted, the way a reviewer should read it."""
    if reply.get("status") in ("error", "unauthorized", "disconnected"):
        return "refused"
    if reply.get("status") == "running":
        return "pending"
    if not reply.get("valid", True):
        return "refused"
    return "warned" if (reply.get("warnings") or []) else "accepted"


def run(url, token, out_path, analysis_id="JAM24.final", only=None):
    client = Client(url, token)
    client.call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "e1", "version": "1"}})
    base_reply = client.tool("campaign_card_from_catalog", {"analysis_id": analysis_id, "replicas": 1},
                             timeout=90)
    if base_reply.get("status") != "ok":
        raise SystemExit("could not get the official card: %s" % json.dumps(base_reply)[:400])
    card = base_reply["card"]
    base = client.tool("validate_campaign", {"card": card}, timeout=180)
    base = _settle(client, base)
    rows = [{"id": "baseline", "what": "the official card, unchanged", "field": "-",
             "outcome": outcome(base), "code": base.get("code"), "valid": base.get("valid"),
             "warnings": (base.get("warnings") or [])[:3],
             "differences": ((base.get("catalog_comparison") or {}).get("differences") or [])[:5],
             "points": (base.get("summary") or {}).get("points_after_cuts"),
             "secs": base.get("_secs")}]
    print("baseline: %s" % rows[0]["outcome"], flush=True)
    for mutant_id, what, field in MUTANTS:
        if only and mutant_id not in only:
            continue
        try:
            card = mutate(base_reply["card"], mutant_id)
        except ValueError as e:
            rows.append({"id": mutant_id, "what": what, "field": field, "outcome": "not_applicable",
                         "message": str(e)})
            continue
        reply = _settle(client, client.tool("validate_campaign", {"card": card}, timeout=180))
        row = {"id": mutant_id, "what": what, "field": field, "outcome": outcome(reply),
               "code": reply.get("code"), "valid": reply.get("valid"),
               "warnings": (reply.get("warnings") or [])[:3],
               "differences": ((reply.get("catalog_comparison") or {}).get("differences") or [])[:6],
               "points": (reply.get("summary") or {}).get("points_after_cuts"),
               "message": (reply.get("message") or "")[:300],
               "secs": reply.get("_secs")}
        rows.append(row)
        print("%-22s %-10s %s" % (mutant_id, row["outcome"], row.get("code") or ""), flush=True)
    report = {"schema": "femtomind.e1_faults.v1", "analysis_id": analysis_id,
              "url": url, "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "rows": rows}
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=1, sort_keys=True)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True)
    p.add_argument("--token-file", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--analysis-id", default="JAM24.final")
    p.add_argument("--only", nargs="*")
    a = p.parse_args()
    with open(os.path.expanduser(a.token_file)) as f:
        token = f.read().strip()
    run(a.url, token, os.path.expanduser(a.out), a.analysis_id, set(a.only or []) or None)


if __name__ == "__main__":
    main()
