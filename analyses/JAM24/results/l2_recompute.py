#!/usr/bin/env python3
"""L2: recompute the JAM24 theory at the archived parameters and compare.

Runs on the farm, inside the scientific container, with the current FitPack:

    python3 -B l2_recompute.py --archive DIR --fitpack ROOT --out DIR --first 0 --last 19 \
        [--grid-path /w/ceph24/JAM/share/fitpack_grids] [--grid8-path ...]

For each archived replica (parameters.npz rows first..last) the fit's own
configuration (replicas_cut['conf'] from original_parameter_bundle.npy.gz,
bootstrap off) is loaded, FitPack's RESMAN is built once, the replica's
parameters are set through parman.order, and get_residuals(par, initial=True)
is called. For every dataset the fitted prediction (shift + theory),
residuals, correlated residuals and normalization residuals are compared with
the archive's stored arrays: max absolute and relative difference, ULP
distance, number of exactly equal points, and the archive builder's own
float64 reassociation bound (gamma_8). A dataset whose experimental value or
alpha arrays differ from the archive's is 'not_comparable' and skipped.

Deviations from the historical run that this recompute cannot avoid are
recorded, not hidden: the grid paths are remapped from the historical
/work/JAM paths to the shared grids, and the environment (FitPack commit,
container, numpy and BLAS configuration, thread settings, node) is captured.
Nothing is written outside --out.
"""
import argparse
import contextlib
import copy
import gzip
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import time
import warnings


