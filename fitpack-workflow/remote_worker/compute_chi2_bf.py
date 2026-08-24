#!/usr/bin/env python3
"""
HPC worker: compute bootstrap=False chi² for a batch of replicas.

Invoked remotely by the MCP server (fitpack-workflow/chi2_bootstrap_false.py)
inside an apptainer sif. Runs on ifarm, writes result JSON to a shared path
that the local MCP host polls via ssh_read_file.

Status protocol (worker writes this file at each phase so the local side can
report progress back to the user):

  {STATUS_FILE}   (plain text, one line):
     starting
     loading_resman
     computing_replica_0e9a28
     writing_output
     done
     error: <message>

  {OUTPUT_FILE}   (JSON, written only on success):
     {schema: 1, batch_id, n_total, n_survivors, survivors, run_state,
      process_table, per_idx, compute_seconds, hpc_username, convergence_gate}

     run_state is a per-replica snapshot: [{run, n_iters, latest_chi2_npts,
     passed_gate, drop_reason}, ...] — surfaces *why* a replica was excluded
     so the caller can tell "too noisy", "didn't write progress.json yet",
     "slurm.out missing params", etc. apart without re-sshing.

Usage:
  python3 compute_chi2_bf.py \
      --batch-id  BATCH_ID \
      --runs      run_name1,run_name2,... \
      --remote-base /w/ceph24/JAM/{user}/fitpack_legacy \
      --output    /w/ceph24/JAM/{user}/chi2_cache/BATCH_ID.json \
      --status    /w/ceph24/JAM/{user}/chi2_cache/BATCH_ID.status \
      [--convergence-gate 3.0] \
      [--process-classes /path/to/process_classes.yaml]
"""
from __future__ import annotations
import argparse, copy, glob, json, os, re, sys, time
from pathlib import Path
import numpy as np


def write_status(path: str, line: str) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(line + "\n")
    except Exception:
        pass  # never let status-write crash the worker


def parse_last_params(run_dir: str) -> list | None:
    """Parse parman.gen_report() from the newest *complete* JAM FITTER block.

    Race-robust against stdout block-buffering: a call made mid-run can catch
    the last print_status mid-flush, leaving blocks[-1] truncated (e.g. only
    params 001-010 of a 039-param report flushed to disk). Scoring by match
    count and returning the newest block that achieves the maximum avoids
    reporting a partial set of params as if it were the converged state.

    Returns:
        [(type, name, value), ...]  params of the newest complete block
        []                          slurm.out has JAM FITTER markers but no
                                    parseable parman lines in any block
        None                        no slurm.out, or no JAM FITTER marker yet
                                    (fit hasn't flushed its first status)
    """
    slurm_files = [f for f in os.listdir(run_dir) if f.startswith("slurm_") and f.endswith(".out")]
    if not slurm_files:
        return None
    data = open(f"{run_dir}/{slurm_files[0]}").read()
    blocks = data.split("JAM FITTER")
    if len(blocks) < 2:
        return None
    pat = re.compile(r"^(\d{3})\s+(pdf|norm)\s+(.+?)\s+([-+0-9.eE]+)\s*$")

    def parse_block(block: str) -> list:
        out = []
        for line in block.splitlines():
            if "|" not in line:
                continue
            m = pat.match(line.split("|", 1)[1].strip())
            if m:
                out.append((m.group(2), m.group(3).strip(), float(m.group(4))))
        return out

    # Look at the last several blocks only — older iterations don't help and
    # keep this O(1) regardless of fit length. 8 is a comfortable margin over
    # any reasonable buffer-race window.
    candidates = [parse_block(b) for b in blocks[-8:]]
    max_count = max((len(c) for c in candidates), default=0)
    if max_count == 0:
        return []
    for c in reversed(candidates):
        if len(c) == max_count:
            return c
    return []


