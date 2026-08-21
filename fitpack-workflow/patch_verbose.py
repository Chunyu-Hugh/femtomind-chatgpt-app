"""Add verbose=True option to compute_summary_bf that returns a
markdown-formatted table ready for the chat AI to paste to the user.

Idempotent — uses anchor markers.
"""
from pathlib import Path
import sys

SERVER = Path(__file__).parent / "server.py"
text = SERVER.read_text()

OLD_SIG = '''def compute_summary_bf(
    batch_id: str,
    chat_id: str = "",
    convergence_gate: float = 3.0,
    wait_seconds: int = 45,
) -> Dict[str, Any]:'''

NEW_SIG = '''def compute_summary_bf(
    batch_id: str,
    chat_id: str = "",
    convergence_gate: float = 3.0,
    wait_seconds: int = 45,
    verbose: bool = True,
) -> Dict[str, Any]:'''

if OLD_SIG not in text:
    print("[patch_verbose] signature anchor missing — already patched?")
    sys.exit(0)

text = text.replace(OLD_SIG, NEW_SIG, 1)

# Wrap the final return to attach a markdown rendering when verbose=True
OLD_FINAL = '''    # still computing — return current status with a "retry later" hint
    final = _chi2bf.check_status(batch_id)
    final["hint"] = (
        "Computation still running on HPC. Tell the user to wait ~5 min "
        "then ask 'show me the chi²' again or call get_summary_bf_result."
    )
    return final'''

NEW_FINAL = '''    # still computing — return current status with a "retry later" hint
    final = _chi2bf.check_status(batch_id)
    final["hint"] = (
        "Computation still running on HPC. Tell the user to wait ~5 min "
        "then ask 'show me the chi²' again or call get_summary_bf_result."
    )
    if verbose:
        _attach_markdown(final)
    return final'''

if OLD_FINAL not in text:
    print("[patch_verbose] final-return anchor missing — already patched?")
    sys.exit(0)
text = text.replace(OLD_FINAL, NEW_FINAL, 1)

# Also attach markdown on the "done in budget" early-return path
OLD_EARLY = '''        status = _chi2bf.check_status(batch_id)
        if status.get("status") in ("done", "error"):
            return status'''
NEW_EARLY = '''        status = _chi2bf.check_status(batch_id)
        if status.get("status") in ("done", "error"):
            if verbose:
                _attach_markdown(status)
            return status'''
text = text.replace(OLD_EARLY, NEW_EARLY, 1)

# And on the cached/error path at the top
OLD_CACHED = '''    # already done (cached) or error — return immediately
    if res.get("status") in ("done", "error"):
        return res'''
NEW_CACHED = '''    # already done (cached) or error — return immediately
    if res.get("status") in ("done", "error"):
        if verbose:
            _attach_markdown(res)
        return res'''
text = text.replace(OLD_CACHED, NEW_CACHED, 1)

# Insert the _attach_markdown helper before the compute_summary_bf @mcp.tool() block
HELPER = '''
def _attach_markdown(payload: Dict[str, Any]) -> None:
    """Attach a chat-ready markdown table to a compute_summary_bf result.

    Mutates `payload` in place: adds payload['markdown'] with a JAM24-style
    table. Safe to call on any payload; no-ops if no result present.
    """
    result = payload.get("result") if isinstance(payload, dict) else None
    if not result or not isinstance(result, dict):
        return
    rows = result.get("process_table") or []
    if not rows:
        return
    n_surv = result.get("n_survivors", "?")
    n_tot  = result.get("n_total", "?")
    cs     = result.get("compute_seconds")
    gate   = result.get("convergence_gate", "?")
    lines = [
        f"**chi² (bootstrap=False) summary — {n_surv}/{n_tot} converged replicas "
        f"(gate chi²/npts < {gate})**",
        "",
        "| Process | N_dat | chi²_red | z-score |",
        "| --- | ---: | ---: | ---: |",
    ]
    for r in rows:
        lines.append(
            f"| {r.get('process','-')} "
            f"| {r.get('N_dat','-')} "
            f"| {r.get('chi2_red', float('nan')):.3f} "
            f"| {r.get('z_score', float('nan')):+.3f} |"
        )
    if cs is not None:
        lines.append("")
        lines.append(f"*computed in {cs:.1f} s*")
    payload["markdown"] = "\\n".join(lines)


'''

# Insert HELPER right before the compute_summary_bf @mcp.tool() decorator
ANCHOR = "@mcp.tool()\ndef compute_summary_bf("
if ANCHOR not in text:
    print("[patch_verbose] cannot find compute_summary_bf anchor")
    sys.exit(1)
text = text.replace(ANCHOR, HELPER + ANCHOR, 1)

# Augment the docstring: add a line telling AI how to use the `markdown` field
OLD_DOC_TAIL = '''    Returns:
        {status:\'done\', result:{process_table:[...], per_idx:[...], ...}}
      or
        {status:\'computing\', phase, elapsed_s, eta_seconds} — ask user to
        retry get_summary_bf_result(batch_id) in a few minutes.
    """'''
NEW_DOC_TAIL = '''    Returns:
        {status:\'done\', result:{...}, markdown: "...JAM24-style table..."}
            When verbose=True (default), `markdown` holds a chat-ready table
            the assistant SHOULD paste directly into the reply.
        or
        {status:\'computing\', phase, elapsed_s, eta_seconds, hint}
            Ask user to retry get_summary_bf_result(batch_id) in a few minutes.
    """'''
text = text.replace(OLD_DOC_TAIL, NEW_DOC_TAIL, 1)

SERVER.write_text(text)
print("[patch_verbose] OK — added _attach_markdown + verbose=True default")
