"""Tables from the matrix's per-run records.

    python -m evaluation.matrix.aggregate --results ~/bench_eval/results [--out aggregate]

Each run leaves RUN.summary.json (written by runner.py: classification, probe
verdict, grade verdict, leak verdict). Runs are grouped by arm, condition and
features; a run whose leak scan is OPEN is excluded and counted. For each
group: n, the success counts with Wilson intervals (job accepted, accepted at
the first attempt, fit started, Tier 0 / Tier 1 parity against the target),
pass@k and pass^k over the repeats, cost, wall time and turns (mean, median,
bootstrap CI), Tier 2 coverage; and for each condition the no-MCP vs MCP
comparison (Fisher's exact test on 'fit started', Newcombe's interval on the
difference of proportions).
"""
import argparse
import glob
import json
import math
import os
import statistics

from evaluation.common import stats

SUCCESSES = {"accepted": lambda s: (s.get("classify") or {}).get("n_successful_executions", 0) > 0,
             "first_try": lambda s: bool((s.get("classify") or {}).get("first_execution_success")),
             "fit_started": lambda s: (s.get("probe") or {}).get("verdict") == "started_fitting",
             "tier0": lambda s: bool((s.get("grade") or {}).get("tier0")),
             "tier1": lambda s: bool((s.get("grade") or {}).get("tier1"))}
NUMBERS = {"cost_usd": lambda s: (s.get("classify") or {}).get("cost_usd"),
           "wall_minutes": lambda s: ((s.get("classify") or {}).get("wall_seconds") or 0) / 60.0 if (s.get("classify") or {}).get("wall_seconds") else None,
           "api_minutes": lambda s: ((s.get("classify") or {}).get("api_seconds") or 0) / 60.0 if (s.get("classify") or {}).get("api_seconds") else None,
           "turns": lambda s: (s.get("classify") or {}).get("turns"),
           "polls": lambda s: (s.get("classify") or {}).get("polls"),
           "tier2": lambda s: (s.get("grade") or {}).get("tier2_fraction"),
           "minutes_to_fit": lambda s: ((s.get("probe") or {}).get("seconds_to_first_fit_mark") or 0) / 60.0
           if (s.get("probe") or {}).get("seconds_to_first_fit_mark") else None}


def load(results_dir):
    runs = []
    for path in sorted(glob.glob(os.path.join(results_dir, "*.summary.json"))):
        with open(path, encoding="utf-8") as fh:
            runs.append(json.load(fh))
    return runs


def fisher_exact(a, b, c, d):
    """Two-sided Fisher exact p for the 2x2 table [[a, b], [c, d]]."""
    def logc(n, k):
        return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
    n = a + b + c + d
    r1, c1 = a + b, a + c
    lo, hi = max(0, r1 + c1 - n), min(r1, c1)
    probs = {x: math.exp(logc(r1, x) + logc(n - r1, c1 - x) - logc(n, c1)) for x in range(lo, hi + 1)}
    p_obs = probs[a]
    return min(1.0, sum(p for p in probs.values() if p <= p_obs + 1e-12))


def newcombe(c1, n1, c2, n2, alpha=0.05):
    """Newcombe's hybrid score interval for p1 - p2."""
    if not n1 or not n2:
        return None
    l1, u1 = stats.wilson(c1, n1, alpha)
    l2, u2 = stats.wilson(c2, n2, alpha)
    d = c1 / n1 - c2 / n2
    return {"diff": d, "low": d - math.sqrt((c1 / n1 - l1) ** 2 + (u2 - c2 / n2) ** 2),
            "high": d + math.sqrt((u1 - c1 / n1) ** 2 + (c2 / n2 - l2) ** 2)}


def group_key(s):
    return (s["arm"], s["condition"], s["features"])