def environment(fitpack, np):
    env = {"python": sys.version.split()[0], "numpy": np.__version__, "node": platform.node(),
           "container": os.environ.get("APPTAINER_CONTAINER") or os.environ.get("SINGULARITY_CONTAINER"),
           "threads": {k: os.environ.get(k) for k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")}}
    try:
        env["fitpack_commit"] = subprocess.check_output(["git", "-C", fitpack, "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        env["fitpack_dirty"] = bool(subprocess.check_output(["git", "-C", fitpack, "status", "--porcelain", "--untracked-files=no"], text=True, stderr=subprocess.DEVNULL).strip())
    except (OSError, subprocess.CalledProcessError):
        env["fitpack_commit"] = None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            np.show_config()
        except Exception:  # noqa: BLE001
            pass
    env["numpy_config"] = buf.getvalue()[:3000]
    return env


def load_bundle(path):
    import numpy as np
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rb") as fh:
        data = fh.read()
    bundle = np.load(io.BytesIO(data), allow_pickle=True, encoding="latin1").item()
    return bundle, hashlib.sha256(data).hexdigest()


def compare(ref, got):
    import numpy as np
    ref = np.asarray(ref, dtype=float)
    got = np.asarray(got, dtype=float)
    if ref.shape != got.shape:
        return {"shape_ref": list(ref.shape), "shape_got": list(got.shape), "comparable": False}
    if ref.size == 0:
        return {"n": 0, "exact": 0, "max_abs": 0.0, "max_rel": 0.0, "max_ulp": 0, "comparable": True}
    diff = np.abs(ref - got)
    scale = np.maximum(np.abs(ref), np.finfo(float).tiny)
    ulp = diff / np.spacing(np.abs(ref))
    return {"n": int(ref.size), "exact": int(np.sum(ref == got)), "max_abs": float(diff.max()),
            "max_rel": float((diff / scale).max()), "max_ulp": float(ulp.max()), "median_ulp": float(np.median(ulp)),
            "comparable": True}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", required=True)
    ap.add_argument("--fitpack", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--first", type=int, default=0)
    ap.add_argument("--last", type=int, default=0)
    ap.add_argument("--grid-path", default="/w/ceph24/JAM/share/fitpack_grids")
    ap.add_argument("--grid8-path", default=None)
    args = ap.parse_args(argv)
    t0 = time.time()
    sys.path.insert(0, args.fitpack)
    os.environ["FITPACK"] = args.fitpack
    import numpy as np
    bundle, bundle_sha = load_bundle(os.path.join(args.archive, "original_parameter_bundle.npy.gz"))
    with open(os.path.join(args.archive, "summary.json"), encoding="utf-8") as fh:
        summary = json.load(fh)
    with open(os.path.join(args.archive, "parameter_metadata.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    params = np.load(os.path.join(args.archive, "parameters.npz"))["values"]
    order = [tuple(o) for o in meta["order"]]
    if params.shape[1] != len(order):
        raise SystemExit("parameter columns %d != order length %d" % (params.shape[1], len(order)))
    from tools.config import conf
    from qcdlib.aux import AUX
    conf.clear()
    conf.update(copy.deepcopy(bundle["conf"]))
    deviations = []
    conf["bootstrap"] = False
    deviations.append({"key": "bootstrap", "historical": bundle["conf"].get("bootstrap"), "used": False,
                       "why": "central data; no resampling of the experimental values"})
    for key, new in (("grid path", args.grid_path), ("grid-8 path", args.grid8_path or args.grid_path)):
        if key in conf and conf[key] != new:
            deviations.append({"key": key, "historical": conf[key], "used": new, "why": "historical path is not mounted here"})
            conf[key] = new
    conf["aux"] = AUX()
    from fitlib.resman import RESMAN
    logs = io.StringIO()
    with contextlib.redirect_stdout(logs), warnings.catch_warnings(record=True) as warns:
        warnings.simplefilter("always")
        resman = RESMAN(nworkers=1, parallel=False, datasets=True)
    resman.parman.order = order
    setup_s = time.time() - t0
    datasets = [(row["reaction"], row["dataset_id"], row["key"]) for row in summary["datasets"]]
    exp = {}
    for reaction, did, key in datasets:
        z = np.load(os.path.join(args.archive, "datasets", key.replace(":", "_") + ".npz"))
        exp[key] = {"value": z["value"], "alpha": z["alpha"]}
    stored = {key: np.load(os.path.join(args.archive, "replicas", key.replace(":", "_") + ".npz")) for _, _, key in datasets}
    eps = np.finfo(np.float64).eps
    gamma8 = 8 * eps / (1 - 8 * eps)
    out = {"schema": "femtomind.l2_recompute.v1", "archive": args.archive, "bundle_sha256": bundle_sha,
           "catalog_ref": summary.get("catalog_ref"), "archive_id": summary.get("archive_id"), "first": args.first, "last": args.last,
           "environment": environment(args.fitpack, np), "deviations": deviations, "setup_seconds": setup_s,
           "resman_warnings": sorted({type(w.message).__name__ + ": " + str(w.message)[:120] for w in warns})[:20],
           "replicas": []}
    for i in range(args.first, min(args.last, params.shape[0] - 1) + 1):
        t1 = time.time()
        with contextlib.redirect_stdout(io.StringIO()):
            resman.get_residuals(params[i], initial=True)
        rec = {"replica": i, "datasets": {}, "chi2_data": 0.0, "seconds": None}
        for reaction, did, key in datasets:
            res_obj = getattr(resman, reaction + "res", None)
            tabs = getattr(res_obj, "tabs", {}) if res_obj is not None else {}
            idx = int(did) if did.isdigit() else did
            tab = tabs.get(idx) or tabs.get(str(idx))
            d = {"comparable": False}
            if tab is None:
                d["reason"] = "dataset not loaded by the current RESMAN"
            elif not (np.array_equal(np.asarray(tab["value"], float), exp[key]["value"]) and np.array_equal(np.asarray(tab["alpha"], float), exp[key]["alpha"])):
                d["reason"] = "experimental value/alpha differ from the archive (workbook changed)"
            else:
                d["comparable"] = True
                st = stored[key]
                pred = np.asarray(tab["prediction"], float)
                res = np.asarray(tab["residuals"], float)
                d["prediction"] = compare(st["thy"][i], pred)
                d["residuals"] = compare(st["res"][i], res)
                if "r-residuals" in tab:
                    d["rres"] = compare(st["rres"][i], np.asarray(tab["r-residuals"], float))
                if "shift" in tab:
                    d["shift"] = compare(st["shift"][i], np.asarray(tab["shift"], float))
                shift_ref = np.abs(st["shift"][i])
                bound = gamma8 * ((np.abs(exp[key]["value"]) + np.abs(st["thy"][i]) + 2 * shift_ref) / exp[key]["alpha"] + np.abs(st["res"][i]))
                d["within_gamma8_bound"] = bool(np.all(np.abs(res - st["res"][i]) <= bound))
                d["chi2_recomputed"] = float(np.sum(res ** 2))
                d["chi2_stored"] = float(np.sum(st["res"][i] ** 2))
                rec["chi2_data"] += d["chi2_recomputed"]
            rec["datasets"][key] = d
        rec["seconds"] = round(time.time() - t1, 2)
        out["replicas"].append(rec)
        print("replica %d done in %.1fs, chi2_data %.6f" % (i, rec["seconds"], rec["chi2_data"]), file=sys.stderr, flush=True)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "l2_%04d_%04d.json" % (args.first, args.last))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, default=str)
    print(path)


if __name__ == "__main__":
    main()
