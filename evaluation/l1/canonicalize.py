"""The three tiers of agreement between a candidate conf and the target.

Tier 0, exact: the compiler's typed comparison (jamai.compiled_input
._differences) after the keys FitPack itself adds at run time are removed.
Tier 1, scientific equivalence: the same comparison after both sides are put
into one spelling: dataset paths reduced to (reaction, file name), filter
expressions with their numbers normalised, numbers compared as numbers, site
paths (grid folders) set aside, and a flag that one side spells out and the
other leaves at its default treated as equal. Tier 2, structural coverage:
a vector of checks (which datasets, which free parameters, which settings) and
the fraction that matched, for partial credit and for seeing what is missing.
"""
import copy
import os
import re

from evaluation.l1 import expected

# Keys FitPack would add to conf while it runs, so that a conf read back from
# a run differs from the input by them. Calibrated against replicas_cut['conf']
# of the JAM24 archive on 2026-10-26 (l1/calibration/2026-10-26/runtime_keys.json):
# the saved conf equals the official input key for key, so the set is empty.
# FEMTOMIND_RUNTIME_KEYS overrides it for an experiment.
RUNTIME_KEYS = frozenset(k for k in os.environ.get("FEMTOMIND_RUNTIME_KEYS", "").split(",") if k)

# Site-specific paths: compared as present/absent, never by value.
ENVIRONMENT_KEYS = ("grid path", "grid-8 path")

# A setting the official input leaves out means FitPack's default. A candidate
# that writes the default explicitly says the same thing.
DEFAULT_EQUIVALENTS = {"pdf positivity": False, "ffpion positivity": False, "ffkaon positivity": False,
                       "ffhadron positivity": False, "ht": False, "offpdf": False, "nuc": True, "ww": False,
                       "flat par": False, "bootstrap": True, "resample version": 0}

SETTINGS = ("order", "Q20", "tmc", "ht", "dglap mode", "alphaSmode", "beta version", "sia version", "ftol",
            "bootstrap", "flat par", "pdf positivity", "ffpion positivity", "ffkaon positivity", "ffhadron positivity")


def strip_runtime(conf, runtime_keys=RUNTIME_KEYS):
    return {k: v for k, v in conf.items() if k not in runtime_keys}


def canonical_filter(expression):
    """One filter expression with its numbers normalised and its spaces removed;
    'A and B' becomes two expressions."""
    parts = [p.strip() for p in re.split(r"\band\b", str(expression))]
    return [re.sub(r"\s+", "", re.sub(r"(?<![A-Za-z_\d.])(\d+\.\d*|\.\d+|\d+)(?![A-Za-z_])",
                                       lambda m: repr(float(m.group(0))), p)) for p in parts]


def canonical_filters(filters):
    if filters is None:
        return None
    if isinstance(filters, dict):
        return {str(k): canonical_filters(v) for k, v in filters.items()}
    out = []
    for f in filters:
        out.extend(canonical_filter(f))
    return sorted(out)


def _num(value):
    """Numbers as floats (1 == 1.0), everything else untouched."""
    return float(value) if type(value) in (int, float) and not isinstance(value, bool) else value


def tier1_normalize(conf):
    """A copy of conf in the shared spelling of Tier 1."""
    c = copy.deepcopy(conf)
    for key in ENVIRONMENT_KEYS:
        c.pop(key, None)
    # a chained input's step table has been applied by the dumper's projection
    c.pop("steps", None)
    for key, default in DEFAULT_EQUIVALENTS.items():
        if key not in c:
            c[key] = default
    for key in list(c):
        if key not in ("params", "datasets"):
            c[key] = _num(c[key])
    for obj, table in (c.get("params") or {}).items():
        for name, d in list(table.items()):
            if isinstance(d, dict):
                for field in ("value", "min", "max", "mean", "std"):
                    if field in d:
                        d[field] = _num(d[field])
    datasets = {}
    for reaction, block in (c.get("datasets") or {}).items():
        b = {}
        xlsx = block.get("xlsx") or {}
        b["xlsx"] = {str(k): reaction + "/" + os.path.basename(str(v)) for k, v in xlsx.items()}
        norm = block.get("norm") or {}
        b["norm"] = {str(k): {f: _num(v) for f, v in (d or {}).items()} if isinstance(d, dict) else d for k, d in norm.items()}
        if "filters" in block:
            b["filters"] = canonical_filters(block["filters"])
        for extra in block:
            if extra not in ("xlsx", "norm", "filters"):
                b[extra] = block[extra]
        datasets[reaction] = b
    c["datasets"] = datasets
    return c