def aggregate(runs):
    groups = {}
    for s in runs:
        groups.setdefault(group_key(s), []).append(s)
    out = {"schema": "femtomind.matrix_aggregate.v1", "groups": {}, "comparisons": {}}
    for key, members in sorted(groups.items()):
        included = [s for s in members if (s.get("leak") or {}).get("verdict") != "OPEN"]
        g = {"arm": key[0], "condition": key[1], "features": key[2], "n": len(included),
             "excluded_open_leak": len(members) - len(included), "runs": [s["run"] for s in included],
             "successes": {}, "numbers": {}}
        for name, fn in SUCCESSES.items():
            flags = [bool(fn(s)) for s in included]
            g["successes"][name] = stats.summarize_successes(sum(flags), len(flags))
        for name, fn in NUMBERS.items():
            values = [fn(s) for s in included if fn(s) is not None]
            g["numbers"][name] = stats.summarize_values(values) if values else None
        g["probe_verdicts"] = {}
        for s in included:
            v = (s.get("probe") or {}).get("verdict") or "no_probe"
            g["probe_verdicts"][v] = g["probe_verdicts"].get(v, 0) + 1
        g["grade_targets"] = sorted({(s.get("grade") or {}).get("requested_target") or "?" for s in included})
        out["groups"]["/".join(key)] = g
    conditions = sorted({k[1] for k in groups})
    for cond in conditions:
        nomcp = [s for k, ms in groups.items() if k[0] == "nomcp" and k[1] == cond for s in ms if (s.get("leak") or {}).get("verdict") != "OPEN"]
        for feat in sorted({k[2] for k in groups if k[0] == "mcp" and k[1] == cond}):
            mcp = [s for s in groups.get(("mcp", cond, feat), []) if (s.get("leak") or {}).get("verdict") != "OPEN"]
            if not nomcp or not mcp:
                continue
            cmp = {}
            for name, fn in SUCCESSES.items():
                a = sum(1 for s in mcp if fn(s)); b = len(mcp) - a
                c = sum(1 for s in nomcp if fn(s)); d = len(nomcp) - c
                cmp[name] = {"mcp": [a, len(mcp)], "nomcp": [c, len(nomcp)], "fisher_p": fisher_exact(a, b, c, d),
                             "newcombe_mcp_minus_nomcp": newcombe(a, len(mcp), c, len(nomcp))}
            for name in ("cost_usd", "wall_minutes", "turns"):
                xs = [NUMBERS[name](s) for s in mcp if NUMBERS[name](s) is not None]
                ys = [NUMBERS[name](s) for s in nomcp if NUMBERS[name](s) is not None]
                if xs and ys:
                    cmp[name] = {"mcp_mean": statistics.fmean(xs), "nomcp_mean": statistics.fmean(ys),
                                 "ratio_nomcp_over_mcp": statistics.fmean(ys) / statistics.fmean(xs) if statistics.fmean(xs) else None}
            out["comparisons"]["%s/mcp-%s_vs_nomcp" % (cond, feat)] = cmp
    return out


def markdown(agg):
    lines = ["| arm | cond | features | n | accepted | first try | fit started | Tier 0 | Tier 1 | Tier 2 | cost $ | wall min | turns |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]

    def succ(g, k):
        s = g["successes"].get(k) or {}
        c, n = s.get("successes", 0), s.get("runs", 0)
        ci = s.get("wilson95")
        return "%d/%d" % (c, n) + (" [%.2f, %.2f]" % tuple(ci) if ci and n else "")

    def num(g, k):
        v = g["numbers"].get(k)
        if not v or not v.get("n"):
            return "–"
        return "%.2f [%.2f, %.2f]" % (v["mean"], v["mean95"][0], v["mean95"][1])
    for key, g in agg["groups"].items():
        lines.append("| %s | %s | %s | %d | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            g["arm"], g["condition"], g["features"], g["n"], succ(g, "accepted"), succ(g, "first_try"), succ(g, "fit_started"),
            succ(g, "tier0"), succ(g, "tier1"), num(g, "tier2"), num(g, "cost_usd"), num(g, "wall_minutes"), num(g, "turns")))
    if agg["comparisons"]:
        lines += ["", "| comparison | metric | mcp | nomcp | Fisher p | Newcombe diff [95%] |", "| --- | --- | --- | --- | --- | --- |"]
        for key, cmp in agg["comparisons"].items():
            for name in ("accepted", "first_try", "fit_started", "tier1"):
                c = cmp.get(name)
                if c:
                    nc = c["newcombe_mcp_minus_nomcp"] or {}
                    lines.append("| %s | %s | %d/%d | %d/%d | %.3f | %.2f [%.2f, %.2f] |" % (
                        key, name, c["mcp"][0], c["mcp"][1], c["nomcp"][0], c["nomcp"][1], c["fisher_p"],
                        nc.get("diff", float("nan")), nc.get("low", float("nan")), nc.get("high", float("nan"))))
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.matrix.aggregate")
    ap.add_argument("--results", required=True)
    ap.add_argument("--out", help="basename for .json and .md (default: <results>/aggregate)")
    args = ap.parse_args(argv)
    agg = aggregate(load(args.results))
    base = args.out or os.path.join(args.results, "aggregate")
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump(agg, fh, indent=1, sort_keys=True, default=str)
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write(markdown(agg))
    print(markdown(agg))


if __name__ == "__main__":
    main()
