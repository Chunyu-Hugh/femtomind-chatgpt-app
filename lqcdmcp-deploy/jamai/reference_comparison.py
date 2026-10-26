"""L3: compare a campaign's fit with the published fit's archive, dataset by dataset.

This file is a program for the compute host (shipped as text, run inside the
scientific container where numpy is available; the gateway itself stays
stdlib-only). REQUEST is filled in before it is sent:

    REQUEST = {"campaign_cache": "<campaign>/analysis/cache",   # the gateway's per-dataset documents
               "archive": "<archive dir>",                       # reference_results/JAM24/data/<id>
               "subsamples": 1000, "seed": 20261026}

Two chi2 definitions are computed for every dataset both sides have:
  mean_prediction   chi2 of the ensemble-mean prediction against the data
                    (the archive's chi2_data; the JAM papers' table);
  mean_replica      the mean over replicas of each replica's chi2.
A dataset is comparable only when the campaign's and the archive's value and
alpha arrays are exactly equal; otherwise it is listed as not_comparable with
the reason (a replaced workbook, a different cut).

The null distribution answers "is the campaign's number what a subsample of
the published ensemble of the same size would give?": N_c replicas are drawn
from the archive's 791 without replacement, B times, with the SAME draws for
every dataset (so the global sum is a proper statistic), and both chi2
definitions are computed on each draw. The empirical two-sided p uses the +1
correction; z is (observed - null mean) / null sd. Multiple-comparison
corrections (Benjamini-Hochberg, Holm) are applied by the caller.
"""
import json
import os
import re
import sys

REQUEST = None  # filled in before the program is sent: __REQUEST__


def _key_file(cache, key):
    return os.path.join(cache, "datasets", re.sub(r"[^A-Za-z0-9._-]", "_", key.replace(":", "_")) + ".json")


def main():
    import numpy as np
    req = REQUEST
    with open(os.path.join(req["archive"], "summary.json"), encoding="utf-8") as fh:
        summary = json.load(fh)
    rows = summary["datasets"]
    out = {"schema": "femtomind.reference_comparison.v1", "archive_id": summary.get("archive_id"),
           "catalog_ref": summary.get("catalog_ref"), "reference_replicas": summary.get("replica_count"),
           "campaign_cache": req["campaign_cache"], "datasets": [], "not_comparable": [], "global": {}}
    camp, ref = {}, {}
    n_c = None
    for row in rows:
        key = row["key"]
        path = _key_file(req["campaign_cache"], key)
        if not os.path.isfile(path):
            out["not_comparable"].append({"key": key, "reason": "not in the campaign"})
            continue
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        value = np.asarray(doc["columns"]["value"], float)
        alpha = np.asarray(doc["columns"]["alpha"], float)
        thy_c = np.asarray(doc["theory"], float)
        if n_c is None:
            n_c = thy_c.shape[0]
        z = np.load(os.path.join(req["archive"], "datasets", key.replace(":", "_") + ".npz"))
        if not (np.array_equal(value, z["value"]) and np.array_equal(alpha, z["alpha"])):
            out["not_comparable"].append({"key": key, "reason": "experimental value/alpha differ (changed workbook or cut)",
                                          "campaign_points": int(value.size), "archive_points": int(z["value"].size)})
            continue
        thy_r = np.load(os.path.join(req["archive"], "replicas", key.replace(":", "_") + ".npz"))["thy"]
        camp[key] = (value, alpha, thy_c)
        ref[key] = thy_r
    if not camp:
        out["error"] = "no comparable dataset"
        print(json.dumps(out))
        return
    out["campaign_replicas"] = int(n_c)
    rng = np.random.default_rng(int(req.get("seed", 20261026)))
    B = int(req.get("subsamples", 1000))
    n_ref = next(iter(ref.values())).shape[0]
    draws = [rng.choice(n_ref, size=min(n_c, n_ref), replace=False) for _ in range(B)]

    def chi2_mean_prediction(thy, value, alpha):
        return float(np.sum(((thy.mean(axis=0) - value) / alpha) ** 2))

    def chi2_mean_replica(thy, value, alpha):
        return float(np.mean(np.sum(((thy - value) / alpha) ** 2, axis=1)))

    defs = {"mean_prediction": chi2_mean_prediction, "mean_replica": chi2_mean_replica}
    global_obs = {d: 0.0 for d in defs}
    global_ref = {d: 0.0 for d in defs}
    global_null = {d: np.zeros(B) for d in defs}
    for key in camp:
        value, alpha, thy_c = camp[key]
        thy_r = ref[key]
        rec = {"key": key, "npts": int(value.size)}
        for d, fn in defs.items():
            obs = fn(thy_c, value, alpha)
            full = fn(thy_r, value, alpha)
            null = np.array([fn(thy_r[idx], value, alpha) for idx in draws])
            mu, sd = float(null.mean()), float(null.std(ddof=1)) if B > 1 else 0.0
            lo = float(np.sum(null <= obs)); hi = float(np.sum(null >= obs))
            p = min(1.0, 2.0 * (min(lo, hi) + 1.0) / (B + 1.0))
            rec[d] = {"campaign": obs, "reference_full": full, "null_mean": mu, "null_sd": sd,
                      "z": (obs - mu) / sd if sd > 0 else None, "p": p,
                      "null_quantiles": {q: float(np.quantile(null, float(q))) for q in ("0.025", "0.5", "0.975")}}
            global_obs[d] += obs
            global_ref[d] += full
            global_null[d] += null
        out["datasets"].append(rec)
    for d in defs:
        null = global_null[d]
        obs = global_obs[d]
        mu, sd = float(null.mean()), float(null.std(ddof=1)) if B > 1 else 0.0
        lo = float(np.sum(null <= obs)); hi = float(np.sum(null >= obs))
        out["global"][d] = {"campaign": obs, "reference_full": global_ref[d], "null_mean": mu, "null_sd": sd,
                            "z": (obs - mu) / sd if sd > 0 else None, "p": min(1.0, 2.0 * (min(lo, hi) + 1.0) / (B + 1.0)),
                            "datasets": len(camp), "points": int(sum(v[0].size for v in camp.values()))}
    out["subsamples"] = B
    out["seed"] = int(req.get("seed", 20261026))
    print(json.dumps(out))


if __name__ == "__main__":
    main()
