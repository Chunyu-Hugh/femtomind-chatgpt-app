#!/usr/bin/env python3
"""
Result saving utilities for HPC tasks.
Handles saving, retrieving, and restoring best task parameters.
"""
import math
import sqlite3
import json
import yaml
import logging
from typing import Dict, Any, List
from pathlib import Path

from ssh_runner import ssh_read_file, ssh_write_file, ssh_file_exists, get_remote_base

logger = logging.getLogger(__name__)
DB_PATH = str(Path(__file__).parent.resolve() / "hpc_tasks.db")


def _remote_default_config() -> str:
    """Resolve at call time so it tracks the active HPC user."""
    return f"{get_remote_base()}/inputs/default.yaml"

def get_db_connection():
    """Get database connection."""
    return sqlite3.connect(DB_PATH, timeout=30)

def save_task_result(
    task_path: str,
    user_note: str = "",
    mark_as_best: bool = False,
    tags: List[str] = None
) -> Dict[str, Any]:
    """
    Save a task's parameters and results for future reference.
    
    Args:
        task_path: Path to the task directory
        user_note: Optional user comment
        mark_as_best: Whether to mark as the best result in this batch
        tags: List of tags for categorization
    
    Returns:
        Result dictionary with saved ID and metrics
    """
    try:
        tags = tags or []
        
        # 1. Get task information from database
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT batch_id, chat_id, path 
                FROM tasks 
                WHERE path = ?
            """, (task_path,))
            task = cursor.fetchone()
            
            if not task:
                return {"error": f"Task not found: {task_path}"}
            
            batch_id = task['batch_id']
            chat_id = task['chat_id']
            
            # 2. Read final results from progress.json on HPC
            progress_path = f"{task_path}/progress.json"
            if not ssh_file_exists(progress_path):
                return {"error": f"No progress.json found in {task_path}"}

            content = ssh_read_file(progress_path)
            progress_data = json.loads(content)
            
            if not progress_data:
                return {"error": "Progress data is empty"}
            
            last_iter = progress_data[-1]
            
            # Calculate final metrics
            total_chi2 = 0
            total_npts = 0
            
            if last_iter.get("datasets"):
                for ds in last_iter["datasets"].values():
                    total_chi2 += ds.get("chi2", 0)
                    total_npts += ds.get("npts", 0)
            else:
                total_chi2 = last_iter.get("chi2", 0)
                total_npts = last_iter.get("npts", 1)
            
            final_chi2 = total_chi2
            final_chi2_npts = total_chi2 / total_npts if total_npts > 0 else 0
            final_iteration = last_iter.get("iter", len(progress_data))
            
            # 3. Read parameter configuration from HPC
            params_path = f"{task_path}/parameters.yaml"
            if not ssh_file_exists(params_path):
                return {"error": f"No parameters.yaml found in {task_path}"}

            content = ssh_read_file(params_path)
            params = yaml.safe_load(content)
            
            # 4. If marking as best, clear other best marks in the same batch
            if mark_as_best:
                cursor.execute("""
                    UPDATE saved_results 
                    SET is_best = 0 
                    WHERE batch_id = ?
                """, (batch_id,))
                logger.info(f"Cleared best marks for batch {batch_id}")
            
            # 5. Check if this task is already saved
            cursor.execute("""
                SELECT id FROM saved_results 
                WHERE task_path = ?
            """, (task_path,))
            existing = cursor.fetchone()
            
            if existing:
                # Update existing record
                cursor.execute("""
                    UPDATE saved_results
                    SET chi2 = ?,
                    chi2_npts = ?,
                    final_iteration = ?,
                    parameters_snapshot = ?,
                    user_note = ?,
                    is_best = ?,
                    tags = ?,
                    saved_at = CURRENT_TIMESTAMP
                    WHERE task_path = ?
                """, (
                    final_chi2,
                    final_chi2_npts,
                    final_iteration,
                    json.dumps(params),
                    user_note,
                    1 if mark_as_best else 0,
                    ','.join(tags) if tags else '',
                    task_path
                ))
                saved_id = existing['id']
                action = "updated"
            else:
                # Insert new record
                cursor.execute("""
                    INSERT INTO saved_results (
                        batch_id, task_path, chat_id, chi2, chi2_npts,
                        final_iteration, parameters_snapshot, user_note, is_best, tags
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    batch_id,
                    task_path,
                    chat_id,
                    final_chi2,
                    final_chi2_npts,
                    final_iteration,
                    json.dumps(params),
                    user_note,
                    1 if mark_as_best else 0,
                    ','.join(tags) if tags else ''
                ))
                saved_id = cursor.lastrowid
                action = "saved"
            
            conn.commit()
            
            return {
                "status": "success",
                "action": action,
                "saved_id": saved_id,
                "batch_id": batch_id,
                "chi2": final_chi2,
                "chi2_npts": final_chi2_npts,
                "final_iteration": final_iteration,
                "is_best": mark_as_best
            }
            
    except Exception as e:
        logger.error(f"Failed to save result: {e}")
        return {"error": str(e)}

