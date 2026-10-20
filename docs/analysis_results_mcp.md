# Read-only numerical reference results

These tools orchestrate access to saved results. They do not evaluate physics,
recompute chi-square, create campaigns, submit jobs, or calculate new intervals.
Original numerical arrays and their derived archive statistics are authoritative.
Historical publication-comparison records are retained as provenance only, not
active issues, retrieval requirements, or blockers.

## Requests

1. `list_analysis_results {}` returns compact discovery and a revision-pinned
   `results_ref`. `JAM24.final` resolves through
   `reference_results/JAM24/reference_manifest.json`; callers never need the
   archive's hashed directory name.
2. `get_analysis_fit_statistics {"analysis_id":"JAM24.final","reaction":"wc"}`
   returns only the five W+charm records. `scope` can be `overall`, `reactions`,
   or `datasets` (default). A dataset ID requires its reaction. All statistics
   retain their saved precision; normalization records include missing reasons,
   fitted value/uncertainty, chi-square and linked-root information. The saved
   experimental-point total is `chi2_data`, distinct from the explicitly named
   objective-like sum. Normalization aggregate totals count linked roots once.
3. `get_analysis_theory_predictions
   {"analysis_id":"JAM24.final","reaction":"wc","dataset_id":"10002"}`
   returns a compact description and available fields, without numerical arrays.
   Set `detail="points"` to retrieve ordered point records. Default fields are
   `source_row`, `theory`, `value`, and `alpha`. Request up to 12 explicit fields
   per call, including `exp.*` kinematics/uncertainties. Point indices are zero
   based, in original accepted-point order. Reconstruct arrays by concatenating
   pages; no rounding, reordering or interpolation is performed.

Use `next_cursor` with identical selectors until `complete=true`. `limit` is
1–50, with pages shortened further to keep the complete MCP response below
10 KiB. Cursors bind to revision, view and selectors. `results_ref` values reject
stale archive revisions. Discovery revisions also change when the index changes.

`theory` is the saved mean fitted prediction, already including FitPack's
normalization and profiled shifts. Do not normalize or shift it again.
`prediction_std` is the archived replica population standard deviation, not a
credible interval. Missing metadata is returned as JSON null; experimental
values, alpha and predictions are required to be finite. No complete replica
arrays are exposed by these tools.

## Storage, authorization and integrity

The gateway ships only the validated small index manifests and the retrieval
module. Both deployment paths include them; hot deployment validates them in
the staged snapshot and swaps/rolls them back with the application. No `data/`
directory or numerical archive is included in the deployment payload.

Numerical requests reuse `_run_ifarm_py` / `_remote` and the authenticated user's
existing HPC SSH chain. The worker is sent as base64-encoded Python using the
existing helper. It needs the already installed HPC Python/NumPy environment,
not FitPack or a newly installed dependency. No Ceph mount is needed on the
gateway node. Unavailable storage/SSH, checksum failures and stale references
produce explicit errors, never substituted local statistics.

The worker reads the pinned archive manifest, summary, and (for predictions)
only the selected dataset's `means/` and `datasets/` shards. SHA256 and byte
counts are verified. Files/ancestor links, arbitrary tool paths, unknown IDs,
unsupported arguments and mismatched revisions are rejected. Client selectors
never become shell commands or filenames: dataset filenames come from checked
archive records. Existing gateway authentication is unchanged.

Register future analyses by adding a validated `reference_manifest.json` under
their own folder with schema `femtomind.reference_results.index.1` and the same
summary/shard schema. No JAM24-specific retrieval logic is required. Keep large
numerical files in persistent HPC storage, outside Git. Indexes pin both the
archive manifest checksum and the scientific `catalog_ref`.

## Tests

`python -B -m unittest discover -s tests -p test_analysis_results_mcp.py -v`
uses disposable fixtures and a real loopback HTTP MCP dispatcher. Optionally set
`JAM_RESULTS_REAL_SSH=1` to exercise the actual archived JAM24 results through the
existing local SSH tunnel (read-only). Set `RESULTS_MCP_TEST_REPORT` to an output
path outside the checkout to retain measured response sizes and sample records.
No live gateway calls, deployment, campaigns or jobs are involved.
