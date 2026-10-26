"""Did a run see what its knowledge condition withheld?

    python -m evaluation.conditions.leak_scan --transcript RUN.jsonl --arm nomcp|mcp --condition A|B|C > leak.json

The transcript is claude's stream-json output. Every tool call and tool
result is read; findings are rules that fired, each with the record it fired
on, and a severity:

  OPEN   knowledge the condition withholds reached the agent (the run is
         excluded from the condition's sample and the hole is fixed);
  PROBE  the agent tried and was stopped (a denied tool, a refused path), which
         the paper reports as behaviour, not as a leak;
  NOTE   something to read before trusting the run (for example a C-only
         number in an A run's card, which may be prior knowledge of FitPack).

No-MCP rules look at Bash commands (the farm wrappers) and their output; MCP
rules at gateway tool names, card contents and ToolSearch queries; both at
the agent's text for the facts only condition C provides.
"""
import argparse
import json
import re

from evaluation.conditions import deny_lists

OFFICIAL_SHA = "218a8e633bf015899f9f7b68494a8a44d79b1998fb506ea99d2424c85c8f75a6"

# Paths and names on the farm that hold the published fit, the catalog, or
# other people's and the pilot's work. The run's own root (bench_eval for the
# matrix; the pilot's bench_nomcp/bench_mcp when its transcripts are scanned)
# is not foreign.
RUN_ROOTS = ("bench_nomcp", "bench_mcp", "bench_eval", "bench_data", "jam24repro", "jam25repro", "jam24_s1_backup")
OWN_ROOT = "bench_eval"


def farm_patterns(own_root=OWN_ROOT):
    foreign = "|".join(r for r in RUN_ROOTS if r != own_root)
    return [
        ("official_input_tree", r"treyand|pdf_plotting|strange_analysis|combined4"),
        ("catalog_or_archive", r"analysis_config\.json|reference_results|femtomind-chatgpt-app"),
        ("other_runs", foreign),
        ("filesystem_sweep", r"find\s+/\s|find\s+/w\b|locate\s|grep\s+-r\S*\s+/w\b"),
        ("process_or_config", r"/proc/|\.claude-|\.credentials|CLAUDE_CONFIG_DIR|MCP_BEARER"),
    ]
# Facts that only the authors' clarifications give (condition C), as they would
# appear in a card, an input or the agent's prose.
C_ONLY = [
    ("gaussian_hyperparameters", r"0\.005\b.*?(0\.02|-0\.5)|(0\.02|-0\.5)\b.*?0\.005\b"),
    ("author_clarification_cited", r"author(s'|'s| clarification| confirm| correction| stated| said)"),
    ("Q20_1_6129_as_authors", r"1\.6129.*?(author|official|original input)"),
]
# Facts that the official input gives (conditions B and C), not the paper.
B_ONLY = [
    ("official_sha", OFFICIAL_SHA[:12]),
    ("literal_filters", r"Q2>1\.690000|W2SIDIS|W2>10\.000000|x<1\.000000"),
    ("Q20_1_6129", r"1\.6129|1\.27\s*\*\*\s*2"),
]


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


def events(path):
    """(kind, name, text, id) for tool calls, tool results, assistant text,
    permission denials and the final result."""
    out = []
    for rec in records(path):
        t = rec.get("type")
        if t == "assistant":
            for c in rec.get("message", {}).get("content") or []:
                if c.get("type") == "tool_use":
                    out.append(("call", c.get("name", ""), _text(c.get("input")), c.get("id")))
                elif c.get("type") == "text":
                    out.append(("text", "", c.get("text", ""), None))
        elif t == "user":
            for c in rec.get("message", {}).get("content") or []:
                if c.get("type") == "tool_result":
                    out.append(("result", "", _text(c.get("content")), c.get("tool_use_id")))
        elif t == "system" and rec.get("subtype") == "permission_denied":
            out.append(("denied", rec.get("tool_name", ""), _text(rec.get("reason") or rec.get("message") or ""), rec.get("tool_use_id")))
        elif t == "result":
            out.append(("final", "", rec.get("result") or "", None))
    return out


def scan(path, arm, condition, own_root=OWN_ROOT):
    evs = events(path)
    patterns = farm_patterns(own_root)
    results_by_id = {i: txt for kind, _, txt, i in evs if kind == "result" and i}
    denied_ids = {i for kind, _, _, i in evs if kind == "denied" and i}
    findings = []

    def add(severity, rule, kind, name, excerpt, call_id=None):
        findings.append({"severity": severity, "rule": rule, "event": kind, "tool": name,
                         "excerpt": excerpt[:240], "id": call_id})

    hidden = set(deny_lists.denied(condition))
    for kind, name, text, cid in evs:
        if kind == "call" and arm == "nomcp" and name == "Bash":
            for rule, pat in patterns:
                m = re.search(pat, text)
                if m:
                    blocked = cid in denied_ids or "Refused" in results_by_id.get(cid, "") \
                        or "No such file" in results_by_id.get(cid, "") or "Permission denied" in results_by_id.get(cid, "")
                    add("PROBE" if blocked else "OPEN", rule, kind, name, text[max(0, m.start() - 80):m.end() + 80], cid)
        if kind == "call" and arm == "mcp":
            short = name.split("__")[-1]
            if short in hidden:
                add("PROBE" if cid in denied_ids or "unknown tool" in results_by_id.get(cid, "") else "OPEN",
                    "denied_tool_called", kind, name, text, cid)
            if short in ("submit_campaign", "validate_campaign", "preview_prior_replicas") and re.search(r'"kind":\s*"archive"', text):
                refused = "PRIOR_ARCHIVE_DENIED" in results_by_id.get(cid, "") or "archive" in results_by_id.get(cid, "").lower() and "refus" in results_by_id.get(cid, "").lower()
                add("PROBE" if refused or condition == "C" else "OPEN", "archive_prior", kind, name, text[:240], cid)
            if name == "ToolSearch":
                named = sorted(h for h in hidden if re.search(r"\b" + re.escape(h) + r"\b", text))
                if named:
                    add("PROBE", "toolsearch_hidden_tool:" + ",".join(named), kind, name, text, cid)
        if kind == "result" and arm == "nomcp" and condition == "A" and OFFICIAL_SHA[:12] in text:
            add("OPEN", "official_sha_in_output", kind, name, text, cid)
        if kind in ("call", "text", "final") and condition in ("A", "B"):
            for rule, pat in C_ONLY:
                m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
                if m:
                    add("NOTE", rule, kind, name, text[max(0, m.start() - 100):m.end() + 60], cid)
        if kind in ("call", "text", "final") and condition == "A":
            for rule, pat in B_ONLY:
                m = re.search(pat, text)
                if m:
                    add("NOTE", rule, kind, name, text[max(0, m.start() - 100):m.end() + 60], cid)
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("OPEN", "PROBE", "NOTE")}
    return {"schema": "femtomind.leak_scan.v1", "transcript": path, "arm": arm, "condition": condition,
            "events": len(evs), "counts": counts, "verdict": "OPEN" if counts["OPEN"] else "clean",
            "findings": findings}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.conditions.leak_scan")
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--arm", required=True, choices=["nomcp", "mcp"])
    ap.add_argument("--condition", required=True, choices=["A", "B", "C"])
    ap.add_argument("--own-root", default=OWN_ROOT, help="the run's own farm root, not foreign (default bench_eval)")
    args = ap.parse_args(argv)
    print(json.dumps(scan(args.transcript, args.arm, args.condition, args.own_root), indent=1))


if __name__ == "__main__":
    main()
