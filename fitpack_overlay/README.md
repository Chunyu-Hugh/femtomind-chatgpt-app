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
