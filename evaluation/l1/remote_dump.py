"""Load a candidate FitPack input the way FitPack does, and dump its conf.

This file is shipped as source to wherever the candidate can be executed (the
farm's apptainer image for an agent's input, the local machine for a test) and
run as `python3 -B -I -` with REQUEST filled in:

    REQUEST = {"inputs": [paths in execution order], "fitpack_root": path,
               "cwd": path or null}
    REQUEST = {"bundle": path of a parameter bundle (replicas_cut.npy[.gz]),
               "fitpack_root": path}      # grades its saved conf instead

Loading is exactly FitPack's tools.config._load_python: exec the file with
__file__ set and take its `conf`. A job script that runs several inputs in a
row (the pilot's no-MCP agents wrote step1.py .. step7.py) is given all of
them; each is loaded on its own and the LAST one is graded. An input with
conf['steps'] is projected onto its last step as fitlib.maxlike.get_conf
does: distributions outside the step's active/passive lists are removed,
passive ones are fixed. Values are then typed (jamai.compiled_input._typed:
int against string keys, bool against int, floats as hex) so that nothing is
lost on the way back. The dump says what had to be coerced (tuples, numpy
scalars) and what could not be represented (callables), each with its path.

Only the standard library is needed; numpy is used when the candidate did.
"""
import hashlib
import json
import math
import os
import sys
import traceback

REQUEST = __REQUEST__  # noqa: E999 - filled in before the program is sent


def _typed(value):
    if isinstance(value, dict):
        return ["dict", sorted([[_typed(k), _typed(v)] for k, v in value.items()], key=repr)]
    if isinstance(value, list):
        return ["list", [_typed(v) for v in value]]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Nonfinite scientific number")
        return ["float", value.hex()]
    if type(value) not in (str, int, bool, type(None)):
        raise ValueError("Unsupported scientific literal")
    return [type(value).__name__, value]


def _coerce(value, path, coercions):
    """A JSON-representable copy of value; every change is recorded with its path."""
    name = type(value).__name__
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = k
            if not isinstance(k, (str, int, bool)) and k is not None:
                key = str(k)
                coercions.append({"path": path + "/" + str(k), "from": type(k).__name__, "to": "str (key)"})
            out[key] = _coerce(v, path + "/" + str(k), coercions)
        return out
    if isinstance(value, tuple):
        coercions.append({"path": path, "from": "tuple", "to": "list"})
        return [_coerce(v, path + "/" + str(i), coercions) for i, v in enumerate(value)]
    if isinstance(value, list):
        return [_coerce(v, path + "/" + str(i), coercions) for i, v in enumerate(value)]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            coercions.append({"path": path, "from": "float", "to": "nonfinite marker"})
            return {"__nonfinite__": repr(value)}
        return value
    # numpy scalars and arrays, without importing numpy
    if hasattr(value, "item") and getattr(value, "shape", None) == ():
        coercions.append({"path": path, "from": name, "to": "scalar"})
        return _coerce(value.item(), path, coercions)
    if hasattr(value, "tolist"):
        coercions.append({"path": path, "from": name, "to": "list"})
        return _coerce(value.tolist(), path, coercions)
    coercions.append({"path": path, "from": name, "to": "unsupported marker"})
    return {"__unsupported__": name}


def _load(path):
    """FitPack's tools.config._load_python, verbatim in effect."""
    with open(path, encoding="utf-8") as fh:
        code = fh.read()
    namespace = {"__file__": path}
    exec(compile(code, path, "exec"), namespace)
    return namespace["conf"]


