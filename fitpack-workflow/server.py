#!/usr/bin/env python3
"""
FitPack MCP Server

Provides MCP tools for PDF fitting parameter management, task generation,
SLURM job submission, status monitoring, result saving, and cancellation.

Self-contained: all functionality lives within the fitpack-workflow/ package.
No imports from agents/, api/, or other project-level modules.
"""
import json
import logging
import os
import random
import sqlite3
import sys
import time
import uuid
import yaml
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("fitpack-mcp")

# --- Absolute paths ---
_SERVER_DIR = Path(__file__).parent.resolve()
DB_PATH = os.environ.get("DB_PATH", str(_SERVER_DIR / "hpc_tasks.db"))

from ssh_runner import (
    ssh_run, ssh_read_file, ssh_write_file, ssh_file_exists, ssh_mkdir,
    get_remote_base, get_ssh_config, set_hpc_user,
)
from iri_backend import (
    iri_find,
    is_iri_enabled,
    is_iri_ready,
    iri_submit_script,
    iri_tail_first_matching,
)


def _default_config(username: str) -> str:
    """Per-user path to default.yaml on the remote HPC."""
    return f"{get_remote_base(username)}/inputs/default.yaml"


def _runs_dir(username: str) -> str:
    """Per-user path to the runs/ directory on the remote HPC."""
    return f"{get_remote_base(username)}/runs"

# --- FastMCP ---
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    logger.error("mcp-python-sdk not installed. Run: pip install 'mcp[cli]'")
    sys.exit(1)

# --- Local helpers (all within fitpack-workflow/) ---
try:
    from param_tools import (
        list_params as _list_params,
        explain_params as _explain_params,
        get_current_params as _get_current_params,
        modify_params as _modify_params,
        check_params_readiness as _check_params_readiness,
    )
    PARAM_TOOLS_OK = True
except ImportError as e:
    logger.error(f"Failed to import param_tools: {e}")
    PARAM_TOOLS_OK = False

try:
    from slurm_tools import generate_slurm_script, parse_job_id, get_server_info, get_user_usage
    SLURM_TOOLS_OK = True
except ImportError as e:
    logger.error(f"Failed to import slurm_tools: {e}")
    SLURM_TOOLS_OK = False

try:
    from status_sync import sync_task_statuses, get_task_status_info
    STATUS_SYNC_OK = True
except ImportError:
    STATUS_SYNC_OK = False

try:
    from result_manager import (
        save_task_result,
        get_saved_results,
        restore_parameters_from_saved,
        get_batch_best_result,
        compute_batch_prior as _compute_batch_prior,
    )
    RESULT_MANAGER_OK = True
except ImportError:
    RESULT_MANAGER_OK = False

try:
    from task_cancellation import cancel_task, cancel_tasks_by_batch
    CANCELLATION_OK = True
except ImportError as e:
    logger.warning(f"Task cancellation not available: {e}")
    CANCELLATION_OK = False

try:
    from status_monitor import start_monitor
    STATUS_MONITOR_OK = True
except ImportError:
    STATUS_MONITOR_OK = False


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    # A thousand-replica campaign makes status sync a long write, and the
    # default three-second busy timeout turned every concurrent read into
    # "backend unreachable". WAL lets readers through while that write runs;
    # the longer timeout covers the checkpoint.
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _init_db() -> None:
    """Apply the canonical schema. Single source of truth lives in
    fitpack-workflow/schema.py, shared with open-webui/routers/hpc.py so the
    two processes can never drift."""
    try:
        from schema import apply_schema
    except ImportError:
        # When this module is imported from open-webui (which prepends
        # MCP_SERVER_PATH to sys.path), `schema` resolves; when invoked from
        # within fitpack-workflow's own tooling the same resolution applies.
        # If neither path works, surface the failure rather than silently
        # falling back to a stale inline copy.
        raise
    try:
        with _get_db() as conn:
            apply_schema(conn)
    except Exception as e:
        logger.error(f"DB init failed: {e}")


def _record_job_history(task_path: str, batch_id: str = "", slurm_job_id: str = "", username: str = "") -> None:
    """Record a completed task's actual runtime and memory usage for future resource prediction."""
    try:
        # Read progress.json for runtime data
        elapsed_min = None
        total_iterations = None
        final_chi2 = None
        final_npts = None
        n_datasets = 0
        # Recorded on the task row at generation. It was initialised here and
        # never assigned, so every row landed with observable='' and the
        # estimator's history lookup could never match anything.
        observable = ""
        try:
            with _get_db() as _c:
                _row = _c.execute("SELECT observable FROM tasks WHERE path=?",
                                  (task_path,)).fetchone()
            if _row and _row[0]:
                observable = str(_row[0])
        except Exception:
            pass

        try:
            progress_content = ssh_read_file(f"{task_path}/progress.json", username=username)
            import json as _json
            progress_data = _json.loads(progress_content)
            # progress.json is written either as a bare list or as
            # {"history": [...]}. Only the list form was handled, so on the
            # other shape elapsed_min stayed NULL and the history branch of the
            # estimator had nothing to average over.
            _hist = (progress_data if isinstance(progress_data, list)
                     else (progress_data or {}).get("history") or [])
            if _hist:
                last = _hist[-1]
                elapsed_min = last.get("elapsed_min")
                total_iterations = last.get("iter")
                final_chi2 = last.get("chi2")
                final_npts = last.get("npts")
                # Count datasets from the progress data
                datasets_field = last.get("datasets", {})
                n_datasets = len(datasets_field) if datasets_field else final_npts  # approximate
        except Exception as e:
            logger.warning(f"Could not read progress.json for {task_path}: {e}")

        # Read peak memory from SLURM sacct.
        # IRI does not expose MaxRSS; skip the lookup entirely under IRI to
        # avoid an unproductive translation round-trip.
        peak_memory_mb = None
        node_name = ""
        if slurm_job_id and not is_iri_enabled():
            try:
                result = ssh_run(
                    ["sacct", "-j", slurm_job_id, "--format=MaxRSS,NodeList", "-n", "--parsable2"],
                    timeout=10, username=username
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split("\n"):
                        parts = line.split("|")
                        if len(parts) >= 2 and parts[0].strip():
                            mem_str = parts[0].strip()
                            node_name = parts[1].strip() if len(parts) > 1 else ""
                            # Parse memory: "485.50M" or "1.2G" or "512K"
                            if mem_str.endswith("M"):
                                peak_memory_mb = float(mem_str[:-1])
                            elif mem_str.endswith("G"):
                                peak_memory_mb = float(mem_str[:-1]) * 1024
                            elif mem_str.endswith("K"):
                                peak_memory_mb = float(mem_str[:-1]) / 1024
                            if peak_memory_mb:
                                break
            except Exception as e:
                logger.warning(f"Could not read sacct for job {slurm_job_id}: {e}")

        # Count observables from config
        n_observables = 0
        try:
            config_content = ssh_read_file(f"{task_path}/parameters.yaml", username=username)
            config = yaml.safe_load(config_content)
            ds = config.get("conf", {}).get("datasets", {})
            n_observables = len(ds)
            n_datasets = sum(len(v.get("xlsx", {})) for v in ds.values())
            # Get first observable name
            observable = list(ds.keys())[0] if ds else ""
        except Exception:
            pass

        final_chi2_npts = round(final_chi2 / final_npts, 4) if final_chi2 and final_npts else None

        with _get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO job_history
                (batch_id, task_path, slurm_job_id, hpc_username, observable,
                 n_datasets, n_observables, step_index, elapsed_min, total_iterations,
                 final_chi2, final_npts, final_chi2_npts, peak_memory_mb, cpus, node_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 4, ?)
            """, (batch_id, task_path, slurm_job_id, username, observable,
                  n_datasets, n_observables, 0, elapsed_min, total_iterations,
                  final_chi2, final_npts, final_chi2_npts, peak_memory_mb, node_name))

        logger.info(f"Recorded job history: {task_path} elapsed={elapsed_min}min mem={peak_memory_mb}MB chi2/npts={final_chi2_npts}")

    except Exception as e:
        logger.error(f"Failed to record job history for {task_path}: {e}")


def _get_hpc_username_by_user_id(user_id: str) -> Optional[str]:
    """Look up the HPC username by Open WebUI user_id."""
    try:
        with _get_db() as conn:
            row = conn.execute("SELECT hpc_username FROM user_hpc_config WHERE user_id = ?", (user_id,)).fetchone()
            if row:
                return row[0]
    except Exception:
        pass
    return None


def _get_hpc_username(chat_id: str) -> Optional[str]:
    """Look up HPC username for a chat. Tries: 1) chat's user_id in config, 2) any configured user (single-user fallback)."""
    # Try to find the Open WebUI user_id for this chat
    try:
        import sqlite3 as _sq
        _project_root = _SERVER_DIR.parent.parent  # fitpack-workflow -> fitpack_legacy_copy -> project root
        _default_webui_db = str(_project_root / "open-webui" / "backend" / "data" / "webui.db")
        webui_db = os.environ.get("WEBUI_DB_PATH", _default_webui_db)
        if os.path.exists(webui_db):
            conn = _sq.connect(webui_db)
            row = conn.execute("SELECT user_id FROM chat WHERE id = ?", (chat_id,)).fetchone()
            conn.close()
            if row:
                username = _get_hpc_username_by_user_id(row[0])
                if username:
                    return username
    except Exception as e:
        logger.warning(f"Could not look up user_id for chat {chat_id}: {e}")

    # The caller id IS the config key for non-WebUI clients (the ChatGPT app
    # passes an authenticated user id). Without this lookup the function only
    # ever resolved when exactly one account existed in the whole table, which
    # silently breaks the moment a second collaborator is onboarded.
    direct = _get_hpc_username_by_user_id(chat_id)
    if direct:
        return direct

    # No "single configured user" fallback: it silently lends one person's
    # cluster account to any caller who has not configured their own, which
    # reads as "you are connected" to someone who is not.
    return None


def _set_hpc_username(user_id: str, username: str) -> None:
    """Store the HPC username for an Open WebUI user."""
    remote_base = get_remote_base(username)
    try:
        with _get_db() as conn:
            conn.execute("""
                INSERT INTO user_hpc_config (user_id, hpc_username, remote_base)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    hpc_username = excluded.hpc_username,
                    remote_base = excluded.remote_base,
                    configured_at = CURRENT_TIMESTAMP
            """, (user_id, username, remote_base))
    except Exception as e:
        logger.error(f"Failed to set HPC username: {e}")


def _username_for_task(task_path: str) -> str:
    """Look up the HPC username recorded against a task path."""
    try:
        with _get_db() as conn:
            row = conn.execute(
                "SELECT hpc_username, chat_id FROM tasks WHERE path = ?",
                (task_path,),
            ).fetchone()
            if not row:
                return ""
            if row[0]:
                return row[0]
            # Legacy row without hpc_username: fall back to the chat's user
            return _get_hpc_username(row[1] or "") or ""
    except Exception:
        return ""


def _batch_owned_by(batch_id: str, chat_id: str) -> bool:
    """Does at least one task row of this batch belong to the caller?

    The server itself is unauthenticated at the transport layer (deployment
    boundary: mcpo must bind loopback only — see README). Where a tool DOES
    receive an identity, it must still verify that the resource it is asked
    to act on belongs to that identity, otherwise any caller can address any
    batch by guessing an id. An empty chat_id keeps the legacy call path
    (internal callers) working.
    """
    if not batch_id or not chat_id:
        return True
    try:
        with _get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE batch_id = ? AND chat_id = ?",
                (batch_id, chat_id),
            ).fetchone()
            return bool(row and row[0])
    except Exception:
        return True  # never break legacy flows on a ledger read failure


def _require_hpc_username(chat_id: str) -> str:
    """Get HPC username or raise an error with a helpful message."""
    username = _get_hpc_username(chat_id)
    if not username:
        raise ValueError(
            "HPC account not configured. "
            "Please connect to HPC first using the HPC Connection button in the top bar."
        )
    return username


def _get_chain_state(chat_id: str) -> Dict[str, Any]:
    """Get the current chain state for a chat. Returns empty state if none exists."""
    try:
        with _get_db() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM chain_state WHERE chat_id = ?", (chat_id,)).fetchone()
            if row:
                state = dict(row)
                state["history"] = json.loads(state.get("history", "[]"))
                return state
    except Exception as e:
        logger.warning(f"Could not read chain state: {e}")
    return {"chat_id": chat_id, "current_step": 0, "active_batch_id": "", "active_observable": "", "history": []}


def _update_chain_state(chat_id: str, step: int, batch_id: str, observable: str, history: list) -> None:
    """Update or insert chain state for a chat."""
    try:
        with _get_db() as conn:
            conn.execute("""
                INSERT INTO chain_state (chat_id, current_step, active_batch_id, active_observable, history, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET
                    current_step = excluded.current_step,
                    active_batch_id = excluded.active_batch_id,
                    active_observable = excluded.active_observable,
                    history = excluded.history,
                    updated_at = CURRENT_TIMESTAMP
            """, (chat_id, step, batch_id, observable, json.dumps(history)))
    except Exception as e:
        logger.error(f"Chain state update failed: {e}")


# ---------------------------------------------------------------------------
# Fit config scratchpad (per-chat) — stored on chain_state.staged_config
# ---------------------------------------------------------------------------
# The scratchpad is a free-form JSON dict keyed by field names that
# DatasetSelector understands (observable, selected_ids, filters, replicas,
# max_iterations, tolerance, optimizer, bootstrap, chi2_ok, chi2_warn,
# good_fraction, pdf_params, qcd_config, job_number, label). Nothing here
# strictly validates the shape — the LLM stages whatever the user says, the
# UI merges it over catalog defaults on open, and the user can still edit in
# the UI before committing. Callers must always defend against a missing row
# because _init_db may not have run for this chat yet.

def _get_staged_config(chat_id: str) -> Dict[str, Any]:
    _init_db()
    try:
        with _get_db() as conn:
            row = conn.execute(
                "SELECT staged_config FROM chain_state WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if row and row[0]:
                return json.loads(row[0])
    except Exception as e:
        logger.warning(f"Could not read staged_config for {chat_id}: {e}")
    return {}


def _set_staged_config(chat_id: str, staged: Dict[str, Any]) -> None:
    _init_db()
    payload = json.dumps(staged)
    try:
        with _get_db() as conn:
            # Upsert without clobbering the other chain_state columns if the
            # row already exists (run_fitting_job keeps history/current_step).
            existing = conn.execute(
                "SELECT 1 FROM chain_state WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE chain_state SET staged_config = ?, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ?",
                    (payload, chat_id),
                )
            else:
                conn.execute(
                    "INSERT INTO chain_state (chat_id, staged_config, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (chat_id, payload),
                )
    except Exception as e:
        logger.error(f"Staged config write failed for {chat_id}: {e}")


# ---------------------------------------------------------------------------
# stage_fit_param validator. Shares fitting_defaults.json with the UI and the
# prompt (fit_scratchpad.py) so rejection reasons match what the model was
# told. Degrades to no-op if the JSON isn't reachable — the UI is still the
# ultimate gate before commit, so validation here is a defense-in-depth
# layer, not a correctness requirement.
# ---------------------------------------------------------------------------

_FITTING_DEFAULTS_CACHE: Optional[Dict[str, Any]] = None
_FLAVORS_NO_CD = {"sea1", "sea2", "s1", "sb1", "mix"}
_ALLOWED_STAGE_KEYS = {
    "observable", "selected_ids", "filters",
    "replicas", "max_iterations", "tolerance", "optimizer",
    "bootstrap", "chi2_ok", "chi2_warn", "good_fraction",
    "pdf_params", "qcd_config", "job_number", "label",
}


def _load_fitting_defaults() -> Dict[str, Any]:
    global _FITTING_DEFAULTS_CACHE
    if _FITTING_DEFAULTS_CACHE is not None:
        return _FITTING_DEFAULTS_CACHE
    candidates: List[Path] = []
    env_path = os.environ.get("FITTING_DEFAULTS_JSON")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(
        Path(__file__).resolve().parents[2]
        / "open-webui" / "src" / "lib" / "data" / "fitting_defaults.json"
    )
    for p in candidates:
        try:
            if p.exists():
                with p.open() as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    logger.info(f"stage_fit_param validator: defaults loaded from {p}")
                    _FITTING_DEFAULTS_CACHE = data
                    return data
        except Exception as e:
            logger.warning(f"stage_fit_param validator: could not read {p}: {e}")
    logger.warning("stage_fit_param validator: fitting_defaults.json not found; validation DISABLED")
    _FITTING_DEFAULTS_CACHE = {}
    return {}


def _validate_stage_param(key: str, value: Any) -> Optional[str]:
    """Return rejection reason if (key, value) is invalid, else None.

    Conservative: rejects only provably-wrong values (unknown enum, unknown
    flavor/param, min>=max, out-of-range value). Types that are ambiguous
    ("tolerance" can be float or string) are left alone so the existing
    permissive contract survives.
    """
    if key not in _ALLOWED_STAGE_KEYS:
        return f"unknown scratchpad key {key!r}; allowed: {sorted(_ALLOWED_STAGE_KEYS)}"

    d = _load_fitting_defaults()
    if not d:
        return None

    if key == "observable":
        obs = d.get("observables") or []
        if obs and value not in obs:
            return f"observable {value!r} not in {obs}"
        return None

    if key == "filters":
        if not isinstance(value, list):
            return "filters must be a list of strings"
        allowed_lhs = d.get("filter_expressions") or []
        ops = ("<=", ">=", "==", "<", ">")
        for f in value:
            if not isinstance(f, str):
                return f"filter {f!r} is not a string"
            if allowed_lhs and not any(f.startswith(lhs) for lhs in allowed_lhs):
                return f"filter {f!r} LHS not in {allowed_lhs}"
            if not any(op in f for op in ops):
                return f"filter {f!r} has no comparison operator"
        return None

    if key == "qcd_config":
        if not isinstance(value, dict):
            return "qcd_config must be a dict"
        qcd_enums = (d.get("qcd_config") or {}).get("enums") or {}
        for subk, subv in value.items():
            if subk in qcd_enums and subv not in qcd_enums[subk]:
                return f"qcd_config.{subk}={subv!r} not in {qcd_enums[subk]}"
        return None

    if key == "optimizer":
        opt_enum = ((d.get("job_config") or {}).get("enums") or {}).get("optimizer") or []
        if opt_enum and value not in opt_enum:
            return f"optimizer {value!r} not in {opt_enum}"
        return None

    if key == "pdf_params":
        if not isinstance(value, dict):
            return "pdf_params must be a dict keyed by flavor"
        flavors = set(d.get("pdf_flavors") or [])
        params = set(d.get("pdf_param_names") or [])
        for flavor, flavor_params in value.items():
            if flavors and flavor not in flavors and flavor != "mix":
                return f"pdf_params: unknown flavor {flavor!r}, allowed {sorted(flavors)}"
            if not isinstance(flavor_params, dict):
                return f"pdf_params.{flavor} must be a dict"
            for pname, pbody in flavor_params.items():
                if params and pname not in params:
                    return f"pdf_params.{flavor}.{pname}: unknown param, allowed {sorted(params)}"
                if pname in ("c", "d") and flavor in _FLAVORS_NO_CD:
                    return f"pdf_params.{flavor}.{pname}: flavor {flavor} has no {pname} param (only N/a/b)"
                if isinstance(pbody, dict):
                    vmin, vmax, v = pbody.get("min"), pbody.get("max"), pbody.get("value")
                    try:
                        if vmin is not None and vmax is not None and float(vmin) >= float(vmax):
                            return f"pdf_params.{flavor}.{pname}: min({vmin}) >= max({vmax})"
                        if (
                            v is not None and vmin is not None and vmax is not None
                            and not (float(vmin) <= float(v) <= float(vmax))
                        ):
                            return f"pdf_params.{flavor}.{pname}: value {v} outside [{vmin}, {vmax}]"
                    except (TypeError, ValueError):
                        pass
        return None

    if key in ("replicas", "max_iterations"):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return f"{key} must be an integer, got {value!r}"
        if n <= 0:
            return f"{key} must be > 0, got {value}"
        return None

    if key in ("chi2_ok", "chi2_warn", "good_fraction"):
        try:
            f = float(value)
        except (TypeError, ValueError):
            return f"{key} must be numeric, got {value!r}"
        if f <= 0:
            return f"{key} must be > 0, got {value}"
        if key == "good_fraction" and not (0 < f <= 1):
            return f"good_fraction must be in (0, 1], got {f}"
        return None

    return None


def _is_batch_completed(batch_id: str) -> bool:
    """Check if all tasks in a batch are completed."""
    try:
        with _get_db() as conn:
            rows = conn.execute(
                "SELECT status FROM tasks WHERE batch_id = ?", (batch_id,)
            ).fetchall()
            if not rows:
                return False
            return all(r[0] in ('completed', 'failed', 'cancelled') for r in rows)
    except Exception:
        return False


def _collect_parent_msr_paths(batch_id: str, username: str = "") -> List[str]:
    """Find .msr output files for each completed task in a parent batch.

    Returns a list of absolute paths to .msr files on HPC, one per
    completed replica.  Tasks that failed or have no .msr output are
    silently skipped.

    Args:
        batch_id: Parent batch identifier.
        username: HPC username whose account holds the task directories. If
                  empty, falls back to the ``hpc_username`` recorded on each
                  task row.
    """
    try:
        with _get_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT path, hpc_username FROM tasks "
                "WHERE batch_id = ? AND status = 'completed'",
                (batch_id,),
            ).fetchall()
    except Exception as e:
        logger.warning(f"_collect_parent_msr_paths DB error: {e}")
        return []

    msr_paths: List[str] = []
    for row in rows:
        task_path = row["path"]
        task_user = username or (row["hpc_username"] or "")
        if not task_user:
            logger.warning(
                f"Cannot resolve hpc_username for task {task_path}; skipping"
            )
            continue
        msr_dir = f"{task_path}/msr"
        if is_iri_enabled():
            try:
                matches = iri_find(msr_dir, "*.msr")
            except Exception as e:
                logger.warning(f"iri_find failed for {msr_dir}: {e}")
                matches = []
            if matches:
                msr_paths.append(matches[0])
            else:
                logger.warning(f"No .msr file found in {msr_dir}")
        else:
            result = ssh_run(
                ["find", msr_dir, "-maxdepth", "1", "-name", "*.msr", "-type", "f"],
                timeout=10,
                username=task_user,
            )
            if result.returncode == 0 and result.stdout.strip():
                msr_file = result.stdout.strip().split("\n")[0]
                msr_paths.append(msr_file)
            else:
                logger.warning(f"No .msr file found in {msr_dir}")
    return msr_paths


def _latest_track_selections(history: list, track: Optional[str]) -> List[Dict[str, Any]]:
    """Return the accumulated observable selections for each observable on `track`.

    Walks chain_state history chronologically.  For observables that appear only
    once (the common alpha-track pattern: idis -> dy -> jet) this is equivalent
    to keeping the single entry.  When the *same* observable appears in multiple
    steps (the beta-track pattern: sia(Pi) -> sia(K) -> sia(h)), selected_ids
    are **accumulated** (union, preserving insertion order) so that the full
    dataset overlay is replayed.  Filters always take the value from the latest
    entry.

    Output entries are ordered by the observable's first appearance on this
    track and are shaped like ``{"observable", "selected_ids", "filters"}`` --
    ready to feed into ``compose_dataset_config``.

    Legacy history entries that predate track-aware storage may lack
    ``selected_ids``/``filters``; those fields come back as empty lists and the
    caller is expected to fall back to catalog defaults.
    """
    if not history or track is None:
        return []
    accumulated: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        if entry.get("track") != track:
            continue
        obs = entry.get("observable", "")
        if not obs:
            continue
        if obs not in accumulated:
            order.append(obs)
            accumulated[obs] = {
                "observable": obs,
                "selected_ids": [],
                "filters": [],
            }
        # Accumulate selected_ids (union, preserving order)
        existing_ids = set(accumulated[obs]["selected_ids"])
        for sid in (entry.get("selected_ids") or []):
            if sid not in existing_ids:
                accumulated[obs]["selected_ids"].append(sid)
                existing_ids.add(sid)
        # Filters: latest entry wins
        new_filters = list(entry.get("filters") or [])
        if new_filters:
            accumulated[obs]["filters"] = new_filters
    return [accumulated[o] for o in order]


def _latest_completed_batch(history: list, track: Optional[str]) -> Optional[str]:
    """Return the most recent completed batch_id on the given track, or None.

    Used by the stage B routing path in run_fitting_job to pick α and β parent
    batches for random-pairing. Scans history newest-first; the first entry
    whose ``track`` matches and whose batch is fully completed wins.
    """
    if not history:
        return None
    for entry in reversed(history):
        if not isinstance(entry, dict):
            continue
        if entry.get("track") != track:
            continue
        bid = entry.get("batch_id", "")
        if bid and _is_batch_completed(bid):
            return bid
    return None


