"""
Compute worker for the plot_replica_observable MCP tool.

Reads a JSON request from stdin, computes the requested observable across a
set of replicas, and writes a JSON response to stdout. Progress events for
the live UI trace are emitted on stderr as lines prefixed with `TRACE:`.

Request (stdin):
  {
    "fitpack_root": "/path/to/fitpack_legacy_copy",
    "config_file":  "examples/input.py",
    "replica_paths": ["runs/000_.../parameters.yaml", ...],
    "dist":    "pdf",
    "flavor":  "uv",
    "q2":      100.0,
    "x_grid":  [1e-4, ..., 0.99]
  }

Response (stdout):
  {
    "curves":       [[...], ...],        # N_replica × N_x
    "x_grid":       [...],
    "n_params_free":    int,
    "n_params_varying": int,
    "elapsed_compute_s": float
  }

Invoked by the MCP tool in server.py:
  subprocess.run([fitpack_python, str(WORKER_PATH)],
                 cwd=fitpack_root, input=json.dumps(req),
                 env={...PYTHONPATH=fitpack_root...})

Side-effect: stubs nuclib.deuterium.DEUTERON and nuclib.helium.HELIUM so
RESMAN can bootstrap without the nuclear-SMF grid files being on disk.
This mirrors the documented "hacks" cell in scripts/workbook.ipynb.
"""

from __future__ import annotations

import json
import os
import sys
import time


def _trace(icon: str, text: str, **extra) -> None:
    """Emit a progress event to stderr for the caller to forward to UI."""
    msg = {"icon": icon, "text": text, **extra}
    print("TRACE:" + json.dumps(msg), file=sys.stderr, flush=True)


def _die(msg: str) -> None:
    print(json.dumps({"error": msg}), flush=True)
    sys.exit(1)


def _stub_nuclib() -> None:
    """Prevent RESMAN from touching deuteron/helium grid files.

    These are only needed for nuclear-target observables, not PDF eval.
    The stub keeps the module shape that fitlib/resman.py expects.
    """
    import nuclib.deuterium as _d
    import nuclib.helium as _h

    class _Stub:
        def __init__(self, *a, **kw):
            pass

    _d.DEUTERON = _Stub  # type: ignore[assignment]
    _h.HELIUM = _Stub    # type: ignore[assignment]


def _par_from_replica(rep_conf: dict, order: list, fallback, dist: str = "") -> list:
    """Project a replica's yaml conf onto the current parman.order vector.

    Returns (par_vector, n_missing). When `n_missing > 0`, that many
    parman positions fell back to the central/default value because the
    replica yaml didn't carry a value for them — caller should surface
    this in the trace because a high miss count means we're plotting a
    mix of fitted + default parameters, which is usually a bug
    (mismatched parman order between RESMAN and the replica yaml).
    """
    import numpy as np
    par = np.array(fallback, dtype=float, copy=True)
    n_missing = 0
    n_missing_dist = 0
    for i, entry in enumerate(order):
        kind, k, kk = entry[0], entry[1], entry[2]
        try:
            if kind == 1:
                par[i] = rep_conf["params"][k][kk]["value"]
            else:
                par[i] = rep_conf["datasets"][k]["norm"][kk]["value"]
        except KeyError:
            n_missing += 1
            if kind == 1 and dist and str(k) == dist:
                n_missing_dist += 1
    return par, n_missing, n_missing_dist


def _par_from_flat(flat: dict, order: list, fallback, dist: str = ""):
    """Project a flat {"<family> <key>": value} dict onto parman.order.

    This is the mid-run path. fitlib.maxlike_jam writes progress.json
    entries whose "params" dict is keyed by " ".join(order_entry[1:]) —
    the same join used here — so a running replica projects through the
    identical code path as a finished one, just from a different file.
    """
    import numpy as np
    par = np.array(fallback, dtype=float, copy=True)
    n_missing = 0
    n_missing_dist = 0
    for i, entry in enumerate(order):
        key = " ".join(str(x) for x in entry[1:])
        if key in flat:
            par[i] = float(flat[key])
        else:
            n_missing += 1
            if entry[0] == 1 and dist and str(entry[1]) == dist:
                n_missing_dist += 1
    return par, n_missing, n_missing_dist


