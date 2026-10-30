# E5 — the JAM24 robustness atlas (proposal)

The paper needs a physics result produced *through* the system, or it is a tools
paper. The atlas is the cheapest defensible one: hold the data and the
procedure fixed, vary one published choice at a time, and report how far the
strangeness result moves. Every variant is one campaign card built from the
catalog with `overrides` (so the change is declared and the provenance record
shows it), one replica started from the published JAM24 replicas, and the same
readouts.

This file is a proposal for Nobuo and Trey: the list, the readouts and the cost
are theirs to change. Nothing here runs until they sign off.

## Design

- **Base**: `JAM24.final` card from the catalog (215 free parameters, 96
  datasets, 5853 points), one replica, `priorSource` = the published replicas.
- **Tier 1 (14 runnable variants today, cheap)**: `maxIterations` 3–5 from the published
  solution. This is a *local* stability probe: it answers "if this choice is
  changed, which way and how far does the fit move", not "where does it
  converge". Measured cost: ~24 min per replica at 8 CPUs.
- **Tier 2 (3 variants, expensive)**: the three that move the most in Tier 1,
  run to convergence (production partition, 24 h, recovery on) to confirm the
  direction is not a one-step artefact.
- **Readouts, identical for every variant**: global χ²/N on mean predictions;
  χ²/N by reaction; Δ(s−s̄)(x, Q²=4) at x = 0.01, 0.05, 0.1; R_s; and the
  W+charm vs SIDIS residual pattern (the paper's Q1 tension).

## Tier 1 — checked against the card builder (2026-10-30)

Fourteen variants are expressible today as one declared `overrides` block; each
was built from the catalog card offline and the "moves" column is what actually
changed (`python3 -m evaluation.e5.plan`). One replica each.

| # | Variant | Change | Moves | Free params | Readout |
|---|---|---|---|---|---|
| 0 | `control` | none | — | 215 | reproduces χ²/N ≈ 1 |
| 1 | `no_wcharm` | drop W+charm | reactions | 215 | the strangeness pull |
| 2 | `no_sidis` | drop SIDIS | reactions | 215 | the FF side of the tension |
| 3 | `sidis_z09` | SIDIS z upper 0.8 → 0.9 | cuts | 215 | cut sensitivity |
| 4 | `sidis_q2_2` | SIDIS Q² lower 1.69 → 2.0 | cuts | 215 | higher-twist / cut sensitivity |
| 5 | `no_sia` | drop SIA | reactions | 215 | FF-only constraint removed |
| 6 | `normals_free` | free every normalization | normalizations | 215 | normalization model |
| 7 | `q0_paper` | Q0² 1.6129 → 1.6384 | qcd | 215 | the paper's 1.28² |
| 8 | `tmc_off` | TMC off | qcd | 215 | higher-twist treatment |
| 9 | `positivity_on` | positivity on for all objects | qcd | 215 | the author-confirmed flags |
| 10 | `no_bootstrap` | bootstrap off | job | 215 | statistical error budget |
| 11 | `one_ff_shape` | one FF shape per object | objects | 125 | shape flexibility |
| 12 | `fixed_kaon` | fix the kaon FF | objects | 155 | which FF carries the tension |
| 13 | `more_iterations` | maxIterations 500 → 2000 | job | 215 | convergence, not physics |

## Seven variants that need a decision, not a run

These cannot be written as an override today; each is reported by
`evaluation.e5.plan` instead of being approximated silently. They are the ones
Nobuo and Trey should rule on first, because two of them (SU(3) and the strange
shape) are exactly the assumptions the atlas is meant to test.

| Variant | Why it is not an override |
|---|---|
| `jam24_current` | needs the other catalog entry (`analysis_id='JAM24.current'`), which is a second card, not a change to this one |
| `s2_independent` | the strange-shape tie lives in the parameter table; the catalog does not expose it as an override |
| `su3_sea` | imposing s⁺ = s̄ means new per-flavour ties, not expressible as an override |
| `three_ff_shapes` | the shape count is library-level (`ff_registry`), not a card field |
| `nnlo` | the composer's order enum is LO/NLO; NNLO is not runnable here |
| `drop_flagged` | the datasets `compare_with_reference` flags are chosen after a fit, not before it |
| `mix_sidis_workbooks` | deliberately a must-refuse case (dis-version mix), so it belongs in E1's table, not the atlas |

## Cost

- Tier 1: 20 replicas × ~24 min ≈ 8 node-hours, parallel across the array (4
  replicas in flight → about 2 h wall), well inside one 6 h gateway job.
- Tier 2: 3 converged fits, production partition, ~24 h each, recovery on.

## What the system already provides

`campaign_card_from_catalog` + `overrides` (declared changes, recorded in
`reproduce.json`); `preview_prior_replicas` / `priorSource` (start from the
published replicas); `compare_with_reference` (per-dataset χ² against the
published fit, already implemented); `campaign_replica_values` (per-replica
numbers); `campaign_pdf_plots` (Δs−s̄ and R_s figures).

## What we need from Nobuo and Trey

1. Which variants belong in the atlas, and how to reach the seven that are
   not overrides — above all `su3_sea` and `s2_independent`, the two
   assumptions the atlas exists to test.
2. Which single physics statement the atlas should support: "the strangeness
   result is stable against X" or "the W+charm vs SIDIS tension survives Y".
   The readouts follow from that choice.
3. Whether a Tier 2 converged fit is worth 3 × 24 h, or the one-step probe is
   enough for a first paper.
