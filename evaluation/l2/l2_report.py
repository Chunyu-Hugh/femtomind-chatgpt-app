"""The L2 report from the recompute chunks.

    python -m evaluation.l2.l2_report --chunks ~/bench_eval/l2/<stamp> [--md]

Honest sections: how many replica x dataset predictions were bit-identical,
the distribution of the largest ULP distances, the gamma_8 reassociation
verdict per dataset, the datasets that were not comparable (changed
workbooks), the recomputed data chi2 per replica against the stored one, and
the environment and deviations the worker recorded (grid paths, threads).
"""
import argparse
import glob
import json
import os
import statistics


def load(chunks_dir):
    chunks = []
    for path in sorted(glob.glob(os.path.join(chunks_dir, "l2_[0-9]*.json"))):
        with open(path, encoding="utf-8") as fh:
            chunks.append(json.load(fh))
    return chunks


def report(chunks):
    per_dataset = {}
    replicas = 0
    chi2_pairs = []
    envs, devs = [], []
    for ch in chunks:
        envs.append({k: ch["environment"].get(k) for k in ("fitpack_commit", "fitpack_dirty", "numpy", "container", "threads", "node")})
        devs.append(ch.get("deviations"))
        for rec in ch["replicas"]:
            replicas += 1
            stored = 0.0
            for key, d in rec["datasets"].items():
                row = per_dataset.setdefault(key, {"comparable": 0, "not_comparable": 0, "reasons": {}, "exact_prediction_replicas": 0,
                                                   "max_ulp_prediction": 0.0, "max_abs_prediction": 0.0, "max_ulp_residuals": 0.0,
                                                   "gamma8_ok": 0, "gamma8_fail": 0, "points": None})
                if not d.get("comparable"):
                    row["not_comparable"] += 1
                    row["reasons"][d.get("reason", "?")] = row["reasons"].get(d.get("reason", "?"), 0) + 1
                    continue
                row["comparable"] += 1
                p, r = d["prediction"], d["residuals"]
                row["points"] = p.get("n")
                if p.get("exact") == p.get("n"):
                    row["exact_prediction_replicas"] += 1
                row["max_ulp_prediction"] = max(row["max_ulp_prediction"], p.get("max_ulp") or 0)
                row["max_abs_prediction"] = max(row["max_abs_prediction"], p.get("max_abs") or 0)
                row["max_ulp_residuals"] = max(row["max_ulp_residuals"], r.get("max_ulp") or 0)
                row["gamma8_ok" if d.get("within_gamma8_bound") else "gamma8_fail"] += 1
                stored += d.get("chi2_stored") or 0.0
            chi2_pairs.append((rec["replica"], rec["chi2_data"], stored))
    comparable = [k for k, r in per_dataset.items() if r["comparable"]]
    exact_all = sum(1 for k in comparable if per_dataset[k]["exact_prediction_replicas"] == per_dataset[k]["comparable"])
    ulps = [per_dataset[k]["max_ulp_prediction"] for k in comparable]
    rel = [abs(a - b) / b for _, a, b in chi2_pairs if b]
    out = {"schema": "femtomind.l2_report.v1", "chunks": len(chunks), "replicas": replicas, "datasets": len(per_dataset),
           "datasets_comparable": len(comparable), "datasets_not_comparable": sorted(k for k in per_dataset if not per_dataset[k]["comparable"]),
           "datasets_bit_identical_all_replicas": exact_all,
           "max_ulp_prediction": {"max": max(ulps) if ulps else None, "median_over_datasets": statistics.median(ulps) if ulps else None},
           "gamma8": {"datasets_all_ok": sum(1 for k in comparable if per_dataset[k]["gamma8_fail"] == 0),
                      "datasets_with_failures": sorted(k for k in comparable if per_dataset[k]["gamma8_fail"])},
           "chi2_data": {"max_relative_difference": max(rel) if rel else None, "replicas": len(chi2_pairs)},
           "environment": envs[0] if envs else None,
           "nodes": sorted({e.get("node") for e in envs if e.get("node")}),
           "environments_differ_beyond_node": len({json.dumps({k: v for k, v in e.items() if k != "node"}, sort_keys=True) for e in envs}) > 1,
           "deviations": devs[0] if devs else None, "per_dataset": per_dataset,
           "caveats": ["BLAS thread count can change floating-point sums; the worker pins OPENBLAS/OMP threads to 1 and records them.",
                       "The historical grid files (U09) are not the mounted ones; where predictions differ, the ULP distance is reported, not a bit-identity claim.",
                       "Residuals and shifts follow the current FitPack definitions; the archive stores the original notebook's arrays."]}
    return out


def markdown(r):
    lines = ["# L2 recompute at the archived parameters", "",
             "- replicas recomputed: %d; datasets: %d (%d comparable)" % (r["replicas"], r["datasets"], r["datasets_comparable"]),
             "- datasets bit-identical for every replica: %d" % r["datasets_bit_identical_all_replicas"],
             "- largest ULP distance of a prediction: %s (median over datasets %s)" % (r["max_ulp_prediction"]["max"], r["max_ulp_prediction"]["median_over_datasets"]),
             "- gamma_8 bound held for all replicas in %d datasets; failures in: %s" % (r["gamma8"]["datasets_all_ok"], ", ".join(r["gamma8"]["datasets_with_failures"]) or "none"),
             "- data chi2 per replica, max relative difference recomputed vs stored: %s" % r["chi2_data"]["max_relative_difference"],
             "- not comparable: %s" % (", ".join(r["datasets_not_comparable"]) or "none"),
             "- environment: FitPack %s (%s), numpy %s, %d node(s); software differs between chunks: %s" % (
                 (r["environment"] or {}).get("fitpack_commit"), "dirty" if (r["environment"] or {}).get("fitpack_dirty") else "clean",
                 (r["environment"] or {}).get("numpy"), len(r["nodes"]), r["environments_differ_beyond_node"]), ""]
    lines += ["| dataset | points | replicas | exact | max ULP (pred) | max |Δ| (pred) | gamma8 fail |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for k, d in sorted(r["per_dataset"].items()):
        lines.append("| %s | %s | %d | %d | %.3g | %.3g | %d |" % (k, d["points"], d["comparable"], d["exact_prediction_replicas"],
                                                                   d["max_ulp_prediction"], d["max_abs_prediction"], d["gamma8_fail"]))
    lines += ["", "Caveats:"] + ["- " + c for c in r["caveats"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.l2.l2_report")
    ap.add_argument("--chunks", required=True)
    ap.add_argument("--md", action="store_true")
    args = ap.parse_args(argv)
    r = report(load(args.chunks))
    print(markdown(r) if args.md else json.dumps(r, indent=1, default=str))


if __name__ == "__main__":
    main()
