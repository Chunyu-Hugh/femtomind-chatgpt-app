"""E5 — build the atlas cards without running anything.

Each variant is the catalog card with one declared `overrides` block, so the
change is recorded in the card's provenance and `validate_campaign` will refuse
anything that differs in a way nobody declared. This module builds all of them
offline and reports what each one actually changes: free parameters, dataset
counts, and the sections that moved. A variant that cannot be expressed as an
override is reported as such instead of being silently approximated.

    python3 -m evaluation.e5.plan [--analysis-id JAM24.final] [--json out.json]
"""
import argparse
import copy
import json
import sys

# (id, what it changes, overrides, the readout it is meant to move)
VARIANTS = (
    ("control", "the published card, unchanged", None, "reproduces chi2/N = 1"),
    ("no_wcharm", "drop W+charm", {"reactions": "-wc"}, "strangeness pulls"),
    ("no_sidis", "drop SIDIS", {"reactions": "-sidis"}, "FF side of the tension"),
    ("sidis_z09", "SIDIS z upper 0.8 -> 0.9", {"cuts": {"sidis": [{"variable": "Z", "op": "<", "value": 0.9}]}},
     "cut sensitivity of the FF constraint"),
    ("sidis_q2_2", "SIDIS Q2 lower 1.69 -> 2.0", {"cuts": {"sidis": [{"variable": "Q2", "op": ">", "value": 2.0}]}},
     "higher-twist / cut sensitivity"),
    ("no_sia", "drop SIA", {"reactions": "-sia"}, "FF-only constraint removed"),
    ("normals_free", "free every normalization", {"normalizations": "free"}, "normalization model"),
    ("q0_paper", "Q0^2 1.6129 -> 1.6384", {"qcd": {"Q20": 1.6384}}, "the paper's 1.28^2"),
    ("tmc_off", "TMC off", {"qcd": {"tmc": "off"}}, "higher-twist treatment"),
    ("positivity_on", "positivity on for all objects",
     {"qcd": {"pdfPositivity": True, "ffpionPositivity": True, "ffkaonPositivity": True, "ffhadronPositivity": True}},
     "the author-confirmed flags"),
    ("no_bootstrap", "bootstrap off", {"job": {"bootstrap": False}}, "statistical error budget"),
    ("one_ff_shape", "one FF shape per object", {"objects": "one_shape"}, "shape flexibility"),
    ("fixed_kaon", "fix the kaon FF", {"objects": "fix_ffkaon"}, "which FF carries the tension"),
    ("more_iterations", "maxIterations 500 -> 2000", {"job": {"maxIterations": 2000}}, "convergence, not physics"),
)

# Variants that need a card the builder cannot produce from overrides alone.
NOT_EXPRESSIBLE = {
    "jam24_current": "needs the other catalog entry (analysis_id='JAM24.current'), not an override",
    "s2_independent": "the strange-shape tie is a parameter-level change the catalog does not expose as an override",
    "su3_sea": "imposing s+ = sbar means a new tie per flavor, not expressible as an override",
    "three_ff_shapes": "the composer's FF shape count is library-level (ff_registry), not a card field",
    "nnlo": "the composer's order enum is LO/NLO; NNLO is not runnable here",
    "drop_flagged": "the 9 flagged datasets are chosen by compare_with_reference after a fit, not here",
    "mix_sidis_workbooks": "deliberately refused (dis version): a must-refuse case, not an atlas row",
}


def _apply(card, overrides):
    """Apply the override kinds this plan uses, the way the gateway does."""
    out = copy.deepcopy(card)
    o = overrides or {}
    if isinstance(o.get("datasets"), dict):
        for reaction, ids in o["datasets"].items():
            out.setdefault("datasets", {})[reaction] = list(ids)
    if isinstance(o.get("reactions"), str) and o["reactions"].startswith("-"):
        drop = o["reactions"][1:]
        out["reactions"] = [r for r in (out.get("reactions") or []) if r != drop]
    if isinstance(o.get("qcd"), dict):
        out.setdefault("qcd", {}).update(o["qcd"])
    if isinstance(o.get("job"), dict):
        out.setdefault("job", {}).update(o["job"])
    if isinstance(o.get("cuts"), dict):
        for reaction, rows in o["cuts"].items():
            current = out.setdefault("cuts", {}).setdefault(reaction, [])
            for row in rows:
                for existing in current:
                    if existing.get("variable") == row["variable"] and existing.get("op") == row["op"]:
                        existing.update(row)
                        break
                else:
                    current.append(dict(row, enabled=True))
    if o.get("normalizations") == "free":
        for reaction, block in (out.get("normalizations") or {}).items():
            for entry in block.values():
                entry["fixed"] = False
    if o.get("objects") == "one_shape":
        for obj in out.get("objects") or []:
            if obj.get("type") in ("ffpion", "ffkaon", "ffhadron"):
                free = [k for k, p in obj["parameters"].items() if p.get("fixed") is False]
                for k in free[len(free) // 2:]:
                    obj["parameters"][k]["fixed"] = True
    if o.get("objects") == "fix_ffkaon":
        for obj in out.get("objects") or []:
            if obj.get("type") == "ffkaon":
                for p in obj["parameters"].values():
                    p["fixed"] = True
    return out


def free_parameters(card):
    return sum(1 for o in card["objects"] for p in o["parameters"].values() if p.get("fixed") is False)


def summary(base, variant):
    card = _apply(base, variant[2])
    changed = [k for k in ("qcd", "job", "datasets", "cuts", "normalizations", "objects", "reactions")
               if card.get(k) != base.get(k)]
    return {"id": variant[0], "what": variant[1], "readout": variant[3], "changed": changed,
            "free_parameters": free_parameters(card),
            "datasets": {r: len(v) for r, v in (card.get("datasets") or {}).items()},
            "points": None}


def build(analysis_id="JAM24.final"):
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lqcdmcp-deploy"))
    import jam_gateway as g
    g._tls.user = getattr(g._tls, "user", "") or "chunyu"
    base = g.campaign_card_from_catalog(analysis_id, 1)["card"]
    rows = [summary(base, v) for v in VARIANTS]
    return {"schema": "femtomind.e5_plan.v1", "analysis_id": analysis_id,
            "base": {"free_parameters": free_parameters(base),
                     "datasets": {r: len(v) for r, v in (base.get("datasets") or {}).items()}},
            "variants": rows, "not_expressible": NOT_EXPRESSIBLE}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--analysis-id", default="JAM24.final")
    p.add_argument("--json")
    a = p.parse_args()
    plan = build(a.analysis_id)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(plan, fh, indent=1, sort_keys=True)
    print("%-16s %-42s %-20s %s" % ("variant", "what", "changed", "free params"))
    for r in plan["variants"]:
        print("%-16s %-42s %-20s %s" % (r["id"], r["what"][:42], ",".join(r["changed"]) or "-", r["free_parameters"]))
    if plan["not_expressible"]:
        print("\nnot expressible as an override (need a decision, not a run):")
        for k, why in sorted(plan["not_expressible"].items()):
            print("  %-22s %s" % (k, why))


if __name__ == "__main__":
    main()
