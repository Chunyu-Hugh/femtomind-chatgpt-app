"""Grade a candidate conf dump against the catalog's targets.

grade(dump, condition) returns one report (schema femtomind.l1_grade.v1):
both targets (JAM24.final and JAM24.current) at all three tiers, the view of
the 650 author-overlay paths, the by-construction differences between the two
targets, and a verdict for the target the run was meant for. Conditions A and
B are graded against the literal official input; condition C, which had the
author clarifications, against the overlaid configuration.
"""
import time

from evaluation.l1 import canonicalize, expected, typed

SCHEMA = "femtomind.l1_grade.v1"


def candidate_conf(dump):
    """The candidate's conf as Python values, from the dump's typed form."""
    if dump.get("error"):
        raise ValueError(dump["error"])
    if "typed" in dump:
        return typed.untype(dump["typed"])
    if "plain" in dump:
        return dump["plain"]
    raise ValueError("dump has neither typed nor plain conf")


def overlay_view(targets, candidate):
    """How many of the author-overlay paths (literal -> reproduction) the
    candidate has: a condition-C run should have them, A and B should not."""
    t = targets["JAM24.final"]
    overlay = expected.differences(t["literal"], t["reproduction"])
    paths = {d["path"] for d in overlay}
    # a path is 'matched' when the candidate differs from the literal there
    # exactly as the reproduction does
    vs_literal = {d["path"] for d in expected.differences(t["literal"], candidate)}
    vs_reproduction = {d["path"] for d in expected.differences(t["reproduction"], candidate)}
    matched = len([p for p in paths if p in vs_literal and p not in vs_reproduction])
    return {"of": len(paths), "matched": matched}


def grade(dump, condition="A", requested_target="JAM24.final", targets=None):
    targets = targets or expected.all_targets()
    candidate = candidate_conf(dump)
    kind = "reproduction" if str(condition).upper() == "C" else "literal"
    report = {"schema": SCHEMA, "graded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "condition": str(condition).upper(), "requested_target": requested_target, "target_kind": kind,
              "candidate": {"inputs": dump.get("inputs"), "chain_length": len(dump.get("inputs") or []),
                            "graded": (dump.get("inputs") or [{}])[-1].get("path"), "loader": dump.get("loader"),
                            "step_projection": dump.get("step_projection"), "coercions": dump.get("coercions"),
                            "environment": dump.get("environment")},
              "targets": {}, "tier0": {}, "tier1": {}, "tier2": {},
              "runtime_keys_removed": sorted(canonicalize.RUNTIME_KEYS)}
    for name, t in targets.items():
        report["targets"][name] = t["refs"]
        target_conf = t[kind]
        report["tier0"][name] = canonicalize.tier0(target_conf, candidate)
        report["tier1"][name] = canonicalize.tier1(target_conf, candidate)
        report["tier2"][name] = canonicalize.tier2_coverage(canonicalize.tier1_normalize(target_conf),
                                                            canonicalize.tier1_normalize(candidate))
    report["reproduction_overlay_view"] = overlay_view(targets, candidate)
    cvf = expected.current_vs_final(targets)
    report["current_vs_final"] = {"n": len(cvf), "paths": sorted({d["path"] for d in cvf})[:100]}
    r = requested_target
    report["verdict"] = {"requested_target": r, "tier0": report["tier0"][r]["passed"],
                         "tier1": report["tier1"][r]["passed"], "tier2_fraction": report["tier2"][r]["fraction"],
                         "free_parameters": {"expected": expected.free_parameters(targets[r][kind]),
                                             "found": expected.free_parameters(candidate)}}
    return report