def environment_view(target, candidate):
    """Which site paths each side names; never compared by value."""
    return {"target": sorted(k for k in ENVIRONMENT_KEYS if k in target),
            "candidate": sorted(k for k in ENVIRONMENT_KEYS if k in candidate)}


def tier2_coverage(target, candidate):
    """The structural checks and the fraction matched."""
    checks, result = [], {}
    # datasets and normalizations per reaction
    ds = {}
    for reaction in sorted(set(target.get("datasets", {})) | set(candidate.get("datasets", {}))):
        t = target.get("datasets", {}).get(reaction, {})
        c = candidate.get("datasets", {}).get(reaction, {})
        exp_ids = {str(k) for k in (t.get("xlsx") or {})}
        got_ids = {str(k) for k in (c.get("xlsx") or {})}
        exp_norm = {str(k) for k in (t.get("norm") or {})}
        got_norm = {str(k) for k in (c.get("norm") or {})}
        ds[reaction] = {"expected": len(exp_ids), "found": len(got_ids), "missing": sorted(exp_ids - got_ids),
                        "extra": sorted(got_ids - exp_ids), "normalizations_expected": len(exp_norm),
                        "normalizations_found": len(got_norm), "normalizations_missing": sorted(exp_norm - got_norm),
                        "filters_match": canonical_filters(t.get("filters")) == canonical_filters(c.get("filters"))}
        checks.append(exp_ids == got_ids)
        checks.append(exp_norm == got_norm)
        checks.append(ds[reaction]["filters_match"])
    result["datasets"] = ds
    # parameters per object
    params = {}
    for obj in sorted(set(target.get("params", {})) | set(candidate.get("params", {}))):
        t = target.get("params", {}).get(obj) or {}
        c = candidate.get("params", {}).get(obj) or {}
        t_free = {n for n, d in t.items() if isinstance(d, dict) and d.get("fixed") is False}
        c_free = {n for n, d in c.items() if isinstance(d, dict) and d.get("fixed") is False}
        status_mismatch = sorted(n for n in set(t) & set(c)
                                 if isinstance(t[n], dict) and isinstance(c[n], dict)
                                 and (t[n].get("fixed") is False) != (c[n].get("fixed") is False))
        link_mismatch = sorted(n for n in set(t) & set(c)
                               if isinstance(t[n], dict) and isinstance(c[n], dict)
                               and isinstance(t[n].get("fixed"), str) and t[n]["fixed"] != c[n].get("fixed"))
        params[obj] = {"present": bool(c), "names_expected": len(t), "names_found": len(c),
                       "names_missing": sorted(set(t) - set(c))[:20], "names_extra": sorted(set(c) - set(t))[:20],
                       "free_expected": len(t_free), "free_found": len(c_free),
                       "free_missing": sorted(t_free - c_free)[:20], "free_extra": sorted(c_free - t_free)[:20],
                       "fixed_status_mismatch": status_mismatch[:20], "link_mismatch": link_mismatch[:20]}
        checks.append(bool(c) == bool(t))
        checks.append(set(t) == set(c))
        checks.append(t_free == c_free)
        checks.append(not status_mismatch and not link_mismatch)
    result["parameters"] = params
    # settings
    settings = {}
    for key in SETTINGS:
        exp_v = target.get(key, DEFAULT_EQUIVALENTS.get(key))
        got_v = candidate.get(key, DEFAULT_EQUIVALENTS.get(key))
        match = _num(exp_v) == _num(got_v)
        settings[key] = {"expected": exp_v, "found": got_v, "match": match}
        checks.append(match)
    result["settings"] = settings
    result["checks"] = len(checks)
    result["matched"] = sum(1 for x in checks if x)
    result["fraction"] = result["matched"] / len(checks) if checks else None
    return result


def tier0(target, candidate):
    diffs = expected.differences(strip_runtime(target), strip_runtime(candidate))
    return {"passed": not diffs, "n": len(diffs), "differences": diffs[:200]}


def tier1(target, candidate):
    diffs = expected.differences(tier1_normalize(strip_runtime(target)), tier1_normalize(strip_runtime(candidate)))
    return {"passed": not diffs, "n": len(diffs), "differences": diffs[:200],
            "environment": environment_view(target, candidate),
            "rules": ["runtime keys removed", "xlsx paths as reaction/basename",
                      "filter numbers as repr(float), spaces removed, 'and' split", "numbers compared as floats",
                      "site paths (grid folders) set aside", "explicit defaults equal to absent",
                      "conf['steps'] dropped once projected"]}
