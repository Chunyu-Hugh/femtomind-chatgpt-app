"""
Local orchestrator for bootstrap=False chi² summary computation.

This module is imported by fitpack-workflow/server.py. It handles:
  - DB lookups (batch -> run paths, cached summaries)
  - SSH staging of the worker script to HPC
  - Kicking off the worker in background on ifarm
  - Polling remote status/output files
  - Persisting results back into `batch_summaries` DB table

It does NOT do any chi² arithmetic itself — that happens in the worker
(compute_chi2_bf.py) inside apptainer on ifarm.
"""
from __future__ import annotations
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ssh_runner import (
    ssh_run, ssh_file_exists, ssh_read_file, ssh_write_file, ssh_mkdir,
    get_ssh_config, get_remote_base,
)
from iri_backend import is_iri_enabled, iri_submit_script

logger = logging.getLogger("chi2_bf")

# --- Constants ---
_SERVER_DIR = Path(__file__).parent.resolve()
WORKER_LOCAL_PATH = _SERVER_DIR / "remote_worker" / "compute_chi2_bf.py"
WORKER_REMOTE_SUBPATH = "tools/compute_chi2_bf.py"   # placed under /w/ceph24/.../fitpack_legacy/
CACHE_REMOTE_SUBDIR = "chi2_cache"                   # under /w/ceph24/.../fitpack_legacy/
SIF_PATH = os.environ.get(
    "APPTAINER_SIF",
    "/u/scigroup/scicomp/jupyterhub/theory/theory-notebook.sif",
)
ETA_SECONDS = 360              # typical ~6 min end-to-end
JOB_MAX_SECONDS = 1200         # kill/fail after 20 min

DB_PATH = os.environ.get("DB_PATH", str(_SERVER_DIR / "hpc_tasks.db"))


