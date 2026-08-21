#!/usr/bin/env python3
"""
Background status monitor for SLURM jobs.

This module provides a daemon thread that continuously monitors SLURM job statuses
and synchronizes them with the database. It runs independently of MCP tools.
"""

import logging
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Set

from ssh_runner import ssh_run
from iri_backend import (
    iri_get_slurm_code,
    iri_get_task_status,
    iri_list_jobs,
    is_iri_enabled,
)

logger = logging.getLogger(__name__)

# Database path (local — tracking ledger only)
import os as _os
DB_PATH = _os.environ.get("DB_PATH", str(Path(__file__).parent.resolve() / "hpc_tasks.db"))

# Monitor configuration
POLL_INTERVAL_SECONDS = float(_os.environ.get("FEMTOMIND_POLL_INTERVAL_SECONDS", "20"))
# Each poll is an SSH round trip to the cluster. SLURM state does not move
# meaningfully in 2 s, and polling that fast made sweeps overlap.
_monitor_thread = None
_monitor_active = False


def get_db_connection():
    """Get database connection.

    This sweep holds its connection across SSH round trips, so it needs the
    same WAL + long busy timeout as everyone else writing this ledger.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return conn


def _get_any_username() -> str:
    """Get any configured HPC username from the database or SSH sockets."""
    import glob
    # Try from user_hpc_config table
    try:
        with get_db_connection() as conn:
            row = conn.execute("SELECT hpc_username FROM user_hpc_config LIMIT 1").fetchone()
            if row and row[0]:
                return row[0]
    except Exception:
        pass
    # Try from SSH socket filenames
    socket_dir = _os.path.expanduser("~/.ssh/controlmasters")
    sockets = glob.glob(_os.path.join(socket_dir, "jlab-ifarm-*"))
    for s in sockets:
        try:
            name = _os.path.basename(s)
            user = name.rsplit("-", 1)[-1].split("@")[0]
            if user:
                return user
        except Exception:
            pass
    return ""


def query_slurm_statuses() -> Dict[str, str]:
    """
    Query current SLURM-shape job statuses (active jobs only).

    Returns a dict of job_id -> SLURM status code (R / PD / CG / ...). Under
    the IRI transport this calls `iri_list_jobs()` directly instead of paying
    a round-trip through the squeue translation in `_iri_run`.
    """
    if is_iri_enabled():
        statuses: Dict[str, str] = {}
        try:
            for job in iri_list_jobs():
                jid = str(job.get("id", ""))
                if not jid:
                    continue
                code = iri_get_slurm_code(jid)
                if code:
                    statuses[jid] = code
        except Exception as e:
            logger.warning(f"IRI list_jobs failed: {e}")
        return statuses

    try:
        username = _get_any_username()
        result = ssh_run(
            ["squeue", "-h", "-o", "%i %t"],
            timeout=10,
            username=username or None,
        )

        if result.returncode != 0:
            logger.warning(f"squeue failed: {result.stderr}")
            return {}

        statuses = {}
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                job_id = parts[0]
                status = parts[1]
                statuses[job_id] = status

        return statuses

    except subprocess.TimeoutExpired:
        logger.warning("squeue query timed out")
        return {}
    except ConnectionError:
        logger.warning("SSH ControlMaster not connected")
        return {}
    except Exception as e:
        logger.error(f"Error querying squeue: {e}")
        return {}


def normalize_slurm_status(slurm_status: str) -> str:
    """
    Normalize SLURM status codes to our database status values.
    
    Args:
        slurm_status: Raw SLURM status (R, PD, CG, etc.)
    
    Returns:
        Normalized status string
    """
    status_map = {
        "PENDING": "pending",
        "PD": "pending",
        "RUNNING": "running",
        "R": "running",
        "COMPLETED": "completed",
        "CD": "completed",
        "COMPLETING": "running",
        "CG": "running",  # Treat COMPLETING as still running
        "FAILED": "failed",
        "F": "failed",
        "TIMEOUT": "timeout",
        "TO": "timeout",
        "CANCELLED": "cancelled",
        "CA": "cancelled",
        "NODE_FAIL": "failed",
        "NF": "failed",
        "PREEMPTED": "cancelled",
        "PR": "cancelled",
    }
    
    return status_map.get(slurm_status.upper(), slurm_status.lower())


def _sync_job_statuses_inner():
    """
    Synchronize all active job statuses with SLURM.
    This is the core monitoring function.
    """
    try:
        # Get current SLURM statuses
        slurm_statuses = query_slurm_statuses()
        
        # Get all tasks with SLURM job IDs from database
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Find all tasks that have been submitted to SLURM
            cursor.execute("""
                SELECT path, slurm_job_id, status
                FROM tasks
                WHERE slurm_job_id IS NOT NULL
                AND status NOT IN ('completed', 'failed', 'timeout', 'cancelled')
            """)
            
            tasks = cursor.fetchall()
            
            updates = []
            for task in tasks:
                job_id = task['slurm_job_id']
                current_db_status = task['status']
                path = task['path']
                
                if job_id in slurm_statuses:
                    # Job is still in SLURM queue
                    slurm_status = slurm_statuses[job_id]
                    normalized_status = normalize_slurm_status(slurm_status)
                    
                    if normalized_status != current_db_status:
                        updates.append((normalized_status, path))
                        logger.info(f"Status change: {path} ({job_id}): {current_db_status} -> {normalized_status}")
                elif is_iri_enabled():
                    # Job no longer active in IRI; ask for its terminal state directly.
                    # iri_get_task_status returns already-normalized values
                    # (pending/running/completed/failed/cancelled/unknown).
                    try:
                        normalized_status = iri_get_task_status(job_id)
                        if normalized_status not in ("", "unknown") and normalized_status != current_db_status:
                            updates.append((normalized_status, path))
                            logger.info(f"Status change (IRI historical): {path} ({job_id}): {current_db_status} -> {normalized_status}")
                        else:
                            logger.debug(f"IRI returned {normalized_status!r} for {job_id}, keeping status as {current_db_status}")
                    except Exception as e:
                        logger.debug(f"IRI get_task_status failed for {job_id}: {e}, keeping status as {current_db_status}")
                else:
                    # Job no longer in queue - check if it completed or failed
                    # Use sacct to check historical status
                    try:
                        sacct_result = ssh_run(
                            ["sacct", "-j", job_id, "-n", "-o", "State", "--parsable2"],
                            timeout=10,
                            username=_get_any_username() or None,
                        )

                        if sacct_result.returncode == 0 and sacct_result.stdout.strip():
                            # Filter out shell warnings
                            lines = [l.strip() for l in sacct_result.stdout.strip().split('\n') if l.strip() and not l.startswith('/')]
                            if lines:
                                historical_status = lines[0]
                                normalized_status = normalize_slurm_status(historical_status)

                                if normalized_status != current_db_status:
                                    updates.append((normalized_status, path))
                                    logger.info(f"Status change (historical): {path} ({job_id}): {current_db_status} -> {normalized_status}")
                            else:
                                logger.debug(f"sacct returned no valid data for {job_id}, keeping status as {current_db_status}")
                        else:
                            # sacct failed or no data — do NOT assume completed
                            logger.debug(f"sacct failed for {job_id}, keeping status as {current_db_status}")
                    except Exception as e:
                        # If sacct fails, do NOT assume completed — keep current status
                        logger.debug(f"sacct exception for {job_id}: {e}, keeping status as {current_db_status}")
            
            # Batch update changed statuses or just refresh timestamp
            from datetime import datetime, timezone, timedelta
            eastern_time = datetime.now(timezone(timedelta(hours=-5))).strftime('%Y-%m-%d %H:%M:%S')
            
            if updates:
                cursor.executemany(f"""
                    UPDATE tasks
                    SET status = ?, updated_at = '{eastern_time}'
                    WHERE path = ?
                """, updates)
                conn.commit()
                logger.info(f"Updated {len(updates)} task statuses to new states")
            
            # Also occasionally update the 'updated_at' for all active tasks anyway
            # so the user knows the monitor is alive
            cursor.execute(f"""
                UPDATE tasks
                SET updated_at = '{eastern_time}'
                WHERE slurm_job_id IS NOT NULL
                AND status IN ('running', 'pending', 'submitted')
            """)
            conn.commit()
    
    except Exception as e:
        logger.error(f"Error in status sync: {e}")


def monitor_loop():
    """
    Main monitoring loop that runs in background thread.
    """
    global _monitor_active
    
    logger.info("Status monitor started")
    
    while _monitor_active:
        try:
            sync_job_statuses()
        except Exception as e:
            logger.error(f"Error in monitor loop: {e}")
        
        # Sleep for configured interval
        time.sleep(POLL_INTERVAL_SECONDS)
    
    logger.info("Status monitor stopped")


def start_monitor():
    """
    Start the background status monitor thread.
    """
    global _monitor_thread, _monitor_active
    
    if _monitor_thread and _monitor_thread.is_alive():
        logger.warning("Monitor already running")
        return
    
    _monitor_active = True
    _monitor_thread = threading.Thread(target=monitor_loop, daemon=True, name="StatusMonitor")
    _monitor_thread.start()
    logger.info("Background status monitor started (poll interval: %ss)" % POLL_INTERVAL_SECONDS)


def stop_monitor():
    """
    Stop the background status monitor thread.
    """
    global _monitor_active
    
    _monitor_active = False
    if _monitor_thread:
        _monitor_thread.join(timeout=5)
    logger.info("Background status monitor stopped")


def is_monitor_running() -> bool:
    """
    Check if the monitor is currently running.
    """
    global _monitor_thread
    return _monitor_thread is not None and _monitor_thread.is_alive()


# ---------------------------------------------------------------------------
# This module and status_sync each carry their own SLURM sweep, and both write
# the same ledger while an SSH call is outstanding. They share one lock so a
# user-facing request never waits out the busy timeout behind a background poll.
# ---------------------------------------------------------------------------

def sync_job_statuses():
    """Run the monitor's sweep unless another sweep already holds the lock."""
    try:
        from status_sync import _SWEEP_LOCK, SWEEP_WAIT_SECONDS
    except Exception:
        return _sync_job_statuses_inner()

    if not _SWEEP_LOCK.acquire(timeout=SWEEP_WAIT_SECONDS):
        logger.debug("another status sweep is in flight; skipping this poll")
        return None
    try:
        return _sync_job_statuses_inner()
    finally:
        _SWEEP_LOCK.release()
