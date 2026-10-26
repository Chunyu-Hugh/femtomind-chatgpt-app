"""What happened in one run, from its transcript.

    python -m evaluation.matrix.classify --transcript RUN.jsonl --arm nomcp|mcp [--started-at ISO --ended-at ISO --rc N]

The transcript is claude's stream-json output (--output-format stream-json
--verbose). The numbers follow the pilot's summary() (bench_nomcp/
run_stage1.sh): cost, model time and token counts are cumulative in the last
result record; turns add up over result records; permission denials and rate
limit events are counted; wall time is measured by the launcher
(started_at/ended_at) because the transcript's timestamps are not reliable
across a resumed session.

An execution is one attempt to run a fit on the farm: without MCP, a
farm-submit call; with MCP, a submit_campaign call followed through the
analysis_result polls of its job_id. It succeeded when the farm accepted the
job (a Slurm id came back).
"""
import argparse
import json
import re
import time

POLL_TOOLS = {"nomcp": {"farm-status"}, "mcp": {"campaign_status", "campaign_logs", "analysis_result"}}


def records(path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _iso(ts):
    return time.mktime(time.strptime(ts.strip(), "%Y-%m-%dT%H:%M:%SZ")) if ts else None


def _short(name):
    return name.split("__")[-1]


def classify(path, arm, started_at=None, ended_at=None, rc=None):
    calls, results, denied, finals, rate_limits = {}, {}, set(), [], 0
    order = []
    init = None
    for rec in records(path):
        t = rec.get("type")
        if t == "system" and rec.get("subtype") == "init":
            init = {"model": rec.get("model"), "tools": len(rec.get("tools") or []), "mcp_servers": rec.get("mcp_servers"),
                    "session_id": rec.get("session_id")}
        elif t == "system" and rec.get("subtype") == "permission_denied":
            denied.add(rec.get("tool_use_id"))
        elif t == "rate_limit_event":
            rate_limits += 1
        elif t == "assistant":
            for c in rec.get("message", {}).get("content") or []:
                if c.get("type") == "tool_use":
                    calls[c["id"]] = {"name": c.get("name", ""), "input": c.get("input")}
                    order.append(c["id"])
        elif t == "user":
            for c in rec.get("message", {}).get("content") or []:
                if c.get("type") == "tool_result":
                    results[c.get("tool_use_id")] = {"is_error": bool(c.get("is_error")), "text": _text(c.get("content"))}
        elif t == "result":
            finals.append(rec)
    last = finals[-1] if finals else {}
    tool_counts, tool_errors = {}, {"permission": 0, "runtime": 0}
    polls = 0
    for cid in order:
        name = _short(calls[cid]["name"])
        tool_counts[name] = tool_counts.get(name, 0) + 1
        if cid in denied:
            tool_errors["permission"] += 1
        elif results.get(cid, {}).get("is_error"):
            tool_errors["runtime"] += 1
        if name in POLL_TOOLS[arm] or (arm == "nomcp" and name == "Bash" and "farm-status" in _text(calls[cid]["input"])):
            polls += 1
    executions = _executions(arm, order, calls, results, denied)
    out = {"schema": "femtomind.run_classification.v1", "transcript": path, "arm": arm, "init": init,
           "finished": bool(finals), "timed_out": (rc == 124) if rc is not None else None, "rc": rc,
           "endings": [f.get("subtype") for f in finals], "is_error": bool(last.get("is_error")) if last else None,
           "cost_usd": last.get("total_cost_usd"), "turns": sum(f.get("num_turns") or 0 for f in finals),
           "api_seconds": (last.get("duration_api_ms") or 0) / 1000.0 if last else None,
           "duration_seconds": (last.get("duration_ms") or 0) / 1000.0 if last else None,
           "wall_seconds": (_iso(ended_at) - _iso(started_at)) if started_at and ended_at else None,
           "tokens": {m: {k: v.get(k) for k in ("inputTokens", "outputTokens", "cacheReadInputTokens",
                                                 "cacheCreationInputTokens", "thinkingTokens", "costUSD")}
                      for m, v in (last.get("modelUsage") or {}).items()},
           "tool_calls": sum(tool_counts.values()), "tool_counts": dict(sorted(tool_counts.items())),
           "tool_errors": tool_errors, "permission_denials": len(denied), "rate_limit_events": rate_limits,
           "polls": polls, "executions": executions,
           "n_executions": len(executions), "n_successful_executions": sum(1 for e in executions if e["accepted"]),
           "first_execution_success": bool(executions) and executions[0]["accepted"],
           "final_answer": (last.get("result") or "")[:4000] if last else "",
           "reported_job_id": _reported_job(last.get("result") or "") if last else None}
    return out


def _reported_job(text):
    m = re.search(r"\b(?:job(?: id)?|Slurm job|JobID)\D{0,12}(\d{6,9})", text, re.IGNORECASE)
    return m.group(1) if m else None


def _executions(arm, order, calls, results, denied):
    out = []
    if arm == "nomcp":
        for cid in order:
            c = calls[cid]
            if _short(c["name"]) == "Bash" and "farm-submit" in _text(c["input"]):
                r = results.get(cid, {})
                m = re.search(r"Submitted job (\d+)", r.get("text", ""))
                out.append({"tool_use_id": cid, "kind": "farm-submit", "accepted": bool(m), "slurm_job_id": m.group(1) if m else None,
                            "denied": cid in denied, "error": r.get("text", "")[:300] if not m else None})
        return out
    # MCP: submit_campaign, followed through analysis_result polls of its job_id
    job_results = {}
    for cid in order:
        c = calls[cid]
        if _short(c["name"]) == "analysis_result":
            jid = (c["input"] or {}).get("job_id") if isinstance(c["input"], dict) else None
            if jid and cid in results:
                job_results.setdefault(jid, []).append(results[cid]["text"])
    for cid in order:
        c = calls[cid]
        if _short(c["name"]) != "submit_campaign":
            continue
        r = results.get(cid, {})
        texts = [r.get("text", "")]
        try:
            first = json.loads(r.get("text", "") or "{}")
        except ValueError:
            first = {}
        jid = first.get("job_id") if isinstance(first, dict) else None
        if jid:
            texts += job_results.get(jid, [])
        final = None
        for t in texts:
            try:
                d = json.loads(t)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("status") not in (None, "running"):
                final = d
        # the finished submission says status "submitted" with the collection and the Slurm id as job_id
        accepted = bool(final) and final.get("status") in ("ok", "submitted") and bool(final.get("collection_id"))
        slurm = None
        if final:
            m = re.search(r'"(?:array_job_id|slurm_job_id|job_id|job)":\s*"?(\d{6,9})', json.dumps(final))
            slurm = m.group(1) if m else None
        out.append({"tool_use_id": cid, "kind": "submit_campaign", "accepted": bool(accepted), "job_id": jid,
                    "collection_id": (final or {}).get("collection_id"), "slurm_job_id": slurm,
                    "denied": cid in denied, "confirmed": bool(isinstance(c["input"], dict) and c["input"].get("confirm")),
                    "error": None if accepted else ((final or first).get("message") or (final or first).get("error") or r.get("text", ""))[:300]})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.matrix.classify")
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--arm", required=True, choices=["nomcp", "mcp"])
    ap.add_argument("--started-at")
    ap.add_argument("--ended-at")
    ap.add_argument("--rc", type=int)
    args = ap.parse_args(argv)
    print(json.dumps(classify(args.transcript, args.arm, args.started_at, args.ended_at, args.rc), indent=1))


if __name__ == "__main__":
    main()