def default_process_classes(conf: dict) -> list[tuple[str, callable]]:
    """Auto-build process classes from conf['datasets'].
    Heuristics mirror JAM24: HERMES standalone, fixed-target DIS, HERA DIS, DY."""
    # HERA idx range (JAM/CJ convention): 10026-10039 (NC/CC)
    hera_idx = {i for i in range(10026, 10040)}
    classes = []
    idis_idx = set(conf.get("datasets", {}).get("idis", {}).get("xlsx", {}).keys())
    if 10007 in idis_idx:
        classes.append(("DIS (HERMES)", lambda r, i: r == "idis" and i == 10007))
    ft_idx = {i for i in idis_idx if i not in hera_idx and i != 10007}
    if ft_idx:
        classes.append(("DIS (fixed target)",
                        lambda r, i, s=ft_idx: r == "idis" and i in s))
    hera_present = {i for i in idis_idx if i in hera_idx}
    if hera_present:
        classes.append(("DIS (HERA)",
                        lambda r, i, s=hera_present: r == "idis" and i in s))
    if conf.get("datasets", {}).get("dy"):
        classes.append(("Drell-Yan", lambda r, i: r == "dy"))
    if conf.get("datasets", {}).get("pidis"):
        classes.append(("Polarized DIS", lambda r, i: r == "pidis"))
    return classes