# --- DB helpers ---
def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def ensure_schema() -> None:
    """Idempotent: create batch_summaries table if missing."""
    with _db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS batch_summaries (
                batch_id         TEXT PRIMARY KEY,
                hpc_username     TEXT NOT NULL,
                mode             TEXT NOT NULL DEFAULT 'bootstrap_false',
                status           TEXT NOT NULL DEFAULT 'computing',
                   -- computing | done | error
                convergence_gate REAL DEFAULT 3.0,
                n_total          INTEGER,
                n_survivors      INTEGER,
                started_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                computed_at      TIMESTAMP,
                compute_seconds  REAL,
                remote_output    TEXT,
                remote_status    TEXT,
                result_json      TEXT,
                error_message    TEXT
            )
        """)


# --- Worker staging ---
def _ensure_worker_on_remote(hpc_username: str) -> str:
    """Upload worker script if missing or stale. Return remote path."""
    remote_base = get_remote_base(hpc_username)
    remote_path = f"{remote_base}/{WORKER_REMOTE_SUBPATH}"
    local_text = WORKER_LOCAL_PATH.read_text()
    # naive: always write (worker is small, ~7 KB); idempotent
    ssh_mkdir(os.path.dirname(remote_path), username=hpc_username)
    ssh_write_file(remote_path, local_text, username=hpc_username)
    return remote_path


def _collect_run_names(batch_id: str) -> List[str]:
    """Query tasks table for all run paths in this batch."""
    with _db() as con:
        rows = con.execute(
            "SELECT DISTINCT path FROM tasks WHERE batch_id = ? ORDER BY path",
            (batch_id,),
        ).fetchall()
    names = []
    for r in rows:
        p = r["path"] or ""
        if p:
            names.append(os.path.basename(p.rstrip("/")))
    return names


def _collect_hpc_username(batch_id: str) -> Optional[str]:
    with _db() as con:
        row = con.execute(
            "SELECT hpc_username FROM tasks WHERE batch_id = ? AND hpc_username != '' LIMIT 1",
            (batch_id,),
        ).fetchone()
    return row["hpc_username"] if row else None


# --- Public API: start a compute job ---
def start_compute(
    batch_id: str,
    hpc_username: str = "",
    convergence_gate: float = float("inf"),
) -> Dict[str, Any]:
    """Kick off bootstrap=False chi² computation in HPC background.

    Returns immediately with {status, batch_id, eta_seconds, cached?}.
    """
    ensure_schema()

    # check cache
    with _db() as con:
        row = con.execute(
            "SELECT * FROM batch_summaries WHERE batch_id = ?", (batch_id,)
        ).fetchone()
    # Cache key is (batch_id, convergence_gate). Different gates → different
    # aggregations, so a row for gate=inf is NOT reusable for gate=1.5. We
    # fall through (restart) when the gate doesn't match. float equality is
    # safe here: gate values are either argparse-parsed user input or
    # previously-stored identical floats (inf == inf is True).
    def _same_gate(row_val, requested):
        if row_val is None:
            return False
        try:
            return float(row_val) == requested
        except (TypeError, ValueError):
            return False

    if row is not None and _same_gate(row["convergence_gate"], convergence_gate):
        if row["status"] == "done":
            return {
                "status": "done",
                "batch_id": batch_id,
                "cached": True,
                "computed_at": row["computed_at"],
                "compute_seconds": row["compute_seconds"],
                "result": json.loads(row["result_json"]),
            }
        if row["status"] == "computing":
            # check age — if too old, overwrite
            age = time.time() - time.mktime(time.strptime(row["started_at"], "%Y-%m-%d %H:%M:%S"))
            if age < JOB_MAX_SECONDS:
                return {
                    "status": "computing",
                    "batch_id": batch_id,
                    "cached": False,
                    "eta_seconds": max(0, int(ETA_SECONDS - age)),
                    "started_at": row["started_at"],
                }
            # stale — fall through to restart

    # resolve hpc_username
    if not hpc_username:
        hpc_username = _collect_hpc_username(batch_id) or ""
    if not hpc_username:
        return {"status": "error", "error": "hpc_username not found for batch"}

    run_names = _collect_run_names(batch_id)
    if not run_names:
        return {"status": "error", "error": f"no tasks found for batch_id={batch_id}"}

    # stage worker
    remote_worker = _ensure_worker_on_remote(hpc_username)

    remote_base = get_remote_base(hpc_username)
    remote_cache = f"{remote_base}/{CACHE_REMOTE_SUBDIR}"
    remote_output = f"{remote_cache}/{batch_id}.json"
    remote_status = f"{remote_cache}/{batch_id}.status"
    ssh_mkdir(remote_cache, username=hpc_username)

    # remove any stale status/output so polling doesn't mis-read old data
    # (ssh_run takes List[str], not a string — internally does [ssh_args] + remote_cmd)
    ssh_run(
        ["rm", "-f", remote_output, remote_status, f"{remote_status}.traceback"],
        username=hpc_username, timeout=10,
    )

    # Write a launcher shell script on remote, then invoke it. This avoids all
    # shell-quoting hazards with nohup/apptainer arg escaping.
    remote_log = f"{remote_cache}/{batch_id}.log"
    remote_pid = f"{remote_cache}/{batch_id}.pid"
    remote_launcher = f"{remote_cache}/{batch_id}.run.sh"
    runs_arg = ",".join(run_names)
    if is_iri_enabled():
        launcher_script = f"""#!/bin/bash
# auto-generated launcher for batch {batch_id}
apptainer exec --bind /w/ceph24,/work \\
  --env FITPACK={remote_base} \\
  --env LHAPDF_DATA_PATH=/work/JAM/ccocuzza/lhapdf/python3/sets \\
  --env PYTHONPATH={remote_base} \\
  {SIF_PATH} \\
  python3 {remote_worker} \\
    --batch-id {batch_id} \\
    --runs {runs_arg} \\
    --remote-base {remote_base} \\
    --output {remote_output} \\
    --status {remote_status} \\
    --convergence-gate {convergence_gate} \\
  > {remote_log} 2>&1
"""
    else:
        launcher_script = f"""#!/bin/bash
# auto-generated launcher for batch {batch_id}
nohup apptainer exec --bind /w/ceph24,/work \\
  --env FITPACK={remote_base} \\
  --env LHAPDF_DATA_PATH=/work/JAM/ccocuzza/lhapdf/python3/sets \\
  --env PYTHONPATH={remote_base} \\
  {SIF_PATH} \\
  python3 {remote_worker} \\
    --batch-id {batch_id} \\
    --runs {runs_arg} \\
    --remote-base {remote_base} \\
    --output {remote_output} \\
    --status {remote_status} \\
    --convergence-gate {convergence_gate} \\
  > {remote_log} 2>&1 &
