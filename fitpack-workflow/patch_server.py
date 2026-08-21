"""Idempotent in-place patch: add chi2_bootstrap_false tool endpoints to server.py.

Safe to run multiple times — detects existing patch markers.
"""
from pathlib import Path
import sys

SERVER = Path(__file__).parent / "server.py"
MARKER_IMPORT = "import chi2_bootstrap_false as _chi2bf"
MARKER_TOOLS  = "# --- chi2-bootstrap-false tools (auto-added) ---"

TOOLS_BLOCK = '''

# --- chi2-bootstrap-false tools (auto-added) ---
try:
    import chi2_bootstrap_false as _chi2bf
    CHI2_BF_OK = True
except Exception as _e:
    logger.warning(f"chi2_bootstrap_false unavailable: {_e}")
    CHI2_BF_OK = False


@mcp.tool()
def compute_summary_bf(
    batch_id: str,
    chat_id: str = "",
    convergence_gate: float = 3.0,
) -> Dict[str, Any]:
    """Kick off the publication-quality (bootstrap=False) chi²_red + Z-score
    summary for a completed bootstrap batch. Runs in HPC background (~5-8 min);
    poll with `get_summary_bf_result(batch_id)`.

    Args:
        batch_id: batch_id of a completed 10-replica bootstrap fit (must
            already exist in `tasks` table).
        chat_id: optional, used to resolve hpc_username if not on task rows.
        convergence_gate: replicas whose fit-objective chi²/npts (from
            progress.json last iter) exceeds this are excluded. Default 3.0.

    Returns:
        {status: 'computing'|'done'|'error', batch_id, eta_seconds?, result?}
        If cached, returns the result immediately with cached=True.
    """
    if not CHI2_BF_OK:
        return {"error": "chi2_bootstrap_false module unavailable"}
    hpc_username = _get_hpc_username(chat_id) if chat_id else ""
    return _chi2bf.start_compute(
        batch_id=batch_id,
        hpc_username=hpc_username or "",
        convergence_gate=convergence_gate,
    )


@mcp.tool()
def get_summary_bf_result(batch_id: str) -> Dict[str, Any]:
    """Poll the bootstrap=False summary job started by `compute_summary_bf`.

    Returns one of:
      {status: 'done',      result: {process_table: [...], per_idx: [...], ...}}
      {status: 'computing', phase, elapsed_s, eta_seconds}
      {status: 'error',     error}
      {status: 'not_found'}
    """
    if not CHI2_BF_OK:
        return {"error": "chi2_bootstrap_false module unavailable"}
    return _chi2bf.check_status(batch_id)
'''


def main() -> int:
    text = SERVER.read_text()

    if MARKER_TOOLS in text:
        print(f"[patch_server] already patched — no changes made")
        return 0

    # Insert just before 'def main()' (the entry point of server.py)
    anchor = "\n# ---------------------------------------------------------------------------\n# Entry point\n# ---------------------------------------------------------------------------\n"
    if anchor not in text:
        print(f"[patch_server] ERROR: cannot find 'Entry point' anchor in server.py",
              file=sys.stderr)
        return 1

    new_text = text.replace(anchor, TOOLS_BLOCK + "\n" + anchor, 1)
    SERVER.write_text(new_text)
    print(f"[patch_server] OK — inserted {len(TOOLS_BLOCK.splitlines())} lines before 'def main()'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
