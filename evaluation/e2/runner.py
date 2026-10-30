"""E2 — ablation: which layer catches which fault.

E1 asks the full gateway about each mutant. E2 asks four gateways: the full one
and one with each layer removed (validate, catalog parity, human gate). A fault
whose verdict changes when a layer is removed is caught by that layer; the
matrix names the layer, and the paper can say what the architecture *does*
rather than what it is.

    python3 -m evaluation.e2.runner --commit HEAD --port 8766 [--dry-run] [--out DIR]

The gateways are local (127.0.0.1) with the evaluation's own roots, as in the
matrix; each ablation is a `JAM_GW_ABLATE` value the gateway itself records in
every reply it produced with a check removed. Costs nothing but a minute per
ablation: nothing is queued, validate is read-only.
"""
import argparse
import json
import os
import time

from evaluation.e1 import runner as e1
from evaluation.e1.mutants import MUTANTS
from evaluation.matrix import gateway

# "" is the full gateway; the rest are single-layer removals.
ABLATIONS = ("", "validate", "catalog", "gate")
LABEL = {"": "full", "validate": "-validate", "catalog": "-catalog", "gate": "-gate"}


def run(commit, port, out_dir, analysis_id="JAM24.final", ablations=ABLATIONS):
    os.makedirs(out_dir, exist_ok=True)
    reports = {}
    for ab in ablations:
        label = "e2_" + (ab or "full")
        info = gateway.start(label, commit, "full", "C", port, out_dir, ablate=ab)
        try:
            tok = gateway.token()
            path = os.path.join(out_dir, "e1_%s.json" % (ab or "full"))
            reports[ab] = e1.run(gateway.url(port), tok, path, analysis_id)
            reports[ab]["gateway"] = {k: info.get(k) for k in ("label", "sha", "ablate", "tools")}
        finally:
            gateway.stop(port, out_dir)
    combined = {"schema": "femtomind.e2_ablation.v1", "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "commit": commit, "ablations": list(ablations), "runs": reports}
    with open(os.path.join(out_dir, "e2.json"), "w") as fh:
        json.dump(combined, fh, indent=1, sort_keys=True)
    return combined


def _verdict(report, fault):
    for row in report.get("rows") or []:
        if row.get("id") == fault:
            return row.get("outcome"), row.get("code")
    return None, None


def caught_by(combined):
    """Fault -> the layer(s) whose removal lets it through, or None."""
    faults = [r["id"] for r in (combined["runs"].get("") or {}).get("rows") or [] if r["id"] != "baseline"]
    out = {}
    for fault in faults:
        full = _verdict(combined["runs"].get(""), fault)[0]
        layers = []
        for ab in combined["ablations"]:
            if not ab:
                continue
            v = _verdict(combined["runs"].get(ab) or {}, fault)[0]
            if v and v != full:
                layers.append(ab)
        out[fault] = layers
    return out


def markdown(combined):
    ablations = [a for a in combined["ablations"]]
    head = "| Fault | " + " | ".join(LABEL.get(a, a) for a in ablations) + " | Caught by |"
    sep = "|---" * (len(ablations) + 2) + "|"
    by = caught_by(combined)
    rows = [head, sep]
    faults = [r["id"] for r in (combined["runs"].get("") or {}).get("rows") or [] if r["id"] != "baseline"]
    for fault in faults:
        cells = []
        for ab in ablations:
            v, code = _verdict(combined["runs"].get(ab) or {}, fault)
            cells.append("%s%s" % (v or "?", " (%s)" % code if code else ""))
        layers = by.get(fault) or []
        rows.append("| `%s` | %s | %s |" % (fault, " | ".join(cells), ", ".join(layers) or "not caught here"))
    return "\n".join(rows)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", default="HEAD")
    p.add_argument("--port", type=int, default=int(os.environ.get("EVAL_GW_PORT", "8766")))
    p.add_argument("--out", default=os.path.expanduser("~/bench_eval/e2"))
    p.add_argument("--analysis-id", default="JAM24.final")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    if a.dry_run:
        print(json.dumps({"commit": a.commit, "port": a.port, "out": a.out,
                          "gateways": [{"label": "e2_" + (ab or "full"), "ablate": ab} for ab in ABLATIONS],
                          "faults": [m for m, _w, _f in MUTANTS],
                          "cost": "no queue, no tokens; about a minute per gateway"}, indent=1))
        return
    out = run(a.commit, a.port, os.path.expanduser(a.out), a.analysis_id)
    text = markdown(out)
    with open(os.path.join(os.path.expanduser(a.out), "e2.md"), "w") as fh:
        fh.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
