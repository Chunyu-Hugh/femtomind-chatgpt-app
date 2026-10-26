"""The failure table: tool x outcome x refusal code x workflow stage.

    python -m evaluation.audit.failure_table --log ~/.jam_gateway/audit.jsonl [--since ISO --until ISO] [--md]

Reads the gateway's audit log (one JSON record per line; kinds 'call', 'job',
'effect'; see lqcdmcp-deploy/jam_gateway.py _audit_call/_audit_job) and
counts, for every tool, how often it returned ok, refused (by stable code) or
failed, with the p50/p90 of its duration. Tools are mapped to the stages of
the workflow the paper describes, so the table also says where in a campaign
refusals happen. Refusals inside background jobs (validate, submit) are
counted from the 'job' records, where the gateway puts them.

Nothing in the log is an argument value: the gateway records argument names
and hashes only, so this table never sees a card, a password or a token.
"""
import argparse
import json
import statistics
import time

STAGES = {
    "connect": ("ping", "check_hpc_connection", "connect_hpc", "use_workspace", "setup_workspace", "use_ai_branch", "check_project", "deploy"),
    "explore": ("campaign_options", "list_analysis_catalog", "get_analysis_catalog", "get_analysis_requirements",
                "preview_analysis_configuration", "preview_compiled_input", "campaign_card_from_catalog", "jam_guide",
                "fs_list", "fs_read", "slurm_partitions", "list_campaigns", "preview_prior_replicas"),
    "validate": ("validate_campaign",),
    "submit": ("submit_campaign", "append_replicas", "rerun_replicas", "reproduce_campaign", "submit_slurm_job", "draft_slurm_job",
               "pending_confirmations", "confirmation_phrase"),
    "monitor": ("campaign_status", "campaign_logs", "analysis_result", "get_console", "cancel_campaign", "delete_campaign"),
    "analyze": ("campaign_summary", "campaign_datasets", "analyze_by_experiment", "campaign_pdf_plots", "campaign_ff_plots",
                "postprocess_campaign", "list_analysis_results", "get_analysis_fit_statistics", "get_analysis_theory_predictions",
                "compare_with_reference", "get_thesis_plot", "open_panel"),
}
STAGE_OF = {t: s for s, tools in STAGES.items() for t in tools}


def records(path, since=None, until=None):
    lo = _ts(since) if since else None
    hi = _ts(until) if until else None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            t = _ts(rec.get("ts")) if rec.get("ts") else None
            if (lo is not None and t is not None and t < lo) or (hi is not None and t is not None and t > hi):
                continue
            yield rec


def _ts(value):
    try:
        return time.mktime(time.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def _pct(values, q):
    if not values:
        return None
    values = sorted(values)
    k = max(0, min(len(values) - 1, int(round(q * (len(values) - 1)))))
    return values[k]


def table(recs):
    rows = {}
    for rec in recs:
        kind = rec.get("kind")
        if kind == "call":
            tool = rec.get("tool") or "?"
            outcome = rec.get("outcome") or {}
            status, code = outcome.get("status") or "?", outcome.get("code")
            dur = rec.get("duration_ms")
        elif kind == "job":
            tool = (rec.get("job_kind") or "?") + " (job)"
            status, code, dur = rec.get("status") or "?", rec.get("code"), None
        else:
            continue
        row = rows.setdefault(tool, {"tool": tool, "stage": STAGE_OF.get(tool.replace(" (job)", ""), "other"), "calls": 0,
                                     "ok": 0, "refused": 0, "error": 0, "codes": {}, "durations_ms": []})
        row["calls"] += 1
        if status == "ok" or (status not in ("error", "refused", "failed") and not code):
            row["ok"] += 1
        elif code:
            row["refused"] += 1
            row["codes"][code] = row["codes"].get(code, 0) + 1
        else:
            row["error"] += 1
        if isinstance(dur, (int, float)):
            row["durations_ms"].append(dur)
    out = []
    for tool, row in sorted(rows.items(), key=lambda kv: (kv[1]["stage"], kv[0])):
        d = row.pop("durations_ms")
        row["p50_ms"], row["p90_ms"] = _pct(d, 0.5), _pct(d, 0.9)
        row["codes"] = dict(sorted(row["codes"].items(), key=lambda kv: -kv[1]))
        out.append(row)
    by_stage = {}
    for row in out:
        s = by_stage.setdefault(row["stage"], {"calls": 0, "ok": 0, "refused": 0, "error": 0, "codes": {}})
        for k in ("calls", "ok", "refused", "error"):
            s[k] += row[k]
        for c, n in row["codes"].items():
            s["codes"][c] = s["codes"].get(c, 0) + n
    return {"schema": "femtomind.failure_table.v1", "tools": out, "stages": by_stage,
            "codes": dict(sorted(((c, n) for s in by_stage.values() for c, n in s["codes"].items()), key=lambda kv: -kv[1]))}


def markdown(t):
    lines = ["| stage | tool | calls | ok | refused | error | refusal codes | p50 ms | p90 ms |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in t["tools"]:
        codes = ", ".join("%s %d" % kv for kv in r["codes"].items()) or "–"
        lines.append("| %s | %s | %d | %d | %d | %d | %s | %s | %s |" % (r["stage"], r["tool"], r["calls"], r["ok"], r["refused"], r["error"], codes,
                                                                       r["p50_ms"] if r["p50_ms"] is not None else "–", r["p90_ms"] if r["p90_ms"] is not None else "–"))
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.audit.failure_table")
    ap.add_argument("--log", required=True)
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--md", action="store_true")
    args = ap.parse_args(argv)
    t = table(records(args.log, args.since, args.until))
    print(markdown(t) if args.md else json.dumps(t, indent=1))


if __name__ == "__main__":
    main()