def _project_steps(conf):
    """fitlib.maxlike.MAXLIKE.get_conf for the last step, without an optimizer:
    distributions the step neither fits nor keeps passive are removed (with
    their parametrization key), passive and 'fix parameters' entries are
    fixed, and the datasets are narrowed to the step's lists, reaction by
    reaction. The prior values a real chain copies in from the steps it
    depends on are unknown here, so parameter values stay as written."""
    steps = conf.get("steps")
    if not isinstance(steps, dict) or not steps:
        return conf, None
    key = max(steps, key=lambda k: (int(k) if str(k).isdigit() else -1, str(k)))
    step = steps[key]
    notes = []
    active = list(step.get("active distributions", []))
    passive = list(step.get("passive distributions", []))
    if "active distributions" not in step:
        notes.append("step has no 'active distributions': FitPack would raise KeyError")
    removed = []
    for dist in list(conf.get("params", {})):
        if dist in active or dist in passive:
            continue
        removed.append(dist)
        conf["params"].pop(dist, None)
        conf.pop("%s parametrization" % dist, None)
    fixed = []
    for dist in passive:
        for name, par in (conf.get("params", {}).get(dist) or {}).items():
            if isinstance(par, dict):
                if "fixed" not in par:
                    notes.append("%s/%s has no 'fixed': FitPack would raise KeyError" % (dist, name))
                elif par["fixed"] is False:
                    par["fixed"] = True
                    fixed.append(dist + "/" + name)
    for dist, names in (step.get("fix parameters") or {}).items():
        for name in names:
            par = (conf.get("params", {}).get(dist) or {}).get(name)
            if isinstance(par, dict):
                par["fixed"] = True
                fixed.append(dist + "/" + name)
            else:
                notes.append("fix parameters names %s/%s, which the conf lacks: FitPack would raise KeyError" % (dist, name))
    dropped_reactions, dropped_ids = [], {}
    if "datasets" not in step:
        notes.append("step has no 'datasets': FitPack would raise KeyError; datasets left as written")
    else:
        for reaction in list(conf.get("datasets", {})):
            if reaction not in step["datasets"]:
                dropped_reactions.append(reaction)
                del conf["datasets"][reaction]
                continue
            keep = step["datasets"][reaction]
            for table in ("xlsx", "norm"):
                entries = conf["datasets"][reaction].get(table)
                if not isinstance(entries, dict):
                    continue
                for idx in list(entries):
                    if idx not in keep:
                        del entries[idx]
                        dropped_ids.setdefault(reaction, []).append(str(idx))
    return conf, {"method": "reimplemented from fitlib.maxlike.MAXLIKE.get_conf", "steps": sorted(str(k) for k in steps),
                  "last_step": str(key), "active": active, "passive": passive, "removed_distributions": removed,
                  "fixed_parameters": len(fixed), "dropped_reactions": dropped_reactions,
                  "dropped_dataset_ids": {k: len(v) for k, v in dropped_ids.items()},
                  "prior_values": "not applied (no optimizer history)", "notes": notes}


def main():
    req = REQUEST
    root = req.get("fitpack_root")
    if root:
        sys.path.insert(0, root)
        os.environ["FITPACK"] = root
    if req.get("cwd"):
        os.chdir(req["cwd"])
    out = {"schema": "femtomind.l1_dump.v1", "inputs": [], "loader": "tools.config._load_python (exec with __file__)",
           "environment": {"python": sys.version.split()[0], "fitpack_root": root, "cwd": os.getcwd()}}
    conf = None
    if req.get("bundle"):
        rec = {"path": req["bundle"], "kind": "bundle"}
        try:
            import gzip
            import numpy as np
            opener = gzip.open if req["bundle"].endswith(".gz") else open
            with opener(req["bundle"], "rb") as fh:
                data = fh.read()
            rec.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
            import io
            loaded = np.load(io.BytesIO(data), allow_pickle=True, encoding="latin1").item()
            conf = loaded["conf"]
            rec.update(loaded=True, keys=sorted(str(k) for k in loaded), numpy=np.__version__)
            out["loader"] = "np.load(allow_pickle=True, encoding='latin1').item()['conf']"
        except BaseException as exc:  # noqa: BLE001
            rec.update(loaded=False, error="%s: %s" % (type(exc).__name__, exc), traceback=traceback.format_exc()[-2000:])
        out["inputs"].append(rec)
    for path in req.get("inputs") or []:
        rec = {"path": path}
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            rec.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
            conf = _load(path)
            rec["loaded"] = True
        except SystemExit as exc:
            rec.update(loaded=False, error="SystemExit: %r" % (exc.code,))
        except BaseException as exc:  # noqa: BLE001 - a candidate may do anything
            rec.update(loaded=False, error="%s: %s" % (type(exc).__name__, exc), traceback=traceback.format_exc()[-2000:])
        out["inputs"].append(rec)
    if conf is None or not isinstance(conf, dict):
        out["error"] = "no conf: the last input did not load or did not define a conf dict"
        print(json.dumps(out))
        return
    conf, projection = _project_steps(conf)
    out["step_projection"] = projection
    coercions = []
    plain = _coerce(conf, "", coercions)
    out["coercions"] = coercions
    try:
        out["typed"] = _typed(plain)
    except ValueError as exc:
        out["typed_error"] = str(exc)
    out["plain"] = plain
    try:
        import numpy  # noqa: F401
        out["environment"]["numpy"] = numpy.__version__
    except Exception:
        pass
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()
