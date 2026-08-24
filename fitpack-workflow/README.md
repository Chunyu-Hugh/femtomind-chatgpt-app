# fitpack-workflow

The physics backend. `femtomind_app.py` (the MCP server ChatGPT connects to)
holds no domain logic of its own — it authenticates the caller, resolves that
identity to an HPC account, and forwards to the tools here, which own the
dataset catalogue, job submission, SLURM tracking, chi-square evaluation and
replica plotting.

Runs behind `mcpo` on the application host and reaches Jefferson Lab over SSH.
Nothing scientific executes here: every replica is a SLURM job in the group's
container image, and even the Analysis-tab curves are computed on the cluster.

## Layout

| | |
|---|---|
| `server.py` | every physics tool; the file the app forwards to |
| `dataset_catalog.py` | the group's dataset table, with the compatibility flags |
| `ssh_runner.py` | two-hop JLab login, ControlMaster reuse, per-identity scoping |
| `status_sync.py` | SLURM status sweep — one `squeue` + one `sacct` per account |
| `status_monitor.py` | background sweep that keeps task rows fresh |
| `replica_plot_worker.py` | runs **on the cluster**; projects replica parameters onto `parman.order` and evaluates `get_xF` |
| `replica_plot_env.py` | local fallback env when the cluster is unreachable |
| `chi2_bootstrap_false.py`, `remote_worker/` | chi-square summary workers |
| `result_manager.py`, `task_cancellation.py`, `slurm_tools.py` | run history, cancellation, SLURM helpers |
| `schema.py`, `advanced_schema.py`, `workflows.yaml` | tool schemas and the fit form |

## Two things that bite

**The plot worker runs under tcsh.** ifarm's login shell is tcsh, so `_rc=$?`,
unquoted parentheses and `&&`-chained cleanup are syntax errors there. A worker
invocation that "fails" with an unrelated local error is usually this: the
remote command never ran, and the caller fell through to the local path.

**`squeue` format strings must be one argv entry.** `--format=%i %t` is split on
the space and squeue rejects the whole invocation with
`Unrecognized option: %t`. Every status sweep then falls back to `sacct` for
every job id, which is the difference between a 0.4-second campaign listing and
a 30-second one. Use `-o "%i|%t"`.

## Deployment

Copied to the application host and served by `mcpo`; it is not installed as a
package. `hpc_tasks.db` lives beside the code and is **not** in this repo — it
holds per-user run history. Back it up before any destructive operation.

## Trust boundary (read before exposing anything)

`server.py` performs **no transport-level authentication**: it trusts the
`chat_id` its callers pass. The security of the whole system depends on the
gateway in front of it. Two rules, in order:

1. **`mcpo` must bind loopback only** (or otherwise enforce the same identity
   resolution the front end does). The ChatGPT-facing server
   (`femtomind_app.py`) is the authenticated entry point; the tools here must
   never be reachable from the public internet directly.
2. Tools that DO receive an identity (`compute_summary_bf`,
   `get_summary_bf_result`) verify batch ownership against the ledger
   (`_batch_owned_by`). Any new tool that acts on a `batch_id`/`task_path`
   should do the same rather than trusting the argument.

