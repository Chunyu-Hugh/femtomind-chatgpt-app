# L1 grader calibration (2026-10-26T14:08:18Z)

## official

**Verdict:** official input changed after the catalog read (218a8e63 -> 56f61920); the Tier 0 differences are that drift

- drift_since_catalog_read: `true`
- tier0_vs_literal_final: `2`
- tier0_vs_literal_current: `19`
- free_parameters: `{"expected": 215, "found": 215}`

## bundle

**Verdict:** saved conf == literal catalog conf + 0 runtime keys

- runtime_keys: `[]`
- n_differences_after_runtime_keys_removed: `0`

## overlay

**Verdict:** exactly 650 overlay paths

- n_paths: `650`

## compiler

**Verdict:** compiler output == reproduction conf

- tier0_vs_reproduction_final: `0`

## pilot_mcp

**Verdict:** MCP-side by-construction differences vs JAM24.current: 774 Tier 1 paths; free parameters {'expected': 215, 'found': 140}

- free_parameters: `{"expected": 215, "found": 140}`
- step_projection: `null`

## pilot_nomcp

**Verdict:** graded the 7-step chain's last step: Tier 2 0.776, free parameters {'expected': 215, 'found': 215}

- free_parameters: `{"expected": 215, "found": 215}`
- settings_mismatch: `["Q20", "sia version", "ftol", "flat par"]`
- step_projection: `{"method": "reimplemented from fitlib.maxlike.MAXLIKE.get_conf", "steps": ["1", "2", "3", "4", "5", "6", "7"], "last_step": "7", "active": ["pdf", "ffpion", "ffkaon", "ffhadron"], "passive": [], "removed_distributions": [], "fixed_parameters": 0, "dropped_reactions": [], "dropped_dataset_ids": {}, "prior_values": "not applied (no optimizer history)", "notes": []}`
