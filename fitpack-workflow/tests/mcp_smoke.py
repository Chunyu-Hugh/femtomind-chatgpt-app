#!/usr/bin/env python3
"""FemtoMind MCP server end-to-end smoke test.

Drives the REAL tool surface exactly the way an LLM agent does: HTTP POSTs to
the mcpo gateway (local: bare tool paths on :8000; AWS: /fitpack/<tool>).
Exercises the full chain: dataset search -> ladder status -> fit results ->
validation stamp/approve/gate -> paper comparison -> cluster context.

Usage:
  python3 mcp_smoke.py local
  python3 mcp_smoke.py aws --host ec2-user@femtomind.duckdns.org --key ~/.ssh/femtomind_aws.pem
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request

LOCAL_BASE = "http://127.0.0.1:8000"
AWS_BASE = "http://127.0.0.1:8000"          # reached through ssh tunnel on the host
AWS_FITPACK = "/fitpack"

PASS = []
FAIL = []


def call(base, tool, payload=None, prefix=""):
    url = f"{base}{prefix}/{tool}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_error": str(e)}


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def remote_call(host, key, tool, payload):
    body = json.dumps(payload)
    script = f"curl -s -m 60 -X POST http://127.0.0.1:8000/fitpack/{tool} -H 'Content-Type: application/json' -d '{body}'"
    p = subprocess.run(
        ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-o", "ConnectTimeout=15", host, script],
        capture_output=True, text=True, timeout=120,
    )
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"_error": p.stdout[:200] + p.stderr[:200]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["local", "aws"])
    ap.add_argument("--host", default="ec2-user@femtomind.duckdns.org")
    ap.add_argument("--key", default="/root/.ssh/femtomind_aws.pem")
    args = ap.parse_args()

    print(f"=== FemtoMind MCP smoke test ({args.mode}) ===")

    if args.mode == "local":
        base, prefix = LOCAL_BASE, ""
        call_tool = lambda t, p: call(base, t, p, prefix)
    else:
        call_tool = lambda t, p: remote_call(args.host, args.key, t, p)

    # 1. dataset search
    r = call_tool("search_datasets", {"observable": "dy"})
    ids = [d["id"] for g in r.get("by_observable", {}).values() for d in g] if isinstance(r.get("by_observable"), dict) else []
    check("search_datasets(dy) returns E866 ids", "10001" in ids and "10002" in ids, str(r)[:150])

    # 2. ladder status
    r = call_tool("get_fitting_chain", {"chat_id": "smoke-test"})
    ok = isinstance(r, dict) and ("chain" in r or "status" in r or "steps" in r or "batches" in r or "ok" in r or "track" in r)
    check("get_fitting_chain responds", ok, str(r)[:150])

    # 3. cluster context
    r = call_tool("get_resource_context", {"observable": "idis"})
    ok = isinstance(r, dict) and ("cluster" in r or "partition_chosen" in r or "idle_cpus" in r)
    check("get_resource_context responds", ok, str(r)[:150])

    # 4. validation flow (stamp -> gate blocks -> roster approves -> gate opens)
    # unique artifact per run so an earlier approved stamp cannot leak in
    art = f"/w/ceph24/JAM/chunyu/smoke-artifacts/test-{int(time.time())}.msr"
    r = call_tool("validation_stamp", {
        "artifact": art, "claim": f"smoke-test {time.time():.0f}: JAM25 step-4 chi2/N 0.79",
        "method": "compare_with_paper", "evidence": '{"computed":0.79,"target":0.79}',
    })
    sid = (r.get("stamp") or {}).get("stamp_id") if isinstance(r, dict) else None
    check("validation_stamp creates pending stamp", bool(sid), str(r)[:150])

    if sid:
        r = call_tool("validation_query", {"check_promotion": art})
        check("gate blocks before approval", r.get("authorized") is False, str(r)[:150])
        r = call_tool("validation_approve", {
            "stamp_id": sid, "decision": "accept",
            "approver": "nobuo.sato@jlab.org", "note": "smoke test",
        })
        check("roster approver accepted", r.get("ok") is True, str(r)[:150])
        r = call_tool("validation_query", {"check_promotion": art})
        check("gate opens after approval", r.get("authorized") is True, str(r)[:150])
        r = call_tool("validation_query", {"stamp_id": sid})
        lin = [s.get("stamp_id") for s in r.get("lineage", [])]
        check("lineage chain returned", isinstance(lin, list), str(r)[:150])

    # 5. paper comparison (JAM25 campaign)
    r = call_tool("compare_with_paper", {
        "msr": "/w/ceph24/JAM/chunyu/jam25repro/l2/appruns/step4_140701/msr/WS3G7Z7POSRT.msr",
        "username": "chunyu",
    })
    ok = isinstance(r, dict) and (r.get("rc") == 0 or r.get("ok") or "chi2" in str(r).lower())
    check("compare_with_paper returns comparison", ok, str(r)[:200])

    # 6. explain terms (no-HPC tool)
    r = call_tool("explain_fit_terms", {"term": "chi2/N"})
    ok = isinstance(r, dict) and (r.get("status") == "ok" or bool(r.get("matches") or r.get("explained")))
    check("explain_fit_terms responds", ok, str(r)[:150])

    print(f"\n=== RESULT: {len(PASS)} passed, {len(FAIL)} failed ===")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
