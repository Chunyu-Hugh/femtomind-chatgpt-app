"""
Patch to add to fitpack-workflow/server.py.

Insert these two @mcp.tool() functions anywhere after the other tool
definitions (e.g., right before the __main__ block). Also add the import
at the top of the file.
"""

# ==== Add near the top of server.py (imports section) ====
import chi2_bootstrap_false as _chi2bf


# ==== Add as new @mcp.tool() endpoints ====

@mcp.tool()
def compute_summary_bf(
    batch_id: str,
    chat_id: str = "",
    convergence_gate: float = 3.0,
) -> Dict[str, Any]:
    """Start a bootstrap=False chi²_red and z-score summary on HPC.

    This is the PUBLICATION-QUALITY summary — theory evaluated against the
    real central data (not the Gaussian-shifted pseudodata used during the
    fit). The computation runs inside an apptainer sif on ifarm in the
    background; this call returns immediately. Poll with
    `get_summary_bf_result(batch_id)`.

    Args:
        batch_id: the batch_id whose 10 bootstrap replicas should be
            aggregated. Must already exist in the `tasks` table.
        chat_id: optional — used to resolve hpc_username from user_hpc_config
            if no hpc_username is available from the tasks row.
        convergence_gate: replicas whose fit-objective chi²/npts (from
            progress.json last iter) exceeds this are excluded from the
            aggregation. Default 3.0.

    Returns:
        {status: 'computing'|'done'|'error', batch_id, eta_seconds?, cached?,
         result?: {process_table, per_idx, n_survivors, ...}}
    """
    hpc_username = _get_hpc_username(chat_id) if chat_id else ""
    return _chi2bf.start_compute(
        batch_id=batch_id,
        hpc_username=hpc_username or "",
        convergence_gate=convergence_gate,
    )


@mcp.tool()
def get_summary_bf_result(batch_id: str) -> Dict[str, Any]:
    """Poll the status of a previously-triggered bootstrap=False summary
    computation. If the result is ready, return the full aggregated table.

    Returns one of:
      {status: 'done',      result: {process_table: [...], per_idx: [...]}}
      {status: 'computing', phase: 'loading_resman'|'computing_<rep>'|...,
       elapsed_s, eta_seconds}
      {status: 'error',     error: '...'}
      {status: 'not_found'}    # batch_id never had a job started
    """
    return _chi2bf.check_status(batch_id)
