"""Render an E1 sweep as the paper's fault table.

    python3 -m evaluation.e1.report ~/bench_eval/e1/e1_20261030.json [--md out.md]

Verdicts: a card the gateway refuses is caught before it can queue; one it
accepts with a warning is flagged; one it accepts silently is the failure mode
the paper is about. The baseline row (the official card, unchanged) must read
"accepted" with no warning: if it does not, the sweep is not comparable.
"""
import argparse
import json

from evaluation.e1 import pilot

VERDICT = {"refused": "caught", "warned": "flagged", "accepted": "silent",
           "pending": "pending", "not_applicable": "n/a", "mcp_error": "error"}


def table(report):
    rows = []
    rows.append("| Fault | Field | What was changed | Gateway | Code | Verdict | Differences it reported |")
    rows.append("|---|---|---|---|---|---|---|")
    for r in report["rows"]:
        diffs = "; ".join(str(d)[:60] for d in (r.get("differences") or [])[:2]) or "-"
        rows.append("| `%s` | %s | %s | %s | %s | **%s** | %s |" % (
            r["id"], r.get("field") or "-", (r.get("what") or "")[:70], r.get("outcome") or "?",
            r.get("code") or "-", VERDICT.get(r.get("outcome"), r.get("outcome")), diffs))
    return "\n".join(rows)


def summary(report):
    counts = {}
    for r in report["rows"]:
        if r["id"] == "baseline":
            continue
        counts[r.get("outcome")] = counts.get(r.get("outcome"), 0) + 1
    total = sum(counts.values())
    return ("%d injected faults: %d caught, %d flagged, %d silent, %d other"
            % (total, counts.get("refused", 0), counts.get("warned", 0),
               counts.get("accepted", 0), total - counts.get("refused", 0) - counts.get("warned", 0)
               - counts.get("accepted", 0)))


def markdown(report):
    base = next((r for r in report["rows"] if r["id"] == "baseline"), None)
    out = ["# E1 fault injection, %s" % report.get("analysis_id", ""), "",
           "Gateway: `%s`  " % report.get("url", ""),
           "Run: %s" % report.get("when", ""), ""]
    if base:
        out += ["Baseline (official card, unchanged): **%s**, %s points, code `%s`."
                % (VERDICT.get(base.get("outcome"), base.get("outcome")), base.get("points"), base.get("code") or "-"), ""]
    out += [summary(report), "", table(report), "",
            "A fault is *caught* when `validate_campaign` refuses the card, *flagged* when it returns a warning, "
            "and *silent* when it accepts a card the published fit does not match.", "",
            "## The same faults, as they actually happened", "",
            pilot.summary(), "", pilot.table(), "",
            "Each row is a fault a real agent committed in the pilot; the mutant named in the first column is "
            "that fault re-run against the gateway. The pilot agents reported both campaigns as submitted and "
            "fitting; the settings above were found afterwards, by grading the input."]
    return "\n".join(out) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("report")
    p.add_argument("--md")
    a = p.parse_args()
    with open(a.report) as f:
        report = json.load(f)
    text = markdown(report)
    if a.md:
        with open(a.md, "w") as f:
            f.write(text)
    print(text)


if __name__ == "__main__":
    main()
