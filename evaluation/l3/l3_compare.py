"""Compare a campaign with the JAM24 archive and correct for multiple datasets.

    python -m evaluation.l3.l3_compare --campaign-cache /w/ceph24/JAM/chunyu/.../campaigns/<folder>/analysis/cache \
        [--subsamples 1000 --seed 20261026] [--md] > l3.json

Ships lqcdmcp-deploy/jamai/reference_comparison.py to the farm (container,
numpy) with the request filled in, then adds Benjamini-Hochberg and Holm
adjusted p-values per chi2 definition and renders the table the paper prints:
reaction | id | npts | chi2 (mean prediction) ref | campaign | null mean | sd |
z | p | p_BH | chi2 (mean replica) ref | campaign | z | p | comparable | note.
The campaign's cache is the gateway's analysis/cache (postprocess_campaign /
campaign_summary write it).
"""
import argparse
import json
import os

from evaluation.common import paths, ssh

PROGRAM = os.path.join(paths.APP_ROOT, "lqcdmcp-deploy", "jamai", "reference_comparison.py")


def archive_dir():
    with open(os.path.join(paths.APP_ROOT, "analyses", "JAM24", "results", "reference_manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)["server_directory"]


def program(request):
    with open(PROGRAM, encoding="utf-8") as fh:
        src = fh.read()
    return src.replace("REQUEST = None  # filled in before the program is sent: __REQUEST__",
                       "REQUEST = json.loads(%s)" % json.dumps(json.dumps(request)))


def benjamini_hochberg(ps):
    """Adjusted p-values (step-up), same order as ps."""
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    adj = [0.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        prev = min(prev, ps[i] * m / rank)
        adj[i] = min(1.0, prev)
    return adj


def holm(ps):
    """Holm step-down adjusted p-values, same order as ps."""
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, ps[i] * (m - rank))
        adj[i] = min(1.0, running)
    return adj


def adjust(result):
    for d in ("mean_prediction", "mean_replica"):
        ps = [row[d]["p"] for row in result.get("datasets", [])]
        if not ps:
            continue
        for row, bh, hm in zip(result["datasets"], benjamini_hochberg(ps), holm(ps)):
            row[d]["p_BH"] = bh
            row[d]["p_Holm"] = hm
    return result


def compare(campaign_cache, subsamples=1000, seed=20261026, timeout=1200):
    request = {"campaign_cache": campaign_cache, "archive": archive_dir(), "subsamples": subsamples, "seed": seed}
    out = ssh.python(program(request), container=True, timeout=timeout)
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    return adjust(json.loads(lines[-1]))


def markdown(r):
    lines = ["| key | npts | chi2 mean-pred ref | campaign | null mean | null sd | z | p | p_BH | chi2 mean-rep ref | campaign | z | p |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in r.get("datasets", []):
        a, b = row["mean_prediction"], row["mean_replica"]
        lines.append("| %s | %d | %.2f | %.2f | %.2f | %.2f | %s | %.3f | %.3f | %.2f | %.2f | %s | %.3f |" % (
            row["key"], row["npts"], a["reference_full"], a["campaign"], a["null_mean"], a["null_sd"],
            "%.2f" % a["z"] if a["z"] is not None else "–", a["p"], a.get("p_BH", float("nan")),
            b["reference_full"], b["campaign"], "%.2f" % b["z"] if b["z"] is not None else "–", b["p"]))
    for key, g in (r.get("global") or {}).items():
        lines.append("| GLOBAL %s (%d datasets, %d points) | | %.2f | %.2f | %.2f | %.2f | %s | %.3f | | | | | |" % (
            key, g["datasets"], g["points"], g["reference_full"], g["campaign"], g["null_mean"], g["null_sd"],
            "%.2f" % g["z"] if g["z"] is not None else "–", g["p"]))
    if r.get("not_comparable"):
        lines += ["", "Not comparable: " + "; ".join("%s (%s)" % (x["key"], x["reason"]) for x in r["not_comparable"])]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.l3.l3_compare")
    ap.add_argument("--campaign-cache", required=True)
    ap.add_argument("--subsamples", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20261026)
    ap.add_argument("--md", action="store_true")
    args = ap.parse_args(argv)
    r = compare(args.campaign_cache, args.subsamples, args.seed)
    print(markdown(r) if args.md else json.dumps(r, indent=1))


if __name__ == "__main__":
    main()
