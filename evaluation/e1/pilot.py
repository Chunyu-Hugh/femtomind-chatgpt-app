"""The same faults, as they actually happened.

E1's mutants are deliberate. This module is the other half of the figure: the
faults a real agent made in the pilot (2026-10-24/25, condition A/B, the
JAM24 paper and a FitPack tree, no catalog), graded afterwards by the L1
grader. Each observed fault is mapped to the mutant that reproduces it, so the
table reads "this is what the agent did, this is what the grader said, this is
what the gateway would have said before the job was queued".

Sources (all in evaluation/l1/calibration/2026-10-26/):
  * summary.md — pilot_nomcp: tier 2 0.776, 215/215 free parameters,
    settings_mismatch [Q20, sia version, ftol, flat par];
  * summary.md — pilot_mcp: 140/215 free parameters (the old gateway's library
    defaults, before the catalog card), 774 tier-1 paths against JAM24.current;
  * grade.pilot_nomcp.json / grade.pilot_mcp.json — the full reports.
The pilot transcripts showed both agents reporting the campaign as submitted
and fitting: nothing in either run flagged these settings.
"""
PILOT_OBSERVATIONS = (
    {"mutant": "q0_squared", "arm": "paper only (pilot run3)",
     "what_happened": "Q0^2 written as 1.6384 (the paper's 1.28^2); the fit-time value is 1.6129",
     "grader": "settings_mismatch: Q20"},
    {"mutant": "flat_par", "arm": "paper only (pilot run3)",
     "what_happened": "flat par not set, so FitPack does not draw the author's Gaussian starts",
     "grader": "settings_mismatch: flat par"},
    {"mutant": "loose_tolerance", "arm": "paper only (pilot run3)",
     "what_happened": "ftol set to a value the published fit does not use",
     "grader": "settings_mismatch: ftol"},
    {"mutant": "sia_version", "arm": "paper only (pilot run3)",
     "what_happened": "sia version not the published one",
     "grader": "settings_mismatch: sia version"},
    {"mutant": "ff_shape_off", "arm": "gateway without the catalog (pilot mcp run3)",
     "what_happened": "35 + 40/30/30 free FF parameters (140) instead of 35 + 60/60/60 (215): "
                      "the library's default shapes, not the published two",
     "grader": "free parameters 140 vs 215; 774 tier-1 paths"},
    {"mutant": "zero_point_dataset", "arm": "gateway without the catalog (pilot mcp run1/2)",
     "what_happened": "SIA 1012 left in without a filter entry, the job died at data load (KeyError 1012)",
     "grader": "not graded: the run never produced a campaign to grade"},
)

MUTANT_TO_OBSERVATION = {o["mutant"]: o for o in PILOT_OBSERVATIONS}


def table():
    rows = ["| Fault | Who made it | What was written | How the grader saw it |",
            "|---|---|---|---|"]
    for o in PILOT_OBSERVATIONS:
        rows.append("| `%s` | %s | %s | %s |" % (o["mutant"], o["arm"], o["what_happened"], o["grader"]))
    return "\n".join(rows)


def summary():
    arms = {}
    for o in PILOT_OBSERVATIONS:
        arms[o["arm"]] = arms.get(o["arm"], 0) + 1
    return "%d observed faults in the pilot (%s); none was flagged at submission time" % (
        len(PILOT_OBSERVATIONS), ", ".join("%s: %d" % (k, v) for k, v in sorted(arms.items())))
