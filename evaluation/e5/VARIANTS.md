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
- **Tier 1 (20 variants, cheap)**: `maxIterations` 3–5 from the published
  solution. This is a *local* stability probe: it answers "if this choice is
  changed, which way and how far does the fit move", not "where does it
  converge". Measured cost: ~24 min per replica at 8 CPUs.
- **Tier 2 (3 variants, expensive)**: the three that move the most in Tier 1,
  run to convergence (production partition, 24 h, recovery on) to confirm the
  direction is not a one-step artefact.
- **Readouts, identical for every variant**: global χ²/N on mean predictions;
  χ²/N by reaction; Δ(s−s̄)(x, Q²=4) at x = 0.01, 0.05, 0.1; R_s; and the
  W+charm vs SIDIS residual pattern (the paper's Q1 tension).

## Tier 1 — twenty variants

| # | Change | What it tests | Field |
|---|---|---|---|
| 0 | none (the published card) | control: reproduces χ²/N ≈ 1 | — |
| 1 | drop W+charm | the dataset that pulls strangeness | `datasets.wc` |
| 2 | drop SIDIS | the dataset that pulls the FF side | `datasets.sidis` |
| 3 | SIDIS z upper 0.8 → 0.9 | cut sensitivity of the FF constraint | `cuts.sidis` |
| 4 | SIDIS Q² lower 1.69 → 2.0 | higher-twist / cut sensitivity | `cuts.sidis` |
| 5 | drop SIA | FF-only constraint removed | `datasets.sia` |
| 6 | JAM24.current workbooks instead of final | the 2026-10-23 data update | `source` |
| 7 | free all 43 normalizations | normalization model dependence | `normalizations` |
| 8 | S2 independent of S1 (strange shape freedom) | the SU(3) assumption | `parameters` |
| 9 | impose s+ = s̄ (SU(3) symmetric sea) | the opposite assumption | `parameters` |
| 10 | one FF shape instead of two (35/40/30/30) | shape flexibility | `objects` |
| 11 | three FF shapes for the pion | shape flexibility, other direction | `objects` |
| 12 | fix the kaon FF, fit pion + hadron only | which FF carries the tension | `objects` |
| 13 | Q0² 1.6129 → 1.6384 | the paper's 1.28² vs the fit-time value | `qcd` |
| 14 | TMC off | higher-twist treatment | `qcd` |
| 15 | positivity on (pdf, pion, kaon, hadron) | the author-confirmed flags | `qcd` |
| 16 | bootstrap off | statistical vs systematic error budget | `job` |
| 17 | drop the datasets `compare_with_reference` flags at p_BH < 0.05 | the 9 datasets that already differ | `datasets` |
| 18 | NNLO instead of NLO | the theory order (check FitPack support first) | `qcd` |
| 19 | add the 2026 SIDIS workbooks (1105–3111) on top of final | mixing the two data generations (expected to be refused: `dis version`) | `datasets` |

Variant 19 is deliberately a *must-refuse* case (EasyScan_HEP 2 has one too):
the gateway should refuse the mix, and the paper should show that it does.

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

1. Which 20 variants belong in the atlas — the list above is a proposal, and
   variants 18 (NNLO) and 6 (the workbooks) may not be runnable as written.
2. Which single physics statement the atlas should support: "the strangeness
   result is stable against X" or "the W+charm vs SIDIS tension survives Y".
   The readouts follow from that choice.
3. Whether a Tier 2 converged fit is worth 3 × 24 h, or the one-step probe is
   enough for a first paper.
