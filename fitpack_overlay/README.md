# fitpack overlay

Files the FemtoMind workflow needs that the `fitpack_legacy` repository does
not carry. `setup_workspace` writes them into every workspace it clones, so a
new collaborator gets a working checkout without hunting down someone else's.

| File | Why it is here |
|---|---|
| `fitlib/maxlike_jam.py` | The fit driver the SLURM template invokes. A copy of `maxlike.py` that writes `progress.json` after each status print, so a running fit can be monitored and its convergence judged. Also records the live parameter vector each step. |
| `tools/config.py` | `load_config` upstream `exec`s the input card line by line, which only works for a Python file. FemtoMind passes YAML, so this version dispatches on the extension. Without it every replica prints `ERR at the input.py` and exits **zero**, which SLURM reports as success. |
| `tools/env.py` | `bootstrap_fitpack_path`, imported by `maxlike_jam`. |
| `tools/compute_chi2_bf.py` | Computes the chi-square summary of a finished batch on the cluster. |
| `inputs/default.yaml` | Starting input card. Submission rewrites the dataset section each time, but the file has to exist first. |

These are patches against a moving target: as they land upstream, delete the
entry here rather than carrying two copies. Everything else the driver imports
(`tools.tools`, `tools.inputmod`, `tools.randomstr`, `fitlib.resman`) already
ships with the repository.

## JAM25 reproduction additions (2026-09-08)

| File | Why it is here |
|---|---|
| `inputs/l2_step1..4.py` | From-scratch ladder cards for the JAM25 (arXiv:2510.11979) reproduction: step1 dy-pion (g1 N fixed), step2 +LN, step3 +pion lattice moments, step4 +NA3 ratio + kaon sector (scenario 6). Authored from the paper text; documented in the FemtoMind repro-jam20 workspace (`stage_logs/JAM25/L2_DESIGN.md`). |
| `obslib/lqcd_mom/` | Lattice PDF-moment reaction module (reader/theory/residuals) for the ETMC Table-1 moments. Written for this campaign; upstream fitpack has no moment module. |
| `qcdlib/pdfkaon.py` | Patched copy: adds `kaon fitting scenario 6` (sea normalization free, gluon normalization by the kaon momentum sum rule — the paper's "normalizations vary"). |
| `fitlib/resman.py` | Patched copy: lqcd_mom reaction wiring (imports, dispatch, setup, chi2 aggregation, gen_report). |
| `fitlib/maxlike_jam.py` | Previous copy + deviation-buffer fix: plain posterior->prior chains (no msr-buffer) initialize `conf['deviation-buffer']` so `get_residuals` can snapshot a buffer msr without KeyError. |

Cluster-side data tables (`database/lqcd_mom/expdata/1000.xlsx`, `1001.xlsx`) are
generated from the paper's Table 1 by the generator in the repro-jam20 workspace.