def main() -> None:
    t0 = time.time()
    try:
        req = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        _die(f"bad stdin JSON: {e}")
        return

    fitpack_root = req.get("fitpack_root")
    config_file = req.get("config_file", "examples/input.py")
    replica_paths = req.get("replica_paths", [])
    dist = req.get("dist", "pdf")
    # Multi-flavor: caller passes flavors=[...]; the legacy single-flavor
    # request shape is still accepted so older clients don't break.
    flavors = req.get("flavors") or [req.get("flavor", "uv")]
    q2 = float(req.get("q2", 100.0))
    x_grid = req.get("x_grid")

    if not fitpack_root or not os.path.isdir(fitpack_root):
        _die(f"fitpack_root missing or not a directory: {fitpack_root!r}")
        return
    if not replica_paths:
        _die("replica_paths is empty — nothing to plot")
        return
    if not x_grid:
        _die("x_grid is required")
        return

    os.chdir(fitpack_root)
    if fitpack_root not in sys.path:
        sys.path.insert(0, fitpack_root)

    # Imports that require fitpack_root on PYTHONPATH ────────────────────
    import numpy as np
    import yaml

    _stub_nuclib()
    _trace("🔧", "stubbed nuclib.deuterium / nuclib.helium")

    from tools.config import load_config, conf
    load_config(config_file)
    os.environ.setdefault("FITPACK", "")

    from fitlib.resman import RESMAN
    t_boot = time.time()
    resman = RESMAN(None, parallel=False, datasets=False)
    n_free = len(resman.parman.order)
    _trace("🚀", f"bootstrap RESMAN ({n_free} free params)",
           elapsed=round(time.time() - t_boot, 2))

    # Build parameter arrays per replica ─────────────────────────────────
    _central = np.array(resman.parman.par, copy=True)
    parameters: list = []
    fallback_counts: list[int] = []
    dist_missing: list[int] = []
    for p in replica_paths:
        try:
            with open(p) as fh:
                rep = yaml.safe_load(fh)
            if isinstance(rep, dict) and "femtomind_live_params" in rep:
                par, n_missing, n_miss_dist = _par_from_flat(
                    rep["femtomind_live_params"], resman.parman.order, _central, dist
                )
            else:
                par, n_missing, n_miss_dist = _par_from_replica(
                    rep["conf"], resman.parman.order, _central, dist
                )
            parameters.append(par)
            fallback_counts.append(n_missing)
            dist_missing.append(n_miss_dist)
        except (OSError, KeyError, yaml.YAMLError) as e:
            _trace("⚠️", f"skipping replica {p}: {e!r}")
    if not parameters:
        _die("no replica yamls were loaded successfully")
        return

    n_varying = int((np.stack(parameters).std(axis=0) > 0).sum())
    _trace("📐", f"projected {len(parameters)} replicas onto parman.order "
                 f"({n_varying}/{n_free} varying)")

    # Surface silent fallbacks: if any replica was missing parameter
    # values for some parman.order positions, those positions used the
    # central/default — which can mean we're plotting curves that aren't
    # the actual fit. Almost always indicates the replica yaml's parameter
    # set drifted from RESMAN's order (different observables loaded, etc).
    # Only positions belonging to the distribution being drawn can change
    # this curve. RESMAN is bootstrapped from a full multi-distribution input
    # card, so a PDF-only fit legitimately leaves the FF/ppdf/ht4 families and
    # every dataset normalisation at their central values — counting those as
    # "missing" fired a scary warning on every single plot and made correct
    # curves look untrustworthy. Warn only when the plotted family itself is
    # short of values.
    bad = sum(1 for n in dist_missing if n > 0)
    if bad:
        worst = max(dist_missing)
        _trace(
            "⚠️",
            f"{bad}/{len(parameters)} replicas are missing {worst} "
            f"'{dist}' parameters, which DO enter this curve — those "
            f"positions fell back to central. Check that the chat's "
            f"RESMAN config matches the batch's.",
            kind="phase",
        )
    else:
        outside = max(fallback_counts) if fallback_counts else 0
        if outside:
            _trace(
                "ℹ️",
                f"{outside}/{n_free} parman positions lie outside '{dist}' "
                f"(other distributions and dataset normalisations) and keep "
                f"their central values — they do not enter this curve.",
            )

    # Compute observable ─────────────────────────────────────────────────
    if dist not in conf:
        _die(f"distribution {dist!r} not in conf — is the config right?")
        return
    getter = getattr(conf[dist], "get_xF", None)
    if getter is None:
        _die(f"conf[{dist!r}] has no get_xF method")
        return

    # Derived-flavor recipes (PDF-paper convention): get_xF only knows the
    # primary flavors (g, u, ub, d, db, s, sb, c, cb, b, bb); valence and
    # sea are linear combinations the user expects to plot. Each entry is
    # (sign, primary_flavor) pairs to sum.
    DERIVED = {
        "uv":  [(+1, "u"),  (-1, "ub")],
        "dv":  [(+1, "d"),  (-1, "db")],
        "sv":  [(+1, "s"),  (-1, "sb")],
        "sea": [(+1, "ub"), (+1, "db"), (+1, "sb")],
        "ubp": [(+1, "ub")],   # aliases for renderer-side label parity
        "dbp": [(+1, "db")],
        "sbp": [(+1, "sb")],
    }

    def _eval(x: float, q2: float, f: str) -> float:
        recipe = DERIVED.get(f)
        if recipe is None:
            return float(getter(x, q2, f))
        total = 0.0
        for sign, prim in recipe:
            total += sign * float(getter(x, q2, prim))
        return total

    # Outer loop over replicas (set_new_params is the expensive bit, so do
    # it once per replica and evaluate all flavors in the inner loop). Skip
    # flavors that raise — we collect the failure into the trace and ship
    # whatever flavors did work, instead of crashing the whole compute.
    t_compute = time.time()
    curves_by_flavor: dict[str, list[list[float]]] = {f: [] for f in flavors}
    failed_flavors: dict[str, str] = {}
    for par in parameters:
        resman.parman.set_new_params(par)
        for f in list(flavors):
            if f in failed_flavors:
                continue
            try:
                row = [_eval(x, q2, f) for x in x_grid]
            except Exception as e:
                failed_flavors[f] = type(e).__name__ + ": " + str(e)[:120]
                _trace("⚠️", f"flavor {f!r} eval failed, dropping: {failed_flavors[f]}")
                curves_by_flavor.pop(f, None)
                continue
            curves_by_flavor[f].append(row)
    surviving = [f for f in flavors if f in curves_by_flavor]
    dt_compute = time.time() - t_compute

    if not surviving:
        _die(
            "all flavors failed to evaluate; first error: "
            + (next(iter(failed_flavors.values()), "unknown"))
        )
        return

    _trace(
        "📝",
        f"compute {len(parameters)} × {len(x_grid)} × {len(surviving)} curves "
        f"({dist}.get_xF, flavors={surviving}, Q²={q2})",
        elapsed=round(dt_compute, 2),
    )

    response = {
        "curves_by_flavor":   curves_by_flavor,
        "flavors":            surviving,
        "failed_flavors":     failed_flavors,
        "x_grid":             x_grid,
        "n_params_free":      n_free,
        "n_params_varying":   n_varying,
        "elapsed_compute_s":  round(dt_compute, 3),
        "elapsed_total_s":    round(time.time() - t0, 3),
    }
    print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