def get_saved_results(
    batch_id: str = "",
    chat_id: str = "",
    best_only: bool = False
) -> List[Dict[str, Any]]:
    """
    Retrieve saved results.
    
    Args:
        batch_id: Filter by batch (optional)
        chat_id: Filter by chat (optional)
        best_only: Only return results marked as best
    
    Returns:
        List of saved results
    """
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            query = "SELECT * FROM saved_results WHERE 1=1"
            params = []
            
            if batch_id:
                query += " AND batch_id = ?"
                params.append(batch_id)
            
            if chat_id:
                query += " AND chat_id = ?"
                params.append(chat_id)
            
            if best_only:
                query += " AND is_best = 1"
            
            query += " ORDER BY chi2_npts ASC"
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
            results = []
            for row in rows:
                row_dict = dict(row)
                results.append({
                    "id": row["id"],
                    "batch_id": row["batch_id"],
                    "task_path": row["task_path"],
                    "chat_id": row["chat_id"],
                    "chi2": row["chi2"],
                    "chi2_npts": row["chi2_npts"],
                    "final_iteration": row["final_iteration"],
                    "parameters": json.loads(row["parameters_snapshot"]),
                    "user_note": row["user_note"],
                    "is_best": bool(row["is_best"]),
                    "tags": row["tags"].split(',') if row["tags"] else [],
                    "saved_at": row["saved_at"]
                })
            
            return results
            
    except Exception as e:
        logger.error(f"Failed to get saved results: {e}")
        return []

def restore_parameters_from_saved(saved_id: int) -> Dict[str, Any]:
    """
    Restore parameters from a saved result to the default configuration.
    
    Args:
        saved_id: ID of the saved result
    
    Returns:
        Result dictionary
    """
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT parameters_snapshot, batch_id, chi2_npts 
                FROM saved_results 
                WHERE id = ?
            """, (saved_id,))
            row = cursor.fetchone()
            
            if not row:
                return {"error": f"Saved result #{saved_id} not found"}
            
            params = json.loads(row["parameters_snapshot"])
            
            # Write to default.yaml on HPC
            content = yaml.dump(params, default_flow_style=False)
            config_path = _remote_default_config()
            ssh_write_file(config_path, content)

            return {
                "status": "success",
                "message": f"Parameters restored from saved result #{saved_id}",
                "batch_id": row["batch_id"],
                "chi2_npts": row["chi2_npts"],
                "config_path": config_path
            }
            
    except Exception as e:
        logger.error(f"Failed to restore parameters: {e}")
        return {"error": str(e)}

def delete_saved_result(saved_id: int) -> Dict[str, Any]:
    """
    Delete a saved result.
    
    Args:
        saved_id: ID of the saved result to delete
    
    Returns:
        Result dictionary
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("DELETE FROM saved_results WHERE id = ?", (saved_id,))
            
            if cursor.rowcount == 0:
                return {"error": f"Saved result #{saved_id} not found"}
            
            conn.commit()
            
            return {
                "status": "success",
                "message": f"Saved result #{saved_id} deleted"
            }
            
    except Exception as e:
        logger.error(f"Failed to delete saved result: {e}")
        return {"error": str(e)}

