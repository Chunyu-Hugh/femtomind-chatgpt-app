#!/usr/bin/env python3
"""
Status synchronization utilities for HPC tasks.
Handles querying SLURM job statuses and updating the database.
"""
import os
import time
import threading
import sqlite3
import logging
from typing import Dict, Any
from pathlib import Path

from ssh_runner import ssh_run
from iri_backend import is_iri_enabled, iri_get_task_status

logger = logging.getLogger(__name__)
DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).parent.resolve() / "hpc_tasks.db"))

# Task status vocabulary — must stay in sync with
# open-webui/routers/hpc.py BATCH_TASK_TERMINAL_STATES / BATCH_TASK_PENDING_STATES.
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "error"})
_PENDING_STATES = frozenset({"created", "pending", "submitted", "running"})


def get_db_connection():
    """Get database connection.

    Same pragmas as server._get_db: WAL plus a long busy timeout, so a sweep
    that overlaps an API write waits instead of raising "database is locked".
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return conn


def _emit_batch_event_if_done(cursor, batch_id: str) -> str:
    """DB-level batch-completion signal. Idempotent via UNIQUE(batch_id, status).

    Counterpart to open-webui/routers/hpc.py _emit_batch_event_if_done — same
    logic, inlined here so the MCP server can emit without importing from the
    web backend. See docs/DEV_PLAN.md §3.
    """
    import json as _json
    try:
        cursor.execute(
            "SELECT chat_id, status FROM tasks WHERE batch_id = ?", (batch_id,)
        )
        rows = cursor.fetchall()
        if not rows:
            return ""
        chat_id = rows[0][0] or ""
        statuses = [r[1] for r in rows]
        if any(s in _PENDING_STATES for s in statuses):
            return ""
        n_success = sum(1 for s in statuses if s == "completed")
        n_total = len(statuses)
        if n_success == n_total:
            overall = "done"
        elif n_success == 0:
            overall = "failed"
        else:
            overall = "partial"
        cursor.execute(
            "INSERT OR IGNORE INTO batch_events (chat_id, batch_id, reaction, status, payload) "
            "VALUES (?, ?, NULL, ?, ?)",
            (chat_id, batch_id, overall,
             _json.dumps({"n_success": n_success, "n_total": n_total})),
        )
        return overall
    except Exception as e:
        logger.warning(f"_emit_batch_event_if_done({batch_id}) failed: {e}")
        return ""

def query_slurm_job_status(job_id: str, username: str = "") -> str:
    """
    Query the current status of a SLURM job.

    Args:
        job_id:   SLURM job ID.
        username: HPC username whose account owns this job. Required — if
                  empty the ssh_runner will raise.

    Returns:
        Status string: 'pending', 'running', 'completed', 'failed', 'unknown'
    """
    try:
        if is_iri_enabled():
            return iri_get_task_status(job_id)

        # First try squeue (for active jobs)
        cmd = ["squeue", "--job", job_id, "--format=%t", "--noheader"]
        result = ssh_run(cmd, timeout=10, username=username or None)
        
        if result.stdout.strip():
            # Filter out shell warning lines (e.g. "No such file or directory")
            lines = [l.strip() for l in result.stdout.strip().split('\n') if l.strip() and not l.startswith('/')]
            if lines:
                state_code = lines[-1].strip()  # Last non-empty, non-error line
                state_mapping = {
                    'PD': 'pending',
                    'R': 'running',
                    'CG': 'completing',
                    'S': 'suspended',
                    'CF': 'pending',
                }
                if state_code in state_mapping:
                    return state_mapping[state_code]
        
        # If not in queue, check sacct (for completed jobs)
        cmd = ["sacct", "-j", job_id, "--format=State", "--noheader", "-n"]
        result = ssh_run(cmd, timeout=10, username=username or None)
        
        if result.returncode == 0 and result.stdout.strip():
            state = result.stdout.strip().split()[0].upper()
            if 'COMPLETED' in state:
                return 'completed'
            elif 'FAILED' in state or 'CANCELLED' in state or 'TIMEOUT' in state:
                return 'failed'
            elif 'RUNNING' in state:
                return 'running'
            elif 'PENDING' in state:
                return 'pending'
            else:
                logger.warning(f"Unknown sacct state for job {job_id}: {state}")
                return 'unknown'  # Do NOT assume completed

        # If job not found in either, might be too old or simulated
        return 'unknown'
        
    except Exception as e:
        if "Timeout" in str(type(e).__name__):
            logger.warning(f"Timeout querying status for job {job_id}")
        else:
            logger.error(f"Error querying job status: {e}")
        return 'unknown'

def query_slurm_statuses(job_ids, username: str = ""):
    """Resolve many jobs in two SSH calls instead of two per job.

    The per-job helper was fine for ten replicas and quietly fatal for a
    thousand: one squeue plus one sacct round-trip each, run in a loop, is
    minutes of latency, and every caller waiting on the sync timed out. squeue
    reports the whole queue in one shot and one sacct covers everything that
    has already left it.
    """
    out = {}
    ids = [str(j) for j in job_ids if j and not str(j).startswith("sim_")]
    if not ids:
        return out

    state_mapping = {"PD": "pending", "R": "running", "CG": "completing",
                     "S": "suspended", "CF": "pending"}
    try:
        # `--format=%i %t` makes squeue reject the whole invocation
        # ("Unrecognized option: %t") because the space splits it into two
        # argv entries. Every sweep therefore fell through to the sacct
        # branch for ALL job ids, which is why a plain campaign listing
        # could take 30s. `-o` with a delimiter keeps it one argument.
        r = ssh_run(["squeue", "-u", username, "-o", "%i|%t", "--noheader"],
                    timeout=30, username=username or None)
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line or line.startswith("/"):
                continue
            parts = line.split("|")
            if len(parts) >= 2 and parts[1].strip() in state_mapping:
                out[parts[0].strip()] = state_mapping[parts[1].strip()]
    except Exception as e:
        logger.warning("batch squeue failed: %s" % e)

    missing = [j for j in ids if j not in out]
    if missing:
        try:
            r = ssh_run(["sacct", "-j", ",".join(missing), "--format=JobID,State",
                         "--noheader", "-n", "--parsable2"],
                        timeout=60, username=username or None)
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if not line or "|" not in line:
                    continue
                jid, _, state = line.partition("|")
                jid = jid.split(".")[0]
                if jid not in missing:
                    continue
                st = state.strip().upper()
                if "COMPLETED" in st:
                    out[jid] = "completed"
                elif any(k in st for k in ("FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_ME", "NODE_FAIL")):
                    out[jid] = "failed"
                elif "RUNNING" in st:
                    out[jid] = "running"
                elif "PENDING" in st:
                    out[jid] = "pending"
        except Exception as e:
            logger.warning("batch sacct failed: %s" % e)
    return out


def _sync_task_statuses_inner(batch_id: str = "") -> Dict[str, Any]:
    """
    Synchronize task statuses by querying SLURM and updating the database.
    
    Args:
        batch_id: Optional batch ID to sync. If empty, syncs all active tasks.
        
    Returns:
        Summary of status updates
    """
    results = {
        "updated_count": 0,
        "failed_count": 0,
        "tasks": []
    }
    
    # Track batch_ids whose tasks flipped to a terminal state in this sweep
    # so we can emit batch-completion events exactly once per sync pass.
    touched_batches: set[str] = set()

    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Query tasks that need status sync. Pull hpc_username/chat_id so
            # each job's SLURM queries are scoped to the correct HPC account.
            query = """
                SELECT path, slurm_job_id, status, batch_id, hpc_username, chat_id
                FROM tasks
                WHERE status IN ('submitted', 'pending', 'running')
                AND slurm_job_id IS NOT NULL
            """
            params = []

            if batch_id:
                query += " AND batch_id = ?"
                params.append(batch_id)

            cursor.execute(query, params)
            tasks = cursor.fetchall()

            # One squeue + one sacct per cluster account, before the loop.
            bulk = {}
            by_user = {}
            for _t in tasks:
                _u = _t["hpc_username"] or ""
                if _u and _t["slurm_job_id"]:
                    by_user.setdefault(_u, []).append(str(_t["slurm_job_id"]))
            for _u, _ids in by_user.items():
                try:
                    bulk.update(query_slurm_statuses(_ids, username=_u))
                except Exception as _e:
                    logger.warning("bulk status sweep failed for %s: %s" % (_u, _e))

            # Cache per-chat lookups so we don't hit the DB repeatedly.
            from server import _get_hpc_username as _lookup_chat_user
            chat_user_cache: Dict[str, str] = {}

            for task in tasks:
                path = task['path']
                job_id = task['slurm_job_id']
                old_status = task['status']

                # Skip simulated jobs (they don't exist in SLURM)
                if job_id and job_id.startswith('sim_'):
                    continue

                # Resolve the task's HPC user (per-row, supporting multi-tenant).
                task_user = task['hpc_username'] or ''
                if not task_user:
                    row_chat = task['chat_id'] or ''
                    if row_chat:
                        if row_chat not in chat_user_cache:
                            chat_user_cache[row_chat] = _lookup_chat_user(row_chat) or ''
                        task_user = chat_user_cache[row_chat]
                if not task_user:
                    logger.warning(f"Cannot resolve hpc_username for task {path}; skipping")
                    continue

                # Resolved in bulk before the loop; falls back to the
                # per-job query only for anything the sweep missed.
                new_status = bulk.get(str(job_id)) or query_slurm_job_status(job_id, username=task_user)

                # Update if status changed
                if new_status != old_status and new_status != 'unknown':
                    update_query = "UPDATE tasks SET status = ?"
                    update_params = [new_status]

                    # Set timestamps based on status transitions
                    if new_status == 'running' and old_status in ['submitted', 'pending']:
                        update_query += ", started_at = CURRENT_TIMESTAMP"
                    elif new_status in ['completed', 'failed']:
                        update_query += ", completed_at = CURRENT_TIMESTAMP"

                    update_query += " WHERE path = ?"
                    update_params.append(path)

                    cursor.execute(update_query, update_params)

                    results["updated_count"] += 1
                    results["tasks"].append({
                        "path": path,
                        "job_id": job_id,
                        "old_status": old_status,
                        "new_status": new_status
                    })

                    logger.info(f"Updated task {path}: {old_status} -> {new_status}")

                    # Record job history when task completes. Use the task's
                    # own batch_id, not the sweep's filter argument: a full
                    # sweep passes no batch_id, and recording "" broke every
                    # later WHERE batch_id=? join (drop/redo previews, the
                    # plot's job_history join).
                    if new_status == 'completed':
                        try:
                            from server import _record_job_history
                            _record_job_history(
                                task_path=path,
                                batch_id=task.get('batch_id') or "",
                                slurm_job_id=job_id,
                                username=task_user,
                            )
                        except Exception as hist_err:
                            logger.warning(f"Could not record job history for {path}: {hist_err}")

                    # If this task reached a terminal state, remember its batch_id
                    # so we can try emitting a batch-completion event after the sweep.
                    if new_status in _TERMINAL_STATES:
                        tbid = task['batch_id'] if 'batch_id' in task.keys() else ''
                        if tbid:
                            touched_batches.add(tbid)

            # After all task updates are written, check each touched batch once.
            # The helper is idempotent (UNIQUE(batch_id, status)), so repeated
            # sweeps that see the same terminal state just no-op.
            for tbid in touched_batches:
                event_status = _emit_batch_event_if_done(cursor, tbid)
                if event_status:
                    logger.info(f"batch_events: {tbid} → {event_status}")

            conn.commit()
            
    except Exception as e:
        logger.error(f"Status sync failed: {e}")
        results["failed_count"] += 1
        return {"error": str(e)}
    
    return results

def get_task_status_info(task_path: str) -> Dict[str, Any]:
    """
    Get the current status of a specific task.

    The ssh calls use the username recorded on the task row.

    Args:
        task_path: Path to the task directory

    Returns:
        Task status information
    """
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("""
                SELECT status, slurm_job_id, created_at, submitted_at,
                       started_at, completed_at, hpc_username, chat_id
                FROM tasks
                WHERE path = ?
            """, (task_path,))

            task = cursor.fetchone()

            if not task:
                return {"error": "Task not found"}

            username = task['hpc_username'] or ''
            if not username:
                from server import _get_hpc_username as _lookup_chat_user
                username = _lookup_chat_user(task['chat_id'] or '') or ''

            # Query live status if job is active
            live_status = None
            if task['slurm_job_id'] and task['status'] in ['submitted', 'pending', 'running']:
                live_status = query_slurm_job_status(task['slurm_job_id'], username=username)

            result = {
                "path": task_path,
                "status": task['status'],
                "live_status": live_status,
                "slurm_job_id": task['slurm_job_id'],
                "created_at": task['created_at'],
                "submitted_at": task['submitted_at'],
                "started_at": task['started_at'],
                "completed_at": task['completed_at']
            }

            # Read progress.json from HPC to get chi2 data
            try:
                from ssh_runner import ssh_read_file
                import json as _json
                progress_path = f"{task_path}/progress.json"
                progress_content = ssh_read_file(progress_path, username=username or None)
                progress_data = _json.loads(progress_content)
                if isinstance(progress_data, list) and len(progress_data) > 0:
                    result["progress"] = progress_data
                    last = progress_data[-1]
                    chi2 = last.get("chi2_npts") or last.get("chi2/npts")
                    npts = last.get("npts")
                    if chi2 is None and last.get("chi2") and npts:
                        chi2 = last["chi2"] / npts
                    result["final_chi2_npts"] = round(chi2, 4) if chi2 else None
                    result["final_chi2"] = last.get("chi2")
                    result["final_npts"] = npts
                    result["final_iteration"] = last.get("iter", last.get("iteration", len(progress_data)))
                    result["total_iterations"] = len(progress_data)
                elif isinstance(progress_data, dict):
                    result["progress_summary"] = progress_data
            except Exception as pe:
                result["progress_error"] = str(pe)

            return result
            
    except Exception as e:
        logger.error(f"Error getting task status: {e}")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Sweep serialisation
#
# The background monitor and every user-facing list_chat_tasks call both want a
# fresh SLURM sweep. Left unguarded they overlap: each sweep holds a write
# connection across an SSH round trip, so the second one blocks on the busy
# timeout and a user request that should take 0.3 s takes 30 s. One sweep at a
# time, plus a short freshness window so a request arriving right after a sweep
# reuses its result instead of starting another.
# ---------------------------------------------------------------------------

SWEEP_FRESH_SECONDS = float(os.environ.get("FEMTOMIND_SWEEP_FRESH_SECONDS", "10"))
SWEEP_WAIT_SECONDS = float(os.environ.get("FEMTOMIND_SWEEP_WAIT_SECONDS", "8"))

_SWEEP_LOCK = threading.Lock()
_LAST_SWEEP = {"at": 0.0, "key": None, "result": None}


def sync_task_statuses(batch_id: str = "") -> Dict[str, Any]:
    """Serialised, freshness-cached wrapper around the SLURM status sweep.

    Returns the previous sweep's result while it is younger than
    SWEEP_FRESH_SECONDS, and gives up rather than queueing when another sweep
    is already in flight -- the caller then reads whatever that sweep writes.
    """
    key = batch_id or ""
    now = time.monotonic()

    cached = _LAST_SWEEP.get("result")
    if (cached is not None and _LAST_SWEEP.get("key") == key
            and now - _LAST_SWEEP.get("at", 0.0) < SWEEP_FRESH_SECONDS):
        return dict(cached, cached=True)

    if not _SWEEP_LOCK.acquire(timeout=SWEEP_WAIT_SECONDS):
        logger.warning("status sweep already in flight; serving without a fresh sweep")
        return {"updated_count": 0, "failed_count": 0, "tasks": [],
                "skipped": "sweep_in_flight"}

    try:
        now = time.monotonic()
        cached = _LAST_SWEEP.get("result")
        if (cached is not None and _LAST_SWEEP.get("key") == key
                and now - _LAST_SWEEP.get("at", 0.0) < SWEEP_FRESH_SECONDS):
            return dict(cached, cached=True)

        result = _sync_task_statuses_inner(batch_id)
        _LAST_SWEEP["at"] = time.monotonic()
        _LAST_SWEEP["key"] = key
        _LAST_SWEEP["result"] = result
        return result
    finally:
        _SWEEP_LOCK.release()
