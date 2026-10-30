"""E1 — fault injection: what happens to a campaign card that is wrong.

The paper's claim is not that an agent can produce a card, but that a wrong
card cannot reach the queue silently. This module builds "mutants" of the
official JAM24.final card, one realistic error each, and asks two instruments
what they see:

  * the gateway's validate_campaign (refused / warned / accepted), and
  * the L1 grader's typed parity (tier 0/1/2 differences), offline.

A mutant the gateway accepts while the grader says it differs from the
published input is a *silent* fault: exactly the failure mode the raw-LLM arm
shows in the pilot (four runs, four different cards, Q0^2 wrong every time).
A mutant the gateway refuses is a caught fault, and the refusal code says
which layer caught it.

Every mutation is applied to a copy of a card obtained from the gateway
itself, so the base is the parity-checked official configuration, not a card
this code wrote.
"""
import copy

# (id, what a user or an agent did, which scientific field it changes)
MUTANTS = (
    ("zero_point_dataset",
     "put back SIA dataset 1012, which the current readers keep no point of",
     "datasets"),
    ("missing_cut",
     "drop the SIDIS W2 > 10 cut",
     "cuts"),
    ("q0_squared",
     "set Q0^2 to 1.6384 (the paper's 1.28^2) instead of the fitted 1.6129",
     "qcd"),
    ("flat_par",
     "turn flat par off (initialization fixed instead of random_within_bounds)",
     "parameters"),
    ("loose_tolerance",
     "loosen the fit tolerance from 1e-3 to 5e-2",
     "job"),
    ("ff_shape_off",
     "fix every kaon FF parameter, i.e. run one FF shape instead of the published two",
     "objects"),
    ("extra_replica_setting",
     "raise maxIterations from 500 to 5000 (a job setting, not a scientific one)",
     "job"),
    ("sia_version",
     "set siaVersion to 0 instead of the published 1",
     "qcd"),
)


def _find_cut(cuts, reaction, variable, op):
    for i, row in enumerate(cuts.get(reaction) or []):
        if isinstance(row, dict) and row.get("variable") == variable and row.get("op") == op:
            return i
    return None


def mutate(card, mutant_id):
    """A deep copy of `card` with one realistic fault, or a ValueError."""
    out = copy.deepcopy(card)
    if mutant_id == "zero_point_dataset":
        ids = out.setdefault("datasets", {}).setdefault("sia", [])
        if "1012" not in ids:
            ids.append("1012")
            ids.sort()
        out.get("source", {}).pop("excluded_zero_point_datasets", None)
    elif mutant_id == "missing_cut":
        i = _find_cut(out.get("cuts") or {}, "sidis", "W2", ">")
        if i is None:
            raise ValueError("no W2 > cut to drop")
        out["cuts"]["sidis"][i]["enabled"] = False
    elif mutant_id == "q0_squared":
        out.setdefault("qcd", {})["Q20"] = 1.6384
    elif mutant_id == "flat_par":
        out.setdefault("parameters", {})["initialization"] = "fixed"
    elif mutant_id == "loose_tolerance":
        out.setdefault("job", {})["tolerance"] = "0.05"
    elif mutant_id == "ff_shape_off":
        objects = out.get("objects") or []
        for obj in objects:
            if obj.get("type") == "ffkaon":
                for row in obj.get("parameters", {}).values():
                    row["fixed"] = True
                    row.pop("gaussian", None)
                break
        else:
            raise ValueError("no ffkaon object to fix")
    elif mutant_id == "extra_replica_setting":
        out.setdefault("job", {})["maxIterations"] = 5000
    elif mutant_id == "sia_version":
        out.setdefault("qcd", {})["siaVersion"] = 0 if out["qcd"].get("siaVersion") != 0 else 1
    else:
        raise ValueError("unknown mutant %r" % (mutant_id,))
    return out


def describe():
    return [{"id": m, "what": w, "field": f} for m, w, f in MUTANTS]
