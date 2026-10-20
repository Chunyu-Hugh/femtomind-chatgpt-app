# JAM24 original numerical reference

This reference archives the original final fitted ensemble, not a new fit and
not a declaration that the historical fit can yet be reproduced completely.
`reference_manifest.json` binds the result archive to the approved
`JAM24.final` catalog revision, the source checksums, and the archive checksum.
`fit_statistics.json` contains compact dataset, reaction, and overall statistics;
`appendix_preview.md` presents them in the requested table format.

## Storage and provenance

All numerical arrays remain on Ceph under this repository's
`reference_results/JAM24/data/<archive-id>/`. The nested `.gitignore` excludes
every numerical archive; only its ignore policy is eligible for version control.
No large array is copied into Git or the gateway deployment snapshot.

Each immutable archive contains:

- `parameters.npz`: all original replica values, exact column ordering, source
  ordinals, and per-row checksums. There are 215 independent PDF/FF model
  columns and 43 independent normalization columns.
- `original_parameter_bundle.npy.gz`: a checksum-verified, byte-exact compressed
  copy of `replicas_cut.npy`, preserving its full original configuration and
  original Python key types.
- `parameter_metadata.json`: original column triples and configuration, all 369
  catalog parameter definitions, links, and the pinned catalog reference. Linked
  parameters are reconstructed through their original links, not independently
  sampled. Four PDF sum-rule normalizations require native FitPack recomputation;
  their literal initial values are not mislabeled as fitted replica values.
- `precut_order.npz`: exact row-byte matches to `replicas_cut_all.npy`, when unique.
  These are source ordinals, not invented original fit-job IDs.
- `replicas/<reaction>_<id>.npz`: every original per-replica metric, including
  fitted predictions, residuals, correlated pulls, normalization residuals, and
  shifts, in their original order.
- `means/<reaction>_<id>.npz`: averaged fitted predictions, population standard
  deviations, original experimental values/uncorrelated errors, shifts, residuals,
  and accepted original workbook row indices.
- `datasets/<reaction>_<id>.npz`: current native-reader kinematics and uncertainty
  columns, validated against the original values/errors and approved workbook
  checksums. Current covariance eigenvector names/signs are not asserted to be
  the original nuisance-coordinate basis. Original `rres` arrays are preserved.
- `summary.json`, `validation.json`, `provenance.json`, and `manifest.json`:
  scientific conventions, publication comparisons, source/code checksums, sizes,
  and explicit missing-value reasons.

Numeric NPZ members require no pickle support. Native SIA nullable flavor tags
use explicitly declared JSON-cell encoding in `__archive_json_columns__`;
`build_archive.load_numeric()` restores `None` rather than converting it into a
string. Other numeric columns are unchanged. The exact original parameter bundle
retains the original trusted NumPy pickle format for archival preservation.

The original `2-0` directory and FitPack are read-only. The development driver
uses the existing scientific container with a read-only Ceph mount and a single
writable bind for this new ignored archive directory. It transmits source over
SSH stdin without synchronizing or deploying application code.

## Statistical definitions

`thy_cut['thy']` already stores FitPack's **fitted prediction**, including the
normalization division and correlated systematic shift. Neither operation is
applied again. The original writer and current FitPack residual implementation
establish this interpretation; every original residual/point pairing is checked
within an explicit float64 arithmetic-reassociation bound.

- Appendix data chi2 is calculated **after averaging predictions**:
  `sum(((mean(prediction)-value)/alpha)**2)`. It is not mean replica chi2.
- Reduced chi2 divides the data term by the number of accepted points; it does
  not include the separately reported correlated/normalization terms and does
  not subtract the number of model parameters.
- Correlated chi2 is `sum(mean(rres, axis=0)**2)`. `N_cor` is the original nuisance
  residual dimension, checked against native uncertainty columns; it is not
  assumed equal to the number of points.
- Normalization chi2 uses the original `(mean(norm)-1)**2/dN**2` convention.
  The fitted normalization uncertainty is the replica population standard
  deviation (`ddof=0`), as in the original table notebook, not the prior width.
  Shared rows retain their linked normalization for display; aggregate objective
  contributions count independent normalization roots only. Both the unique
  penalty sum and the display-row sum are retained so duplication stays explicit.
- Z-score uses the original `get_zscore()` from
  `2-0/chi2_zscore_plot_paper.ipynb`, extracted without executing notebook cells:
  `stats.norm.ppf(stats.chi2.cdf(chi2, Ndat))`. No such function was found in the
  current FitPack library. The archived original function and the catalog agree.
- Missing/not-applicable metrics are `null` plus a reason. The table displays a
  dash. An absent normalization is not an implicit fitted unity normalization.

The stored objective-like sum combines the data term of mean fitted predictions,
mean-pull penalties, and unique normalization penalties. It is not a new fit,
an evaluation at mean model parameters, or a mean replica objective.

All comparisons use cataloged publication rows only as validation references.
No printed value is substituted into numerical results. Publication discrepancies
remain visible in `validation.json` and `reference_manifest.json`.

## Reuse and validation

`build_archive.py` creates a new immutable archive and fails rather than
overwriting one. It imports only native readers, AUX kinematics and statistical
preparation routines: no RESMAN, PDF/FF evaluation, fitting, or Slurm operations.
`verify_archive.py` independently reopens the archived arrays and original files
and verifies checksums, replica order, all per-replica metrics, and mean predictions.
Focused tests live in `tests/test_jam24_reference_results.py` and run in the
existing NumPy/SciPy environment.

The read-only MCP tools `list_analysis_results`, `get_analysis_fit_statistics`,
and `get_analysis_theory_predictions` resolve `JAM24.final` through the stable
manifest. Only manifests and retrieval code are packaged for the gateway;
numerical shards remain on Ceph and are read through the authorized HPC chain.
See `docs/analysis_results_mcp.md` for retrieval and deployment details.

## Limits of the historical record

The original experimental bundle stores ordered values and uncorrelated errors,
not historical kinematic labels. The additional native-reader kinematics are
matched to that ordered value/error sequence and the approved workbook checksums;
the exact historical reader revision is not asserted. Some original workbook
`units` metadata cells are blank (NaN); they remain blank. Experimental values,
errors, fitted parameters and predictions are finite.

The original saved numerical arrays and their derived statistics are the
numerical source of truth. Historical publication comparisons retained in
immutable validation records are audit history, not active issues or blockers.
Future numerical comparisons use this archive, without adjusting values to match
publication tables.