echo $! > {remote_pid}
"""
    ssh_write_file(remote_launcher, launcher_script, username=hpc_username)

    if is_iri_enabled():
        # IRI submits the launcher as a managed job; no chmod / nohup needed.
        try:
            iri_job_id = iri_submit_script(remote_launcher, username=hpc_username)
            logger.info(f"[chi2_bf] submitted IRI job {iri_job_id} for {batch_id} as {hpc_username}")
        except Exception as e:
            logger.warning(f"[chi2_bf] IRI submit failed: {e}")
    else:
        ssh_run(["chmod", "+x", remote_launcher], username=hpc_username, timeout=5)
        # Fire via ssh; launcher backgrounds itself so ssh returns immediately
        logger.info(f"[chi2_bf] starting worker for {batch_id} as {hpc_username}")
        result = ssh_run(
            ["bash", remote_launcher],
            username=hpc_username, timeout=15,
        )
        if result.returncode != 0:
            logger.warning(f"[chi2_bf] launcher exited {result.returncode}: {result.stderr[:200]}")

    # record in DB
    with _db() as con:
        con.execute("""
            INSERT OR REPLACE INTO batch_summaries
              (batch_id, hpc_username, mode, status, convergence_gate,
               n_total, started_at, remote_output, remote_status)
            VALUES (?, ?, 'bootstrap_false', 'computing', ?,
                    ?, CURRENT_TIMESTAMP, ?, ?)
        """, (batch_id, hpc_username, convergence_gate,
              len(run_names), remote_output, remote_status))

    return {
        "status": "computing",
        "batch_id": batch_id,
        "cached": False,
        "eta_seconds": ETA_SECONDS,
        "n_replicas": len(run_names),
    }


# --- Public API: poll status / get result ---
_NOT_STARTED_RE = re.compile(
    r"failed to parse params from any slurm\.out \[(\d+) gate-passed runs: "
    r"not_started=(\d+), unparseable=(\d+)\]"
)


def _as_not_ready_if_all_not_started(err_msg: str, elapsed: float | None = None) -> Optional[Dict[str, Any]]:
    """Convert a worker error string into a not_ready result, if it is one.

    Worker raises RuntimeError "failed to parse params from any slurm.out
    [N gate-passed runs: not_started=N, unparseable=0]" when every replica
    that passed the convergence gate hasn't yet produced a parseable
    slurm.out. That's not an error — it's the SLURM queue state right after
    submission (jobs still PENDING, haven't been dispatched / started
    writing stdout yet). Returning it as {status:'error'} makes the LLM
    narrate "summary computation failed"; {status:'not_ready'} lets the
    caller say "still initializing, check back in a few min".

    Returns None if the message is a real error (unparseable>0 or shape
    doesn't match).
    """
    if not err_msg:
        return None
    m = _NOT_STARTED_RE.search(err_msg)
    if m is None:
        return None
    _total, n_not_started, n_unparseable = (int(x) for x in m.groups())
    if n_unparseable > 0:
        # A mix of not-started and unparseable is still a real worker
        # failure on the unparseable ones — don't mask it.
        return None
    out: Dict[str, Any] = {
        "status": "not_ready",
        "reason": "replicas_not_started",
        "n_not_started": n_not_started,
        "detail": (
            f"All {n_not_started} gate-passed replicas are still waiting on "
            "SLURM to dispatch them — slurm.out files haven't appeared yet. "
            "This is normal right after submission; retry in ~5–10 min once "
            "jobs transition from PENDING to RUNNING."
        ),
    }
    if elapsed is not None:
        out["elapsed_s"] = elapsed
    return out


def check_status(batch_id: str) -> Dict[str, Any]:
    """Poll the compute job. Returns one of:
        {status: 'done',      result: {...}, compute_seconds, ...}
        {status: 'computing', phase: str, elapsed_s: float}
        {status: 'not_ready', reason: 'replicas_not_started', detail: str}
        {status: 'error',     error: str}
        {status: 'not_found'}
    """
    ensure_schema()
    with _db() as con:
        row = con.execute(
            "SELECT * FROM batch_summaries WHERE batch_id = ?", (batch_id,)
        ).fetchone()
    if row is None:
        return {"status": "not_found", "batch_id": batch_id}

    if row["status"] == "done":
        return {
            "status": "done",
            "batch_id": batch_id,
            "cached": True,
            "computed_at": row["computed_at"],
            "compute_seconds": row["compute_seconds"],
            "result": json.loads(row["result_json"]),
        }
    if row["status"] == "error":
        err_msg = row["error_message"] or ""
        # Stale "not-started" error? The replicas may have started producing
        # output since this error was recorded. Clear the cached row and
        # return not_ready so the next start_compute can retry cleanly.
        nr = _as_not_ready_if_all_not_started(err_msg)
        if nr is not None:
            logger.info(
                f"[chi2_bf] clearing stale not_ready error for {batch_id}; "
                f"next call will retry"
            )
            with _db() as con:
                con.execute("DELETE FROM batch_summaries WHERE batch_id=?", (batch_id,))
            return {"batch_id": batch_id, **nr}
        return {"status": "error", "batch_id": batch_id, "error": err_msg}

    # status == 'computing' — check remote
    elapsed = time.time() - time.mktime(
        time.strptime(row["started_at"], "%Y-%m-%d %H:%M:%S"))
    hpc_username = row["hpc_username"]
    remote_output = row["remote_output"]
    remote_status = row["remote_status"]

    # check for completion: output JSON file exists
    try:
        if ssh_file_exists(remote_output, username=hpc_username):
            raw = ssh_read_file(remote_output, username=hpc_username)
            try:
                result = json.loads(raw)
            except Exception as e:
                _mark_error(batch_id, f"result JSON malformed: {e}")
                return {"status": "error", "batch_id": batch_id,
                        "error": f"result JSON malformed: {e}"}
            _mark_done(batch_id, result)
            return {"status": "done", "batch_id": batch_id,
                    "cached": False,
                    "compute_seconds": result.get("compute_seconds"),
                    "result": result}
    except Exception as e:
        logger.warning(f"[chi2_bf] ssh_file_exists failed: {e}")

    # still running — read phase marker
    phase = "unknown"
    try:
        phase_txt = ssh_read_file(remote_status, username=hpc_username)
        phase = (phase_txt or "").strip().splitlines()[-1] if phase_txt else "unknown"
    except Exception:
        phase = "unknown"

    # check for explicit error from worker
    if phase.startswith("error:"):
        raw = phase[len("error:"):].strip()
        # "All gate-passed replicas haven't started yet" is not a real
        # error — translate to not_ready so the LLM says "still
        # initializing" instead of "computation failed", and do NOT
        # persist via _mark_error so the next call can re-check once
        # SLURM actually dispatches the jobs.
        nr = _as_not_ready_if_all_not_started(raw, elapsed=elapsed)
        if nr is not None:
            # Clear any stale row so start_compute can fire a fresh worker.
            with _db() as con:
                con.execute("DELETE FROM batch_summaries WHERE batch_id=?", (batch_id,))
            return {"batch_id": batch_id, **nr}
        _mark_error(batch_id, raw)
        return {"status": "error", "batch_id": batch_id, "error": phase}

    # timeout check
    if elapsed > JOB_MAX_SECONDS:
        _mark_error(batch_id, f"job timeout ({elapsed:.0f}s > {JOB_MAX_SECONDS}s)")
        return {"status": "error", "batch_id": batch_id,
                "error": f"job timeout {elapsed:.0f}s"}

    return {
        "status": "computing",
        "batch_id": batch_id,
        "phase": phase,
        "elapsed_s": elapsed,
        "eta_seconds": max(0, int(ETA_SECONDS - elapsed)),
    }


def _mark_done(batch_id: str, result: Dict[str, Any]) -> None:
    with _db() as con:
        con.execute("""
            UPDATE batch_summaries
               SET status='done',
                   computed_at=CURRENT_TIMESTAMP,
                   compute_seconds=?,
                   n_survivors=?,
                   result_json=?
             WHERE batch_id=?
        """, (result.get("compute_seconds"),
              result.get("n_survivors"),
              json.dumps(result),
              batch_id))


def _mark_error(batch_id: str, msg: str) -> None:
    with _db() as con:
        con.execute("""
            UPDATE batch_summaries
               SET status='error',
                   error_message=?,
                   computed_at=CURRENT_TIMESTAMP
             WHERE batch_id=?
        """, (msg, batch_id))
