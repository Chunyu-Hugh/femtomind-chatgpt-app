"""The headline number: how often a wrong card reaches the queue unflagged.

Two arms, and the paper must not conflate them:

  * observed (raw LLM): of the faults a paper-only agent actually committed in
    the pilot, how many were flagged before the job started. Six were found
    afterwards by grading the input; none was flagged at submission time.
    This is observational: the faults were not chosen by us.
  * injected (gateway): of the mutants E1 sends to `validate_campaign`, how
    many are refused or flagged. This is controlled.

The apples-to-apples claim uses only the faults that appear in both, joined by
`pilot.MUTANT_TO_OBSERVATION` (n = 6 today). Everything else is context, and the
report says so.

    python3 -m evaluation.e1.silent [--report ~/bench_eval/e1/e1_x.json]
"""
import argparse
import json

from evaluation.common.stats import wilson
from evaluation.e1 import pilot
from evaluation.e1.mutants import EXPECTED


def raw_arm():
    n = len(pilot.PILOT_OBSERVATIONS)
    flagged = 0                      # nothing was flagged at submission time
    return {"n": n, "flagged": flagged, "silent": n - flagged,
            "rate": (n - flagged) / n if n else None, "wilson": wilson(n - flagged, n),
            "source": "pilot 2026-10-24/25, graded 2026-10-26"}


def gateway_arm(report):
    rows = [r for r in report["rows"] if r["id"] != "baseline"]
    refused = [r for r in rows if r.get("outcome") == "refused"]
    flagged = [r for r in rows if r.get("outcome") == "warned"]
    silent = [r for r in rows if r.get("outcome") == "accepted"]
    n = len(rows)
    stopped = len(refused) + len(flagged)
    return {"n": n, "refused": len(refused), "flagged": len(flagged), "silent": len(silent),
            "rate": stopped / n if n else None, "wilson": wilson(stopped, n),
            "silent_ids": [r["id"] for r in silent], "refused_ids": [r["id"] for r in refused],
            "flagged_ids": [r["id"] for r in flagged], "source": report.get("when")}


def paired(report):
    """The faults in both arms: what the raw agent wrote, what the gateway says."""
    verdict = {r["id"]: r for r in report["rows"]}
    out = []
    for obs in pilot.PILOT_OBSERVATIONS:
        row = verdict.get(obs["mutant"])
        if not row or row["id"] == "baseline":
            continue
        out.append({"fault": obs["mutant"], "raw": "silent", "gateway": row.get("outcome"),
                    "code": row.get("code"), "what": obs["what_happened"]})
    stopped = sum(1 for p in out if p["gateway"] in ("refused", "warned"))
    return {"n": len(out), "stopped": stopped, "rate": stopped / len(out) if out else None,
            "wilson": wilson(stopped, len(out)), "rows": out}


def score(report):
    """How the gateway's verdicts compare with what a correct gateway should do."""
    rows = {r["id"]: r for r in report["rows"] if r["id"] != "baseline"}
    ok, wrong = [], []
    for mutant_id, expected in EXPECTED.items():
        row = rows.get(mutant_id)
        if not row:
            continue
        actual = row.get("outcome")
        hit = ((expected == "refused" and actual == "refused")
               or (expected == "downstream" and actual in ("accepted", "warned"))
               or (expected == "accepted" and actual == "accepted"))
        (ok if hit else wrong).append({"fault": mutant_id, "expected": expected, "actual": actual,
                                       "code": row.get("code")})
    n = len(ok) + len(wrong)
    return {"n": n, "correct": len(ok), "rate": len(ok) / n if n else None,
            "wilson": wilson(len(ok), n), "wrong": wrong}


def markdown(report=None):
    raw = raw_arm()
    lines = ["### Silent faults, by arm", ""]
    lines.append("| Arm | n | Stopped before the queue | Silent | Rate [95% Wilson] |")
    lines.append("|---|---|---|---|---|")
    lines.append("| observed (paper-only agent) | %d | %d | %d | %.2f [%.2f, %.2f] |" % (
        raw["n"], raw["flagged"], raw["silent"], raw["rate"], *raw["wilson"]))
    if report:
        gw = gateway_arm(report)
        lines.append("| injected (gateway) | %d | %d | %d | %.2f [%.2f, %.2f] |" % (
            gw["n"], gw["refused"] + gw["flagged"], gw["silent"], gw["rate"], *gw["wilson"]))
        p = paired(report)
        lines += ["", "Paired subset (the same faults in both arms, n = %d): %d of %d stopped by the "
                      "gateway, %.2f [%.2f, %.2f]; every one of them reached the job unflagged without it." % (
                          p["n"], p["stopped"], p["n"], p["rate"] or 0.0, *p["wilson"]), ""]
        lines.append("| Fault | Without the gateway | With it | Code |")
        lines.append("|---|---|---|---|")
        for row in p["rows"]:
            lines.append("| `%s` | silent | %s | %s |" % (row["fault"], row["gateway"], row.get("code") or "-"))
        sc = score(report)
        lines += ["", "Expected-verdict agreement: %d/%d [%.2f, %.2f]%s" % (
            sc["correct"], sc["n"], *sc["wilson"],
            "" if not sc["wrong"] else "; off: " + ", ".join("%s (%s, expected %s)" % (
                w["fault"], w["actual"], w["expected"]) for w in sc["wrong"]))]
        silent_ids = gw["silent_ids"]
        if silent_ids:
            lines += ["", "Still silent with the gateway in place: %s. These are the rows the paper must "
                          "name, not hide." % ", ".join("`%s`" % s for s in silent_ids)]
    else:
        lines += ["", "The injected arm needs an E1 run (`evaluation.e1.runner`); until then only the "
                      "observed arm is known."]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report", help="an E1 report JSON")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    report = json.load(open(a.report)) if a.report else None
    if a.json:
        out = {"raw": raw_arm(), "gateway": gateway_arm(report) if report else None,
               "paired": paired(report) if report else None}
        print(json.dumps(out, indent=1))
        return
    print(markdown(report))


if __name__ == "__main__":
    main()
