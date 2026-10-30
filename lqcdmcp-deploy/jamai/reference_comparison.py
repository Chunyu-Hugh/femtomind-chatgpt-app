"""Compare a campaign's fit with a published fit's archive, dataset by dataset.

This file is a program for the compute host (shipped as text, run inside the
scientific container where numpy is available; the gateway itself stays
stdlib-only). REQUEST is filled in before it is sent:

    REQUEST = {"campaign_cache": "<the campaign's published cohort folder>",
               "archives": ["<archive dir>", "<alternate>"],   # the first one present is read
               "subsamples": 1000, "seed": 20261026,
               "replicas": [2, 3]}                              # optional: only these campaign replicas

The campaign side is the gateway's central-data cache: one document per
dataset, <campaign_cache>/datasets/<reaction>_<id>.json, with the data
(columns.value, columns.alpha) and each fitted replica's complete prediction
(theory: raw theory / normalization + profiled systematic shift). The archive
keeps the same quantity for the published replicas (replicas/<key>.npz "thy")
and the data it was fitted to (datasets/<key>.npz).

Two chi2 definitions are computed for every dataset both sides have, both the
point term only (sum of ((prediction - value) / alpha)^2, no correlated or
normalization penalty), which is the per-dataset chi2 campaign_summary shows:
  mean_prediction   chi2 of the ensemble-mean prediction against the data
                    (the archive's chi2_data; the JAM papers' table);
  mean_replica      the mean over replicas of each replica's chi2.
A dataset is comparable only when the campaign's and the archive's value and
alpha arrays are exactly equal; otherwise it is listed as not_comparable with
the reason (a replaced workbook, a different cut). With "replicas", only those
campaign replicas count (the cache names the replica of each prediction row),
and the null draws subsamples of that size.

The null distribution answers "is the campaign's number what a subsample of
the published ensemble of the same size would give?": N_c replicas are drawn
from the archive's replicas without replacement, B times, with the SAME draws
for every dataset (so the global sum is a proper statistic), and both chi2
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


def _replica_number(identity):
    m = re.search(r"(\d+)$", str(identity))
    return int(m.group(1)) if m else None


def _archive_dir(req):
    for path in req.get("archives") or [req.get("archive")]:
        if path and os.path.isfile(os.path.join(path, "summary.json")):
            return path
    return None


def main():
    import numpy as np
    req = REQUEST
    archive = _archive_dir(req)
    if archive is None:
        print(json.dumps({"error": "the reference archive is not readable at %s" % (req.get("archives") or req.get("archive"))}))
        return
    with open(os.path.join(archive, "summary.json"), encoding="utf-8") as fh:
        summary = json.load(fh)
    rows = summary["datasets"]
    out = {"schema": "femtomind.reference_comparison.v2", "archive_id": summary.get("archive_id"),
           "archive_dir": archive, "catalog_ref": summary.get("catalog_ref"),
           "reference_replicas": summary.get("replica_count"), "campaign_cache": req["campaign_cache"],
           "datasets": [], "not_comparable": [], "global": {}}
    camp, ref, published = {}, {}, {}
    n_c = None
    keep = set(int(i) for i in req.get("replicas") or [])
    if keep:
        out["selected_replicas"] = sorted(keep)
    for row in rows:
        key = row["key"]
        path = _key_file(req["campaign_cache"], key)
        if not os.path.isfile(path):
            out["not_comparable"].append({"key": key, "reason": "not in the campaign"})
            continue
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if doc.get("unavailable") or not doc.get("npts"):
            out["not_comparable"].append({"key": key, "reason": "no point of it is left after the campaign's cuts"})
            continue
        value = np.asarray(doc["columns"]["value"], float)
        alpha = np.asarray(doc["columns"]["alpha"], float)
        thy_c = np.asarray(doc["theory"], float)
        if thy_c.ndim != 2 or thy_c.shape[1] != value.size:
            out["not_comparable"].append({"key": key, "reason": "the campaign's predictions do not match its points"})
            continue
        if keep:
            names = doc.get("replicas") or []
            if len(names) != thy_c.shape[0]:
                print(json.dumps({"error": "the campaign's cache does not name the replica of each prediction (%s), "
                                           "so a replica selection cannot be applied" % key}))
                return
            rows_kept = [i for i, name in enumerate(names) if _replica_number(name) in keep]
            if not rows_kept:
                print(json.dumps({"error": "none of the selected replicas %s is in the campaign's cache" % sorted(keep)}))
                return
            thy_c = thy_c[rows_kept]
        if n_c is None:
            n_c = thy_c.shape[0]
        elif thy_c.shape[0] != n_c:
            print(json.dumps({"error": "the campaign's datasets have predictions of different replica counts "
                                       "(%s has %d, others %d): the cache is not one cohort" % (key, thy_c.shape[0], n_c)}))
            return
        data_file = os.path.join(archive, "datasets", key.replace(":", "_") + ".npz")
        thy_file = os.path.join(archive, "replicas", key.replace(":", "_") + ".npz")
        if not (os.path.isfile(data_file) and os.path.isfile(thy_file)):
            out["not_comparable"].append({"key": key, "reason": "the archive has no data or replica predictions for it"})
            continue
        z = np.load(data_file)
        if not (np.array_equal(value, z["value"]) and np.array_equal(alpha, z["alpha"])):
            out["not_comparable"].append({"key": key, "reason": "experimental value/alpha differ (changed workbook or cut)",
                                          "campaign_points": int(value.size), "archive_points": int(z["value"].size)})
            continue
        camp[key] = (value, alpha, thy_c)
        ref[key] = np.load(thy_file)["thy"]
        published[key] = row.get("chi2_data")
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

    def test(obs, null):
        mu, sd = float(null.mean()), float(null.std(ddof=1)) if B > 1 else 0.0
        lo, hi = float(np.sum(null <= obs)), float(np.sum(null >= obs))
        return {"null_mean": mu, "null_sd": sd, "z": (obs - mu) / sd if sd > 0 else None,
                "p": min(1.0, 2.0 * (min(lo, hi) + 1.0) / (B + 1.0)),
                "null_quantiles": {q: float(np.quantile(null, float(q))) for q in ("0.025", "0.5", "0.975")}}

    defs = {"mean_prediction": chi2_mean_prediction, "mean_replica": chi2_mean_replica}
    global_obs = {d: 0.0 for d in defs}
    global_ref = {d: 0.0 for d in defs}
    global_null = {d: np.zeros(B) for d in defs}
    for key in camp:
        value, alpha, thy_c = camp[key]
        thy_r = ref[key]
        rec = {"key": key, "npts": int(value.size), "published_chi2_data": published[key]}
        for d, fn in defs.items():
            obs = fn(thy_c, value, alpha)
            full = fn(thy_r, value, alpha)
            null = np.array([fn(thy_r[idx], value, alpha) for idx in draws])
            rec[d] = dict({"campaign": obs, "reference_full": full}, **test(obs, null))
            global_obs[d] += obs
            global_ref[d] += full
            global_null[d] += null
        out["datasets"].append(rec)
    for d in defs:
        out["global"][d] = dict({"campaign": global_obs[d], "reference_full": global_ref[d],
                                 "datasets": len(camp), "points": int(sum(v[0].size for v in camp.values()))},
                                **test(global_obs[d], global_null[d]))
    out["subsamples"] = B
    out["seed"] = int(req.get("seed", 20261026))
    print(json.dumps(out))


if __name__ == "__main__":
    main()
