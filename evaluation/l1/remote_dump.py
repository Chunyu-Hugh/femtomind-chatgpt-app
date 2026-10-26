"""Load a candidate FitPack input the way FitPack does, and dump its conf.

This file is shipped as source to wherever the candidate can be executed (the
farm's apptainer image for an agent's input, the local machine for a test) and
run as `python3 -B -I -` with REQUEST filled in:

    REQUEST = {"inputs": [paths in execution order], "fitpack_root": path,
               "cwd": path or null}

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
    """fitlib.maxlike.get_conf for the last step, without an optimizer: drop the
    distributions the step neither fits nor keeps passive, fix the passive ones.
    Prior values a real chain would carry over between steps are not known."""
    steps = conf.get("steps")
    if not isinstance(steps, dict) or not steps:
        return conf, None
    last = steps[max(steps, key=lambda k: int(k) if str(k).isdigit() else k)]
    active = set(last.get("active distributions", []))
    passive = set(last.get("passive distributions", []))
    removed, fixed = [], []
    for dist in list(conf.get("params", {})):
        if dist in active or dist in passive:
            continue
        removed.append(dist)
        conf["params"].pop(dist, None)
        conf.pop("%s parametrization" % dist, None)
    for dist in passive:
        for name, par in (conf.get("params", {}).get(dist) or {}).items():
            if isinstance(par, dict) and par.get("fixed") is False:
                par["fixed"] = True
                fixed.append(dist + "/" + name)
    return conf, {"method": "reimplemented", "steps": sorted(steps, key=str), "last_step": str(max(steps, key=str)),
                  "active": sorted(active), "passive": sorted(passive), "removed_distributions": removed,
                  "fixed_passive_parameters": len(fixed)}


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
    for path in req["inputs"]:
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
