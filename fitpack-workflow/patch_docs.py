"""Update tool docstrings so chat AI prefers compute_summary_bf for 'summary' requests.

Also adds a short inline poll inside compute_summary_bf (cached result returns instantly).
Idempotent — detects markers.
"""
from pathlib import Path
import sys

SERVER = Path(__file__).parent / "server.py"

# Edit 1: compare_step_results docstring — demote it
OLD_COMPARE_DOC = '''    """
    Compare chi2/npts across all completed fitting steps in this chat.

    For each step in the chain history, retrieves the best task's per-dataset
    chi2/npts breakdown. Returns a side-by-side comparison showing how fit
    quality evolved across steps (e.g., IDIS → IDIS+DY → IDIS+DY+WASYM).

    Use this for the thinking framework's quality judgment step — it shows
    whether adding new data improved or worsened specific datasets.

    Args:
        chat_id: Current chat/session ID.
    """'''

NEW_COMPARE_DOC = '''    """
    (Fit-monitor data only — NOT publication chi²)

    Returns bootstrap=True fit-objective chi²/npts per step, as shown on the
    live fitting monitor dashboard. These numbers include Gaussian pseudodata
    shifts and are systematically ~40-70%% higher than the true data-vs-theory
    chi². They are only suitable for tracking whether a fit is converging.

    For the REAL chi² (publication-ready, bootstrap=False, per process class,
    with z-score) that should be reported to users asking "give me the
    summary" or "what is the chi²" — use compute_summary_bf() instead.

    Args:
        chat_id: Current chat/session ID.
    """'''

# Edit 2: compute_summary_bf docstring — promote it + add polling
OLD_BF_TOOL = '''@mcp.tool()
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
    )'''

NEW_BF_TOOL = '''@mcp.tool()
def compute_summary_bf(
    batch_id: str,
    chat_id: str = "",
    convergence_gate: float = 3.0,
    wait_seconds: int = 45,
) -> Dict[str, Any]:
    """**PRIMARY tool for user chi² summary questions.**

    Use this whenever the user asks any of:
      - "give me the summary" / "show me the chi²"
      - "how is the fit / what is chi²_red / zscore"
      - "is this a good fit" / "is the chi² reasonable"
      - "compare to JAM24" / "publication numbers"

    Returns the PUBLICATION-QUALITY chi²_red and z-score: theory evaluated
    against the real central data (bootstrap=False), aggregated across
    converged replicas, grouped by process class (DIS fixed target, HERA,
    Drell-Yan, ...). This is the number JAM papers report.

    Do NOT use `compare_step_results` for this — that tool reports the
    fit-objective chi² (bootstrap=True, with Gaussian data shifts) which is
    ~40-70%% inflated and only useful for monitoring convergence on the
    live dashboard.

    Computation runs on HPC in the background (~5-8 min when cold). If a
    cached result exists this returns instantly. Otherwise this call waits
    up to `wait_seconds` (default 45s) in case the compute finishes quickly,
    then returns {status: 'computing'} — the user should retry with
    `get_summary_bf_result(batch_id)` in ~5 min.

    Args:
        batch_id: batch_id of a completed bootstrap fit batch.
        chat_id: optional, used to resolve hpc_username.
        convergence_gate: replicas whose fit chi²/npts exceeds this are
            dropped before aggregation (default 3.0).
        wait_seconds: seconds to poll inline before returning a 'computing'
            handle. Keep ≤60 for responsive MCP calls. Set 0 to fire-and-forget.

    Returns:
        {status:'done', result:{process_table:[...], per_idx:[...], ...}}
      or
        {status:'computing', phase, elapsed_s, eta_seconds} — ask user to
        retry get_summary_bf_result(batch_id) in a few minutes.
    """
    if not CHI2_BF_OK:
        return {"error": "chi2_bootstrap_false module unavailable"}
    hpc_username = _get_hpc_username(chat_id) if chat_id else ""

    res = _chi2bf.start_compute(
        batch_id=batch_id,
        hpc_username=hpc_username or "",
        convergence_gate=convergence_gate,
    )
    # already done (cached) or error — return immediately
    if res.get("status") in ("done", "error"):
        return res

    # inline poll so cached/near-done cases return a final result in one call
    import time as _time
    budget = max(0, int(wait_seconds))
    for _ in range(budget // 5):
        _time.sleep(5)
        status = _chi2bf.check_status(batch_id)
        if status.get("status") in ("done", "error"):
            return status
    # still computing — return current status with a "retry later" hint
    final = _chi2bf.check_status(batch_id)
    final["hint"] = (
        "Computation still running on HPC. Tell the user to wait ~5 min "
        "then ask 'show me the chi²' again or call get_summary_bf_result."
    )
    return final'''


def main() -> int:
    text = SERVER.read_text()
    changed = False

    if OLD_COMPARE_DOC in text:
        text = text.replace(OLD_COMPARE_DOC, NEW_COMPARE_DOC, 1)
        print("[patch_docs] updated compare_step_results docstring")
        changed = True
    else:
        print("[patch_docs] compare_step_results docstring already updated or anchor missing")

    if OLD_BF_TOOL in text:
        text = text.replace(OLD_BF_TOOL, NEW_BF_TOOL, 1)
        print("[patch_docs] upgraded compute_summary_bf (docstring + inline poll)")
        changed = True
    else:
        print("[patch_docs] compute_summary_bf already upgraded or anchor missing")

    if changed:
        SERVER.write_text(text)
        print("[patch_docs] server.py written")
    else:
        print("[patch_docs] no changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
