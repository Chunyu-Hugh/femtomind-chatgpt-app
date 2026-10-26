"""Real-use window: who used the gateway, how much, and what went wrong.

    python -m evaluation.audit.usage_report --log audit.jsonl --consent consent.json --since ISO --until ISO

consent.json lists the users who agreed to be reported individually
({"users": ["alice", ...]}). For them: calls, sessions, active days, campaigns
submitted, refusals by code, median latency. Everyone else is counted only in
the totals, never named. The report says how many users there were in all.
"""
import argparse
import json
import statistics

from evaluation.audit.failure_table import records


def report(recs, consent):
    users, totals = {}, {"calls": 0, "sessions": set(), "users": set(), "effects": 0, "submissions": 0, "refusals": {}}
    for rec in recs:
        kind = rec.get("kind")
        who = rec.get("acting") or rec.get("user") or rec.get("principal") or ""
        if kind not in ("call", "job", "effect") or not who:
            continue
        totals["users"].add(who)
        u = users.setdefault(who, {"calls": 0, "sessions": set(), "days": set(), "submissions": 0, "refusals": {}, "latencies": [], "tools": {}})
        day = str(rec.get("ts", ""))[:10]
        if rec.get("sid"):
            u["sessions"].add(rec["sid"])
            totals["sessions"].add(rec["sid"])
        if day:
            u["days"].add(day)
        if kind == "call":
            totals["calls"] += 1
            u["calls"] += 1
            u["tools"][rec.get("tool", "?")] = u["tools"].get(rec.get("tool", "?"), 0) + 1
            if isinstance(rec.get("duration_ms"), (int, float)):
                u["latencies"].append(rec["duration_ms"])
            code = (rec.get("outcome") or {}).get("code")
            if code:
                u["refusals"][code] = u["refusals"].get(code, 0) + 1
                totals["refusals"][code] = totals["refusals"].get(code, 0) + 1
        elif kind == "job" and rec.get("code"):
            u["refusals"][rec["code"]] = u["refusals"].get(rec["code"], 0) + 1
            totals["refusals"][rec["code"]] = totals["refusals"].get(rec["code"], 0) + 1
        elif kind == "effect":
            totals["effects"] += 1
            if rec.get("effect") in ("submit", "append", "rerun", "reproduce"):
                u["submissions"] += 1
                totals["submissions"] += 1
    agreed = set(consent.get("users", []))
    out = {"schema": "femtomind.usage_report.v1", "users_total": len(totals["users"]), "users_reported": sorted(agreed & set(users)),
           "users_not_reported": len(set(users) - agreed), "calls": totals["calls"], "sessions": len(totals["sessions"]),
           "effects": totals["effects"], "submissions": totals["submissions"],
           "refusals": dict(sorted(totals["refusals"].items(), key=lambda kv: -kv[1])), "per_user": {}}
    for who in out["users_reported"]:
        u = users[who]
        out["per_user"][who] = {"calls": u["calls"], "sessions": len(u["sessions"]), "active_days": len(u["days"]),
                                "submissions": u["submissions"], "refusals": dict(sorted(u["refusals"].items(), key=lambda kv: -kv[1])),
                                "median_latency_ms": statistics.median(u["latencies"]) if u["latencies"] else None,
                                "top_tools": dict(sorted(u["tools"].items(), key=lambda kv: -kv[1])[:8])}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.audit.usage_report")
    ap.add_argument("--log", required=True)
    ap.add_argument("--consent", required=True)
    ap.add_argument("--since")
    ap.add_argument("--until")
    args = ap.parse_args(argv)
    with open(args.consent, encoding="utf-8") as fh:
        consent = json.load(fh)
    print(json.dumps(report(records(args.log, args.since, args.until), consent), indent=1))


if __name__ == "__main__":
    main()