def get_batch_best_result(batch_id: str) -> Dict[str, Any]:
    """
    Get the best result for a specific batch (lowest chi2/npts).
    
    Args:
        batch_id: Batch identifier
    
    Returns:
        Best result or None
    """
    results = get_saved_results(batch_id=batch_id)
    
    if not results:
        return {"error": f"No saved results found for batch {batch_id}"}
    
    # Results are already sorted by chi2_npts ASC
    best = results[0]
    
    return {
        "status": "success",
        "best_result": best
    }

def compute_batch_prior(
    batch_id: str,
    top_n: int = None,
    chi2_npts_max: float = None,
) -> Dict[str, Any]:
    """
    Compute mean PDF parameters from the best results in a batch and write
    them back to default.yaml on HPC. This implements the posterior→prior
    transfer needed between steps in a multi-step fit chain.

    Args:
        batch_id:      Batch to aggregate over.
        top_n:         Keep only the best N results (by chi2/npts). None = all.
        chi2_npts_max: Discard results above this chi2/npts threshold.

    Returns:
        Dict with status, how many replicas were used, and the updated param count.
    """
    all_results = get_saved_results(batch_id=batch_id)
    if not all_results:
        return {"error": f"No saved results for batch '{batch_id}'. Save results first."}

    total_count = len(all_results)

    # Filter by threshold
    if chi2_npts_max is not None:
        results = [r for r in all_results if r["chi2_npts"] <= chi2_npts_max]
    else:
        results = list(all_results)
    if not results:
        return {"error": f"No results survived chi2_npts_max={chi2_npts_max} filter."}

    good_fraction = len(results) / total_count

    # Take best N (already sorted ASC by chi2_npts from get_saved_results)
    if top_n is not None:
        results = results[:top_n]

    # Quality assessment based on chi2/npts distribution
    chi2_vals = [r["chi2_npts"] for r in results]
    chi2_vals_sorted = sorted(chi2_vals)
    chi2_best = chi2_vals_sorted[0]
    chi2_median = chi2_vals_sorted[len(chi2_vals_sorted) // 2]

    if chi2_best > 5.0 or good_fraction < 0.1:
        quality = "reject"
        quality_message = (
            f"Poor convergence: best chi²/N={chi2_best:.3f}, "
            f"only {good_fraction*100:.0f}% of tasks passed filter. "
            f"Prior NOT updated. Consider re-running with more tasks or wider parameter ranges."
        )
    elif chi2_median > 3.0 or good_fraction < 0.3:
        quality = "warn"
        quality_message = (
            f"Marginal convergence: median chi²/N={chi2_median:.3f}, "
            f"{good_fraction*100:.0f}% of tasks passed filter. "
            f"Prior updated but consider re-running for a better prior."
        )
    else:
        quality = "ok"
        quality_message = (
            f"Good convergence: median chi²/N={chi2_median:.3f}, "
            f"{good_fraction*100:.0f}% of tasks passed filter."
        )

    # Reject: do not update prior
    if quality == "reject":
        return {
            "status": "reject",
            "batch_id": batch_id,
            "total_tasks": total_count,
            "good_fraction": round(good_fraction, 3),
            "chi2_best": round(chi2_best, 4),
            "chi2_median": round(chi2_median, 4),
            "message": quality_message,
        }

    # Collect ALL parameter distributions (pdf, ffpion, ffkaon, ffhadron, ppdf, etc.)
    # from each saved result's snapshot. Each distribution is a separate key under conf.params.
    # Known distributions: pdf, ppdf, ffpion, ffkaon, ffhadron
    all_distributions: Dict[str, list] = {}  # dist_name → [dict_per_replica]
    for r in results:
        conf_params = r.get("parameters", {}).get("conf", {}).get("params", {})
        for dist_name, dist_params in conf_params.items():
            if isinstance(dist_params, dict) and dist_params:
                if dist_name not in all_distributions:
                    all_distributions[dist_name] = []
                all_distributions[dist_name].append(dist_params)

    if not all_distributions:
        return {"error": "Could not extract any parameter distributions from saved results."}

    # Average the 'value' of each non-fixed parameter across all replicas, per distribution.
    # Guards: skip empty distributions; reject non-finite (NaN/Inf) floats rather than
    # averaging them into the prior and corrupting the next fit.
    all_averaged: Dict[str, Dict[str, float]] = {}  # dist_name → {param: avg}
    for dist_name, replicas in all_distributions.items():
        if not replicas:
            continue
        param_names = list(replicas[0].keys())
        averaged: Dict[str, float] = {}
        for name in param_names:
            vals = []
            for pp in replicas:
                if name in pp and not pp[name].get("fixed", False):
                    v = pp[name].get("value")
                    if v is None:
                        continue
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(fv):
                        logger.warning(
                            f"Dropping non-finite value for {dist_name}.{name}: {v!r}"
                        )
                        continue
                    vals.append(fv)
            if vals:
                averaged[name] = sum(vals) / len(vals)
        if averaged:
            all_averaged[dist_name] = averaged

    # Read current default.yaml from HPC
    config_path = _remote_default_config()
    try:
        content = ssh_read_file(config_path)
        config = yaml.safe_load(content) or {}
    except Exception as e:
        return {"error": f"Failed to read default.yaml from HPC ({config_path}): {e}"}

    # Update parameter values in-place for ALL distributions found
    updated_count = 0
    updated_distributions = []
    conf_params = config.get("conf", {}).get("params", {})
    for dist_name, averaged in all_averaged.items():
        dist_conf = conf_params.get(dist_name, {})
        dist_updated = 0
        for name, avg_val in averaged.items():
            if name in dist_conf:
                dist_conf[name]["value"] = avg_val
                dist_updated += 1
        if dist_updated > 0:
            updated_distributions.append(f"{dist_name}({dist_updated})")
            updated_count += dist_updated

    # Write back to HPC
    try:
        new_content = yaml.dump(config, default_flow_style=False)
        ssh_write_file(config_path, new_content)
    except Exception as e:
        return {"error": f"Failed to write default.yaml to HPC ({config_path}): {e}"}

    chi2_range = [chi2_vals_sorted[0], chi2_vals_sorted[-1]]
    replicas_used = max(len(v) for v in all_distributions.values())
    return {
        "status": quality,
        "batch_id": batch_id,
        "total_tasks": total_count,
        "replicas_used": replicas_used,
        "good_fraction": round(good_fraction, 3),
        "chi2_best": round(chi2_best, 4),
        "chi2_median": round(chi2_median, 4),
        "chi2_npts_range": chi2_range,
        "params_updated": updated_count,
        "distributions_updated": updated_distributions,
        "message": quality_message + f" Updated {updated_count} parameters across {', '.join(updated_distributions)} in default.yaml.",
    }


def get_best_results_aggregated(chat_id: str = None) -> Dict[str, Any]:
    """
    Get the best results: one for the specified chat (if provided) and one global best (lowest chi2/npts).
    
    Args:
        chat_id: Optional chat ID to find the best result for that specific session.
    
    Returns:
        Dictionary with 'current_chat_best' and 'all_chats_best'
    """
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Global best
            cursor.execute("SELECT * FROM saved_results ORDER BY chi2_npts ASC LIMIT 1")
            row_global = cursor.fetchone()
            
            all_chats_best = None
            if row_global:
                all_chats_best = dict(row_global)
                all_chats_best["parameters"] = json.loads(row_global["parameters_snapshot"])
                all_chats_best["tags"] = row_global["tags"].split(',') if row_global["tags"] else []

            # Chat best
            current_chat_best = None
            if chat_id:
                cursor.execute("SELECT * FROM saved_results WHERE chat_id = ? ORDER BY chi2_npts ASC LIMIT 1", (chat_id,))
                row_chat = cursor.fetchone()
                if row_chat:
                    current_chat_best = dict(row_chat)
                    current_chat_best["parameters"] = json.loads(row_chat["parameters_snapshot"])
                    current_chat_best["tags"] = row_chat["tags"].split(',') if row_chat["tags"] else []
                    
        return {
            "current_chat_best": current_chat_best,
            "all_chats_best": all_chats_best
        }
    except Exception as e:
        logger.error(f"Failed to get aggregated best results: {e}")
        return {"error": str(e)}