def _insert_task(task: dict) -> None:
    try:
        with _get_db() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO tasks (chat_id, batch_id, batch_label, file_id, path, is_running, slurm_job_id, parameters_file, step_index, parent_batch_id, hpc_username, observable)
                VALUES (?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?, ?)
            """, (
                task["chat_id"], task["batch_id"], task.get("batch_label", ""),
                task["file_id"], task["path"], task["parameters_file"],
                task.get("step_index", 0), task.get("parent_batch_id", ""),
                task.get("hpc_username", ""), task.get("observable", ""),
            ))
    except Exception as e:
        logger.error(f"Task insert failed: {e}")


# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "FitPack",
    dependencies=["mcp[cli]", "pyyaml", "numpy", "pandas"],
)


# ---------------------------------------------------------------------------
# Workflow definitions
# ---------------------------------------------------------------------------

_WORKFLOWS_FILE = _SERVER_DIR / "workflows.yaml"


def _load_workflows() -> List[Dict[str, Any]]:
    """Load workflow definitions from workflows.yaml."""
    try:
        with open(_WORKFLOWS_FILE, "r") as f:
            return yaml.safe_load(f) or []
    except Exception as e:
        logger.error(f"Failed to load workflows.yaml: {e}")
        return []


# ---------------------------------------------------------------------------
# Dataset catalogs per observable type
# ---------------------------------------------------------------------------

# Each entry: id, xlsx path (relative to database/), description, default norm
from dataset_catalog import DATASET_CATALOG, DATASET_FILTERS


# ---------------------------------------------------------------------------
# Tools: Dataset search
# ---------------------------------------------------------------------------

@mcp.tool()
def search_datasets(
    observable: str = "",
    experiment: str = "",
    target: str = "",
) -> Dict[str, Any]:
    """
    Search the dataset catalog by experiment name, target, or observable.

    Use this to map experiment names to dataset IDs (e.g., user says
    "remove HERMES data" → find all HERMES dataset IDs).

    Args:
        observable: Filter by observable key (e.g. "idis", "dy"). Empty = all.
        experiment: Filter by experiment name (e.g. "SLAC", "HERMES"). Case-insensitive partial match.
        target:     Filter by target (e.g. "p", "d", "pp"). Case-insensitive exact match.

    Returns:
        Matching datasets grouped by observable, with id, label, experiment, target.
    """
    results: Dict[str, list] = {}

    obs_keys = [observable] if observable and observable in DATASET_CATALOG else list(DATASET_CATALOG.keys())

    for obs_key in obs_keys:
        datasets = DATASET_CATALOG.get(obs_key, [])
        matched = []
        for ds in datasets:
            if experiment and experiment.lower() not in ds.get("experiment", "").lower():
                continue
            if target and ds.get("target", "").lower() != target.lower():
                continue
            matched.append({
                "id": ds["id"],
                "label": ds.get("label", ""),
                "experiment": ds.get("experiment", ""),
                "target": ds.get("target", ""),
                "obs": ds.get("obs", ""),
                "default_on": ds.get("default_on", False),
            })
        if matched:
            results[obs_key] = matched

    total = sum(len(v) for v in results.values())
    return {
        "total_matches": total,
        "by_observable": results,
        "query": {"observable": observable, "experiment": experiment, "target": target},
    }


# ---------------------------------------------------------------------------
# Tools: User HPC account setup
# ---------------------------------------------------------------------------

@mcp.tool()
def setup_hpc_account(hpc_username: str, user_id: str = "", chat_id: str = "") -> Dict[str, Any]:
    """
    Configure the HPC username for a user. Called automatically by the
    HPC Connection dialog when a user connects to HPC.

    Args:
        hpc_username:  The user's JLab HPC username.
        user_id:       Open WebUI user ID (preferred key).
        chat_id:       Chat ID (fallback, used as user_id if user_id not provided).
    """
    _init_db()
    if not hpc_username or not hpc_username.strip():
        return {"error": "HPC username cannot be empty."}

    username = hpc_username.strip().lower()
    key = user_id or chat_id
    if not key:
        return {"error": "Either user_id or chat_id must be provided."}

    _set_hpc_username(key, username)

    cfg = get_ssh_config(username)
    if is_iri_enabled():
        connected = is_iri_ready()
        backend_label = "IRI"
    else:
        connected = Path(cfg["control_path"]).exists()
        backend_label = "SSH"

    return {
        "status": "success",
        "hpc_username": username,
        "remote_base": cfg["remote_base"],
        "ssh_connected": connected,
        "backend": "iri" if is_iri_enabled() else "ssh",
        "message": f"HPC account '{username}' configured. "
                   + (f"{backend_label} backend is active." if connected
                      else f"WARNING: {backend_label} backend is not configured. Please connect/configure HPC first.")
    }


@mcp.tool()
def get_hpc_account(chat_id: str) -> Dict[str, Any]:
    """
    Check the current HPC account configuration for this chat.

    Args:
        chat_id: Current chat/session ID.
    """
    _init_db()
    username = _get_hpc_username(chat_id)
    if not username:
        return {"status": "not_configured", "message": "No HPC account configured. Please connect to HPC first using the HPC Connection button."}

    cfg = get_ssh_config(username)
    socket_exists = is_iri_ready() if is_iri_enabled() else Path(cfg["control_path"]).exists()
    return {
        "status": "configured",
        "hpc_username": username,
        "remote_base": cfg["remote_base"],
        "ssh_connected": socket_exists,
        "backend": "iri" if is_iri_enabled() else "ssh",
    }


# ---------------------------------------------------------------------------
# Tools: Workflow discovery
# ---------------------------------------------------------------------------

@mcp.tool()
def list_workflows() -> List[Dict[str, str]]:
    """
    List all available workflows provided by this server.
    Returns id, name, and description for each workflow.
    The model should call this to match user intent to a workflow.
    """
    workflows = _load_workflows()
    return [
        {"id": w["id"], "name": w["name"], "description": w["description"]}
        for w in workflows
    ]


@mcp.tool()
def get_workflow(workflow_id: str, chat_id: str = "") -> Dict[str, Any]:
    """
    Get the full workflow definition by ID, including all steps,
    fields, and tool mappings. The frontend uses this to render
    the workflow panel dynamically.

    For fitting workflows, the dataset_select step is enriched with
    the available dataset catalog, and the review_params step is
    enriched with live parameter data from default.yaml (requires
    ``chat_id`` so the correct user's HPC account is used).

    Args:
        workflow_id: The workflow identifier (e.g. "pdf_fit", "dy_fit").
        chat_id:     Current chat/session ID. Optional if the workflow has
                     no steps that read from the remote HPC.
    """
    workflows = _load_workflows()
    for w in workflows:
        if w["id"] == workflow_id:
            wf = deepcopy(w)
            _enrich_workflow(wf, chat_id=chat_id)
            return wf
    return {"error": f"Workflow '{workflow_id}' not found."}


def _enrich_workflow(wf: Dict[str, Any], chat_id: str = "") -> None:
    """Inject live data into workflow steps that need it."""
    observable = wf.get("observable", "idis")
    for step in wf.get("steps", []):
        if step.get("type") == "dataset_select":
            step["datasets"] = _build_dataset_catalog(step.get("observable", observable))
        if step.get("type") == "params_review":
            username = _get_hpc_username(chat_id) if chat_id else None
            if not username:
                step["params"] = {
                    "error": "HPC account not configured for this chat. "
                             "Connect to HPC first, then reload the workflow."
                }
            else:
                step["params"] = _build_params_for_review(username)


def _build_dataset_catalog(observable: str) -> Dict[str, Any]:
    """
    Build the dataset catalog for a given observable type.
    Returns a structured object the frontend uses to render checkboxes.
    """
    catalog = DATASET_CATALOG.get(observable, [])
    filters = DATASET_FILTERS.get(observable, [])
    items = []
    for ds in catalog:
        items.append({
            "id": ds["id"],
            "xlsx": ds["xlsx"],
            "label": ds["label"],
            "target": ds["target"],
            "obs": ds["obs"],
            "experiment": ds["experiment"],
            "default_on": ds["default_on"],
            "norm": ds["norm"],  # None means no normalization parameter
        })
    return {
        "observable": observable,
        "filters": filters,
        "items": items,
    }


def _build_params_for_review(username: str) -> Dict[str, Any]:
    """
    Read default.yaml from the remote HPC and build a structured parameter
    object for the frontend, separating config settings, PDF parameters,
    and dataset normalizations.
    """
    remote_config = _default_config(username)
    try:
        logger.info(f"Reading remote config: {remote_config}")
        content = ssh_read_file(remote_config, username=username)
        config = yaml.safe_load(content) or {}
        logger.info(f"Successfully read remote config ({len(content)} bytes)")
    except ConnectionError as e:
        logger.error(f"SSH not connected: {e}")
        return {"error": f"HPC not connected: {e}"}
    except FileNotFoundError as e:
        logger.error(f"Remote file not found: {e}")
        return {"error": f"Remote config not found: {e}"}
    except Exception as e:
        logger.error(f"Failed to read remote config: {e}")
        return {"error": f"Failed to read remote config: {e}"}

    conf = config.get("conf", {})
    result: Dict[str, Any] = {"groups": []}

    # Group 1: Global settings (read-only)
    settings_items = []
    for key in ["order", "Q20", "dglap mode", "alphaSmode", "nuc", "tmc",
                 "ht", "offpdf", "ftol", "bootstrap", "flat par"]:
        if key in conf:
            settings_items.append({
                "name": key,
                "value": conf[key],
                "editable": False,
            })
    result["groups"].append({
        "id": "settings",
        "label": "Global Settings",
        "editable": False,
        "items": settings_items,
    })

    # Group 2: PDF parameters (editable for non-fixed)
    pdf_params = conf.get("params", {}).get("pdf", {})
    pdf_items = []
    for name, info in pdf_params.items():
        fixed = info.get("fixed", False)
        pdf_items.append({
            "name": name,
            "value": info.get("value"),
            "min": info.get("min"),
            "max": info.get("max"),
            "fixed": fixed,
            "editable": not fixed,
        })
    result["groups"].append({
        "id": "pdf",
        "label": "PDF Parameters",
        "editable": True,
        "items": pdf_items,
    })

    # Note: Dataset normalizations are now handled by the dataset_select step,
    # so we no longer inject them here as a separate group.

    return result


# ---------------------------------------------------------------------------
# Tools: Parameter information
# ---------------------------------------------------------------------------

@mcp.tool()
def list_params(chat_id: str) -> str:
    """List all available PDF computation parameters from default.yaml.

    Args:
        chat_id: Current chat/session ID (required — the tool reads the
                 caller's remote default.yaml over SSH).
    """
    if not PARAM_TOOLS_OK:
        return "Error: param_tools module not available."
    try:
        username = _require_hpc_username(chat_id)
    except ValueError as e:
        return f"Error: {e}"
    with set_hpc_user(username):
        return _list_params()


@mcp.tool()
def list_parameters(chat_id: str) -> str:
    """Alias for list_params."""
    return list_params(chat_id)


@mcp.tool()
def explain_parameters(chat_id: str, param_names: Optional[List[str]] = None) -> str:
    """
    Get detailed physics explanations for parameters.

    Args:
        chat_id:     Current chat/session ID (required — reads remote config).
        param_names: Optional list of specific parameter names to explain.
                     If omitted, all parameters are explained.
    """
    if not PARAM_TOOLS_OK:
        return "Error: param_tools module not available."
    try:
        username = _require_hpc_username(chat_id)
    except ValueError as e:
        return f"Error: {e}"
    with set_hpc_user(username):
        return _explain_params(param_names)


@mcp.tool()
def check_fitting_readiness(chat_id: str) -> Dict[str, Any]:
    """Check whether the configuration is ready for a fit run.

    Args:
        chat_id: Current chat/session ID (required — reads remote config).
    """
    if not PARAM_TOOLS_OK:
        return {"ready": False, "error": "param_tools module not available."}
    try:
        username = _require_hpc_username(chat_id)
    except ValueError as e:
        return {"ready": False, "error": str(e)}
    with set_hpc_user(username):
        return _check_params_readiness()


# ---------------------------------------------------------------------------
# Tools: Configuration management
# ---------------------------------------------------------------------------

@mcp.tool()
def get_current_parameters(chat_id: str) -> Dict[str, Any]:
    """Return the full configuration currently stored in default.yaml.

    Args:
        chat_id: Current chat/session ID (required so the correct user's
                 remote default.yaml is read).
    """
    try:
        username = _require_hpc_username(chat_id)
    except ValueError as e:
        return {"error": str(e)}
    remote_config = _default_config(username)
    try:
        if not ssh_file_exists(remote_config, username=username):
            return {"error": f"Configuration file not found: {remote_config}"}
        content = ssh_read_file(remote_config, username=username)
        config = yaml.safe_load(content)
        return config or {}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def update_parameters(chat_id: str, parameters: Dict[str, Any]) -> str:
    """
    Update parameter values and persist to default.yaml.

    Args:
        chat_id:    Current chat/session ID (required — resolves the caller's
                    HPC account and writes to their remote default.yaml).
        parameters: Mapping of dot-notation paths to new values.
                    e.g. {"conf.params.pdf.g1 a.value": 0.5}
    """
    if not PARAM_TOOLS_OK:
        return "Error: param_tools module not available."
    try:
        username = _require_hpc_username(chat_id)
    except ValueError as e:
        return f"Error: {e}"
    with set_hpc_user(username):
        return _modify_params(parameters)


# ---------------------------------------------------------------------------
# Tools: Fit config scratchpad (per-chat staging area for DatasetSelector)
# ---------------------------------------------------------------------------

_LOCAL_DEFAULT_YAML = _SERVER_DIR.parent / "inputs" / "default.yaml"


@mcp.tool()
def stage_fit_param(chat_id: str, key: str, value: Any, note: str = "") -> Dict[str, Any]:
    """
    Stage one fit-config preference the user mentioned in chat, BEFORE any
    fit is actually launched. The value is saved to a per-chat scratchpad;
    when the user opens DatasetSelector it will be pre-filled with these
    values merged over the static defaults.

    Call this immediately the first time the user mentions ANY fit setting
    in conversation — don't wait until submission time, and don't try to
    remember it only in the chat transcript.

    Args:
        chat_id: Current chat ID.
        key:     Scratchpad field name. Accepted keys match DatasetSelector
                 payload fields: "observable", "selected_ids", "filters",
                 "replicas", "max_iterations", "tolerance", "optimizer",
                 "bootstrap", "chi2_ok", "chi2_warn", "good_fraction",
                 "pdf_params", "qcd_config", "job_number", "label".
        value:   The value the user asked for (string, number, list, or dict
                 depending on the key).
        note:    Optional free-text reason ("user said 'stricter cuts'").
                 Not shown in UI, just for later auditing.

    Validated against fitting_defaults.json (same source the UI and prompt
    read from). On rejection the scratchpad is NOT mutated and the error
    message names the conflict so the model can retry with a valid value.
    """
    err = _validate_stage_param(key, value)
    if err:
        return {
            "status": "error",
            "message": err,
            "staged": _get_staged_config(chat_id),
        }
    staged = _get_staged_config(chat_id)
    staged[key] = value
    if note:
        notes = staged.setdefault("_notes", {})
        if isinstance(notes, dict):
            notes[key] = note
    _set_staged_config(chat_id, staged)
    return {"status": "ok", "staged": staged}


@mcp.tool()
def unstage_fit_param(chat_id: str, key: str) -> Dict[str, Any]:
    """
    Remove a single previously staged fit-config preference. Use when the
    user says "don't set X", "forget about the iterations thing", etc.
    Leaves the rest of the scratchpad intact.

    Args:
        chat_id: Current chat ID.
        key:     The scratchpad field to drop.
    """
    staged = _get_staged_config(chat_id)
    removed = staged.pop(key, None)
    notes = staged.get("_notes")
    if isinstance(notes, dict):
        notes.pop(key, None)
    _set_staged_config(chat_id, staged)
    return {"status": "ok", "removed": key if removed is not None else None, "staged": staged}


@mcp.tool()
def clear_staged_fit_config(chat_id: str) -> Dict[str, Any]:
    """
    Empty the whole fit-config scratchpad for this chat. Use when the user
    says "reset / start over / forget everything I said about the fit".
    Does NOT touch chain_state history or the active batch — only the
    scratchpad layer used to pre-fill DatasetSelector.
    """
    _set_staged_config(chat_id, {})
    return {"status": "ok", "staged": {}}


@mcp.tool()
def get_staged_fit_config(chat_id: str) -> Dict[str, Any]:
    """
    Read the current fit-config scratchpad for this chat. Use to echo back
    staged values to the user (e.g. after "continue", or when they ask
    "what did I set?"), and internally before emitting a dataset_selector
    action so you know what will be pre-filled.

    Returns an empty dict if nothing is staged.
    """
    return {"status": "ok", "staged": _get_staged_config(chat_id)}


@mcp.tool()
def get_default_fit_config() -> Dict[str, Any]:
    """
    Return the LITERAL contents of fitpack_legacy_copy/inputs/default.yaml
    from local disk. This is the ONLY legal way to answer user questions
    about "what are the current / default fit parameters" — do NOT guess
    from memory, do NOT call web search or literature tools, do NOT
    hallucinate numbers. If this tool errors, say so to the user instead
    of filling in values from training data.

    Unlike get_current_parameters this does not require an HPC connection;
    it reads the local committed copy of default.yaml, which is the source
    of truth before any fit has been submitted.
    """
    try:
        if not _LOCAL_DEFAULT_YAML.exists():
            return {"status": "error", "message": f"default.yaml not found at {_LOCAL_DEFAULT_YAML}"}
        with open(_LOCAL_DEFAULT_YAML, "r") as f:
            config = yaml.safe_load(f) or {}
        return {"status": "ok", "source": str(_LOCAL_DEFAULT_YAML), "config": config}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Tools: Task generation
# ---------------------------------------------------------------------------

# Maps the UI's DEFAULT_QCD_CONFIG keys to the YAML keys expected under `conf`.
_QCD_CONFIG_KEY_MAP = {
    "order": "order",
    "Q20": "Q20",
    "tmc": "tmc",
    "ht": "ht",
    "htType": "ht type",
    "dglapMode": "dglap mode",
    "alphaSmode": "alphaSmode",
    "nuc": "nuc",
}


def _apply_advanced_overrides(
    config: Dict[str, Any],
    advanced: Dict[str, Any],
    applied_out: Dict[str, Any],
) -> bool:
    """Merge UI advanced overrides into a fitpack YAML config dict.

    Returns True if anything was mutated. Records what was applied in ``applied_out``.
    Unknown keys are ignored rather than failing the job.
    """
    mutated = False
    conf = config.setdefault("conf", {})

    # Tolerance → conf.ftol (string "1e-6" is accepted).
    tol = advanced.get("tolerance")
    if tol not in (None, ""):
        try:
            conf["ftol"] = float(tol)
            applied_out["tolerance"] = conf["ftol"]
            mutated = True
        except (TypeError, ValueError):
            logger.warning(f"Ignoring non-numeric tolerance: {tol!r}")

    # Max iterations → conf.maxiter (consumed by maxlike_jam as least_squares max_nfev).
    max_iter = advanced.get("max_iterations")
    if max_iter not in (None, ""):
        try:
            conf["maxiter"] = int(max_iter)
            applied_out["maxiter"] = conf["maxiter"]
            mutated = True
        except (TypeError, ValueError):
            logger.warning(f"Ignoring non-integer max_iterations: {max_iter!r}")

    # Optimizer → conf.method (passed to scipy.optimize.least_squares method=).
    # Only trf/dogbox support bounds; fitpack uses bounds, so 'lm' is excluded.
    optimizer = advanced.get("optimizer")
    if optimizer not in (None, ""):
        if optimizer in ("trf", "dogbox"):
            conf["method"] = optimizer
            applied_out["method"] = optimizer
            mutated = True
        else:
            logger.warning(f"Ignoring unsupported optimizer: {optimizer!r} (allowed: trf, dogbox)")

    # Bootstrap → conf.bootstrap (already present in default.yaml; can be toggled off).
    bootstrap = advanced.get("bootstrap")
    if bootstrap is not None:
        conf["bootstrap"] = bool(bootstrap)
        applied_out["bootstrap"] = conf["bootstrap"]
        mutated = True

    # QCD config → top-level conf keys (order, Q20, tmc, ht, etc.).
    qcd = advanced.get("qcd_config") or {}
    if isinstance(qcd, dict):
        applied_qcd: Dict[str, Any] = {}
        for ui_key, yaml_key in _QCD_CONFIG_KEY_MAP.items():
            if ui_key in qcd and qcd[ui_key] is not None:
                value = qcd[ui_key]
                # tmc: the UI offers AOT / GP / None. fitpack's theory code
                # branches are `tmc == False`, `tmc == 'AOT'`, `tmc == 'GP'` —
                # the string "None" matches none of them, so coerce it to the
                # Python False that `if tmc == False:` expects.
                if ui_key == "tmc" and isinstance(value, str) and value.strip().lower() in ("none", "off", "false", ""):
                    value = False
                conf[yaml_key] = value
                applied_qcd[yaml_key] = value
        if applied_qcd:
            applied_out["qcd_config"] = applied_qcd
            mutated = True

    # PDF param overrides → conf.params.pdf (existing entries only — don't invent new ones).
    pdf_overrides = advanced.get("pdf_params") or {}
    if isinstance(pdf_overrides, dict) and pdf_overrides:
        pdf_section = conf.setdefault("params", {}).setdefault("pdf", {})
        applied_pdf = []
        for name, override in pdf_overrides.items():
            if not isinstance(override, dict):
                continue
            target = pdf_section.get(name)
            if target is None:
                # Skip params the current default.yaml doesn't know about — the UI
                # may list flavors the current run doesn't use.
                continue
            for field in ("value", "min", "max", "fixed"):
                if field in override and override[field] is not None:
                    target[field] = override[field]
            applied_pdf.append(name)
        if applied_pdf:
            applied_out["pdf_params"] = applied_pdf
            mutated = True

    return mutated


def _apply_selected_datasets(config: Dict[str, Any], selected_datasets: Optional[Dict[str, Any]]) -> None:
    """
    Replace the datasets section in config with the user's selection.

    Args:
        config: The full YAML config dict (has 'conf' key).
        selected_datasets: Dict with keys:
            - observable: str (e.g. "idis", "dy")
            - selected_ids: list of dataset ID strings the user checked
    """
    if not selected_datasets:
        return

    observable = selected_datasets.get("observable", "")
    selected_ids = set(selected_datasets.get("selected_ids", []))
    if not observable or not selected_ids:
        return

    catalog = DATASET_CATALOG.get(observable, [])

    # Use user-provided filters if present, otherwise fall back to defaults
    user_filters = selected_datasets.get("filters")
    if user_filters is not None:
        filters = [f for f in user_filters if f.strip()]
    else:
        filters = DATASET_FILTERS.get(observable, [])

    xlsx_map = {}
    norm_map = {}
    for ds in catalog:
        if ds["id"] in selected_ids:
            xlsx_map[int(ds["id"])] = ds["xlsx"]
            if ds.get("norm"):
                norm_map[int(ds["id"])] = ds["norm"]

    # Build the datasets section — merge into existing (cumulative across steps)
    ds_conf: Dict[str, Any] = {}
    ds_conf["filters"] = filters  # always include, even if [] — obslib expects this key
    ds_conf["xlsx"] = xlsx_map
    ds_conf["norm"] = norm_map  # Always include norm (may be empty dict)

    config.setdefault("conf", {}).setdefault("datasets", {})[observable] = ds_conf


@mcp.tool()
def generate_random_tasks(
    count: int,
    chat_id: str,
    job_number: str = "000",
    selected_datasets: Optional[Dict[str, Any]] = None,
    step_index: int = 0,
    parent_batch_id: str = "",
    batch_id_override: Optional[str] = None,
    prior_paths: Optional[List[str]] = None,
    batch_label: str = "",
    observable: str = "",
) -> List[Dict[str, Any]]:
    """
    Generate multiple parameter sets by randomly sampling non-fixed PDF parameters
    within their [min, max] bounds and save each set as a YAML file in runs/.

    Args:
        count:             Number of random parameter sets to generate.
        chat_id:           Unique identifier for the current conversation.
        job_number:        Prefix label for the output directory names.
        selected_datasets: Optional dict with "observable" and "selected_ids"
                           from the dataset_select step. If None, uses whatever
                           is already configured in default.yaml (e.g. after
                           compose_dataset_config was called).
        step_index:        Which step in the multi-step chain (0=first, 1=second, ...).
        parent_batch_id:   Batch ID whose posterior was used as this step's prior.
        batch_id_override: If set, append new tasks to this existing batch instead
                           of creating a new one. Used by run_fitting_job's append
                           mode (eliminate-replenish flow).
        prior_paths:       Optional list of .msr file paths on HPC, one per
                           replica. When provided, each replica inherits the
                           parent replica's fitted parameters via --prior flag
                           instead of random initialization. Parameter values
                           in parameters.yaml are left at defaults since the
                           fitting code overrides them from the .msr prior.
        batch_label:       Canonical human-readable batch name authored by
                           DatasetSelector (`YYYY-MM-DD_HHmm_<job_number>`).
                           Persisted on every inserted task row so frontend,
                           LLM and DB share one display name. Empty in append
                           mode (existing label is reused for the batch).
    """
    _init_db()
    try:
        username = _require_hpc_username(chat_id)
    except ValueError as e:
        return [{"error": str(e)}]
    runs_dir = _runs_dir(username)
    ssh_mkdir(runs_dir, username=username)

    base_config = get_current_parameters(chat_id)
    if "error" in base_config:
        return [base_config]

    timestamp = int(time.time())
    batch_id = batch_id_override or f"batch_{timestamp}_{uuid.uuid4().hex[:4]}"
    results: List[Dict[str, Any]] = []

    for i in range(1, count + 1):
        rand_config = deepcopy(base_config)

        # Apply user-selected datasets
        _apply_selected_datasets(rand_config, selected_datasets)

        sampled: Dict[str, Any] = {}
        has_prior = prior_paths is not None and i <= len(prior_paths)

        if not has_prior:
            # Random initialization: sample non-fixed PDF params within bounds
            pdf_params = rand_config.get("conf", {}).get("params", {}).get("pdf", {})
            for name, info in pdf_params.items():
                if info.get("fixed", False):
                    continue
                lo, hi = info.get("min"), info.get("max")
                if lo is not None and hi is not None:
                    try:
                        info["value"] = random.uniform(float(lo), float(hi))
                    except (ValueError, TypeError):
                        logger.warning(f"Cannot sample {name}: invalid bounds min={lo} max={hi}")
                sampled[name] = {"value": info.get("value"), "min": lo, "max": hi}
        else:
            sampled = {"mode": "inherited", "prior": prior_paths[i - 1]}

        suffix = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
        dir_name = f"{job_number}_{suffix}"
        task_path_str = f"{runs_dir}/{dir_name}"
        ssh_mkdir(task_path_str, username=username)
        file_path_str = f"{task_path_str}/parameters.yaml"

        try:
            content = yaml.dump(rand_config, default_flow_style=False)
            ssh_write_file(file_path_str, content, username=username)

            # Write prior path file so submit_batch_tasks can pass --prior to SLURM
            if has_prior:
                ssh_write_file(f"{task_path_str}/prior.path", prior_paths[i - 1], username=username)
            _insert_task({
                "chat_id": chat_id,
                "batch_id": batch_id,
                "batch_label": batch_label,
                "file_id": dir_name,
                "path": task_path_str,
                "parameters_file": file_path_str,
                "step_index": step_index,
                "parent_batch_id": parent_batch_id,
                "hpc_username": username,
                "observable": observable or (selected_datasets or {}).get("observable", ""),
            })
            results.append({
                "task_index": i,
                "job_dir": task_path_str,
                "file_path": file_path_str,
                "sampled_values": sampled,
                "batch_id": batch_id,
                "batch_label": batch_label,
                "step_index": step_index,
                "parent_batch_id": parent_batch_id,
            })
            logger.info(f"Generated task {i}/{count} → {task_path_str}")
        except Exception as e:
            logger.error(f"Failed to save task {i}: {e}")
            results.append({"task_index": i, "error": str(e)})

        time.sleep(0.01)  # avoid timestamp collisions

    return results


@mcp.tool()
def generate_custom_tasks(
    count: int,
    chat_id: str,
    job_number: str = "000",
    custom_job_params: Optional[Dict[str, Dict[str, Any]]] = None,
    selected_datasets: Optional[Dict[str, Any]] = None,
    batch_label: str = "",
) -> List[Dict[str, Any]]:
    """
    Generate multiple parameter sets using user-provided custom values
    and save each set as a YAML file in runs/.

    Args:
        count:             Number of parameter sets to generate.
        chat_id:           Unique identifier for the current conversation.
        job_number:        Prefix label for the output directory names.
        custom_job_params: Mapping of job index (str) to param overrides.
                           e.g. {"0": {"g1 a": 0.5, "g1 b": 1.2}, "1": {...}}
        selected_datasets: Optional dict with "observable" and "selected_ids"
                           from the dataset_select step.
        batch_label:       Canonical human-readable batch name; see
                           generate_random_tasks for the contract.
    """
    _init_db()
    try:
        username = _require_hpc_username(chat_id)
    except ValueError as e:
        return [{"error": str(e)}]
    runs_dir = _runs_dir(username)
    ssh_mkdir(runs_dir, username=username)

    base_config = get_current_parameters(chat_id)
    if "error" in base_config:
        return [base_config]

    timestamp = int(time.time())
    batch_id = f"batch_{timestamp}_{uuid.uuid4().hex[:4]}"
    results: List[Dict[str, Any]] = []

    for i in range(1, count + 1):
        task_config = deepcopy(base_config)

        # Apply user-selected datasets
        _apply_selected_datasets(task_config, selected_datasets)

        applied: Dict[str, Any] = {}

        # Apply custom parameter values for this job
        job_overrides = (custom_job_params or {}).get(str(i - 1), {})
        pdf_params = task_config.get("conf", {}).get("params", {}).get("pdf", {})
        for param_name, new_value in job_overrides.items():
            if param_name in pdf_params:
                pdf_params[param_name]["value"] = new_value
                applied[param_name] = new_value

        suffix = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
        dir_name = f"{job_number}_{suffix}"
        task_path_str = f"{runs_dir}/{dir_name}"
        ssh_mkdir(task_path_str, username=username)
        file_path_str = f"{task_path_str}/parameters.yaml"

        try:
            content = yaml.dump(task_config, default_flow_style=False)
            ssh_write_file(file_path_str, content, username=username)
            _insert_task({
                "chat_id": chat_id,
                "batch_id": batch_id,
                "batch_label": batch_label,
                "file_id": dir_name,
                "path": task_path_str,
                "parameters_file": file_path_str,
                "hpc_username": username,
            })
            results.append({
                "task_index": i,
                "job_dir": task_path_str,
                "file_path": file_path_str,
                "applied_values": applied,
                "batch_id": batch_id,
                "batch_label": batch_label,
            })
            logger.info(f"Generated custom task {i}/{count} → {task_path_str}")
        except Exception as e:
            logger.error(f"Failed to save custom task {i}: {e}")
            results.append({"task_index": i, "error": str(e)})

        time.sleep(0.01)  # avoid timestamp collisions

    return results


# ---------------------------------------------------------------------------
# Tools: One-shot fitting job (compose + generate + submit)
# ---------------------------------------------------------------------------

@mcp.tool()
def run_fitting_job(
    observable: str,
    selected_ids: List[str],
    count: int,
    chat_id: str,
    job_number: str = "001",
    filters: Optional[List[str]] = None,
    fresh_start: bool = False,
    advanced: Optional[Dict[str, Any]] = None,
    append_to_batch_id: Optional[str] = None,
    extra_observables: Optional[List[Dict[str, Any]]] = None,
    batch_label: str = "",
) -> Dict[str, Any]:
    """
    One-shot tool: compose dataset config, generate random tasks, and submit to SLURM.

    Automatically detects chain state from the database:
    - If this is the first job in the chat (or fresh_start=True): clears datasets, starts step 0.
    - If there's a completed previous batch: computes batch prior, adds new observable
      on top of existing datasets (sequential fitting chain).

    Args:
        observable:       Observable key (e.g. "idis", "dy", "sidis").
        selected_ids:     List of dataset IDs to include. If empty, uses all
                          default_on datasets from the catalog for this observable.
        count:            Number of replica tasks to generate.
        chat_id:          Current chat/session ID.
        job_number:       Prefix for output directory names (default "001").
        filters:          Optional kinematic filters (e.g. ["Q2>1.69", "W2>10.0"]).
        fresh_start:      If True, ignore previous chain state and start from scratch.
        advanced:         Optional dict of advanced fitting overrides from the UI:
                          - pdf_params: {"flavor name": {value, min, max, fixed}, ...}
                            merged into conf.params.pdf
                          - qcd_config: {order, Q20, tmc, ht, dglapMode, alphaSmode, nuc}
                            merged into top-level conf
                          - tolerance:  numeric or string (e.g. "1e-6"), written to conf.ftol
                          - max_iterations: int, written to conf.maxiter and consumed by
                            maxlike_jam as scipy least_squares max_nfev
                          - optimizer: one of "trf" | "dogbox" | "lm", written to conf.method
                            and passed as scipy least_squares method=
                          - bootstrap: bool, written to conf.bootstrap (maxlike_jam already
                            reads this; toggling off disables per-replica resampling)
                          - chi2_ok, chi2_warn, good_fraction: floats, stashed into the
                            chain_state history entry for the next compute_batch_prior call
        append_to_batch_id: If set, append `count` new replicas to this existing batch
                          instead of creating a new one. Used by the eliminate-replenish
                          flow (autonomous.py) to top up a batch after cancelling
                          diverging tasks. In append mode, chain state is NOT advanced,
                          default.yaml is NOT modified, and the advanced/filters/
                          selected_ids arguments are IGNORED (the existing batch's
                          dataset config is reused as-is). Submission only picks up
                          the newly generated tasks thanks to the slurm_job_id guard
                          in submit_batch_tasks.
        batch_label:      Canonical display name for the batch, authored by the
                          DatasetSelector wizard at submit time
                          (`YYYY-MM-DD_HHmm_<job_number>`). Persisted on every
                          tasks row created for this batch and surfaced unchanged
                          to the frontend (FittingMonitor reads `tasks.batch_label`)
                          and to the LLM (auto_summary section). Empty string is
                          accepted (legacy / direct LLM calls); the frontend will
                          fall back to the batch_id prefix in that case.
        extra_observables: Optional list of additional observable selections to merge
                          into the SAME SLURM submission (used by the idis+DY demo
                          wizard). Each entry is
                          ``{"observable": str, "selected_ids": [str], "filters": [str]?}``.
                          RESMAN handles multi-reaction fits natively, so entries are
                          appended to the `compose_dataset_config` selection list and
                          fit alongside the primary observable in one job. Ignored in
                          append mode.
    """
    _init_db()

    # Validate the `advanced` dict against the typed schema (T1-B). Catches
    # frontend/backend drift loudly (unknown key → ValueError) instead of the
    # pre-T1-B silent ``.get()`` miss. Empty/None passes through.
    try:
        from advanced_schema import coerce_advanced
        advanced = coerce_advanced(advanced)
    except Exception as e:
        return {
            "status": "error",
            "phase": "validate",
            "message": f"Invalid `advanced` payload: {e}",
        }

    # Resolve HPC username for this chat. Every ssh_* call below threads
    # `username=hpc_user` explicitly — there is no process-level default.
    try:
        hpc_user = _require_hpc_username(chat_id)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    # Verify the selected HPC backend is alive before doing any work.
    if is_iri_enabled():
        if not is_iri_ready():
            return {
                "status": "error",
                "message": (
                    "IRI transport is not configured (missing token or resource ids). "
                    "Please reconnect/configure HPC and retry."
                ),
            }
    else:
        try:
            probe = ssh_run(["echo", "ok"], timeout=5, username=hpc_user)
            if probe.returncode != 0:
                return {
                    "status": "error",
                    "message": (
                        "HPC backend is not responding. "
                        "Please reconnect/configure HPC and retry."
                    ),
                }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Cannot reach HPC backend: {e}. Please reconnect/configure and retry.",
            }

    # ---- Append path (eliminate-replenish): reuse existing batch, skip config edits ----
    if append_to_batch_id:
        logger.info(
            f"Append mode: adding {count} replicas to existing batch {append_to_batch_id}"
        )
        # Appended replicas were generated with no batch_label at all: the
        # normal path passes one, this branch never did. A campaign therefore
        # split in two the moment it was extended — the original replicas kept
        # the name and the new ones had none. Relabelling from the caller
        # cannot fix it either, because a large append keeps inserting rows
        # long after the caller's HTTP request has timed out, so whatever it
        # relabels is only the prefix that existed at that instant.
        # The batch already knows its observable; a resource estimate made
        # without it silently falls back to the static table.
        match_observable = ""
        try:
            with _get_db() as _c:
                _r = _c.execute("SELECT observable FROM tasks WHERE batch_id=?"
                                " AND observable IS NOT NULL AND observable<>''"
                                " LIMIT 1", (append_to_batch_id,)).fetchone()
            if _r:
                match_observable = str(_r[0])
        except Exception:
            pass
        if not match_observable:
            match_observable = observable

        eff_label = (batch_label or "").strip()
        if not eff_label:
            try:
                with _get_db() as _c:
                    _row = _c.execute(
                        "SELECT batch_label FROM tasks WHERE batch_id=?"
                        " AND batch_label IS NOT NULL AND batch_label<>''"
                        " LIMIT 1", (append_to_batch_id,)).fetchone()
                if _row:
                    eff_label = str(_row[0])
            except Exception as _e:
                logger.warning("could not inherit batch_label for %s: %s"
                               % (append_to_batch_id, _e))
        elif eff_label:
            # An explicit rename covers the whole campaign, not just the
            # arrivals, and doing it here keeps it atomic with the insert.
            try:
                with _get_db() as _c:
                    _c.execute("UPDATE tasks SET batch_label=? WHERE batch_id=?",
                               (eff_label, append_to_batch_id))
            except Exception as _e:
                logger.warning("could not relabel batch %s: %s"
                               % (append_to_batch_id, _e))

        new_tasks = generate_random_tasks(
            count=count,
            chat_id=chat_id,
            job_number=job_number,
            batch_id_override=append_to_batch_id,
            batch_label=eff_label,
            observable=match_observable,
        )
        if not new_tasks or "error" in new_tasks[0]:
            return {"status": "error", "phase": "generate", "detail": new_tasks}

        resource_estimate = _estimate_slurm_resources(
            observable, len(selected_ids) or 1, 0,
            replica_count=count, hpc_username=hpc_user,
        )
        submit_result = submit_batch_tasks(
            append_to_batch_id,
            chat_id=chat_id,
            slurm_config=resource_estimate["slurm_config"],
        estimate=resource_estimate,
        )
        return {
            "status": "success",
            "mode": "append",
            "batch_id": append_to_batch_id,
            "appended_count": len(new_tasks),
            "tasks": [{"path": t.get("job_dir", ""), "file": t.get("file_path", "")} for t in new_tasks],
            "submit_result": submit_result,
        }

    # Auto-fill selected_ids from catalog defaults if empty
    if not selected_ids:
        catalog_datasets = DATASET_CATALOG.get(observable, [])
        selected_ids = [d["id"] for d in catalog_datasets if d.get("default_on", False)]
        if not selected_ids:
            return {"status": "error", "message": f"No default datasets found for observable '{observable}'. Provide selected_ids explicitly."}
        logger.info(f"Using {len(selected_ids)} default datasets for {observable}")

    remote_base = get_remote_base(hpc_user)
    remote_config = f"{remote_base}/inputs/default.yaml"
    remote_runs = f"{remote_base}/runs"

    # Resolve the observable's (stage, track) so chain continuation can stay
    # scoped to its own track. Stage A runs two parallel chains (alpha = PDF-ish
    # observables, beta = FF-ish observables); stage B/C observables have
    # track=None and fall back to single-chain semantics.
    obs_stage, obs_track = _OBS_LOCATION.get(observable, ("A", "alpha"))

    chain = _get_chain_state(chat_id)

    # ---- Stage B routing: α×β random pairing (JAM20-SIDIS methodology) ----
    # When the observable is a stage-B coupled fit (e.g. sidis), do not run
    # the single-track chain continuation path. Instead look up the latest
    # completed α and β batches, then delegate to assemble_paired_prior_tasks
    # which reads both parent posteriors, pairs replicas random-uniform, and
    # launches a joint fit with pdf + ff* both active. fresh_start is ignored
    # here because stage B has no "fresh" meaning — it inherently depends on
    # both parent tracks being finished.
    if obs_stage == "B" and not append_to_batch_id:
        history = chain.get("history", [])
        alpha_parent = _latest_completed_batch(history, "alpha")
        beta_parent = _latest_completed_batch(history, "beta")
        if not alpha_parent or not beta_parent:
            missing = []
            if not alpha_parent:
                missing.append("α (complete track α first — e.g. run IDIS, DY, ...)")
            if not beta_parent:
                missing.append("β (complete track β first — e.g. run SIA)")
            return {
                "status": "error",
                "message": (
                    f"Stage B requires both α and β batches completed. Missing: "
                    f"{', '.join(missing)}."
                ),
            }
        logger.info(
            f"Stage B routing for {observable}: α={alpha_parent}, β={beta_parent}, count={count}"
        )
        return assemble_paired_prior_tasks(
            alpha_batch_id=alpha_parent,
            beta_batch_id=beta_parent,
            chat_id=chat_id,
            observable=observable,
            count=count,
            job_number=job_number,
            selected_ids=list(selected_ids) if selected_ids else None,
            filters=list(filters) if filters else None,
            advanced=advanced,
            batch_label=batch_label,
        )

    parent_batch_id = ""
    step_index = 0
    track_selections = _latest_track_selections(chain.get("history", []), obs_track)

    if fresh_start:
        logger.info(f"Fresh start requested for chat {chat_id} (track={obs_track})")
    elif obs_track is not None:
        # Track-scoped parent lookup: scan history newest-first for the most
        # recent completed batch on the same track.
        for past in reversed(chain.get("history", []) or []):
            if not isinstance(past, dict):
                continue
            if past.get("track") != obs_track:
                continue
            cand = past.get("batch_id", "")
            if cand and _is_batch_completed(cand):
                parent_batch_id = cand
                step_index = int(past.get("step", -1)) + 1
                logger.info(
                    f"Continuing track {obs_track}: step {step_index}, parent={parent_batch_id}"
                )
                break
        if not parent_batch_id:
            logger.info(f"No completed parent on track {obs_track} — starting step 0")
    elif chain["active_batch_id"] and _is_batch_completed(chain["active_batch_id"]):
        # Stage B/C fallback: single-chain semantics preserved.
        parent_batch_id = chain["active_batch_id"]
        step_index = chain["current_step"] + 1
        logger.info(
            f"Continuing chain (stage {obs_stage}): step {step_index}, parent={parent_batch_id}"
        )
    else:
        logger.info(f"No completed previous batch (stage {obs_stage}) — starting step 0")

    # If continuing from previous batch, collect per-replica .msr files
    # for 1:1 parameter inheritance (instead of averaging via compute_batch_prior).
    parent_msr_paths: List[str] = []
    if parent_batch_id:
        parent_msr_paths = _collect_parent_msr_paths(parent_batch_id, username=hpc_user)
        if parent_msr_paths:
            # Adjust replica count to match available parent replicas
            if count > len(parent_msr_paths):
                logger.warning(
                    f"Requested {count} replicas but only {len(parent_msr_paths)} "
                    f"parent .msr files available. Using {len(parent_msr_paths)}."
                )
                count = len(parent_msr_paths)
            elif count < len(parent_msr_paths):
                # Take only the first `count` (could sort by chi2 later)
                parent_msr_paths = parent_msr_paths[:count]
            logger.info(
                f"Replica inheritance: {len(parent_msr_paths)} .msr files "
                f"from parent {parent_batch_id}"
            )
        else:
            logger.warning(
                f"No .msr files found in parent batch {parent_batch_id}. "
                f"Falling back to random initialization."
            )

    # Rebuild default.yaml:
    #   - Stage A (track is α or β): always clear existing datasets so switching
    #     α↔β does not carry over. Past same-track observables are replayed
    #     below via compose_dataset_config to reconstruct the overlay chain.
    #   - Stage B/C (track=None): keep existing datasets. These stages depend
    #     on the accumulated state from stage A; clearing would wipe it. Until
    #     stage B gets a proper α/β merge tool, we preserve the old behavior.
    #   - fresh_start=True: always clear regardless of track.
    advanced_applied: Dict[str, Any] = {}
    try:
        content = ssh_read_file(remote_config, username=hpc_user)
        config = yaml.safe_load(content) or {}
        conf_mutated = False

        if fresh_start or obs_track is not None:
            config.setdefault("conf", {})["datasets"] = {}
            conf_mutated = True
            logger.info(
                f"Cleared datasets (fresh_start={fresh_start}, track={obs_track}, step={step_index}, user={hpc_user})"
            )
        else:
            logger.info(
                f"Keeping existing datasets (stage={obs_stage}, step={step_index}, user={hpc_user})"
            )

        if advanced:
            conf_mutated |= _apply_advanced_overrides(config, advanced, advanced_applied)
            if advanced_applied:
                logger.info(f"Applied advanced overrides: {sorted(advanced_applied.keys())}")

        if conf_mutated:
            ssh_write_file(remote_config, yaml.dump(config, default_flow_style=False), username=hpc_user)
    except Exception as e:
        logger.warning(f"Could not manage datasets: {e}")

    # Assemble the track's replay list.  For observables different from the
    # current one, replay them verbatim.  For the *same* observable appearing
    # in prior steps (beta-track pattern: sia(Pi)->sia(K)->sia(h)), merge
    # prior selected_ids into the current selection so datasets accumulate.
    # Only replay when we have a completed same-track parent — if no prior
    # batch on this track has finished, we start from the default priors and
    # do not carry over in-flight datasets.
    replay_selections: List[Dict[str, Any]] = []
    same_obs_prior_ids: List[str] = []
    same_obs_prior_filters: List[str] = []
    if not fresh_start and parent_batch_id:
        for past_sel in track_selections:
            if past_sel["observable"] == observable:
                # Same observable: collect accumulated prior IDs to merge
                same_obs_prior_ids = past_sel.get("selected_ids", [])
                same_obs_prior_filters = past_sel.get("filters", [])
            elif past_sel["selected_ids"]:
                replay_selections.append(past_sel)
            else:
                # Legacy entry without stored selected_ids → rehydrate from catalog.
                legacy_defaults = [
                    d["id"] for d in DATASET_CATALOG.get(past_sel["observable"], [])
                    if d.get("default_on", False)
                ]
                if legacy_defaults:
                    replay_selections.append({
                        "observable": past_sel["observable"],
                        "selected_ids": legacy_defaults,
                        "filters": past_sel["filters"],
                    })

    # Merge prior same-observable IDs (if any) with current selection.
    # dict.fromkeys preserves insertion order and deduplicates.
    merged_ids = list(dict.fromkeys(same_obs_prior_ids + list(selected_ids)))
    obs_selection = {"observable": observable, "selected_ids": merged_ids}
    if filters:
        obs_selection["filters"] = filters
    elif same_obs_prior_filters:
        obs_selection["filters"] = same_obs_prior_filters

    # Demo (branch demo/idis-dy): extra observables fit in the SAME job.
    extra_selections: List[Dict[str, Any]] = []
    if extra_observables:
        for extra in extra_observables:
            if not isinstance(extra, dict):
                continue
            ex_obs = extra.get("observable")
            ex_ids = extra.get("selected_ids") or []
            if not ex_obs or not ex_ids:
                continue
            ex_sel: Dict[str, Any] = {
                "observable": ex_obs,
                "selected_ids": list(ex_ids),
            }
            ex_filters = extra.get("filters")
            if ex_filters:
                ex_sel["filters"] = list(ex_filters)
            extra_selections.append(ex_sel)

    compose_dataset_config(
        replay_selections + [obs_selection] + extra_selections,
        chat_id=chat_id,
    )

    # Step 2: Generate tasks (inherited from parent .msr or random)
    tasks = generate_random_tasks(
        count=count,
        chat_id=chat_id,
        job_number=job_number,
        step_index=step_index,
        parent_batch_id=parent_batch_id,
        prior_paths=parent_msr_paths or None,
        batch_label=batch_label,
        observable=observable,
    )
    if not tasks or "error" in tasks[0]:
        return {"status": "error", "phase": "generate", "detail": tasks}

    batch_id = tasks[0].get("batch_id", "")

    # Step 3: Estimate resources and submit to SLURM.
    # With multi-observable demo submissions, size the resources on the total
    # dataset count across the primary and any extras (all fit in the same job).
    total_dataset_count = len(selected_ids) + sum(
        len(e.get("selected_ids", [])) for e in extra_selections
    )
    resource_estimate = _estimate_slurm_resources(
        observable, total_dataset_count, step_index,
        replica_count=count, hpc_username=hpc_user,
    )
    submit_result = submit_batch_tasks(
        batch_id,
        chat_id=chat_id,
        slurm_config=resource_estimate["slurm_config"],
        estimate=resource_estimate,
    )

    # Step 4: Update chain state. selected_ids/filters are persisted so that
    # future run_fitting_job calls on the same track can replay the dataset
    # overlay chain deterministically (see _latest_track_selections). The
    # `paired_parents` field is only populated by the stage B routing path
    # (see assemble_paired_prior_tasks); stage A entries leave it empty.
    history = chain.get("history", [])
    entry: Dict[str, Any] = {
        "step": step_index,
        "batch_id": batch_id,
        "observable": observable,
        "stage": obs_stage,
        "track": obs_track,
        "datasets_count": len(selected_ids),
        "selected_ids": list(selected_ids),
        "filters": list(filters) if filters else [],
        "parent_batch_id": parent_batch_id,
        "paired_parents": {},
        "timestamp": int(time.time()),
    }
    if advanced:
        # Stash the full advanced dict so compute_batch_prior (and diagnostics) can
        # honor the user's chi2 thresholds / good_fraction / max_iterations / etc.
        entry["advanced"] = advanced
    if advanced_applied:
        entry["advanced_applied"] = advanced_applied
    if extra_selections:
        entry["extra_observables"] = extra_selections
    history.append(entry)
    _update_chain_state(chat_id, step_index, batch_id, observable, history)

    return {
        "status": "success",
        "batch_id": batch_id,
        "observable": observable,
        "datasets_count": total_dataset_count,
        "replicas": count,
        "step_index": step_index,
        "parent_batch_id": parent_batch_id,
        "chain_history": [f"Step {h['step']}: {h['observable']}" for h in history],
        "tasks": [{"path": t.get("job_dir", ""), "file": t.get("file_path", "")} for t in tasks],
        "submit_result": submit_result,
        "resource_estimate": resource_estimate,
        "advanced_applied": advanced_applied or None,
        "extra_observables": [e["observable"] for e in extra_selections] or None,
    }


# ---------------------------------------------------------------------------
# Tools: Backfill job history from existing completed tasks
# ---------------------------------------------------------------------------

@mcp.tool()
def backfill_job_history(chat_id: str = "") -> Dict[str, Any]:
    """
    Scan all completed tasks and record their runtime/memory data to job_history.
    Run this once to populate historical data from existing completed tasks.

    Args:
        chat_id: Current chat/session ID. If provided, the caller's HPC
                 username is used for tasks that have no ``hpc_username``
                 stored in the tasks table (legacy rows). If empty, each
                 task's own ``hpc_username`` column is used.
    """
    _init_db()

    chat_user = _get_hpc_username(chat_id) if chat_id else ""

    try:
        with _get_db() as conn:
            conn.row_factory = sqlite3.Row
            tasks = conn.execute("""
                SELECT path, batch_id, slurm_job_id, hpc_username FROM tasks
                WHERE status = 'completed' AND slurm_job_id IS NOT NULL
            """).fetchall()

        recorded = 0
        skipped = 0
        errors = 0
        for task in tasks:
            task_user = (task["hpc_username"] or "") or chat_user
            if not task_user:
                errors += 1
                logger.warning(
                    f"Backfill skipped {task['path']}: no hpc_username on row "
                    "and no chat_id provided"
                )
                continue
            # Skip if already recorded
            with _get_db() as conn:
                existing = conn.execute(
                    "SELECT 1 FROM job_history WHERE task_path = ?",
                    (task["path"],),
                ).fetchone()
                if existing:
                    skipped += 1
                    continue
            try:
                _record_job_history(
                    task["path"], task["batch_id"], task["slurm_job_id"], task_user
                )
                recorded += 1
            except Exception as e:
                errors += 1
                logger.warning(f"Backfill failed for {task['path']}: {e}")

        return {
            "status": "success",
            "recorded": recorded,
            "skipped": skipped,
            "errors": errors,
            "total_completed": len(tasks),
        }

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tools: Physics analysis roadmap
# ---------------------------------------------------------------------------

# Roadmap data structure (literature-verified, see MVP_DEFINITION_ZH.md §4.1)
_ROADMAP = {
    "stages": {
        "A": {
            "name": "Independent Basis",
            "description": "Unpolarized PDFs (Track alpha) and Fragmentation Functions (Track beta) fitted independently.",
            "tracks": {
                "alpha": {
                    "name": "Unpolarized PDFs",
                    "target": "q(x,Q²), g(x,Q²)",
                    "required_first": "idis",
                    "required_first_reason": "DIS is the cleanest PDF constraint; largest dataset count; all major analyses (CT18, NNPDF4.0, JAM24) begin here.",
                    "available": ["idis", "dy", "wasym", "zrap", "wzrv", "wc", "jet"],
                    "ordering": "free",
                    "ordering_note": "After IDIS, all other observables have no mutual dependencies. User chooses what to overlay next. Each step adds new data on top of the previous posterior.",
                },
                "beta": {
                    "name": "Fragmentation Functions",
                    "target": "D_pi(z), D_K(z), D_h(z)",
                    "sequence": ["sia"],
                    "start": "sia",
                    "start_reason": "SIA is the only FF constraint that does not require PDFs (NNFF1.0).",
                    "sub_steps": {
                        "sia": [
                            {"name": "Pi", "label": "FFpi (pion)", "id_prefix": "1",
                             "description": "Pion fragmentation functions from SIA pion datasets (1XXX)"},
                            {"name": "K", "label": "FFK (kaon)", "id_prefix": "2",
                             "description": "Kaon fragmentation functions from SIA kaon datasets (2XXX)"},
                            {"name": "h", "label": "FFh (hadron)", "id_prefix": "4",
                             "description": "Unidentified hadron FFs from SIA hadron datasets (4XXX)"},
                        ],
                    },
                    "ordering": "sequential",
                    "ordering_note": "Pi first (most data), then K, then h. Each step adds datasets on top of the previous posterior.",
                },
            },
            "parallel": True,
        },
        "B": {
            "name": "Coupled Fitting",
            "description": "SIDIS: cross-section = PDF(x) x FF(z). Requires both Track alpha and Track beta complete.",
            "requires": ["A.alpha", "A.beta"],
            "sequence": ["sidis"],
            "merge_method": "random_pairing",
            "sub_steps": {
                "sidis": [
                    {"name": "Pi", "label": "SIDIS pi", "description": "Pion SIDIS — couples PDFs with FFpi"},
                    {"name": "K", "label": "SIDIS K", "description": "Kaon SIDIS — adds FFK coupling"},
                    {"name": "h", "label": "SIDIS h", "description": "Hadron SIDIS — adds FFh coupling"},
                ],
            },
        },
        "C": {
            "name": "Triple Coupling",
            "description": "Polarized PDFs coupled with unpol PDFs and FFs. Requires Stage B complete.",
            "requires": ["B"],
            "sequence": ["pidis", "psidis", "pjet"],
        },
    },
    "overlay_rule": "Each step uses the previous step's posterior as the next step's prior. Datasets accumulate (added, never removed between steps).",
}

# Observable → (stage, track) mapping
_OBS_LOCATION = {}
for _obs in _ROADMAP["stages"]["A"]["tracks"]["alpha"]["available"]:
    _OBS_LOCATION[_obs] = ("A", "alpha")
for _obs in _ROADMAP["stages"]["A"]["tracks"]["beta"]["sequence"]:
    _OBS_LOCATION[_obs] = ("A", "beta")
for _obs in _ROADMAP["stages"]["B"]["sequence"]:
    _OBS_LOCATION[_obs] = ("B", None)
for _obs in _ROADMAP["stages"]["C"]["sequence"]:
    _OBS_LOCATION[_obs] = ("C", None)


@mcp.tool()
def get_analysis_roadmap(chat_id: str = "") -> Dict[str, Any]:
    """
    Get the physics analysis roadmap with current progress for this chat.

    Returns the full Stage A→B→C structure, which track/observable has been
    completed, what the next recommended step is, and whether stage transitions
    are available.

    Args:
        chat_id: Current chat/session ID. If provided, includes chain progress.
    """
    _init_db()

    # Get chain state for progress
    completed_observables = []
    chain_history = []
    if chat_id:
        chain = _get_chain_state(chat_id)
        chain_history = chain.get("history", [])
        for entry in chain_history:
            if isinstance(entry, dict):
                obs = entry.get("observable", "")
                if obs:
                    completed_observables.append(obs)

    completed_set = set(o.lower() for o in completed_observables)

    # Count completed sub-steps per (track, observable) from chain history.
    # An observable like "sia" may appear 3 times (Pi, K, h) — each history
    # entry with a completed batch counts as one sub-step.
    def _count_completed_steps(history: list, track: Optional[str]) -> int:
        count = 0
        for entry in history:
            if not isinstance(entry, dict):
                continue
            if track is not None and entry.get("track") != track:
                continue
            bid = entry.get("batch_id", "")
            if bid and _is_batch_completed(bid):
                count += 1
        return count

    def _sub_step_progress(stage_key: str, track: Optional[str] = None) -> Dict[str, Any]:
        """Return sub-step progress for an observable that has sub_steps defined."""
        if track is not None:
            sub_steps_map = _ROADMAP["stages"]["A"]["tracks"].get(
                track, {}
            ).get("sub_steps", {})
        else:
            sub_steps_map = _ROADMAP["stages"].get(stage_key, {}).get("sub_steps", {})
        result = {}
        for obs, steps in sub_steps_map.items():
            # Count how many times this observable appears completed on this track
            done_count = 0
            for entry in chain_history:
                if not isinstance(entry, dict):
                    continue
                if entry.get("observable") != obs:
                    continue
                if track is not None and entry.get("track") != track:
                    continue
                bid = entry.get("batch_id", "")
                if bid and _is_batch_completed(bid):
                    done_count += 1
            total = len(steps)
            done_steps = steps[:done_count]
            remaining_steps = steps[done_count:]
            result[obs] = {
                "total": total,
                "completed": done_count,
                "done": [s["name"] for s in done_steps],
                "remaining": [s["name"] for s in remaining_steps],
                "next": remaining_steps[0] if remaining_steps else None,
                "complete": done_count >= total,
            }
        return result

    # Determine track progress
    alpha_seq = _ROADMAP["stages"]["A"]["tracks"]["alpha"]["available"]
    beta_seq = _ROADMAP["stages"]["A"]["tracks"]["beta"]["sequence"]
    beta_sub_steps = _ROADMAP["stages"]["A"]["tracks"]["beta"].get("sub_steps", {})

    alpha_done = [o for o in alpha_seq if o in completed_set]
    alpha_remaining = [o for o in alpha_seq if o not in completed_set]

    # Beta track: completion is based on sub-steps, not just observable name.
    # "sia" has 3 sub-steps (Pi, K, h); beta is complete only when all 3 are done.
    beta_progress = _sub_step_progress("A", track="beta")
    if beta_sub_steps:
        beta_complete = all(p["complete"] for p in beta_progress.values())
        beta_done_names = []
        beta_remaining_names = []
        for obs, prog in beta_progress.items():
            beta_done_names.extend(f"{obs}({n})" for n in prog["done"])
            beta_remaining_names.extend(f"{obs}({n})" for n in prog["remaining"])
    else:
        beta_done = [o for o in beta_seq if o in completed_set]
        beta_remaining = [o for o in beta_seq if o not in completed_set]
        beta_complete = len(beta_remaining) == 0 and len(beta_done) > 0
        beta_done_names = beta_done
        beta_remaining_names = beta_remaining

    alpha_complete = len(alpha_remaining) == 0 and len(alpha_done) > 0

    # Stage B sub-step progress
    sidis_sub_steps = _ROADMAP["stages"]["B"].get("sub_steps", {})
    sidis_progress = _sub_step_progress("B")
    if sidis_sub_steps:
        sidis_done = all(p["complete"] for p in sidis_progress.values())
    else:
        sidis_done = "sidis" in completed_set

    stage_c_done = [o for o in _ROADMAP["stages"]["C"]["sequence"] if o in completed_set]
    stage_c_remaining = [o for o in _ROADMAP["stages"]["C"]["sequence"] if o not in completed_set]

    # Determine current stage
    if stage_c_done:
        current_stage = "C"
    elif sidis_done:
        current_stage = "B_complete"
    elif alpha_complete and beta_complete:
        current_stage = "ready_for_B"
    elif alpha_done or beta_done_names:
        current_stage = "A"
    else:
        current_stage = "not_started"

    # Build recommendations
    recommendations = []
    if current_stage == "not_started":
        recommendations.append({
            "action": "start_track_alpha",
            "observable": "idis",
            "reason": _ROADMAP["stages"]["A"]["tracks"]["alpha"]["required_first_reason"],
        })
        recommendations.append({
            "action": "start_track_beta",
            "observable": "sia",
            "sub_step": "Pi",
            "reason": _ROADMAP["stages"]["A"]["tracks"]["beta"]["start_reason"],
        })
    elif current_stage == "A":
        if alpha_remaining:
            recommendations.append({
                "action": "continue_track_alpha",
                "available_observables": alpha_remaining,
                "reason": f"Track alpha has {len(alpha_remaining)} remaining observables. User chooses which to overlay next on top of {', '.join(alpha_done)}.",
            })
        if not beta_done_names:
            recommendations.append({
                "action": "start_track_beta",
                "observable": "sia",
                "sub_step": "Pi",
                "reason": "Track beta (FFs) can run in parallel with Track alpha.",
            })
        elif beta_remaining_names:
            # Find the next sub-step
            for obs, prog in beta_progress.items():
                if prog["next"]:
                    recommendations.append({
                        "action": "continue_track_beta",
                        "observable": obs,
                        "sub_step": prog["next"]["name"],
                        "description": prog["next"]["description"],
                        "reason": f"Track beta: {prog['completed']}/{prog['total']} sub-steps done ({', '.join(prog['done'])}). Next: {prog['next']['name']}.",
                    })
                    break
    elif current_stage == "ready_for_B":
        sidis_next = None
        for obs, prog in sidis_progress.items():
            if prog["next"]:
                sidis_next = prog["next"]
                break
        recommendations.append({
            "action": "enter_stage_B",
            "observable": "sidis",
            "sub_step": sidis_next["name"] if sidis_next else "Pi",
            "reason": (
                "Both Track alpha and Track beta are complete. Stage B couples them via "
                "random pairing: for each new replica, draw one alpha PDF replica and one beta FF "
                "replica, then jointly fit both on alpha+beta+SIDIS datasets. Call run_fitting_job "
                "with observable='sidis' — it auto-detects stage B and routes to the pairing "
                "tool internally, so no separate compose/prior/generate calls are needed."
            ),
            "merge_method": "random_pairing (JAM20-SIDIS, arXiv:2101.04664)",
        })
    elif current_stage == "B_complete":
        if stage_c_remaining:
            recommendations.append({
                "action": "enter_stage_C",
                "observable": stage_c_remaining[0],
                "reason": "Stage B (SIDIS) complete. Stage C adds polarized PDFs.",
            })
    elif current_stage == "C":
        if stage_c_remaining:
            recommendations.append({
                "action": "continue_stage_C",
                "observable": stage_c_remaining[0],
                "reason": f"Next polarized observable. Completed so far: {', '.join(stage_c_done)}.",
            })
        else:
            recommendations.append({
                "action": "analysis_complete",
                "reason": "All stages complete. Archive final results.",
            })

    return {
        "roadmap": _ROADMAP["stages"],
        "overlay_rule": _ROADMAP["overlay_rule"],
        "progress": {
            "current_stage": current_stage,
            "completed_observables": completed_observables,
            "track_alpha": {
                "done": alpha_done,
                "remaining": alpha_remaining,
                "complete": alpha_complete,
            },
            "track_beta": {
                "done": beta_done_names,
                "remaining": beta_remaining_names,
                "complete": beta_complete,
                "sub_step_progress": beta_progress,
            },
            "stage_B": {
                "done": sidis_done,
                "sub_step_progress": sidis_progress,
            },
            "stage_C": {
                "done": stage_c_done,
                "remaining": stage_c_remaining,
            },
        },
        "chain_history": chain_history,
        "track_batches": {
            "alpha": next(
                (e["batch_id"] for e in reversed(chain_history)
                 if isinstance(e, dict) and e.get("track") == "alpha"),
                None,
            ),
            "beta": next(
                (e["batch_id"] for e in reversed(chain_history)
                 if isinstance(e, dict) and e.get("track") == "beta"),
                None,
            ),
        },
        "recommendations": recommendations,
    }


# ---------------------------------------------------------------------------
# Tools: Reset chain (start fresh)
# ---------------------------------------------------------------------------

@mcp.tool()
def reset_fitting_chain(chat_id: str) -> Dict[str, Any]:
    """
    Reset the fitting chain for this chat. Next run_fitting_job will start fresh
    (step 0, clear all datasets). Use when the user wants to abandon the current
    chain and start over.

    Args:
        chat_id: The chat/session ID to reset.
    """
    _init_db()
    _update_chain_state(chat_id, 0, "", "", [])
    return {"status": "success", "message": f"Chain reset for chat {chat_id}. Next job will start from step 0."}


# ---------------------------------------------------------------------------
# Tools: MVP-1 post-analysis (drop / redo) — see docs/DEV_PLAN.md PR-4
# ---------------------------------------------------------------------------
# These tools mirror the HTTP endpoints in open-webui/routers/hpc.py so the
# LLM can invoke them via MCP. DB access is shared (same SQLite file), so
# either side's writes are visible to the other.

_MVP1_ENABLED = frozenset({"idis", "dy"})


def _latest_effective_batch_for(chat_id: str, reaction: str) -> Optional[str]:
    """Walk chain_state.history in reverse, return the newest batch_id whose
    effective entry mentions `reaction` (as primary or extra)."""
    chain = _get_chain_state(chat_id)
    target = reaction.lower()
    for entry in reversed(chain.get("history", [])):
        if not isinstance(entry, dict):
            continue
        if entry.get("effective") is False:
            continue
        primary = (entry.get("observable") or "").lower()
        extras = [
            (e.get("observable") or "").lower()
            for e in entry.get("extra_observables", []) or []
            if isinstance(e, dict)
        ]
        if primary == target or target in extras:
            return entry.get("batch_id")
    return None


@mcp.tool()
def preview_drop_replicas(chat_id: str, reaction: str, threshold: float) -> Dict[str, Any]:
    """Preview how many replicas would be dropped at a given χ²/N threshold.

    Does NOT mutate anything. Call this after the user gives a threshold in
    the `drop bad replicas <r>` dialog (see drop_redo_flow prompt section).

    Args:
        chat_id:   chat/session ID.
        reaction:  'idis' or 'dy' (MVP-1 scope).
        threshold: χ²/N cutoff — replicas with final_chi2_npts above this are
                   flagged as "bad". Typical values: 2.0 strict / 3.0 standard
                   / 5.0 loose.

    Returns:
        {n_good, n_bad, n_total, batch_id, bad_paths[]}
    """
    _init_db()
    r = (reaction or "").lower().strip()
    if r not in _MVP1_ENABLED:
        return {"status": "error", "error": f"reaction '{r}' not enabled in MVP-1 (only idis/dy)"}
    if not (0.1 < float(threshold) < 1000.0):
        return {"status": "error", "error": "threshold must be in (0.1, 1000)"}

    batch_id = _latest_effective_batch_for(chat_id, r)
    if not batch_id:
        return {"status": "error", "error": f"no effective batch for {r} in this chat"}

    try:
        with _get_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT task_path, final_chi2_npts FROM job_history "
                "WHERE batch_id = ? AND final_chi2_npts IS NOT NULL",
                (batch_id,),
            ).fetchall()
        bad = [r["task_path"] for r in rows if r["final_chi2_npts"] > float(threshold)]
        good = [r["task_path"] for r in rows if r["final_chi2_npts"] <= float(threshold)]
        return {
            "status": "success",
            "reaction": r,
            "batch_id": batch_id,
            "threshold": float(threshold),
            "n_good": len(good),
            "n_bad": len(bad),
            "n_total": len(rows),
            "bad_paths": bad[:20],
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@mcp.tool()
def apply_drop_replicas(
    chat_id: str, reaction: str, threshold: float, mode: str
) -> Dict[str, Any]:
    """Apply the user's pick from the drop dialog: delete / run_more / redo.

    Mutates DB. Call exactly once after the user picks one of the three modes
    in the `drop bad replicas` dialog.

    - mode='delete':  mark bad tasks status='dropped'; reaction stays done.
    - mode='run_more': same drop step; response includes composer_hint so the
                      caller knows to open dataset_selector next.
    - mode='redo':    full wipe — delegates to redo_reaction(chat_id, r).

    Args:
        chat_id, reaction, threshold: same as preview_drop_replicas.
        mode: one of 'delete' / 'run_more' / 'redo'.
    """
    _init_db()
    r = (reaction or "").lower().strip()
    m = (mode or "").lower().strip()
    if r not in _MVP1_ENABLED:
        return {"status": "error", "error": f"reaction '{r}' not enabled in MVP-1"}
    if m not in {"delete", "run_more", "redo"}:
        return {"status": "error", "error": "mode must be one of delete / run_more / redo"}

    if m == "redo":
        return redo_reaction(chat_id, r)

    batch_id = _latest_effective_batch_for(chat_id, r)
    if not batch_id:
        return {"status": "error", "error": f"no effective batch for {r}"}

    try:
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE tasks SET status = 'dropped' "
                "WHERE batch_id = ? AND status = 'completed' AND path IN ("
                "  SELECT task_path FROM job_history "
                "  WHERE batch_id = ? AND final_chi2_npts > ?"
                ")",
                (batch_id, batch_id, float(threshold)),
            )
            n_dropped = cur.rowcount
            conn.commit()
    except Exception as e:
        return {"status": "error", "error": str(e)}

    return {
        "status": "success",
        "mode": m,
        "reaction": r,
        "batch_id": batch_id,
        "threshold": float(threshold),
        "n_dropped": n_dropped,
        "composer_hint": "open_run_more" if m == "run_more" else None,
    }


@mcp.tool()
def redo_reaction(chat_id: str, reaction: str) -> Dict[str, Any]:
    """Wipe a reaction's history (reaction='idis'|'dy'|'all').

    Flips `effective=False` on matching chain_state.history entries. A joint
    batch (e.g. idis+dy together) gets wiped as a whole if either reaction is
    named — drop → delete is the tool for keeping survivors.

    Args:
        chat_id: chat/session ID.
        reaction: 'idis', 'dy', or 'all'.
    """
    _init_db()
    target = (reaction or "").lower().strip()
    if target != "all" and target not in _MVP1_ENABLED:
        return {"status": "error", "error": f"reaction '{target}' not supported in MVP-1 (idis/dy/all)"}

    wipe_all = target == "all"
    wiped_reactions: set[str] = set()
    touched = 0

    chain = _get_chain_state(chat_id)
    history = chain.get("history", [])
    for entry in history:
        if not isinstance(entry, dict):
            continue
        if entry.get("effective") is False:
            continue
        primary = (entry.get("observable") or "").lower()
        extras = [
            (e.get("observable") or "").lower()
            for e in entry.get("extra_observables", []) or []
            if isinstance(e, dict)
        ]
        if wipe_all or primary == target or target in extras:
            entry["effective"] = False
            touched += 1
            if primary:
                wiped_reactions.add(primary)
            wiped_reactions.update(extras)

    # Persist back via the standard helper — step/active values are
    # meaningless after a wipe; preserve the last known primary.
    _update_chain_state(
        chat_id,
        chain.get("current_step", 0),
        chain.get("active_batch_id", ""),
        chain.get("active_observable", ""),
        history,
    )

    return {
        "status": "success",
        "mode": "all" if wipe_all else target,
        "wiped_reactions": sorted(wiped_reactions),
        "touched_entries": touched,
    }


# ---------------------------------------------------------------------------
# Resource estimation
# ---------------------------------------------------------------------------

# Empirical baselines (from historical runs on JLab HPC):
# IDIS-only (15 datasets, 2713 npts): ~130 min, 4 cpus, ~512 MB
# DY-only (4 datasets, 396 npts):     ~443 min, 4 cpus, ~512 MB (DY grids are slow to load)
# IDIS+DY+SIDIS combined:             ~500 min, 4 cpus, ~1 GB
_OBSERVABLE_COMPLEXITY = {
    "idis":  {"base_time_min": 150, "mem_mb": 512, "cpus": 4, "grid_load_min": 5},
    "pidis": {"base_time_min": 150, "mem_mb": 512, "cpus": 4, "grid_load_min": 5},
    "dy":    {"base_time_min": 480, "mem_mb": 512, "cpus": 4, "grid_load_min": 60},
    "sia":   {"base_time_min": 120, "mem_mb": 512, "cpus": 4, "grid_load_min": 10},
    "sidis": {"base_time_min": 200, "mem_mb": 768, "cpus": 4, "grid_load_min": 20},
    "zrap":  {"base_time_min": 300, "mem_mb": 512, "cpus": 4, "grid_load_min": 30},
    "wasym": {"base_time_min": 300, "mem_mb": 512, "cpus": 4, "grid_load_min": 30},
}

# ---------------------------------------------------------------------------
# LLM-based resource advisor (AMSC Haiku)
# ---------------------------------------------------------------------------

_LLM_API_BASE = os.environ.get("OPENAI_API_BASE_URL", "").rstrip("/")
_LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")
_LLM_MODEL = os.environ.get("RESOURCE_LLM_MODEL", "claude-haiku")


def _llm_resource_advisor(
    observable: str,
    n_datasets: int,
    step_index: int,
    replica_count: int,
    cluster_snapshot: Dict[str, Any],
    user_usage: Dict[str, Any],
    history_summary: Optional[Dict[str, Any]],
    baseline: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Ask an LLM (Haiku via AMSC proxy) for SLURM resource allocation advice.

    Sends a structured prompt with cluster state, historical job data, and
    observable characteristics. Expects a JSON response with recommended
    cpus, time_minutes, mem_mb, and partition.

    Returns None on any failure (timeout, parse error, API down) so the
    caller can fall back to heuristic estimation.
    """
    if not _LLM_API_BASE or not _LLM_API_KEY:
        return None

    import requests as _requests

    prompt = json.dumps({
        "task": "SLURM resource allocation for QCD PDF/FF fitting jobs",
        "observable": observable,
        "observable_baseline": baseline,
        "n_datasets": n_datasets,
        "step_index": step_index,
        "replica_count": replica_count,
        "cluster": cluster_snapshot,
        "user_current_usage": user_usage,
        "history": history_summary,
    }, indent=2)

    messages = [
        {
            "role": "system",
            "content": (
                "You are a SLURM resource allocation advisor for physics fitting jobs on an HPC cluster. "
                "Given the observable type, dataset count, cluster state, user's current resource usage, "
                "and historical job performance, recommend optimal SLURM parameters.\n\n"
                "Rules:\n"
                "- Each replica is one independent SLURM job.\n"
                "- cpus: per-replica CPU count (min 2, max 16). More CPUs = faster single replica, "
                "but fewer can run concurrently. Balance throughput vs speed.\n"
                "- time_minutes: wall-clock limit. Must cover the longest expected replica with safety margin. "
                "Too short = killed jobs. Too long = wasted allocation.\n"
                "- mem_mb: per-replica memory. Must not exceed 90% of node memory.\n"
                "- partition: pick the partition with the most idle resources.\n"
                "- If cluster is mostly idle (>60% CPUs free), boost cpus for speed.\n"
                "- If cluster is busy (<30% free) or user already holds many CPUs, reduce cpus "
                "so more replicas can start immediately.\n"
                "- With historical data, use actual runtime/memory (with 30% margin). "
                "Without history, use baseline values (with 50% margin).\n\n"
                "Respond ONLY with a JSON object, no markdown, no explanation:\n"
                '{"cpus": <int>, "time_minutes": <int>, "mem_mb": <int>, "partition": "<str>", '
                '"reason": "<short explanation>"}'
            ),
        },
        {
            "role": "user",
            "content": prompt,
        },
    ]

    try:
        resp = _requests.post(
            f"{_LLM_API_BASE}/v1/chat/completions",
            json={
                "model": _LLM_MODEL,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 256,
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_LLM_API_KEY}",
            },
            timeout=10,
        )
        resp.raise_for_status()

        content = resp.json()["choices"][0]["message"]["content"].strip()
        # Strip markdown fences if present
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

        advice = json.loads(content)

        # Validate required fields
        cpus = int(advice.get("cpus", 0))
        time_min = int(advice.get("time_minutes", 0))
        mem_mb = int(advice.get("mem_mb", 0))
        partition = str(advice.get("partition", "production"))
        reason = str(advice.get("reason", ""))

        if cpus < 2 or cpus > 16 or time_min < 10 or mem_mb < 256:
            logger.warning(f"LLM advice out of bounds: {advice}")
            return None

        logger.info(f"LLM resource advice: {cpus}CPUs, {time_min}min, {mem_mb}MB, {partition} — {reason}")
        return {
            "cpus": cpus,
            "time_minutes": time_min,
            "mem_mb": mem_mb,
            "partition": partition,
            "reason": reason,
        }

    except Exception as e:
        logger.warning(f"LLM resource advisor failed (falling back to heuristic): {e}")
        return None


def _estimate_slurm_resources(
    observable: str,
    n_datasets: int,
    step_index: int,
    replica_count: int = 1,
    hpc_username: str = "",
) -> Dict[str, Any]:
    """
    Estimate SLURM resources by combining three information sources:

    1. **Historical job data** (job_history table) — actual elapsed time and
       peak memory from past runs of the same observable at a similar scale.
    2. **Live cluster state** (sinfo/squeue) — idle CPUs, memory per node,
       and how many resources the user already holds.
    3. **Static baselines** (_OBSERVABLE_COMPLEXITY) — fallback when history
       is unavailable.

    The function adjusts cpus-per-task based on cluster pressure:
    - Cluster has plenty of idle CPUs  -> give more CPUs per replica (faster)
    - Cluster is tight                 -> give fewer CPUs per replica (more
      replicas can run concurrently instead of queuing)

    It also picks the best available partition.

    Args:
        observable:    Observable key (e.g. "idis", "sia").
        n_datasets:    Number of datasets in this step.
        step_index:    Chain step number (0-based).
        replica_count: How many replicas will be submitted in this batch.
        hpc_username:  HPC username for querying per-user usage.

    Returns:
        Dict with slurm_config, estimation details, and cluster snapshot.
    """
    sources: List[str] = []
    base = _OBSERVABLE_COMPLEXITY.get(
        observable,
        {"base_time_min": 240, "mem_mb": 512, "cpus": 4, "grid_load_min": 15},
    )

    # ------------------------------------------------------------------
    # 1. Historical job data
    # ------------------------------------------------------------------
    hist_time = None
    hist_mem = None
    hist_cpus = None
    hist_n_jobs = 0
    try:
        with _get_db() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT AVG(elapsed_min) as avg_time,
                       MAX(elapsed_min) as max_time,
                       AVG(peak_memory_mb) as avg_mem,
                       MAX(peak_memory_mb) as max_mem,
                       AVG(cpus) as avg_cpus,
                       COUNT(*) as n_jobs
                FROM job_history
                WHERE observable = ? AND n_datasets BETWEEN ? AND ?
            """, (observable, max(1, n_datasets - 5), n_datasets + 5)).fetchone()
            if row and row["n_jobs"] >= 2 and row["avg_time"]:
                hist_time = row["max_time"]
                hist_mem = row["max_mem"] if row["max_mem"] else None
                hist_cpus = int(row["avg_cpus"]) if row["avg_cpus"] else None
                hist_n_jobs = row["n_jobs"]
                sources.append(f"history({hist_n_jobs} jobs)")
                logger.info(
                    f"Historical estimate: {observable} ~{hist_time:.0f}min, "
                    f"~{hist_mem}MB, ~{hist_cpus}CPUs from {hist_n_jobs} jobs"
                )
    except Exception as e:
        logger.warning(f"Could not query job history: {e}")

    # ------------------------------------------------------------------
    # 2. Live cluster state
    # ------------------------------------------------------------------
    cluster = {"idle_cpus": 0, "total_cpus": 0, "idle_nodes": 0,
               "total_nodes": 0, "mem_per_node_mb": 0}
    user_usage = {"running_jobs": 0, "running_cpus": 0,
                  "pending_jobs": 0, "pending_cpus": 0}
    best_partition = "production"
    try:
        if SLURM_TOOLS_OK:
            info = get_server_info(username=hpc_username or None)
            if info.get("success"):
                cluster = info.get("cluster_totals", cluster)
                sources.append("live_cluster")
                # Pick partition with the most idle CPUs
                parts = info.get("partitions", [])
                if parts:
                    best_part = max(parts, key=lambda p: p.get("cpus_idle", 0))
                    if best_part.get("cpus_idle", 0) > 0:
                        best_partition = best_part["name"]
            if hpc_username:
                user_usage = get_user_usage(hpc_username)
                sources.append(f"user({hpc_username})")
    except Exception as e:
        logger.warning(f"Could not query cluster state: {e}")

    # ------------------------------------------------------------------
    # 3. Try LLM advisor (Haiku) — fast path if AMSC API is available
    # ------------------------------------------------------------------
    history_summary = None
    if hist_time:
        history_summary = {
            "n_jobs": hist_n_jobs,
            "max_time_min": hist_time,
            "max_mem_mb": hist_mem,
            "avg_cpus": hist_cpus,
        }

    llm_advice = _llm_resource_advisor(
        observable=observable,
        n_datasets=n_datasets,
        step_index=step_index,
        replica_count=replica_count,
        cluster_snapshot=cluster,
        user_usage=user_usage,
        history_summary=history_summary,
        baseline=base,
    )

    idle_cpus = cluster["idle_cpus"]

    # Known valid partitions at JLab ifarm
    _VALID_PARTITIONS = {"production", "priority", "gpu", "long", "batch"}

    if llm_advice:
        # LLM succeeded — use its recommendations directly
        sources.append(f"llm({_LLM_MODEL})")
        est_time_min = llm_advice["time_minutes"]
        est_mem_mb = llm_advice["mem_mb"]
        chosen_cpus = llm_advice["cpus"]
        llm_partition = llm_advice.get("partition", "")
        if llm_partition and llm_partition in _VALID_PARTITIONS:
            best_partition = llm_partition
        else:
            if llm_partition:
                logger.warning(
                    f"LLM suggested unknown partition '{llm_partition}', "
                    f"keeping '{best_partition}'"
                )
        llm_reason = llm_advice.get("reason", "")
    else:
        # ------------------------------------------------------------------
        # 3b. Heuristic fallback: compute per-replica estimates (time, memory)
        # ------------------------------------------------------------------
        llm_reason = ""
        if hist_time:
            est_time_min = int(hist_time)
            est_mem_mb = int(hist_mem) if hist_mem else base["mem_mb"]
        else:
            est_time_min = base["base_time_min"]
            dataset_factor = max(1.0, n_datasets / 10.0)
            est_time_min = int(est_time_min * dataset_factor)
            est_mem_mb = base["mem_mb"]
            if n_datasets > 10:
                est_mem_mb += (n_datasets - 10) * 20
            sources.append("static_baseline")

        # Multi-step chains load more grids
        if step_index > 0:
            est_time_min += base.get("grid_load_min", 15) * step_index

        # Safety margin: tighter when we have history
        margin = 1.3 if hist_time else 1.5
        est_time_min = int(est_time_min * margin)
        est_mem_mb = int(est_mem_mb * margin)

        # ------------------------------------------------------------------
        # 4. Smart CPU allocation based on cluster pressure
        # ------------------------------------------------------------------
        base_cpus = hist_cpus if hist_cpus else base["cpus"]
        available_for_user = max(0, idle_cpus - user_usage.get("running_cpus", 0))
        total_cpu_demand = replica_count * base_cpus

        if available_for_user > 0 and replica_count > 0:
            if total_cpu_demand <= available_for_user * 0.5:
                max_boost = min(base_cpus * 2, 16)
                chosen_cpus = min(max_boost, max(base_cpus, available_for_user // replica_count))
                chosen_cpus = max(chosen_cpus, 2)
            elif total_cpu_demand > available_for_user:
                chosen_cpus = max(2, available_for_user // replica_count)
                chosen_cpus = min(chosen_cpus, base_cpus)
            else:
                chosen_cpus = base_cpus
        else:
            chosen_cpus = base_cpus

        sources.append("heuristic")

    # ------------------------------------------------------------------
    # 5. Memory cap per node (applies to both LLM and heuristic paths)
    # ------------------------------------------------------------------
    node_mem = cluster.get("mem_per_node_mb", 0)
    if node_mem > 0 and est_mem_mb > node_mem * 0.9:
        est_mem_mb = int(node_mem * 0.85)

    # ------------------------------------------------------------------
    # 6. Build final config
    # ------------------------------------------------------------------
    hours = est_time_min // 60
    mins = est_time_min % 60
    time_str = f"{hours:02d}:{mins:02d}:00"

    slurm_config = {
        "cpus": chosen_cpus,
        "time": time_str,
        "mem": f"{est_mem_mb}M",
        "nodes": 1,
        "partition": best_partition,
        "job_name": "fitpack",
    }

    # Concurrency estimate
    available_for_user = max(0, idle_cpus - user_usage.get("running_cpus", 0))
    concurrent = min(replica_count, available_for_user // chosen_cpus) if chosen_cpus > 0 else 0
    queued = max(0, replica_count - concurrent)

    base_cpus = hist_cpus if hist_cpus else base["cpus"]
    decision_reason = (
        llm_reason if llm_advice
        else "boosted (cluster idle)" if chosen_cpus > base_cpus
        else "reduced (cluster busy)" if chosen_cpus < base_cpus
        else "baseline"
    )

    return {
        "slurm_config": slurm_config,
        "estimated_time_min": est_time_min,
        "estimated_mem_mb": est_mem_mb,
        "estimation_sources": sources,
        "cluster_snapshot": {
            "idle_cpus": idle_cpus,
            "total_cpus": cluster["total_cpus"],
            "idle_nodes": cluster["idle_nodes"],
            "partition_chosen": best_partition,
            "user_running_cpus": user_usage.get("running_cpus", 0),
            "user_running_jobs": user_usage.get("running_jobs", 0),
        },
        "allocation_decision": {
            "baseline_cpus": base_cpus,
            "chosen_cpus": chosen_cpus,
            "reason": decision_reason,
            "concurrent_estimate": concurrent,
            "queued_estimate": queued,
        },
        "explanation": (
            f"{observable}: {n_datasets} ds, step={step_index}, {replica_count} replicas | "
            f"est. {est_time_min}min, {est_mem_mb}MB, {chosen_cpus}CPUs/replica | "
            f"cluster: {idle_cpus} idle CPUs, ~{concurrent} run immediately, "
            f"~{queued} queued | src: {'+'.join(sources)}"
        ),
    }


# ---------------------------------------------------------------------------
# Tools: Job submission
# ---------------------------------------------------------------------------

@mcp.tool()
def submit_batch_tasks(
    batch_id: str,
    chat_id: str = "",
    slurm_config: Optional[Dict[str, Any]] = None,
    estimate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Submit all tasks in a batch to SLURM.

    Args:
        batch_id:     The batch identifier returned by generate_random_tasks.
        chat_id:      Current chat/session ID. Optional if the batch's tasks
                      were inserted with an ``hpc_username`` (typical case) —
                      the username is then recovered from the task row.
        slurm_config: Optional SLURM resource overrides (cpus, time, mem, partition).
    """
    if not SLURM_TOOLS_OK:
        return {"error": "slurm_tools module not available."}

    summary = {"batch_id": batch_id, "submitted_count": 0, "failed_count": 0, "tasks": []}

    # Record what was asked for, before anything runs. Only the actuals were
    # ever stored, so the obvious question -- did the estimate fit the job? --
    # could not be answered from the database at all, and the estimator could
    # never be scored against its own history.
    if estimate:
        try:
            _sc = estimate.get("slurm_config") or {}
            _hhmmss = str(_sc.get("time", "")).split(":")
            _tmin = (int(_hhmmss[0]) * 60 + int(_hhmmss[1])) if len(_hhmmss) >= 2 else None
            _src = estimate.get("estimation_sources")
            if isinstance(_src, (list, tuple)):
                _src = ", ".join(str(x) for x in _src)
            with _get_db() as _c:
                for _row in _c.execute(
                        "SELECT path, slurm_job_id, hpc_username FROM tasks WHERE batch_id=?",
                        (batch_id,)).fetchall():
                    _c.execute(
                        "INSERT INTO job_history (batch_id, task_path, slurm_job_id,"
                        " hpc_username, proposed_cpus, proposed_time_min,"
                        " proposed_mem_mb, estimation_source)"
                        " VALUES (?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(task_path) DO UPDATE SET"
                        "   proposed_cpus=excluded.proposed_cpus,"
                        "   proposed_time_min=excluded.proposed_time_min,"
                        "   proposed_mem_mb=excluded.proposed_mem_mb,"
                        "   estimation_source=excluded.estimation_source",
                        (batch_id, _row[0], _row[1], _row[2] or "",
                         _sc.get("cpus"), _tmin,
                         estimate.get("estimated_mem_mb"), str(_src or "")))
        except Exception as _e:
            logger.warning("could not record proposed resources for %s: %s"
                           % (batch_id, _e))

    try:
        with _get_db() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            # Only submit tasks that have not yet been handed off to SLURM.
            # This makes the function idempotent and append-safe: re-running
            # submit_batch_tasks on an existing batch (e.g. after
            # run_fitting_job's append_to_batch_id path) will pick up only
            # the newly generated tasks, never re-submit the old ones.
            cur.execute(
                "SELECT * FROM tasks WHERE batch_id = ? "
                "AND (slurm_job_id IS NULL OR slurm_job_id = '')",
                (batch_id,),
            )
            tasks = cur.fetchall()

        if not tasks:
            return {"error": f"No un-submitted tasks found for batch_id: {batch_id}"}

        # Resolve the HPC user for this batch. Preference: explicit chat_id,
        # then the hpc_username column on the task rows, then the batch's chat.
        username = ""
        if chat_id:
            username = _get_hpc_username(chat_id) or ""
        if not username:
            for task in tasks:
                task_keys = task.keys() if hasattr(task, "keys") else []
                cand = task["hpc_username"] if "hpc_username" in task_keys else ""
                if cand:
                    username = cand
                    break
        if not username and tasks:
            row_chat = tasks[0]["chat_id"] if "chat_id" in tasks[0].keys() else ""
            if row_chat:
                username = _get_hpc_username(row_chat) or ""
        if not username:
            return {"error": "Cannot resolve HPC username for this batch. Pass chat_id explicitly."}

        with _get_db() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            for task in tasks:
                params_file_str = task["parameters_file"]
                task_path_str = task["path"]

                if not ssh_file_exists(params_file_str, username=username):
                    summary["failed_count"] += 1
                    summary["tasks"].append({
                        "path": task_path_str, "status": "failed",
                        "error": "parameters.yaml not found on HPC",
                    })
                    continue

                try:
                    # Check if this task has a prior .msr file for replica inheritance
                    prior_path_file = f"{task_path_str}/prior.path"
                    task_prior_path = None
                    if ssh_file_exists(prior_path_file, username=username):
                        try:
                            task_prior_path = ssh_read_file(prior_path_file, username=username).strip()
                        except Exception:
                            pass

                    script = generate_slurm_script(
                        input_file=Path(params_file_str),
                        output_dir=Path(task_path_str),
                        workflow={"filename": Path(params_file_str).name},
                        slurm_config=slurm_config,
                        prior_path=task_prior_path,
                    )
                    script_path_str = f"{task_path_str}/submit.sh"
                    ssh_write_file(script_path_str, script, username=username)

                    if is_iri_enabled():
                        job_id = iri_submit_script(
                            script_path_str,
                            task_path=task_path_str,
                            slurm_config=slurm_config,
                            username=username,
                        )
                        status = "submitted"
                    else:
                        res = ssh_run(["sbatch", script_path_str], timeout=15, username=username)
                        if res.returncode == 0:
                            job_id = parse_job_id(res.stdout) or "unknown"
                            status = "submitted"
                        else:
                            logger.error(f"sbatch failed: {res.stderr}")
                            status = "failed"
                            job_id = None

                    if status == "submitted":
                        cur.execute("""
                            UPDATE tasks
                            SET status='submitted', slurm_job_id=?, submitted_at=CURRENT_TIMESTAMP
                            WHERE path=?
                        """, (job_id, task_path_str))
                        summary["submitted_count"] += 1
                        summary["tasks"].append({
                            "path": task_path_str, "status": status, "job_id": job_id,
                        })
                    else:
                        summary["failed_count"] += 1
                        summary["tasks"].append({
                            "path": task_path_str, "status": "failed",
                            "error": "Submission command failed",
                        })

                except Exception as e:
                    logger.error(f"Error submitting {task_path_str}: {e}")
                    summary["failed_count"] += 1
                    summary["tasks"].append({
                        "path": task_path_str, "status": "failed", "error": str(e),
                    })

            conn.commit()

    except Exception as e:
        logger.error(f"Batch submission error: {e}")
        return {"error": str(e)}

    return summary


# ---------------------------------------------------------------------------
# Tools: Status synchronisation
# ---------------------------------------------------------------------------

@mcp.tool()
def list_chat_tasks(chat_id: str, sync: bool = True) -> List[Dict[str, Any]]:
    """
    List all tasks submitted in a given chat session, grouped by batch.
    Automatically syncs active task statuses from SLURM before returning.

    Args:
        chat_id: The chat session identifier.
        sync:    If True, query SLURM for latest statuses of active tasks first.

    Returns:
        List of task records with batch_id, slurm_job_id, status, timestamps.
    """
    _init_db()

    # Sync active task statuses from SLURM before reading
    if sync and STATUS_SYNC_OK:
        try:
            sync_task_statuses()
        except Exception as e:
            logger.warning(f"Status sync failed during list_chat_tasks: {e}")

    with _get_db() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT batch_id, batch_label, file_id, path, status, slurm_job_id,
                   created_at, submitted_at, started_at, completed_at, updated_at
            FROM tasks
            WHERE chat_id = ?
            ORDER BY created_at DESC
        """, (chat_id,))
        rows = cur.fetchall()
    return [dict(r) for r in rows]


@mcp.tool()
def sync_statuses(batch_id: str = "") -> Dict[str, Any]:
    """
    Synchronise task statuses by querying SLURM and updating the database.

    Each task row carries its own ``hpc_username``, which this sweep uses to
    scope the SLURM queries per-task — no ambient user context.

    Args:
        batch_id: Optional – restrict sync to a specific batch.
    """
    if not STATUS_SYNC_OK:
        return {"error": "status_sync module not available."}
    return sync_task_statuses(batch_id)


@mcp.tool()
def get_task_status(task_path: str) -> Dict[str, Any]:
    """
    Get the current status of a specific task directory.

    Args:
        task_path: Absolute path to the task directory.
    """
    if not STATUS_SYNC_OK:
        return {"error": "status_sync module not available."}
    username = _username_for_task(task_path)
    if not username:
        return {"error": f"Cannot resolve hpc_username for task {task_path}. "
                         "Row missing from tasks table or lacks hpc_username."}
    with set_hpc_user(username):
        return get_task_status_info(task_path)


# ---------------------------------------------------------------------------
# Tools: Fit results (per-dataset chi2 breakdown)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_fit_results(task_path: str) -> Dict[str, Any]:
    """
    Read the per-dataset chi2 breakdown from a completed task's SLURM output.
    Returns a table of chi2/npts per experiment, plus the final fitted PDF parameters.

    Use this when the user asks:
    - "Which dataset has the worst chi2?"
    - "How is HERA data fitting?"
    - "Show chi2 per experiment"
    - "Compare the two replicas"

    Args:
        task_path: Absolute path to the task directory on HPC.
    """
    import re

    username = _username_for_task(task_path)
    if not username:
        return {"error": f"Cannot resolve hpc_username for task {task_path}."}

    # One SSH round-trip: glob the slurm_*.out file and tail it in a single
    # remote shell. Previously this was ls + tail (two round-trips per task),
    # which compounded when the LLM fanned out over N replicas.
    import shlex
    quoted_dir = shlex.quote(task_path)
    slurm_file = ""  # resolved below; previously referenced but never assigned
    try:
        if is_iri_enabled():
            matched = iri_find(task_path, "slurm_*.out")
            if not matched:
                raise FileNotFoundError(f"No file matching slurm_*.out found in {task_path}")
            slurm_file = matched[0]
            output = iri_tail_first_matching(task_path, "slurm_*.out", lines=120)
        else:
            remote = (
                f'f=$(ls -1 {quoted_dir}/slurm_*.out 2>/dev/null | head -1); '
                f'if [ -n "$f" ]; then echo __SLURM_FILE__$f; tail -120 "$f"; '
                f'else echo __NO_SLURM_FILE__; fi'
            )
            # ssh joins argv into one command line and the login shell parses it
            # first — on ifarm that shell is tcsh, which chokes on $(...) and $f
            # ("Illegal variable name"). Quoting the script keeps it intact until
            # sh actually runs it. The pre-quoted script element is passed with
            # quote=False so ssh_run does not double-quote it.
            content = ssh_run(
                ["sh", "-c", shlex.quote(remote)], quote=False, timeout=15, username=username
            )
            if content.returncode != 0:
                return {"error": f"Cannot read slurm output in {task_path}: {content.stderr}"}
            output = content.stdout
            if output.strip() == "__NO_SLURM_FILE__":
                return {"error": f"No SLURM output file found in {task_path}"}
            if output.startswith("__SLURM_FILE__"):
                first_line, _, rest = output.partition("\n")
                slurm_file = first_line.replace("__SLURM_FILE__", "", 1).strip()
                output = rest
    except FileNotFoundError:
        return {"error": f"No SLURM output file found in {task_path}"}
    except Exception as e:
        return {"error": f"HPC read failed: {e}"}

    # Parse the per-dataset chi2 table
    # Two formats exist:
    #   IDIS: "  10007   p          HERMES           sig_r    33     190.48       5.77       0.00       0.00"
    #   DY:   "  10001        pp      E866  184   9421.21     51.20      0.00      5.13"
    # Common pattern: line starts with spaces + 5-digit dataset ID + columns of numbers at the end
    datasets = []
    in_table = False
    has_obs_col = False
    for line in output.split("\n"):
        # Detect table header
        if "idx" in line and "tar" in line and "npts" in line and "chi2" in line:
            in_table = True
            has_obs_col = "obs" in line
            continue
        if in_table:
            stripped = line.strip()
            if not stripped or "=====" in stripped:
                if datasets:
                    break
                continue
            # Try to match: dataset_id, then text columns, then numeric columns
            # Split by whitespace and identify the pattern
            parts = stripped.split()
            if len(parts) >= 6 and parts[0].isdigit():
                try:
                    # Find where numbers start (npts is the first pure integer after text columns)
                    num_start = -1
                    for i in range(1, len(parts)):
                        if parts[i].isdigit() and i >= 2:
                            num_start = i
                            break
                    if num_start < 0:
                        continue

                    # Text columns between dataset_id and npts
                    text_cols = parts[1:num_start]
                    num_cols = parts[num_start:]

                    target = text_cols[0] if len(text_cols) >= 1 else ""
                    experiment = text_cols[1] if len(text_cols) >= 2 else ""
                    observable = text_cols[2] if len(text_cols) >= 3 else ""

                    datasets.append({
                        "dataset_id": parts[0],
                        "target": target,
                        "experiment": experiment,
                        "observable": observable,
                        "npts": int(num_cols[0]),
                        "chi2": float(num_cols[1]),
                        "chi2_npts": float(num_cols[2]),
                        "rchi2": float(num_cols[3]) if len(num_cols) > 3 else 0.0,
                        "nchi2": float(num_cols[4]) if len(num_cols) > 4 else 0.0,
                    })
                except (ValueError, IndexError):
                    continue
            elif "reaction:" in stripped:
                if datasets:
                    break

    # Parse fitted parameters (norm values)
    norms = {}
    for line in output.split("\n"):
        m = re.match(r'\s*\|\s*\d+\s+norm\s+(\S+)\s+([\d.eE+-]+)', line)
        if m:
            norms[m.group(1)] = float(m.group(2))

    # Parse final PDF parameters
    pdf_params = {}
    for line in output.split("\n"):
        m = re.match(r'\s*\|\s*\d+\s+pdf\s+(\S+\s+\S+)\s+([\d.eE+-]+)', line)
        if m:
            pdf_params[m.group(1).strip()] = float(m.group(2))

    if not datasets:
        return {"error": "Could not parse chi2 table from SLURM output", "raw_tail": output[-500:]}

    # Compute totals
    total_chi2 = sum(d["chi2"] for d in datasets)
    total_npts = sum(d["npts"] for d in datasets)

    return {
        "status": "success",
        "task_path": task_path,
        "slurm_file": slurm_file,
        "total_chi2": round(total_chi2, 2),
        "total_npts": total_npts,
        "total_chi2_npts": round(total_chi2 / total_npts, 4) if total_npts > 0 else None,
        "datasets": datasets,
        "norms": norms,
        "pdf_params": pdf_params,
        "worst_dataset": max(datasets, key=lambda d: d["chi2_npts"]) if datasets else None,
        "best_dataset": min(datasets, key=lambda d: d["chi2_npts"]) if datasets else None,
    }


# ---------------------------------------------------------------------------
# Tools: Fit diagnostics
# ---------------------------------------------------------------------------

@mcp.tool()
def diagnose_fit(task_path: str, previous_task_path: str = "") -> Dict[str, Any]:
    """
    Analyze a completed task's fit quality and return structured diagnostics.

    Performs server-side numerical analysis instead of relying on the LLM
    to interpret raw chi2 tables. Returns: quality verdict, outlier datasets,
    convergence assessment, and suggested actions.

    Args:
        task_path:          Path to the completed task directory on HPC.
        previous_task_path: Optional path to the previous step's task for comparison.
    """
    results = get_fit_results(task_path)
    if "error" in results:
        return results

    datasets = results.get("datasets", [])
    total_chi2_npts = results.get("total_chi2_npts", 0)

    if not datasets:
        return {"error": "No dataset results found"}

    # --- 1. Quality verdict ---
    chi2_values = [d["chi2_npts"] for d in datasets]
    import statistics
    mean_chi2 = statistics.mean(chi2_values)
    median_chi2 = statistics.median(chi2_values)
    stdev_chi2 = statistics.stdev(chi2_values) if len(chi2_values) > 1 else 0

    if total_chi2_npts < 1.5:
        verdict = "excellent"
    elif total_chi2_npts < 2.0:
        verdict = "good"
    elif total_chi2_npts < 3.0:
        verdict = "acceptable"
    else:
        verdict = "poor"

    # --- 2. Outlier detection (>2 sigma from mean, or chi2/npts > 3.0) ---
    outlier_threshold = max(mean_chi2 + 2 * stdev_chi2, 3.0)
    outliers = []
    for d in datasets:
        if d["chi2_npts"] > outlier_threshold:
            # Suggest possible causes based on dataset properties
            causes = []
            if d.get("target") in ("d", "d/p", "n", "n/d", "h"):
                causes.append("nuclear target — may need nuclear corrections")
            if "SLAC" in d.get("experiment", ""):
                causes.append("SLAC data often has low-Q2 points — consider raising Q2 cut")
            if d["chi2_npts"] > 5.0:
                causes.append("very high chi2 — possible normalization issue or incompatible data")
            if not causes:
                causes.append("elevated chi2 — check kinematic cuts or parameter flexibility")
            outliers.append({
                "dataset_id": d["dataset_id"],
                "experiment": d.get("experiment", ""),
                "target": d.get("target", ""),
                "chi2_npts": round(d["chi2_npts"], 3),
                "npts": d["npts"],
                "possible_causes": causes,
            })

    # --- 3. Convergence assessment ---
    # Check if many datasets have similar chi2 (well-converged) or spread (not converged)
    cv = stdev_chi2 / mean_chi2 if mean_chi2 > 0 else 0  # coefficient of variation
    if cv < 0.3:
        convergence = "well_converged"
        convergence_note = "chi2/npts values are consistent across datasets"
    elif cv < 0.7:
        convergence = "moderate"
        convergence_note = "some spread in chi2/npts — a few datasets may need attention"
    else:
        convergence = "poor"
        convergence_note = "large spread in chi2/npts — likely systematic issues with some datasets"

    # --- 4. Comparison with previous step (if provided) ---
    comparison = None
    if previous_task_path:
        prev = get_fit_results(previous_task_path)
        if "error" not in prev and prev.get("datasets"):
            prev_total = prev.get("total_chi2_npts", 0)
            delta = total_chi2_npts - prev_total
            improved = delta < 0

            # Per-dataset comparison
            prev_by_id = {d["dataset_id"]: d for d in prev.get("datasets", [])}
            changed = []
            for d in datasets:
                if d["dataset_id"] in prev_by_id:
                    prev_chi2 = prev_by_id[d["dataset_id"]]["chi2_npts"]
                    d_delta = d["chi2_npts"] - prev_chi2
                    if abs(d_delta) > 0.2:  # only report significant changes
                        changed.append({
                            "dataset_id": d["dataset_id"],
                            "experiment": d.get("experiment", ""),
                            "previous": round(prev_chi2, 3),
                            "current": round(d["chi2_npts"], 3),
                            "delta": round(d_delta, 3),
                            "improved": d_delta < 0,
                        })

            comparison = {
                "previous_total_chi2_npts": round(prev_total, 4),
                "current_total_chi2_npts": round(total_chi2_npts, 4),
                "delta": round(delta, 4),
                "improved": improved,
                "datasets_changed": sorted(changed, key=lambda x: x["delta"]),
            }

    # --- 5. Suggested actions ---
    # Framework: physics-specific rules to be refined by domain experts
    actions = []
    if verdict == "poor":
        actions.append({
            "action": "review_cuts",
            "reason": f"Total chi2/npts={total_chi2_npts:.2f} is high. Consider tightening kinematic cuts (Q2, W2).",
        })
    if len(outliers) > 0:
        worst = outliers[0]
        actions.append({
            "action": "investigate_outlier",
            "dataset": worst["dataset_id"],
            "experiment": worst["experiment"],
            "reason": f"{worst['experiment']} has chi2/npts={worst['chi2_npts']:.2f}. {worst['possible_causes'][0]}",
        })
    if len(outliers) > len(datasets) * 0.3:
        actions.append({
            "action": "widen_parameters",
            "reason": f"{len(outliers)}/{len(datasets)} datasets are outliers — parameter space may be too constrained.",
        })
    if comparison and comparison["improved"]:
        actions.append({
            "action": "continue_overlay",
            "reason": f"chi2/npts improved ({comparison['delta']:+.3f}). Safe to proceed to next observable.",
        })
    elif comparison and not comparison["improved"]:
        actions.append({
            "action": "investigate_regression",
            "reason": f"chi2/npts worsened ({comparison['delta']:+.3f}). Check if newly added data conflicts with existing fits.",
        })
    if not actions:
        actions.append({
            "action": "continue_overlay",
            "reason": f"Fit quality is {verdict}. Proceed to next observable.",
        })

    return {
        "status": "success",
        "task_path": task_path,
        "quality": {
            "verdict": verdict,
            "total_chi2_npts": round(total_chi2_npts, 4),
            "mean_chi2_npts": round(mean_chi2, 4),
            "median_chi2_npts": round(median_chi2, 4),
            "stdev": round(stdev_chi2, 4),
            "num_datasets": len(datasets),
        },
        "outliers": outliers,
        "convergence": {
            "status": convergence,
            "note": convergence_note,
            "coefficient_of_variation": round(cv, 3),
        },
        "comparison": comparison,
        "suggested_actions": actions,
    }


# ---------------------------------------------------------------------------
# Tools: Result management
# ---------------------------------------------------------------------------

@mcp.tool()
def save_result(
    task_path: str,
    user_note: str = "",
    mark_as_best: bool = False,
    tags: str = "",
) -> Dict[str, Any]:
    """
    Save a completed task's metrics and parameters for future reference.

    Args:
        task_path:    Absolute path to the task directory.
        user_note:    Optional free-text annotation.
        mark_as_best: Flag this result as best in its batch.
        tags:         Comma-separated tags (e.g. "good,low-chi2").
    """
    if not RESULT_MANAGER_OK:
        return {"error": "result_manager module not available."}
    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    username = _username_for_task(task_path)
    if not username:
        return {"error": f"Cannot resolve hpc_username for task {task_path}."}
    with set_hpc_user(username):
        return save_task_result(task_path, user_note, mark_as_best, tag_list)


@mcp.tool()
def list_saved_results_tool(
    batch_id: str = "",
    chat_id: str = "",
    best_only: bool = False,
) -> List[Dict[str, Any]]:
    """
    List saved results, optionally filtered by batch, chat, or best-only flag.
    """
    if not RESULT_MANAGER_OK:
        return [{"error": "result_manager module not available."}]
    return get_saved_results(batch_id, chat_id, best_only)


@mcp.tool()
def restore_saved_parameters(saved_id: int, chat_id: str = "") -> Dict[str, Any]:
    """
    Restore parameters from a previously saved result back to default.yaml.

    Args:
        saved_id: The integer ID of the saved result (from list_saved_results_tool).
        chat_id:  Current chat/session ID. Optional — if empty, the
                  saved row's ``chat_id`` is used to recover the HPC user.
    """
    if not RESULT_MANAGER_OK:
        return {"error": "result_manager module not available."}
    username = ""
    if chat_id:
        username = _get_hpc_username(chat_id) or ""
    if not username:
        try:
            with _get_db() as conn:
                row = conn.execute(
                    "SELECT chat_id FROM saved_results WHERE id = ?", (saved_id,)
                ).fetchone()
                if row and row[0]:
                    username = _get_hpc_username(row[0]) or ""
        except Exception:
            pass
    if not username:
        return {"error": "Cannot resolve hpc_username for saved result. Pass chat_id."}
    with set_hpc_user(username):
        return restore_parameters_from_saved(saved_id)


@mcp.tool()
def find_batch_best(batch_id: str) -> Dict[str, Any]:
    """
    Find the best-performing saved result (lowest chi2/npts) in a batch.
    """
    if not RESULT_MANAGER_OK:
        return {"error": "result_manager module not available."}
    return get_batch_best_result(batch_id)


@mcp.tool()
def compare_step_results(chat_id: str) -> Dict[str, Any]:
    """
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
    """
    _init_db()
    chain = _get_chain_state(chat_id)
    history = chain.get("history", [])

    if not history:
        return {"status": "no_history", "message": "No completed steps in this chat."}

    steps = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        batch_id = entry.get("batch_id", "")
        observable = entry.get("observable", "")
        step_idx = entry.get("step", 0)

        if not batch_id:
            continue

        # Find the best completed task in this batch
        try:
            with _get_db() as conn:
                conn.row_factory = sqlite3.Row
                tasks = conn.execute(
                    "SELECT path FROM tasks WHERE batch_id = ? AND status = 'completed'",
                    (batch_id,)
                ).fetchall()
        except Exception:
            tasks = []

        best_chi2 = None
        best_results = None
        for task in tasks:
            try:
                results = get_fit_results(task["path"])
                if "datasets" in results:
                    total_chi2 = sum(d.get("chi2", 0) for d in results["datasets"])
                    total_npts = sum(d.get("npts", 0) for d in results["datasets"])
                    chi2_npts = total_chi2 / total_npts if total_npts > 0 else 999
                    if best_chi2 is None or chi2_npts < best_chi2:
                        best_chi2 = chi2_npts
                        best_results = results["datasets"]
            except Exception:
                continue

        step_info = {
            "step": step_idx,
            "observable_added": observable,
            "batch_id": batch_id,
            "total_chi2_npts": round(best_chi2, 4) if best_chi2 else None,
        }
        if best_results:
            # Summarize per-dataset
            step_info["datasets"] = [
                {
                    "id": d.get("dataset_id", ""),
                    "experiment": d.get("experiment", ""),
                    "chi2_npts": round(d.get("chi2_npts", 0), 3),
                    "npts": d.get("npts", 0),
                }
                for d in best_results
            ]
            # Worst datasets
            sorted_ds = sorted(best_results, key=lambda d: d.get("chi2_npts", 0), reverse=True)
            step_info["worst_3"] = [
                {"experiment": d.get("experiment", ""), "id": d.get("dataset_id", ""), "chi2_npts": round(d.get("chi2_npts", 0), 3)}
                for d in sorted_ds[:3]
            ]

        steps.append(step_info)

    # Compute deltas between consecutive steps
    for i in range(1, len(steps)):
        prev = steps[i - 1].get("total_chi2_npts")
        curr = steps[i].get("total_chi2_npts")
        if prev is not None and curr is not None:
            steps[i]["delta_chi2_npts"] = round(curr - prev, 4)
            steps[i]["improved"] = curr < prev

    return {
        "status": "success",
        "total_steps": len(steps),
        "steps": steps,
    }


@mcp.tool()
def compute_batch_prior_tool(
    batch_id: str,
    chat_id: str = "",
    top_n: Optional[int] = None,
    chi2_npts_max: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Average the PDF parameters of the best-performing saved results in a batch
    and write the result back to default.yaml as the prior for the next fit step.

    Call this after saving results from step N and before generate_random_tasks
    for step N+1. This implements the Bayesian posterior→prior transfer.

    Args:
        batch_id:      Batch whose saved results to aggregate.
        chat_id:       Current chat/session ID. Optional — if empty, the
                       batch's task rows are consulted to recover the HPC user.
        top_n:         Use only the best N replicas (sorted by chi2/npts ASC).
                       None = use all saved results in the batch.
        chi2_npts_max: Discard replicas with chi2/npts above this threshold
                       before averaging. None = no threshold.

    Returns:
        Status dict with replicas_used, params_updated, chi2_npts_range.
    """
    if not RESULT_MANAGER_OK:
        return {"error": "result_manager module not available."}
    username = ""
    if chat_id:
        username = _get_hpc_username(chat_id) or ""
    if not username:
        try:
            with _get_db() as conn:
                row = conn.execute(
                    "SELECT hpc_username, chat_id FROM tasks WHERE batch_id = ? LIMIT 1",
                    (batch_id,),
                ).fetchone()
                if row:
                    username = row[0] or (_get_hpc_username(row[1] or "") or "")
        except Exception:
            pass
    if not username:
        return {"error": "Cannot resolve hpc_username for batch. Pass chat_id."}
    with set_hpc_user(username):
        return _compute_batch_prior(batch_id, top_n=top_n, chi2_npts_max=chi2_npts_max)


@mcp.tool()
def assemble_paired_prior_tasks(
    alpha_batch_id: str,
    beta_batch_id: str,
    chat_id: str,
    observable: str = "sidis",
    count: int = 30,
    job_number: str = "000",
    selected_ids: Optional[List[str]] = None,
    filters: Optional[List[str]] = None,
    advanced: Optional[Dict[str, Any]] = None,
    batch_label: str = "",
) -> Dict[str, Any]:
    """
    Stage B random pairing: combine α-track (PDF) and β-track (FF) posteriors
    into a joint coupled fit, following the JAM20-SIDIS methodology (arXiv
    2101.04664). For each of `count` new replicas, draw one α replica index
    and one β replica index uniformly at random, merge α's ``conf.params.pdf``
    values with β's ``conf.params.ffpion/ffkaon/ffhadron`` values, and launch
    a single joint fit in which both pdf and ff* parameters are active on the
    combined dataset scaffold (α's observables + β's observables + the stage
    B observable, typically sidis).

    Call path: ``run_fitting_job(observable="sidis", ...)`` auto-routes here
    when the observable's stage in ``_OBS_LOCATION`` is "B". AI tools should
    not call this directly — go through run_fitting_job so the resolution of
    α / β parent batches and the chain_state update stay consistent.

    Args:
        alpha_batch_id: Completed α-track batch whose pdf posterior feeds the
                        pairing.
        beta_batch_id:  Completed β-track batch whose ff* posterior feeds the
                        pairing.
        chat_id:        Current chat/session ID (picks up hpc username and
                        chain_state).
        observable:     Stage B observable (default "sidis").
        count:          Number of paired replicas to generate and submit.
        job_number:     Prefix label for the output directory names.
        selected_ids:   Optional explicit ID list for ``observable``. If None,
                        uses the catalog's default_on entries.
        filters:        Optional kinematic cut strings for ``observable``.
                        If None, falls back to DATASET_FILTERS.
        advanced:       Optional advanced overrides (tolerance, max_iterations,
                        optimizer, bootstrap, qcd_config). ``pdf_params`` is
                        stripped because the paired α snapshot owns pdf values.

    Returns:
        Status dict with the new batch_id, paired_count, and the α/β parent
        batch_ids that were sourced.
    """
    _init_db()

    if not RESULT_MANAGER_OK:
        return {"status": "error", "message": "result_manager module not available"}

    try:
        hpc_user = _require_hpc_username(chat_id)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    remote_config = _default_config(hpc_user)
    runs_dir = _runs_dir(hpc_user)

    # Load α and β saved replicas. Each row's "parameters" field is the full
    # conf dict that was captured when the replica was saved to saved_results.
    alpha_rows = get_saved_results(batch_id=alpha_batch_id)
    if not alpha_rows:
        return {
            "status": "error",
            "message": f"No saved results for α batch '{alpha_batch_id}'. Save α results first.",
        }
    beta_rows = get_saved_results(batch_id=beta_batch_id)
    if not beta_rows:
        return {
            "status": "error",
            "message": f"No saved results for β batch '{beta_batch_id}'. Save β results first.",
        }

    logger.info(
        f"Stage B pairing: α batch {alpha_batch_id} has {len(alpha_rows)} replicas, "
        f"β batch {beta_batch_id} has {len(beta_rows)} replicas, target count={count}"
    )

    # Build the full dataset scaffold by replaying α + β history and then
    # adding the stage B observable on top. _latest_track_selections handles
    # same-observable-reruns correctly (latest entry wins).
    chain = _get_chain_state(chat_id)
    history = chain.get("history", [])
    alpha_sel = _latest_track_selections(history, "alpha")
    beta_sel = _latest_track_selections(history, "beta")

    # Resolve stage-B observable's own selection (explicit args win; otherwise
    # catalog defaults). Match the auto-fill behavior of run_fitting_job.
    if not selected_ids:
        catalog_defaults = [
            d["id"] for d in DATASET_CATALOG.get(observable, [])
            if d.get("default_on", False)
        ]
        if not catalog_defaults:
            return {
                "status": "error",
                "message": f"No default datasets for stage B observable '{observable}'",
            }
        selected_ids = catalog_defaults
    stage_b_sel: Dict[str, Any] = {
        "observable": observable,
        "selected_ids": list(selected_ids),
    }
    if filters:
        stage_b_sel["filters"] = list(filters)

    # Rehydrate legacy α/β entries that predate selected_ids persistence.
    def _rehydrate(selections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for sel in selections:
            if sel["selected_ids"]:
                out.append(sel)
                continue
            legacy_defaults = [
                d["id"] for d in DATASET_CATALOG.get(sel["observable"], [])
                if d.get("default_on", False)
            ]
            if legacy_defaults:
                out.append({
                    "observable": sel["observable"],
                    "selected_ids": legacy_defaults,
                    "filters": sel["filters"],
                })
        return out

    replay_list = _rehydrate(alpha_sel) + _rehydrate(beta_sel) + [stage_b_sel]
    if not any(s["observable"] == observable for s in replay_list):
        return {
            "status": "error",
            "message": f"Stage B observable '{observable}' did not land in the replay scaffold",
        }

    # Clear datasets on HPC default.yaml and apply advanced overrides. Drop
    # any pdf_params from advanced — paired α owns the pdf values and would
    # otherwise clobber them silently.
    advanced_applied: Dict[str, Any] = {}
    try:
        content = ssh_read_file(remote_config, username=hpc_user)
        config = yaml.safe_load(content) or {}
        config.setdefault("conf", {})["datasets"] = {}
        if advanced:
            adv_for_yaml = dict(advanced)
            adv_for_yaml.pop("pdf_params", None)
            _apply_advanced_overrides(config, adv_for_yaml, advanced_applied)
        ssh_write_file(
            remote_config,
            yaml.dump(config, default_flow_style=False),
            username=hpc_user,
        )
    except Exception as e:
        return {"status": "error", "message": f"Failed to rebuild default.yaml: {e}"}

    # Replay α + β history + stage B observable into YAML datasets section.
    compose_dataset_config(replay_list, chat_id=chat_id)

    # Reload the composed YAML — this is what each replica's parameters.yaml
    # will be a deepcopy of, except pdf values come from α_{i_α} and ff*
    # values come from β_{i_β}.
    base_config = get_current_parameters(chat_id)
    if isinstance(base_config, dict) and "error" in base_config:
        return {
            "status": "error",
            "message": f"Failed to reload config after compose: {base_config['error']}",
        }

    ssh_mkdir(runs_dir, username=hpc_user)
    timestamp = int(time.time())
    batch_id = f"batch_{timestamp}_{uuid.uuid4().hex[:4]}"
    ff_namespaces = ("ffpion", "ffkaon", "ffhadron")
    results: List[Dict[str, Any]] = []

    def _overwrite_values(target_ns: Dict[str, Any], source_ns: Dict[str, Any]) -> int:
        """Copy ``value`` fields from source snapshot into target, leaving
        bounds/fixed flags alone. Returns the number of values written."""
        if not isinstance(target_ns, dict) or not isinstance(source_ns, dict):
            return 0
        written = 0
        for name, snap_entry in source_ns.items():
            if name not in target_ns or not isinstance(snap_entry, dict):
                continue
            v = snap_entry.get("value")
            if v is None:
                continue
            try:
                target_ns[name]["value"] = float(v)
                written += 1
            except (TypeError, ValueError):
                continue
        return written

    for i in range(1, count + 1):
        i_alpha = random.randint(0, len(alpha_rows) - 1)
        i_beta = random.randint(0, len(beta_rows) - 1)
        alpha_snap = alpha_rows[i_alpha].get("parameters", {})
        beta_snap = beta_rows[i_beta].get("parameters", {})
        alpha_pdf = alpha_snap.get("conf", {}).get("params", {}).get("pdf", {})

        task_config = deepcopy(base_config)
        task_params = task_config.setdefault("conf", {}).setdefault("params", {})

        pdf_written = _overwrite_values(task_params.get("pdf", {}), alpha_pdf)
        ff_written_total = 0
        for ns in ff_namespaces:
            beta_ns = beta_snap.get("conf", {}).get("params", {}).get(ns, {})
            ff_written_total += _overwrite_values(task_params.get(ns, {}), beta_ns)

        suffix = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
        dir_name = f"{job_number}_{suffix}"
        task_path_str = f"{runs_dir}/{dir_name}"
        ssh_mkdir(task_path_str, username=hpc_user)
        file_path_str = f"{task_path_str}/parameters.yaml"

        try:
            content = yaml.dump(task_config, default_flow_style=False)
            ssh_write_file(file_path_str, content, username=hpc_user)
            _insert_task({
                "chat_id": chat_id,
                "batch_id": batch_id,
                "batch_label": batch_label,
                "file_id": dir_name,
                "path": task_path_str,
                "parameters_file": file_path_str,
                "step_index": 0,
                "parent_batch_id": alpha_batch_id,  # primary lineage = α
                "hpc_username": hpc_user,
            })
            results.append({
                "task_index": i,
                "job_dir": task_path_str,
                "file_path": file_path_str,
                "batch_id": batch_id,
                "alpha_replica_index": i_alpha,
                "beta_replica_index": i_beta,
                "pdf_values_written": pdf_written,
                "ff_values_written": ff_written_total,
            })
            logger.info(
                f"Stage B task {i}/{count}: α_rep={i_alpha}, β_rep={i_beta}, "
                f"pdf={pdf_written}, ff={ff_written_total}, dir={task_path_str}"
            )
        except Exception as e:
            logger.error(f"Stage B task {i}/{count} failed: {e}")
            results.append({"task_index": i, "error": str(e)})

        time.sleep(0.01)  # avoid timestamp collisions

    ok_tasks = [r for r in results if "error" not in r]
    if not ok_tasks:
        return {"status": "error", "phase": "generate", "detail": results}

    # Estimate SLURM resources using the total dataset count across the
    # replay scaffold (α + β + stage B) — stage B joint fits are heavier
    # than stage A single-track runs.
    total_datasets = sum(len(s.get("selected_ids") or []) for s in replay_list)
    resource_estimate = _estimate_slurm_resources(
        observable, max(total_datasets, 1), 0,
        replica_count=count, hpc_username=hpc_user,
    )
    submit_result = submit_batch_tasks(
        batch_id,
        chat_id=chat_id,
        slurm_config=resource_estimate["slurm_config"],
        estimate=resource_estimate,
    )

    # Persist chain_state entry. `paired_parents` is the first-class lineage
    # field for stage B/C — downstream tools (get_analysis_roadmap, any future
    # stage-C chain) read this instead of the single-valued parent_batch_id
    # to reconstruct the multi-parent DAG.
    obs_stage, obs_track = _OBS_LOCATION.get(observable, ("B", None))
    entry: Dict[str, Any] = {
        "step": 0,
        "batch_id": batch_id,
        "observable": observable,
        "stage": obs_stage,
        "track": obs_track,
        "datasets_count": len(selected_ids),
        "selected_ids": list(selected_ids),
        "filters": list(filters) if filters else [],
        "parent_batch_id": alpha_batch_id,
        "paired_parents": {
            "alpha": alpha_batch_id,
            "beta": beta_batch_id,
        },
        "timestamp": int(time.time()),
    }
    if advanced:
        entry["advanced"] = advanced
    if advanced_applied:
        entry["advanced_applied"] = advanced_applied
    history.append(entry)
    _update_chain_state(chat_id, 0, batch_id, observable, history)

    logger.info(
        f"Stage B batch {batch_id}: {len(ok_tasks)}/{count} paired replicas, "
        f"α={alpha_batch_id}, β={beta_batch_id}, observable={observable}"
    )

    return {
        "status": "success",
        "stage": "B",
        "batch_id": batch_id,
        "observable": observable,
        "paired_count": len(ok_tasks),
        "failed_count": len(results) - len(ok_tasks),
        "alpha_batch_id": alpha_batch_id,
        "beta_batch_id": beta_batch_id,
        "alpha_replicas_available": len(alpha_rows),
        "beta_replicas_available": len(beta_rows),
        "total_datasets": total_datasets,
        "submit_result": submit_result,
    }


@mcp.tool()
def compose_dataset_config(
    observable_selections: List[Dict[str, Any]],
    chat_id: str = "",
) -> Dict[str, Any]:
    """
    Merge one or more observable dataset selections into default.yaml, preserving
    existing observables that are not mentioned. Call this before
    generate_random_tasks when you want to add new datasets to a running chain.

    Each entry in observable_selections must have:
        observable:   str  — e.g. "dy", "zrap", "wasym", "wzrv", "jet"
        selected_ids: list — dataset ID strings the user checked
        filters:      list — optional kinematic cut strings (uses defaults if absent)

    Example – add DY on top of a previous IDIS fit:
        compose_dataset_config([
            {"observable": "dy", "selected_ids": ["10001", "10002", "20001", "20002"]}
        ], chat_id="<current-chat-id>")

    Args:
        observable_selections: See above.
        chat_id:               Current chat/session ID (required — resolves the
                               caller's HPC account to read/write the correct
                               remote default.yaml).

    Returns:
        Status dict with observables_configured and per-observable dataset counts.
    """
    try:
        username = _require_hpc_username(chat_id)
    except ValueError as e:
        return {"error": str(e)}
    remote_config = _default_config(username)
    try:
        content = ssh_read_file(remote_config, username=username)
        config = yaml.safe_load(content) or {}
    except Exception as e:
        return {"error": f"Failed to read default.yaml from HPC: {e}"}

    existing_datasets: Dict[str, Any] = config.setdefault("conf", {}).setdefault("datasets", {})

    added: Dict[str, int] = {}
    for sel in observable_selections:
        observable = sel.get("observable", "").strip()
        selected_ids = {str(s) for s in sel.get("selected_ids", [])}
        if not observable or not selected_ids:
            continue

        catalog = DATASET_CATALOG.get(observable, [])
        user_filters = sel.get("filters")
        filters = user_filters if user_filters is not None else DATASET_FILTERS.get(observable, [])

        xlsx_map: Dict[int, str] = {}
        norm_map: Dict[int, Any] = {}
        for ds in catalog:
            if ds["id"] in selected_ids:
                try:
                    key = int(ds["id"])
                except ValueError:
                    key = ds["id"]
                xlsx_map[key] = ds["xlsx"]
                if ds.get("norm"):
                    norm_map[key] = ds["norm"]

        ds_conf: Dict[str, Any] = {}
        ds_conf["filters"] = filters  # always include, even if [] — obslib expects this key
        ds_conf["xlsx"] = xlsx_map
        ds_conf["norm"] = norm_map

        existing_datasets[observable] = ds_conf
        added[observable] = len(xlsx_map)

    if not added:
        return {"error": "No valid observable selections provided."}

    try:
        new_content = yaml.dump(config, default_flow_style=False)
        ssh_write_file(remote_config, new_content, username=username)
    except Exception as e:
        return {"error": f"Failed to write default.yaml to HPC: {e}"}

    all_obs = list(existing_datasets.keys())
    return {
        "status": "success",
        "newly_added_observables": list(added.keys()),
        "dataset_counts_added": added,
        "all_active_observables": all_obs,
    }


# ---------------------------------------------------------------------------
# Tools: Task cancellation
# ---------------------------------------------------------------------------

@mcp.tool()
def cancel_underperforming_tasks(
    batch_id: str,
    keep_best_n: int = 1,
    chi2_npts_threshold: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Cancel underperforming tasks in a batch to free cluster resources.

    Reads chi2/npts from each task's progress.json and keeps the best N running.

    Args:
        batch_id:            Batch identifier.
        keep_best_n:         Number of best tasks to keep running (default 1).
        chi2_npts_threshold: Optional upper chi2/npts cutoff; tasks above this
                             are cancelled regardless of ranking.
    """
    if not CANCELLATION_OK:
        return {"error": "task_cancellation module not available."}

    try:
        with _get_db() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT path, status, hpc_username, chat_id FROM tasks
                WHERE batch_id = ?
                  AND status IN ('submitted', 'pending', 'running')
                ORDER BY path
            """, (batch_id,))
            tasks = cur.fetchall()

        if not tasks:
            return {"message": "No running/pending tasks found in batch."}

        # Rank by chi2/npts (lower is better)
        performance: List[Dict[str, Any]] = []
        for task in tasks:
            task_user = (task["hpc_username"] or "") or (
                _get_hpc_username(task["chat_id"] or "") or ""
            )
            if not task_user:
                logger.warning(f"Skipping task {task['path']}: no hpc_username resolvable")
                continue
            progress_path = f"{task['path']}/progress.json"
            if not ssh_file_exists(progress_path, username=task_user):
                continue
            try:
                content = ssh_read_file(progress_path, username=task_user)
                progress = json.loads(content)
                if not progress:
                    continue
                last = progress[-1]
                total_chi2 = total_npts = 0
                if "datasets" in last:
                    for ds in last["datasets"].values():
                        total_chi2 += ds.get("chi2", 0)
                        total_npts += ds.get("npts", 0)
                else:
                    total_chi2 = last.get("chi2", 0)
                    total_npts = last.get("npts", 1)
                chi2_npts = total_chi2 / total_npts if total_npts > 0 else float("inf")
                performance.append({"path": task["path"], "chi2_npts": chi2_npts})
            except Exception as e:
                logger.warning(f"Could not read progress for {task['path']}: {e}")

        performance.sort(key=lambda x: x["chi2_npts"])
        keep_paths = [t["path"] for t in performance[:keep_best_n]]

        # Resolve HPC user from the batch's tasks so cancel_tasks_by_batch's
        # underlying `scancel` ssh calls go to the right account.
        batch_user = ""
        for task in tasks:
            cand = (task["hpc_username"] or "") or (
                _get_hpc_username(task["chat_id"] or "") or ""
            )
            if cand:
                batch_user = cand
                break
        if not batch_user:
            return {"error": "Cannot resolve hpc_username for this batch."}

        with set_hpc_user(batch_user):
            result = cancel_tasks_by_batch(
                batch_id=batch_id,
                exclude_paths=keep_paths,
                chi2_npts_threshold=chi2_npts_threshold,
            )
        result["kept_tasks"] = keep_paths
        result["performance_ranking"] = performance
        return result

    except Exception as e:
        logger.error(f"cancel_underperforming_tasks failed: {e}")
        return {"error": str(e)}


@mcp.tool()
def stop_task(task_path: str) -> Dict[str, Any]:
    """
    Stop a single running task.

    Args:
        task_path: Absolute path to the task directory.
    """
    if not CANCELLATION_OK:
        return {"error": "task_cancellation module not available."}
    username = _username_for_task(task_path)
    if not username:
        return {"error": f"Cannot resolve hpc_username for task {task_path}."}
    with set_hpc_user(username):
        return cancel_task(task_path)



# --- chi2-bootstrap-false tools (auto-added) ---
try:
    import chi2_bootstrap_false as _chi2bf
    CHI2_BF_OK = True
except Exception as _e:
    logger.warning(f"chi2_bootstrap_false unavailable: {_e}")
    CHI2_BF_OK = False



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
    try:
        gate_is_inf = float(gate) == float("inf")
    except (TypeError, ValueError):
        gate_is_inf = False
    gate_phrase = "no convergence filter" if gate_is_inf else f"gate chi²/npts < {gate}"
    lines = [
        f"**chi² (bootstrap=False) summary — {n_surv}/{n_tot} included replicas "
        f"({gate_phrase})**",
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
    payload["markdown"] = "\n".join(lines)


@mcp.tool()
def compute_summary_bf(
    batch_id: str,
    chat_id: str = "",
    convergence_gate: float = float("inf"),
    wait_seconds: int = 45,
    verbose: bool = True,
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
        convergence_gate: replicas whose latest fit chi²/npts exceeds this
            are dropped before aggregation. Default inf = no filter (include
            every replica, and let the per-replica `run_state` in the result
            tell the user who was close to diverging). Pass a finite number
            (e.g. 3.0) to drop obviously-diverged replicas.
        wait_seconds: seconds to poll inline before returning a 'computing'
            handle. Keep ≤60 for responsive MCP calls. Set 0 to fire-and-forget.

    Returns:
        {status:'done', result:{...}, markdown: "...JAM24-style table..."}
            When verbose=True (default), `markdown` holds a chat-ready table
            the assistant SHOULD paste directly into the reply.
        or
        {status:'computing', phase, elapsed_s, eta_seconds, hint}
            Ask user to retry get_summary_bf_result(batch_id) in a few minutes.
        or
        {status:'not_connected', message, instruction_to_assistant}
            HPC is not reachable (no account configured, or SSH ControlMaster
            socket missing). The assistant MUST relay `message` to the user
            verbatim and MUST NOT invent chi² numbers or schedule a retry.
    """
    if not CHI2_BF_OK:
        return {"error": "chi2_bootstrap_false module unavailable"}
    # Ownership gate: the batch must contain rows filed under this caller.
    # Without it, anyone who can reach this unauthenticated transport could
    # trigger cluster compute against a batch id they guessed.
    if not _batch_owned_by(batch_id, chat_id):
        return {
            "error": "batch does not belong to this caller",
            "batch_id": batch_id,
        }
    hpc_username = _get_hpc_username(chat_id) if chat_id else ""

    # HPC connectivity preflight — must come BEFORE start_compute, because
    # start_compute reads a local SQLite cache first and can return a stale
    # {status:'computing'} row without ever touching SSH. If HPC is offline
    # we'd be telling the user "wait ~5 min" for a job that isn't running.
    # Refuse fast and unambiguously instead.
    if not hpc_username:
        return {
            "status": "not_connected",
            "message": (
                "No HPC account is configured for this chat. Connect via the "
                "HPC Connection button before requesting a chi² summary."
            ),
            "instruction_to_assistant": (
                "Tell the user the HPC account is not connected. Do NOT "
                "fabricate chi² numbers, do NOT promise to retry, and do "
                "NOT cite cached results."
            ),
        }
    if is_iri_enabled():
        probe_ok = is_iri_ready()
        probe_err = "" if probe_ok else "IRI transport not configured (missing token or resource ids)"
    else:
        try:
            probe = ssh_run(["echo", "ok"], timeout=5, username=hpc_username)
            probe_ok = probe.returncode == 0
            probe_err = probe.stderr.strip() if probe.stderr else ""
        except Exception as e:
            probe_ok = False
            probe_err = str(e)
    if not probe_ok:
        return {
            "status": "not_connected",
            "message": (
                f"HPC connection for '{hpc_username}' is not responding"
                f"{': ' + probe_err if probe_err else ''}. "
                "Reconnect via the HPC Connection button and retry."
            ),
            "instruction_to_assistant": (
                "Tell the user the HPC connection is down and they must "
                "reconnect. Do NOT fabricate chi² numbers, do NOT promise "
                "to retry in N minutes, and do NOT cite any cached values."
            ),
        }

    try:
        res = _chi2bf.start_compute(
            batch_id=batch_id,
            hpc_username=hpc_username or "",
            convergence_gate=convergence_gate,
        )
    except ConnectionError as e:
        return {
            "status": "not_connected",
            "message": f"Lost HPC connection during compute submission: {e}",
            "instruction_to_assistant": (
                "Tell the user the HPC link dropped mid-request and they "
                "should reconnect. Do NOT fabricate results."
            ),
        }
    # already done (cached) or error — return immediately
    if res.get("status") in ("done", "error"):
        if verbose:
            _attach_markdown(res)
        return res

    # inline poll so cached/near-done cases return a final result in one call
    import time as _time
    budget = max(0, int(wait_seconds))
    for _ in range(budget // 5):
        _time.sleep(5)
        status = _chi2bf.check_status(batch_id)
        if status.get("status") in ("done", "error"):
            if verbose:
                _attach_markdown(status)
            return status
    # still computing — return current status with a "retry later" hint
    final = _chi2bf.check_status(batch_id)
    final["hint"] = (
        "Computation still running on HPC. Tell the user to wait ~5 min "
        "then ask 'show me the chi²' again or call get_summary_bf_result."
    )
    if verbose:
        _attach_markdown(final)
    return final


@mcp.tool()
def get_summary_bf_result(batch_id: str, chat_id: str = "") -> Dict[str, Any]:
    """Poll the bootstrap=False summary job started by `compute_summary_bf`.

    Returns one of:
      {status: 'done',      result: {process_table: [...], per_idx: [...], ...}}
      {status: 'computing', phase, elapsed_s, eta_seconds}
      {status: 'error',     error}
      {status: 'not_found'}
    """
    if not CHI2_BF_OK:
        return {"error": "chi2_bootstrap_false module unavailable"}
    if not _batch_owned_by(batch_id, chat_id):
        return {"status": "error", "error": "batch does not belong to this caller",
                "batch_id": batch_id}
    return _chi2bf.check_status(batch_id)


# ---------------------------------------------------------------------------
# Replica plot — render a distribution across a chat's completed replicas
# ---------------------------------------------------------------------------

import math as _math
import subprocess as _subprocess

try:
    from replica_plot_env import ensure_env as _plot_ensure_env, FITPACK_LOCAL_DIR as _PLOT_FITPACK_DIR
    _REPLICA_PLOT_OK = True
except ImportError as _e:
    logger.warning(f"replica_plot_env import failed: {_e}")
    _REPLICA_PLOT_OK = False

_REPLICA_PLOT_WORKER = _SERVER_DIR / "replica_plot_worker.py"
_REPLICA_PLOT_WORKER_TIMEOUT_S = 300

# Per-job ndjson trace logs the SSE endpoint tails. Same root as the
# cached fitpack venv so we have one cleanup target.
_PLOT_JOBS_DIR = Path(os.environ.get(
    "FEMTOMIND_ENV_HOME",
    str(Path.home() / ".local" / "share" / "femtomind"),
)) / "jobs"
_PLOT_JOBS_DIR.mkdir(parents=True, exist_ok=True)

# The Open WebUI backend the MCP tool announces jobs to. Loopback by
# default — both processes are colocated under deploy.sh.
_OPEN_WEBUI_BACKEND = os.environ.get("FEMTOMIND_BACKEND_URL", "http://127.0.0.1:8080")


def _announce_plot_job(job_id: str, chat_id: str, message_id: Optional[str], log_path: Path) -> None:
    """Tell the backend this job exists so the frontend can subscribe SSE.

    Best-effort — if the backend is unreachable the tool still works,
    user just won't see live trace events (gets the bundled trace at the
    end like in step 1).
    """
    try:
        import requests as _req
        _req.post(
            f"{_OPEN_WEBUI_BACKEND}/api/v1/replica_plot_trace/announce",
            json={
                "job_id":     job_id,
                "chat_id":    chat_id,
                "message_id": message_id,
                "log_path":   str(log_path),
            },
            timeout=2.0,
        )
    except Exception as e:
        logger.warning(f"plot job announce failed (live trace disabled): {e!r}")


def _make_log_emit(log_path: Path, mem_sink: List[Dict[str, Any]]):
    """Return an emit fn that both appends to in-memory list AND tails to disk.

    The disk file is what the SSE endpoint reads; the in-memory list ends
    up in the final tool response (kept for reload + non-streaming clients).
    """
    def emit(icon: str, text: str, **extra) -> None:
        ev = {"icon": icon, "text": text, **extra}
        mem_sink.append(ev)
        try:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError as e:
            # never let trace IO break the actual compute
            logger.warning(f"trace log write failed for {log_path.name}: {e!r}")
    return emit


def _resolve_plot_replicas(
    chat_id: str,
    batch_ids: Optional[List[str]],
    chi2_npts_max: Optional[float],
    chi2_npts_min: Optional[float],
    limit: Optional[int],
    order_by: str,
    include_failed: bool,
    include_running: bool = False,
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """Resolve which replicas to plot. Returns (rows, auto_picked_batch_id)."""
    auto_batch_id: Optional[str] = None

    with _get_db() as conn:
        conn.row_factory = sqlite3.Row
        # Auto-select the most recently-completed batch when caller omits.
        if not batch_ids:
            # A campaign that is still running has no completed rows yet, but
            # its replicas do carry live parameters in progress.json — so the
            # auto-pick has to see running batches too, or "plot the uv pdf"
            # right after a submit finds nothing and tells the user to submit.
            _auto_states = (
                "'completed','running','submitted','pending'"
                if include_running else "'completed'"
            )
            row = conn.execute(
                f"""
                -- completed_at is never written by the worker, so ordering on
                -- it alone made "most recently completed" arbitrary. Fall back
                -- to the columns that are always populated.
                SELECT batch_id, MAX(COALESCE(completed_at, updated_at, created_at)) AS last_done
                  FROM tasks
                 WHERE chat_id = ? AND status IN ({_auto_states})
              GROUP BY batch_id
              ORDER BY last_done DESC
                 LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
            if not row or not row["batch_id"]:
                return [], None
            auto_batch_id = row["batch_id"]
            batch_ids = [auto_batch_id]

        wheres = ["t.chat_id = ?"]
        params: List[Any] = [chat_id]
        # Which task states may contribute a curve. 'completed' reads the
        # replica's parameters.yaml; the in-flight states read the current
        # parameter vector out of progress.json instead (see
        # _materialize_replica_yamls) so a fit can be watched as it moves.
        _states = ["'completed'"]
        if include_running:
            _states += ["'running'", "'submitted'", "'pending'"]
        if include_failed:
            _states += ["'failed'", "'timeout'"]
        if not (include_failed and not include_running):
            wheres.append("t.status IN (%s)" % ",".join(_states))
        placeholders = ",".join("?" * len(batch_ids))
        wheres.append(f"t.batch_id IN ({placeholders})")
        params.extend(batch_ids)
        if chi2_npts_max is not None:
            wheres.append("j.final_chi2_npts <= ?")
            params.append(chi2_npts_max)
        if chi2_npts_min is not None:
            wheres.append("j.final_chi2_npts >= ?")
            params.append(chi2_npts_min)

        # SQLite sorts NULL first on ASC. Replicas whose chi-square was never
        # recorded therefore won a chi2_asc ordering outright, so asking for
        # "the best N" returned the N with no chi-square at all — the ordering
        # was not merely inert, it was inverted. Push unknowns to the back in
        # both directions.
        order_sql = {
            "chi2_asc":  "(j.final_chi2_npts IS NULL), j.final_chi2_npts ASC",
            "chi2_desc": "(j.final_chi2_npts IS NULL), j.final_chi2_npts DESC",
            "created":   "t.created_at ASC",
        }.get(order_by, "(j.final_chi2_npts IS NULL), j.final_chi2_npts ASC")

        # Cap at 1000 even without an explicit limit so we never load the
        # whole table into JSON if a chat ran a huge sweep.
        cap = int(limit) if limit else 1000

        sql = f"""
            SELECT t.path, t.batch_id, t.status, j.final_chi2_npts AS chi2_npts
              FROM tasks t
         LEFT JOIN job_history j
                ON j.batch_id = t.batch_id AND j.task_path = t.path
             WHERE {' AND '.join(wheres)}
          ORDER BY {order_sql}
             LIMIT {cap}
        """
        rows = conn.execute(sql, params).fetchall()

    return [
        {"path": r["path"], "batch_id": r["batch_id"], "chi2_npts": r["chi2_npts"],
         "status": r["status"]}
        for r in rows if r["path"]
    ], auto_batch_id


def _materialize_replica_yamls(
    replicas: List[Dict[str, Any]],
    chat_id: str,
    on_event,
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Resolve each replica's parameters.yaml to a path the worker can open().

    tasks.path stores the replica's *remote* directory (on the HPC). For
    local-only deployments the path may already be a real local directory;
    for the more common HPC-backed deployment we fetch parameters.yaml via
    SSH into a per-batch cache under FEMTOMIND_ENV_HOME/replica_cache/.

    Returns (filtered_replicas, local_paths) — both lists are aligned and
    only contain entries that resolved successfully. The worker should be
    invoked with `local_paths`, and the rest of the response (replica_ids,
    chi2_npts, …) should be derived from `filtered_replicas`.
    """
    cache_root = Path(os.environ.get(
        "FEMTOMIND_ENV_HOME",
        str(Path.home() / ".local" / "share" / "femtomind"),
    )) / "replica_cache"

    username = _get_hpc_username(chat_id)
    n_local = sum(
        1 for r in replicas if (Path(r["path"]) / "parameters.yaml").is_file()
    )
    if n_local < len(replicas) and not username:
        on_event(
            "⚠️",
            "no HPC username configured — cannot SSH-fetch the missing replicas",
        )

    def _live_params(rep: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[str], str]:
        """Materialise an in-flight replica's CURRENT parameters.

        A running replica's parameters.yaml still holds the randomised
        starting point — it is written once at submit and never touched
        again. The live vector lives in progress.json, which the patched
        fitlib.maxlike_jam appends to every iteration as a flat
        {"<family> <key>": value} dict keyed exactly like parman.order.
        Writing it out as JSON is enough: yaml.safe_load in the worker
        parses JSON, and the worker only ever reads parameter values from
        this file.
        """
        remote_dir = rep["path"]
        replica_id = Path(remote_dir).name or "_unnamed"
        batch_id = rep.get("batch_id") or "_unknown"
        src = Path(remote_dir) / "progress.json"
        try:
            if src.is_file():
                raw = src.read_text()
            elif username:
                raw = ssh_read_file(f"{remote_dir}/progress.json", username=username)
            else:
                return rep, None, "no-hpc-user"
            doc = json.loads(raw)
            hist = doc if isinstance(doc, list) else (doc or {}).get("history") or []
            if not hist:
                return rep, None, "progress.json has no entries yet"
            last = hist[-1]
            flat = last.get("params")
            if not isinstance(flat, dict) or not flat:
                # Pre-patch fitlib: chi2 is recorded but the parameter
                # vector is not, so there is nothing to draw mid-run.
                return rep, None, "progress.json carries no params (old fitlib)"
        except Exception as e:
            return rep, None, f"live-params: {type(e).__name__}: {e}"

        out = cache_root / batch_id / replica_id / "live_params.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "femtomind_live_params": flat,
            "iter": last.get("iter"),
            "chi2": last.get("chi2"),
        }))
        rep["live_iter"] = last.get("iter")
        rep["live_chi2"] = last.get("chi2")
        return rep, str(out), "live"

    def _one(rep: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[str], str]:
        if (rep.get("status") or "completed") != "completed":
            return _live_params(rep)
        remote_dir = rep["path"]
        # Fast path: the path is already a real local file (e.g. fully
        # local fitpack deployment). No SSH needed.
        local_yaml = Path(remote_dir) / "parameters.yaml"
        if local_yaml.is_file():
            return rep, str(local_yaml), "local"

        # SSH path: cache under <cache_root>/<batch>/<replica_id>/parameters.yaml.
        replica_id = Path(remote_dir).name or "_unnamed"
        batch_id = rep.get("batch_id") or "_unknown"
        cached_yaml = cache_root / batch_id / replica_id / "parameters.yaml"
        if cached_yaml.is_file() and cached_yaml.stat().st_size > 0:
            return rep, str(cached_yaml), "cached"

        if not username:
            return rep, None, "no-hpc-user"

        # Surface the actual backend command + its real output to the
        # terminal so the user sees a literal step-by-step session
        # instead of a parallel-fetch summary. Show first 8
        # lines of yaml content with a "[N more lines]" footer so a
        # 9-replica fetch doesn't flood the panel.
        if is_iri_enabled():
            cmd_text = f'iri view "{remote_dir}/parameters.yaml"'
            source_label = "iri"
        else:
            cmd_text = f'ssh {username}@ifarm.jlab.org "cat {remote_dir}/parameters.yaml"'
            source_label = "ssh"
        on_event("$", cmd_text, kind="cmd_start")
        t0 = time.monotonic()
        try:
            content = ssh_read_file(
                f"{remote_dir}/parameters.yaml", username=username
            )
        except Exception as e:
            elapsed = round(time.monotonic() - t0, 2)
            on_event("", str(e), kind="stderr")
            on_event("✗", "exit 1", kind="cmd_end", elapsed=elapsed)
            return rep, None, f"{source_label}: {e}"
        elapsed = round(time.monotonic() - t0, 2)
        lines = content.splitlines()
        head = lines[:8]
        if head:
            on_event("", "\n".join(head), kind="stdout")
        if len(lines) > 8:
            on_event("", f"[... {len(lines) - 8} more lines, {len(content):,} bytes total]",
                     kind="stdout")
        on_event("✓", "exit 0", kind="cmd_end", elapsed=elapsed)
        try:
            cached_yaml.parent.mkdir(parents=True, exist_ok=True)
            cached_yaml.write_text(content)
        except OSError as e:
            return rep, None, f"write: {e}"
        return rep, str(cached_yaml), f"{source_label}-fetched"

    on_event(
        "⬇️",
        f"materializing {len(replicas)} replica yamls "
        f"(local={n_local}, backend-needed={len(replicas) - n_local})",
        section="replica-fetch",
    )

    # Serial fetch — each ssh command gets its own clean cmd_start /
    # output / cmd_end block in the terminal. The previous threadpool
    # parallelism (8 workers) made the trace unreadable: events from
    # 8 concurrent fetches interleaved arbitrarily. Cost is
    # 9 × ~0.4s = ~3.6s vs ~0.4s parallel — acceptable tradeoff for
    # the "watch the AI work step by step" UX.
    results = [_one(rep) for rep in replicas]

    by_kind: Dict[str, int] = {}
    failures: List[tuple[Dict[str, Any], str]] = []
    for rep, path, kind in results:
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if path is None:
            failures.append((rep, kind))

    summary = ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items()))
    on_event("📥", f"replica yaml resolution: {summary}")
    for rep, why in failures[:3]:
        on_event("⚠️", f"skipping {Path(rep['path']).name}: {why}")
    if len(failures) > 3:
        on_event("⚠️", f"... {len(failures) - 3} more failures hidden")

    ok = [(r, p) for r, p, _ in results if p is not None]
    return [r for r, _ in ok], [p for _, p in ok]


def _expand_x_grid(use_log: bool, lo: float, hi: float, n: int) -> List[float]:
    n = max(2, int(n))
    if use_log:
        a, b = _math.log10(lo), _math.log10(hi)
        return [10 ** (a + i * (b - a) / (n - 1)) for i in range(n)]
    return [lo + i * (hi - lo) / (n - 1) for i in range(n)]


def _run_plot_worker_remote(
    fitpack_root: str,
    config_file: str,
    replica_paths: List[str],
    dist: str,
    flavors: List[str],
    q2: float,
    x_grid: List[float],
    on_event,
    username: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the compute worker ON THE HPC instead of locally.

    The replica yamls already live on the cluster and the cluster already has
    a complete fitpack checkout plus the apptainer image, so computing there
    avoids both the local env bootstrap and the per-replica SSH download — and
    removes the requirement that this host carry a full fitpack tree at all.

    Same contract as the local runner: JSON request on stdin, JSON response on
    stdout, `TRACE:` lines on stderr.
    """
    sif = os.environ.get(
        "FITPACK_SIF", "/u/scigroup/scicomp/jupyterhub/theory/theory-notebook.sif"
    )
    ceph_root = os.environ.get("HPC_CEPH_ROOT", "/w/ceph24")
    # One file per invocation. Two plots asked for at once — "compare the gluon
    # at Q2=10 and Q2=100" is a single sentence that produces two calls — both
    # staged to the same fixed path, so one truncated the script while the other
    # was executing it. The loser fell through to the local worker and reported
    # a uv bootstrap failure, which says nothing about what actually went wrong.
    _tag = uuid.uuid4().hex[:8]
    remote_worker = f"{fitpack_root.rstrip('/')}/.femtomind/replica_plot_worker.{_tag}.py"

    on_event("📤", "staging compute worker on HPC", kind="phase")
    ssh_mkdir(remote_worker.rsplit("/", 1)[0], username=username)
    ssh_write_file(remote_worker, _REPLICA_PLOT_WORKER.read_text(), username=username)

    payload = {
        "fitpack_root":  fitpack_root,
        "config_file":   config_file,
        "replica_paths": replica_paths,
        "dist":    dist,
        "flavors": flavors,
        "q2":      q2,
        "x_grid":  x_grid,
    }
    # No trailing cleanup here: ifarm's login shell is tcsh, where `_rc=$?` is a
    # syntax error, and appending it turned a compute that had already finished
    # into a non-zero exit — the caller then fell back to the local worker and
    # reported a uv failure. Stale per-call worker files are swept below.
    inner = (
        f"cd {fitpack_root} && "
        f"apptainer exec --bind {ceph_root} {sif} python3 {remote_worker}"
    )
    on_event(
        "$",
        f"ssh ifarm apptainer exec … python3 replica_plot_worker.py "
        f"--dist {dist} --flavors {','.join(flavors)} --q2 {q2:g} "
        f"--replicas {len(replica_paths)}",
        kind="cmd_start",
    )
    t0 = time.monotonic()
    res = ssh_run(
        ["sh", "-c", inner],
        timeout=_REPLICA_PLOT_WORKER_TIMEOUT_S,
        input_data=json.dumps(payload),
        username=username,
    )
    # stderr carries TRACE: events plus harmless container/tcsh chatter
    for line in (res.stderr or "").splitlines():
        if line.startswith("TRACE:"):
            try:
                ev = json.loads(line[len("TRACE:"):])
                on_event(ev.pop("icon", "•"), ev.pop("text", ""), **ev)
            except Exception:
                pass
    on_event(
        "✓",
        f"remote compute finished in {time.monotonic() - t0:.1f}s",
        kind="cmd_end",
    )
    if res.returncode != 0:
        tail = (res.stderr or "").strip().splitlines()[-4:]
        raise RuntimeError(
            f"remote replica_plot_worker exited {res.returncode}: {' | '.join(tail)}"
        )
    for line in reversed((res.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError("remote replica_plot_worker returned no parseable JSON")


def _run_plot_worker(
    fitpack_python: str,
    fitpack_root: str,
    config_file: str,
    replica_paths: List[str],
    dist: str,
    flavors: List[str],
    q2: float,
    x_grid: List[float],
    on_event,
) -> Dict[str, Any]:
    """Invoke replica_plot_worker.py and stream TRACE: events line-by-line.

    Uses Popen + a background thread reading stderr (per the AI-Design
    pattern in utils/task/TaskOutput.ts) so events reach the SSE endpoint
    as they happen, not in one batch when the subprocess exits. on_event
    is called with each parsed event dict.
    """
    payload = {
        "fitpack_root":  fitpack_root,
        "config_file":   config_file,
        "replica_paths": replica_paths,
        "dist":    dist,
        "flavors": flavors,
        "q2":      q2,
        "x_grid":  x_grid,
    }
    pythonpath = os.pathsep.join([
        fitpack_root, str(_SERVER_DIR), os.environ.get("PYTHONPATH", "")
    ]).rstrip(os.pathsep)

    # Surface the actual subprocess invocation in the terminal panel
    # so the user sees the real command, not just abstract phase events.
    # The replica_paths list can be long; show a count rather than all
    # paths to keep the prompt line readable.
    cmd_label = (
        f"{fitpack_python} {_REPLICA_PLOT_WORKER.name} "
        f"--dist {dist} --flavors {','.join(flavors)} --q2 {q2:g} "
        f"--replicas {len(replica_paths)}"
    )
    on_event("$", cmd_label, kind="cmd_start")
    worker_t0 = time.monotonic()

    proc = _subprocess.Popen(
        [fitpack_python, str(_REPLICA_PLOT_WORKER)],
        stdin=_subprocess.PIPE,
        stdout=_subprocess.PIPE,
        stderr=_subprocess.PIPE,
        text=True,
        cwd=fitpack_root,
        env={**os.environ, "PYTHONPATH": pythonpath},
        bufsize=1,  # line-buffered
    )

    import threading as _threading
    stderr_lines: List[str] = []

    def _drain_stderr():
        for line in proc.stderr:
            line = line.rstrip("\n")
            stderr_lines.append(line)
            if line.startswith("TRACE:"):
                try:
                    ev = json.loads(line[len("TRACE:"):])
                except json.JSONDecodeError:
                    continue
                try:
                    on_event(
                        ev.pop("icon", "•"),
                        ev.pop("text", ""),
                        **ev,
                    )
                except Exception as e:
                    logger.warning(f"on_event raised: {e!r}")
            elif line.strip():
                # Non-TRACE stderr line — forward as a terminal stderr
                # event so the user sees real Python tracebacks, RESMAN
                # init logs, deprecation warnings, etc. instead of
                # silently swallowing them.
                try:
                    on_event("", line, kind="stderr")
                except Exception as e:
                    logger.warning(f"on_event(stderr) raised: {e!r}")

    # Start the background drain thread BEFORE communicate() so stderr
    # events stream live. Earlier edit accidentally removed these two
    # lines; without them the thread was defined-but-never-started and
    # the subsequent `t.join` raised NameError("name 't' is not defined")
    # — surfaced as the plot tool's "internal issue" the user saw.
    t = _threading.Thread(target=_drain_stderr, daemon=True)
    t.start()

    try:
        stdout, _ = proc.communicate(
            input=json.dumps(payload),
            timeout=_REPLICA_PLOT_WORKER_TIMEOUT_S,
        )
    except _subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        worker_elapsed = round(time.monotonic() - worker_t0, 2)
        on_event("✗", "exit timeout", kind="cmd_end", elapsed=worker_elapsed)
        raise RuntimeError(
            f"replica_plot_worker timed out after {_REPLICA_PLOT_WORKER_TIMEOUT_S}s"
        )
    finally:
        t.join(timeout=2.0)

    worker_elapsed = round(time.monotonic() - worker_t0, 2)
    rc = proc.returncode

    if rc != 0:
        try:
            err = json.loads((stdout or "").strip().splitlines()[-1]).get("error", "")
        except (json.JSONDecodeError, IndexError):
            err = ("\n".join(stderr_lines) or stdout or "")[:600]
        # Surface the resolved error in the terminal as a red stderr line
        # *before* cmd_end. The drain thread forwards real-time stderr,
        # but if the worker died via _die() the only failure info lives on
        # stdout (single-line JSON), and was previously silently swallowed
        # by proc.communicate() — user just saw "exit 1" with no context.
        if err:
            on_event("", f"replica_plot_worker failed: {err}", kind="stderr")
        on_event("✗", f"exit {rc}", kind="cmd_end", elapsed=worker_elapsed)
        raise RuntimeError(f"replica_plot_worker failed: {err[:600]}")

    on_event("✓", "exit 0", kind="cmd_end", elapsed=worker_elapsed)

    try:
        return json.loads(stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise RuntimeError("replica_plot_worker returned no parseable JSON")


_PLOT_REPRODUCER_TEMPLATE = '''\
# ===================================================================
# Auto-generated reproducer — plot x*{{flavor}}(x, Q²={q2:g}) for
# flavors={flavors!r} from {n} replicas
# Generated by FemtoMind plot_replica_observable
# ===================================================================
import os, sys, yaml
import numpy as np
import matplotlib.pyplot as plt

FITPACK_ROOT = {fitpack_root!r}
sys.path.insert(0, FITPACK_ROOT)
os.chdir(FITPACK_ROOT)

import nuclib.deuterium, nuclib.helium
class _Stub:
    def __init__(self, *a, **kw): pass
nuclib.deuterium.DEUTERON = _Stub
nuclib.helium.HELIUM = _Stub

from tools.config import load_config, conf
load_config({config_file!r})
os.environ.setdefault("FITPACK", "")
from fitlib.resman import RESMAN
resman = RESMAN(None, parallel=False, datasets=False)

replica_paths = {replica_paths!r}

def _par_from_replica(rep_conf, order, fallback):
    par = np.array(fallback, dtype=float, copy=True)
    for i, (kind, k, kk) in enumerate(order):
        try:
            par[i] = (rep_conf["params"][k][kk]["value"] if kind == 1
                      else rep_conf["datasets"][k]["norm"][kk]["value"])
        except KeyError:
            pass
    return par

_central = np.array(resman.parman.par, copy=True)
parameters = [
    _par_from_replica(yaml.safe_load(open(p))["conf"], resman.parman.order, _central)
    for p in replica_paths
]

DIST, FLAVORS, Q2 = {dist!r}, {flavors!r}, {q2}
X_GRID = np.{xgrid_expr}

curves_by_flavor = {{f: [] for f in FLAVORS}}
for par in parameters:
    resman.parman.set_new_params(par)
    for f in FLAVORS:
        curves_by_flavor[f].append([conf[DIST].get_xF(x, Q2, f) for x in X_GRID])

for f, group in curves_by_flavor.items():
    arr = np.asarray(group)
    for c in arr:
        plt.plot(X_GRID, c, alpha=0.5, label=f if c is arr[0] else None)
plt.xscale("log")
plt.yscale("log")
plt.xlabel("x")
plt.ylabel(f"x·flavor(x, Q²={{Q2:g}})")
plt.legend()
plt.tight_layout()
plt.savefig("replica_curves.png", dpi=120)
print("saved replica_curves.png")
'''


@mcp.tool()
def plot_replica_observable(
    chat_id: str,
    dist: str = "pdf",
    flavors: Optional[List[str]] = None,
    q2: float = 10.0,
    batch_ids: Optional[List[str]] = None,
    chi2_npts_max: Optional[float] = None,
    chi2_npts_min: Optional[float] = None,
    limit: Optional[int] = None,
    order_by: str = "chi2_asc",
    include_failed: bool = False,
    include_running: bool = True,
    x_grid_min: float = 1e-4,
    x_grid_max: float = 0.99,
    x_grid_n: int = 120,
    x_grid_log: bool = True,
    config_file: str = "examples/input.py",
    message_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Plot one or more distributions across the replicas this chat produced.

    Computes ALL requested flavors in a single tool call (one RESMAN
    bootstrap, inner loop over flavors), and returns them in a single
    interactive widget where the user toggles which flavors to display.
    Always prefer one call with multiple flavors over multiple calls.

    Auto-selects the most recently completed batch for `chat_id` when
    `batch_ids` is empty. Bootstraps a fitpack-only python env via uv on
    first use (~40s; cached after that). Worker projects each replica's
    parameters.yaml onto RESMAN's parman.order vector and evaluates
    conf[dist].get_xF(x, Q², flavor) for each (replica, flavor) pair.

    The Open WebUI middleware uploads the returned curves as a gzipped
    blob and renders an inline interactive plot + a Claude-Code-style
    trace block (env bootstrap + compute timings). Do NOT narrate the
    curves — the user sees them directly. Just say what was plotted and
    ask what to do next.

    LLM should fill ONLY the args the user explicitly asked for.

    Args:
        chat_id: REQUIRED. Auto-injected from session metadata.
        dist:    'pdf' (default), 'ppdf', 'tpdf', 'ffpion', 'ffkaon'.
        flavors: List of flavor names to compute. If omitted, defaults
                 to ['uv','dv','g','sea'] — the four standard PDF
                 flavors. Pass an explicit list when the user names
                 specific flavors (e.g. user says "plot sea and s" →
                 flavors=['sea','s']). The user can later toggle which
                 flavors to display in the rendered widget without
                 triggering another tool call, so over-including is
                 cheaper than under-including.
        q2:      Default 10.0 GeV².
        batch_ids: Subset of batches; empty = latest batch that has
                 replicas (running batches count, see include_running).
        chi2_npts_max / chi2_npts_min: Filter replicas by χ²/Npts.
        limit / order_by: Sort + cap (e.g. limit=10 order_by='chi2_asc').
        include_running: Default True. Also draws replicas that are
                 still fitting, taking the parameter vector from the
                 latest progress.json iteration instead of the
                 randomised starting point in parameters.yaml. Those
                 curves are a snapshot of a fit in motion and WILL
                 move — when any are included, say how many are still
                 fitting rather than presenting the band as final.
                 Pass False for finished replicas only.
        x_grid_*: x-axis grid; defaults are usually fine.
        message_id: Auto-injected by middleware. DO NOT fill manually.

    Returns:
        On success — a dict with the discriminator
        kind="femtomind.replica_plot" that the middleware unwraps into
        an inline plot widget. The text the LLM sees is just a short
        confirmation; the actual curves go straight to the user's UI.

        On error — {error: "...", kind: "femtomind.replica_plot.error"}.
    """
    if not _REPLICA_PLOT_OK:
        return {
            "kind": "femtomind.replica_plot.error",
            "error": "replica_plot_env not importable on the MCP server",
        }
    if not chat_id:
        return {
            "kind": "femtomind.replica_plot.error",
            "error": "chat_id is required",
        }

    # Default to the four standard PDF flavors when caller omits.
    if not flavors:
        flavors = ["uv", "dv", "g", "sea"]

    # Per-job trace log — the SSE endpoint tails this for live updates.
    job_id = uuid.uuid4().hex
    log_path = _PLOT_JOBS_DIR / f"{job_id}.ndjson"
    log_path.touch()  # create empty so the SSE endpoint doesn't 404 on race

    trace: List[Dict[str, Any]] = []
    emit = _make_log_emit(log_path, trace)

    # Tell backend the job exists so the frontend opens an EventSource.
    # Best-effort: on failure, live trace is silently disabled but the
    # final bundled trace still works (step-1 fallback).
    _announce_plot_job(job_id, chat_id, message_id, log_path)

    # ── 1. Resolve replicas ────────────────────────────────────────────
    replicas, auto_batch = _resolve_plot_replicas(
        chat_id=chat_id,
        batch_ids=batch_ids,
        chi2_npts_max=chi2_npts_max,
        chi2_npts_min=chi2_npts_min,
        limit=limit,
        order_by=order_by,
        include_failed=include_failed,
        include_running=include_running,
    )
    if not replicas:
        emit("⚠️", "no replicas matched this chat", _final=True, status="error")
        return {
            "kind": "femtomind.replica_plot.error",
            "error": "no replicas matched this chat — submit a fit first",
        }
    n_running = sum(1 for r in replicas if (r.get("status") or "completed") != "completed")
    n_done = len(replicas) - n_running
    auto_note = f" (auto-picked latest batch {auto_batch[:12]}…)" if auto_batch else ""
    live_note = f", {n_running} still fitting" if n_running else ""
    emit("📁", f"resolved {len(replicas)} replicas{live_note}{auto_note}",
         n_replicas=len(replicas), n_completed=n_done, n_running=n_running,
         auto_batch_id=auto_batch)

    # ── 2-3. Pick where the curves get computed ────────────────────────
    # Preferred: on the HPC, where the replica yamls and a complete fitpack
    # checkout already live — no local env, no per-replica download. Falls
    # back to the local worker (which needs a full fitpack tree on this host).
    x_grid = _expand_x_grid(x_grid_log, x_grid_min, x_grid_max, x_grid_n)
    hpc_user = _get_hpc_username(chat_id)
    use_hpc = (
        os.environ.get("FEMTOMIND_PLOT_ON_HPC", "1").lower() not in ("0", "false", "no")
        and bool(hpc_user)
    )

    # Both are reported back in the reproducer, so they must be set on either
    # path — pointing at whichever machine actually did the work.
    worker_result = None
    fitpack_root: str = ""
    local_yaml_paths: List[str] = []
    if use_hpc:
        remote_root = get_remote_base(hpc_user)
        remote_yamls = [f"{r['path'].rstrip('/')}/parameters.yaml" for r in replicas]
        fitpack_root, local_yaml_paths = remote_root, remote_yamls
        emit("🖥️", f"computing on HPC ({len(remote_yamls)} replicas)", kind="phase")
        try:
            worker_result = _run_plot_worker_remote(
                fitpack_root=remote_root,
                config_file=config_file,
                replica_paths=remote_yamls,
                dist=dist,
                flavors=flavors,
                q2=float(q2),
                x_grid=x_grid,
                on_event=emit,
                username=hpc_user,
            )
        except Exception as e:
            emit("⚠️", f"HPC compute failed ({e}); falling back to local", kind="phase")
            worker_result = None

    try:
        if worker_result is None:
            def _bootstrap_emit(ev):
                emit(ev.pop("icon", "•"), ev.pop("text", ""), **ev)
            env = _plot_ensure_env(on_event=_bootstrap_emit)
            replicas, local_yaml_paths = _materialize_replica_yamls(
                replicas, chat_id, emit
            )
            if not local_yaml_paths:
                emit(
                    "❌",
                    "no replica yamls could be loaded — check HPC connectivity "
                    "and ensure user_hpc_config is set for your chat",
                    _final=True,
                    status="error",
                )
                return {
                    "kind": "femtomind.replica_plot.error",
                    "error": "all replica yaml fetches failed",
                    "trace": trace,
                }
            fitpack_root = env["fitpack_root"]
            worker_result = _run_plot_worker(
                fitpack_python=env["python"],
                fitpack_root=fitpack_root,
                config_file=config_file,
                replica_paths=local_yaml_paths,
                dist=dist,
                flavors=flavors,
                q2=float(q2),
                x_grid=x_grid,
                on_event=emit,
            )
    except RuntimeError as e:
        emit("❌", str(e), _final=True, status="error")
        return {
            "kind": "femtomind.replica_plot.error",
            "error": str(e),
            "trace": trace,
        }

    # ── 5. Assemble payload ────────────────────────────────────────────
    # tasks.path is the replica DIRECTORY, not the parameters.yaml file
    # — so the replica id is the basename, not the parent's basename.
    replica_ids = [Path(r["path"]).name for r in replicas]
    if x_grid_log:
        xgrid_expr = (
            f"logspace(np.log10({x_grid_min!r}), np.log10({x_grid_max!r}), {x_grid_n})"
        )
    else:
        xgrid_expr = f"linspace({x_grid_min!r}, {x_grid_max!r}, {x_grid_n})"
    # Use the local cached yaml paths the worker actually opens — they're
    # what someone re-running the reproducer on the same machine needs.
    reproducer = _PLOT_REPRODUCER_TEMPLATE.format(
        flavors=flavors, q2=q2, n=len(replicas),
        fitpack_root=fitpack_root, config_file=config_file,
        replica_paths=local_yaml_paths,
        dist=dist, xgrid_expr=xgrid_expr,
    )
    emit(
        "✅",
        f"rendered {dist}.{{{','.join(flavors)}}} at Q²={q2:g} "
        f"from {len(replicas)} replicas",
        _final=True, status="ok",
    )

    return {
        "kind": "femtomind.replica_plot",
        "job_id": job_id,
        "spec": {
            "params": {
                "dist":    dist,
                "flavors": flavors,
                "q2":      float(q2),
                "x_grid":  {
                    "type": "log" if x_grid_log else "lin",
                    "min":  x_grid_min,
                    "max":  x_grid_max,
                    "n":    x_grid_n,
                },
                "chat_id": chat_id,
            },
            "replica_ids": replica_ids,
            "panel_state": {
                # y_log defaults true so different-magnitude flavors
                # (uv vs sea) don't squish each other on a linear scale.
                "x_log": True,
                "y_log": True,
                "ylim":  None,
                "band":  False,
                "hidden_replicas": [],
                "hidden_flavors":  [],
            },
            "trace":      trace,
            "reproducer": reproducer,
        },
        "curves_inline": {
            "x_grid":           worker_result["x_grid"],
            "curves_by_flavor": worker_result["curves_by_flavor"],
            "flavors":          worker_result.get("flavors", flavors),
            "chi2_npts":        [r["chi2_npts"] for r in replicas],
            "replica_ids":      replica_ids,
        },
        "auto_batch_id": auto_batch,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Starting FitPack MCP Server…")
    _init_db()

    if STATUS_MONITOR_OK:
        start_monitor()
        logger.info("Background status monitor started.")
    else:
        logger.warning("Status monitor not available.")

    mcp.run()


if __name__ == "__main__":
    main()