def zscore_fn():
    import scipy.stats as st
    def f(chi2, dof):
        sf = st.chi2.sf(chi2, dof)
        if sf == 0.0: return float("inf")
        if sf == 1.0: return float("-inf")
        return float(st.norm.isf(sf))
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--runs", required=True, help="comma-separated run directory names")
    ap.add_argument("--remote-base", required=True, help="e.g. /w/ceph24/JAM/<user>/fitpack_legacy")
    ap.add_argument("--output", required=True)
    ap.add_argument("--status", required=True)
    ap.add_argument("--convergence-gate", type=float, default=float("inf"),
                    help="chi2/npts upper bound for including a replica. Default "
                         "inf = no filtering; pass a finite number (e.g. 3.0) "
                         "to drop obviously-diverged replicas.")
    ap.add_argument("--process-classes", default="")
    ap.add_argument("--lhapdf-path", default="/work/JAM/ccocuzza/lhapdf/python3/sets")
    args = ap.parse_args()

    t_start = time.time()
    write_status(args.status, "starting")
    # hoisted so the outer `except` can surface it even when we raise
    # before the output JSON is written
    run_state: list[dict] = []

    try:
        # Environment setup — the apptainer sif entry doesn't set these for us
        os.environ.setdefault("FITPACK", args.remote_base)
        os.environ.setdefault("LHAPDF_DATA_PATH", args.lhapdf_path)
        sys.path.insert(0, args.remote_base)
        os.chdir(args.remote_base)

        runs_dir = f"{args.remote_base}/runs"
        run_names = [r.strip() for r in args.runs.split(",") if r.strip()]
        run_paths = [f"{runs_dir}/{n}" for n in run_names]

        # --- 1. filter by convergence ---
        write_status(args.status, "filtering")
        survivors = []
        for d in run_paths:
            rn = os.path.basename(d)
            entry = {"run": rn, "n_iters": None, "latest_chi2_npts": None,
                     "passed_gate": False, "drop_reason": None}
            try:
                pj = json.load(open(f"{d}/progress.json"))
                entry["n_iters"] = len(pj)
                chi2 = pj[-1]["chi2"]
                npts = sum(dd["npts"] for dd in pj[-1]["datasets"].values())
                if not npts:
                    entry["drop_reason"] = "progress.json has npts=0"
                else:
                    latest = chi2 / npts
                    entry["latest_chi2_npts"] = latest
                    if latest < args.convergence_gate:
                        survivors.append(d)
                        entry["passed_gate"] = True
                    else:
                        entry["drop_reason"] = (
                            f"chi2/npts={latest:.3f} >= gate={args.convergence_gate}"
                        )
            except FileNotFoundError:
                entry["drop_reason"] = "no progress.json"
            except Exception as e:
                entry["drop_reason"] = f"{type(e).__name__}: {e}"
            run_state.append(entry)

        if not survivors:
            gate_desc = ("inf (no filter)" if args.convergence_gate == float("inf")
                         else f"{args.convergence_gate}")
            # summarize drop reasons so the error message is self-contained
            # (run_state sidecar is also written by the outer except handler)
            bucket: dict[str, int] = {}
            for e in run_state:
                reason = e["drop_reason"] or ""
                if reason == "no progress.json":
                    key = "no_progress"
                elif reason.startswith("chi2/npts="):
                    key = "over_gate"
                elif reason == "progress.json has npts=0":
                    key = "npts_zero"
                else:
                    key = "other"
                bucket[key] = bucket.get(key, 0) + 1
            summary = ", ".join(f"{k}={v}" for k, v in sorted(bucket.items()))
            raise RuntimeError(
                f"no replicas pass chi²/npts < {gate_desc} "
                f"[{len(run_state)} runs: {summary}]"
            )

        # --- 2. parse fitted params ---
        write_status(args.status, "parsing_slurm")
        parsed: dict[str, list] = {}
        state_by_run = {e["run"]: e for e in run_state}
        for d in survivors:
            p = parse_last_params(d)
            if p is None:
                st = state_by_run.get(os.path.basename(d))
                if st is not None:
                    st["passed_gate"] = False
                    st["drop_reason"] = "slurm.out: fit has not emitted JAM FITTER yet"
                continue
            if len(p) == 0:
                st = state_by_run.get(os.path.basename(d))
                if st is not None:
                    st["passed_gate"] = False
                    st["drop_reason"] = "slurm.out: JAM FITTER present but no parseable parman lines"
                continue
            parsed[d] = p
        survivors = list(parsed.keys())
        if not survivors:
            n_not_started = sum(
                1 for e in run_state
                if (e.get("drop_reason") or "").startswith("slurm.out: fit has not emitted")
            )
            n_unparseable = sum(
                1 for e in run_state
                if (e.get("drop_reason") or "").startswith("slurm.out: JAM FITTER present")
            )
            raise RuntimeError(
                f"failed to parse params from any slurm.out "
                f"[{n_not_started + n_unparseable} gate-passed runs: "
                f"not_started={n_not_started}, unparseable={n_unparseable}]"
            )

        # --- 3. init RESMAN (expensive: DY grid load ~2 min) ---
        write_status(args.status, "loading_resman")
        from tools.config import conf, load_config
        ref = survivors[0]
        load_config(f"{ref}/parameters.yaml")
        conf["bootstrap"] = False  # <-- the whole point of this worker
        conf["verbose"] = 0
        if not conf.get("grid path"):
            conf["grid path"] = f"{args.remote_base}/grids"
            if not Path(conf["grid path"]).exists():
                conf["grid path"] = "/w/ceph24/JAM/share/fitpack_grids"

        def inject(params_list):
            for typ, name, val in params_list:
                if typ == "pdf" and name in conf["params"]["pdf"]:
                    conf["params"]["pdf"][name]["value"] = val
                elif typ == "norm":
                    rxn, idx = name.split("-")
                    idx = int(idx)
                    if rxn in conf["datasets"] and idx in conf["datasets"][rxn]["norm"]:
                        conf["datasets"][rxn]["norm"][idx]["value"] = val

        inject(parsed[ref])
        from fitlib.resman import RESMAN
        resman = RESMAN(nworkers=1, parallel=False, datasets=True)
        parman = resman.parman

        idx_map = {}
        for i, e in enumerate(parman.order):
            code, where, name = e
            key = ("pdf", name) if code == 1 else ("norm", f"{where}-{name}")
            idx_map[key] = i

        # --- 4. per-replica evaluate ---
        replica_chi2, replica_npts, meta = {}, {}, {}
        for rep_dir in survivors:
            rep_name = os.path.basename(rep_dir)
            write_status(args.status, f"computing_{rep_name}")
            par = copy.copy(parman.par)
            for typ, name, val in parsed[rep_dir]:
                k = (typ, name) if typ == "pdf" else ("norm", name)
                if k in idx_map:
                    par[idx_map[k]] = val
            res, rres, nres = resman.get_residuals(par, initial=True)
            for attr, rxn in [("idis_res", "idis"), ("dy_res", "dy"),
                              ("pidis_res", "pidis")]:
                if not hasattr(resman, attr):
                    continue
                robj = getattr(resman, attr)
                if not hasattr(robj, "tabs"):
                    continue
                for idx in sorted(robj.tabs):
                    t = robj.tabs[idx]
                    val   = np.asarray(t["value"])
                    alpha = np.asarray(t["alpha"])
                    thy   = np.asarray(t["prediction"])
                    # Guard against zero/negative errors and NaN theory: without
                    # this a single bad entry produced NaN/inf chi2 that flowed
                    # silently into the summary table.
                    good = (np.isfinite(val) & np.isfinite(thy)
                            & np.isfinite(alpha) & (alpha > 0))
                    if not bool(good.any()):
                        continue
                    r = (val[good] - thy[good]) / alpha[good]
                    c = float(np.sum(r ** 2))
                    n = int(r.size)
                    key = (rxn, idx)
                    replica_chi2.setdefault(key, []).append(c)
                    replica_npts[key] = n
                    if key not in meta:
                        gc = t.get("col",  ["-"])
                        go = t.get("obs",  ["-"])
                        gt = t.get("target") or t.get("tar") or t.get("particles-in") or ["-"]
                        meta[key] = {
                            "col": gc[0] if hasattr(gc, "__getitem__") else "-",
                            "obs": go[0] if hasattr(go, "__getitem__") else "-",
                            "tar": gt[0] if hasattr(gt, "__getitem__") else "-",
                        }

        # --- 5. aggregate ---
        write_status(args.status, "aggregating")
        mean_chi2 = {k: float(np.mean(v)) for k, v in replica_chi2.items()}
        zscore = zscore_fn()

        if args.process_classes and Path(args.process_classes).exists():
            import yaml as _y
            cfg = _y.safe_load(open(args.process_classes))
            classes = []
            for row in cfg.get("process_classes", []):
                nm = row["name"]; rxn = row.get("reaction"); idxs = set(row.get("idx", []))
                classes.append((nm, (lambda r, i, rx=rxn, s=idxs:
                                     r == rx and (not s or i in s))))
        else:
            classes = default_process_classes(conf)

        process_table = []
        for name, matcher in classes:
            sum_chi2 = sum(mean_chi2[k] for k in mean_chi2 if matcher(*k))
            sum_npts = sum(replica_npts[k] for k in mean_chi2 if matcher(*k))
            if sum_npts == 0:
                continue
            process_table.append({
                "process": name,
                "N_dat": sum_npts,
                "chi2_red": sum_chi2 / sum_npts,
                "z_score": zscore(sum_chi2, sum_npts),
            })
        # TOTAL
        total_chi2 = sum(mean_chi2.values())
        total_npts = sum(replica_npts.values())
        process_table.append({
            "process": "TOTAL",
            "N_dat": total_npts,
            "chi2_red": (total_chi2 / total_npts) if total_npts else None,
            "z_score": zscore(total_chi2, total_npts) if total_npts else None,
        })

        per_idx = []
        for key in sorted(mean_chi2):
            rxn, idx = key
            c = mean_chi2[key]; n = replica_npts[key]; m = meta[key]
            per_idx.append({
                "reaction": rxn, "idx": idx, "col": str(m["col"]),
                "tar": str(m["tar"]), "obs": str(m["obs"]),
                "npts": n, "chi2": c, "chi2_red": c / n,
                "z_score": zscore(c, n),
            })

        # --- 6. write output ---
        write_status(args.status, "writing_output")
        result = {
            "schema": 1,
            "batch_id": args.batch_id,
            "convergence_gate": args.convergence_gate,
            "n_total": len(run_names),
            "n_survivors": len(survivors),
            "survivors": [os.path.basename(d) for d in survivors],
            "run_state": run_state,
            "process_table": process_table,
            "per_idx": per_idx,
            "compute_seconds": time.time() - t_start,
            "mode": "bootstrap_false",
        }
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(result, indent=2))
        write_status(args.status, "done")

    except Exception as e:
        import traceback
        write_status(args.status, f"error: {type(e).__name__}: {e}")
        # traceback for debugging
        tb_path = args.status + ".traceback"
        try:
            Path(tb_path).write_text(traceback.format_exc())
        except Exception:
            pass
        # run_state sidecar so the user can see per-replica drop reasons even
        # on error. CANNOT write args.output itself — the MCP consumer treats
        # output-exists as success and would cache the error as 'done'.
        if run_state:
            try:
                sidecar = Path(args.output + ".run_state.json")
                sidecar.parent.mkdir(parents=True, exist_ok=True)
                sidecar.write_text(json.dumps({
                    "batch_id": args.batch_id,
                    "convergence_gate": args.convergence_gate,
                    "error": f"{type(e).__name__}: {e}",
                    "run_state": run_state,
                }, indent=2))
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
