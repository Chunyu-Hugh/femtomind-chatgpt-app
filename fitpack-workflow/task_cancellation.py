#!/usr/bin/env python3
"""
Task cancellation utilities for HPC tasks.
Handles stopping SLURM jobs and updating database.
"""
import json
import sqlite3
import logging
from pathlib import Path
from typing import Dict, Any, List

from ssh_runner import ssh_run, ssh_read_file, ssh_file_exists
from iri_backend import IriApiError, get_iri_client, is_iri_enabled

logger = logging.getLogger(__name__)
DB_PATH = str(Path(__file__).parent.resolve() / "hpc_tasks.db")

def get_db_connection():
    """Get database connection."""
    return sqlite3.connect(DB_PATH)

def cancel_task(task_path: str) -> Dict[str, Any]:
    """
    Cancel a running SLURM task.
    
    Args:
        task_path: Path to the task directory
        
    Returns:
        Result dictionary
    """
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Get task info
            cursor.execute("""
                SELECT slurm_job_id, status 
                FROM tasks 
                WHERE path = ?
            """, (task_path,))
            task = cursor.fetchone()
            
            if not task:
                return {"error": f"Task not found: {task_path}"}
            
            job_id = task['slurm_job_id']
            status = task['status']
            
            if not job_id:
                return {"error": "Task has no SLURM job ID"}
            
            # Skip if already completed/failed
            if status in ['completed', 'failed', 'cancelled']:
                return {
                    "status": "skipped",
                    "message": f"Task already {status}",
                    "task_path": task_path
                }
            
            # Cancel SLURM job (skip simulated jobs)
            if job_id.startswith('sim_'):
                cancel_result = "simulated"
                logger.info(f"Simulated cancellation for job {job_id}")
            elif is_iri_enabled():
                try:
                    get_iri_client().cancel_job(job_id)
                    cancel_result = "cancelled"
                except IriApiError as e:
                    msg = str(e).lower()
                    if "404" in msg or "not found" in msg or "already" in msg:
                        cancel_result = "already_finished"
                    else:
                        return {"error": f"Failed to cancel via IRI: {e}"}
                except Exception as e:
                    return {"error": f"Error cancelling IRI job: {e}"}
            else:
                try:
                    result = ssh_run(["scancel", job_id], timeout=10)

                    if result.returncode == 0:
                        cancel_result = "cancelled"
                    else:
                        if "Invalid job id" in result.stderr:
                            cancel_result = "already_finished"
                        else:
                            return {"error": f"Failed to cancel: {result.stderr}"}
                except Exception as e:
                    return {"error": f"Error cancelling job: {e}"}
            
            # Update database
            cursor.execute("""
                UPDATE tasks 
                SET status = 'cancelled',
                    completed_at = CURRENT_TIMESTAMP
                WHERE path = ?
            """, (task_path,))
            
            conn.commit()
            
            return {
                "status": "success",
                "action": cancel_result,
                "task_path": task_path,
                "job_id": job_id,
                "message": f"Task cancelled successfully"
            }
            
    except Exception as e:
        logger.error(f"Failed to cancel task: {e}")
        return {"error": str(e)}

def cancel_tasks_by_batch(
    batch_id: str,
    exclude_paths: List[str] = None,
    chi2_npts_threshold: float = None
) -> Dict[str, Any]:
    """
    Cancel multiple tasks in a batch based on criteria.
    
    Args:
        batch_id: Batch identifier
        exclude_paths: List of task paths to NOT cancel (e.g., best performing)
        chi2_npts_threshold: Cancel tasks with chi2/npts above this threshold
        
    Returns:
        Summary of cancellations
    """
    exclude_paths = exclude_paths or []
    results = {
        "batch_id": batch_id,
        "cancelled_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
        "tasks": []
    }
    
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Get all running/pending tasks in batch
            query = """
                SELECT path, slurm_job_id, status 
                FROM tasks 
                WHERE batch_id = ? 
                AND status IN ('submitted', 'pending', 'running')
            """
            
            cursor.execute(query, (batch_id,))
            tasks = cursor.fetchall()
            
            for task in tasks:
                task_path = task['path']
                
                # Skip excluded tasks (e.g., best performing)
                if task_path in exclude_paths:
                    results["skipped_count"] += 1
                    results["tasks"].append({
                        "path": task_path,
                        "action": "skipped",
                        "reason": "excluded"
                    })
                    continue
                
                # Check chi2/npts threshold if provided
                if chi2_npts_threshold is not None:
                    # Read current chi2/npts from progress on remote HPC
                    progress_path = f"{task_path}/progress.json"
                    if ssh_file_exists(progress_path):
                        try:
                            content = ssh_read_file(progress_path)
                            progress = json.loads(content)
                            
                            if progress:
                                last = progress[-1]
                                total_chi2 = 0
                                total_npts = 0
                                
                                if "datasets" in last:
                                    for ds in last["datasets"].values():
                                        total_chi2 += ds.get("chi2", 0)
                                        total_npts += ds.get("npts", 0)
                                else:
                                    total_chi2 = last.get("chi2", 0)
                                    total_npts = last.get("npts", 1)
                                
                                current_chi2_npts = total_chi2 / total_npts if total_npts > 0 else float('inf')
                                
                                # Skip if better than threshold
                                if current_chi2_npts <= chi2_npts_threshold:
                                    results["skipped_count"] += 1
                                    results["tasks"].append({
                                        "path": task_path,
                                        "action": "skipped",
                                        "reason": f"chi2/npts ({current_chi2_npts:.2f}) <= threshold ({chi2_npts_threshold:.2f})"
                                    })
                                    continue
                        except Exception as e:
                            logger.warning(f"Could not read progress for {task_path}: {e}")
                
                # Cancel this task
                cancel_result = cancel_task(task_path)
                
                if cancel_result.get("status") == "success":
                    results["cancelled_count"] += 1
                    results["tasks"].append({
                        "path": task_path,
                        "action": "cancelled",
                        "job_id": cancel_result.get("job_id")
                    })
                else:
                    results["failed_count"] += 1
                    results["tasks"].append({
                        "path": task_path,
                        "action": "failed",
                        "error": cancel_result.get("error", "Unknown error")
                    })
    
    except Exception as e:
        logger.error(f"Batch cancellation failed: {e}")
        return {"error": str(e)}
    
    return results
