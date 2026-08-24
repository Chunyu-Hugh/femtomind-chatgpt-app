#!/usr/bin/env python3
"""
FitPack HPC Monitoring MCP Server

Provides lightweight SLURM cluster monitoring tools and direct job management,
independent of the main FitPack workflow.

Shares the task database with the FitPack MCP server (read-only for most tools).
"""
import logging
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from ssh_runner import ssh_run, ssh_write_file, REMOTE_BASE

# --- Configuration ---
# Shared database (local tracking ledger)
import os as _os
DB_PATH = _os.environ.get("DB_PATH", str((Path(__file__).parent / ".." / ".." / "fitpack-workflow" / "hpc_tasks.db").resolve()))
REMOTE_RUNS_DIR = f"{REMOTE_BASE}/runs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("hpc-mcp")

mcp = FastMCP("hpc-mcp")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def _get_any_username() -> str:
    """Get any available HPC username from SSH sockets."""
    import glob
    socket_dir = _os.path.expanduser("~/.ssh/controlmasters")
    for s in glob.glob(_os.path.join(socket_dir, "jlab-ifarm-*")):
        try:
            name = _os.path.basename(s)
            user = name.rsplit("-", 1)[-1].split("@")[0]
            if user:
                return user
        except Exception:
            pass
    return ""


def _run_cmd(command: List[str], username: Optional[str] = None) -> str:
    """Run a command on the remote HPC via SSH and return stdout, or an error string.

    `username` selects whose ControlMaster -- and therefore whose cluster
    account -- the command runs under. The "any configured user" fallback below
    is only safe while a single person uses this server; with several
    collaborators it would run one person's query down another's connection.
    """
    try:
        username = username or _get_any_username()
        result = ssh_run(command, timeout=15, username=username or None)
        if result.returncode != 0:
            return f"Error running {' '.join(command)}:\n{result.stderr}"
        return result.stdout
    except ConnectionError as e:
        return f"SSH connection error: {e}"
    except subprocess.TimeoutExpired:
        return f"Timeout running {' '.join(command)}"
    except Exception as e:
        return f"Exception running {' '.join(command)}: {e}"


# ---------------------------------------------------------------------------
# Tools: Batch history
# ---------------------------------------------------------------------------

@mcp.tool()
def list_batches_in_chat(chat_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    List the most recent task batches generated in the current conversation.

    Args:
        chat_id: Unique conversation identifier.
        limit:   Maximum number of recent batches to return (default 5).
    """
    conn = _get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                batch_id,
                MIN(created_at) AS db_time,
                COUNT(*)        AS total,
                SUM(is_running) AS submitted
            FROM tasks
            WHERE chat_id = ?
            GROUP BY batch_id
            ORDER BY db_time DESC
            LIMIT ?
        """, (chat_id, limit))
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return [{"message": "No task records found for this conversation."}]

    results = []
    for row in rows:
        bid = row["batch_id"]
        total = row["total"]
        submitted = row["submitted"] or 0

        # Try to extract human-readable time from batch_id (format: batch_<unix_ts>_<hex>)
        display_time = row["db_time"]
        try:
            parts = bid.split("_")
            if len(parts) >= 2 and parts[1].isdigit():
                display_time = datetime.fromtimestamp(int(parts[1])).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

        if submitted == total:
            status_summary = "All Submitted"
        elif submitted > 0:
            status_summary = f"Partially Submitted ({submitted}/{total})"
        else:
            status_summary = "Pending"

        results.append({
            "batch_id": bid,
            "created_time": display_time,
            "summary": f"[{display_time}] {total} tasks – {status_summary}",
            "stats": {"total": total, "submitted": submitted, "pending": total - submitted},
        })

    return results


# ---------------------------------------------------------------------------
# Tools: SLURM cluster monitoring
# ---------------------------------------------------------------------------

@mcp.tool()
def get_slurm_jobs(user: Optional[str] = None) -> str:
    """View the SLURM queue (squeue)."""
    # ssh joins argv with spaces before the remote shell sees it, so a format
    # string containing spaces must carry its own quotes or squeue parses the
    # later fields as options ("Unrecognized option: %10P").
    cmd = ["squeue", "--format='%10i %10P %20j %10u %10t %10M %10L %R'"]
    if user:
        cmd.extend(["-u", user])
    return _run_cmd(cmd, username=user)


@mcp.tool()
def get_slurm_info(username: Optional[str] = None) -> str:
    """View node status (sinfo)."""
    return _run_cmd(["sinfo", "-N", "-l"], username=username)


@mcp.tool()
def get_job_details(job_id: str, username: Optional[str] = None) -> str:
    """View detailed information for a SLURM job (scontrol show job)."""
    return _run_cmd(["scontrol", "show", "job", job_id], username=username)


@mcp.tool()
def quota_check(username: Optional[str] = None) -> str:
    """Check disk quota for the caller's own cluster account."""
    return _run_cmd(["quota", "-s"], username=username)


# ---------------------------------------------------------------------------
# Tools: Job control
# ---------------------------------------------------------------------------

@mcp.tool()
def cancel_job(job_id: str) -> str:
    """
    Cancel a SLURM job and mark it as cancelled in the shared database.

    Args:
        job_id: SLURM job ID.
    """
    # Update shared DB (best-effort)
    try:
        with _get_db() as conn:
            conn.execute(
                "UPDATE tasks SET is_running = 0, status = 'cancelled' WHERE slurm_job_id = ?",
                (job_id,),
            )
    except Exception:
        pass
    return _run_cmd(["scancel", job_id])


@mcp.tool()
def submit_custom_job(
    script_content: str,
    job_name: str = "mcp_custom",
    nodes: int = 1,
    mem: str = "4G",
    time_limit: str = "01:00:00",
) -> str:
    """
    Expert mode: submit an arbitrary SLURM script, bypassing the workflow database.

    Args:
        script_content: The body of the sbatch script (after the header).
        job_name:       SLURM job name.
        nodes:          Number of nodes.
        mem:            Memory allocation (e.g. "4G", "16GB").
        time_limit:     Wall-clock time limit (HH:MM:SS).
    """
    header = "\n".join([
        "#!/bin/bash",
        f"#SBATCH --job-name={job_name}",
        f"#SBATCH --nodes={nodes}",
        f"#SBATCH --mem={mem}",
        f"#SBATCH --time={time_limit}",
        "#SBATCH --output=custom_%j.log",
    ])
    full_script = header + "\n\n" + script_content
    script_name = f"manual_job_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sbatch"
    remote_script_path = f"{REMOTE_RUNS_DIR}/{script_name}"
    try:
        ssh_write_file(remote_script_path, full_script)
        return _run_cmd(["sbatch", remote_script_path])
    except Exception as e:
        return f"Submission failed: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="FitPack HPC Monitoring MCP Server")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse"])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8223)
    args, _ = parser.parse_known_args()

    if args.transport == "sse":
        logger.info(f"Starting SSE transport on {args.host}:{args.port}")
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
