"""
Self-contained SLURM utilities for the FitPack MCP server.

Provides SLURM script generation, job-ID parsing, and cluster status queries.
No external dependencies on the agents/ module.
"""
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ssh_runner import ssh_run
from iri_backend import is_iri_enabled, iri_list_jobs


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _run_cmd(
    command: List[str], timeout: int = 10, username: Optional[str] = None
) -> "subprocess.CompletedProcess":
    """Execute a command on the remote HPC via SSH."""
    return ssh_run(command, timeout=timeout, username=username)


# ---------------------------------------------------------------------------
# Job-ID parsing
# ---------------------------------------------------------------------------

def parse_job_id(sbatch_output: str) -> Optional[str]:
    """
    Extract the job ID from sbatch-like stdout.

    Example:
        >>> parse_job_id("Submitted batch job 67890")
        '67890'
        >>> parse_job_id("Submitted batch job job-abc")
        'job-abc'
        >>> parse_job_id("bad output")
        None
    """
    match = re.search(r"Submitted batch job (\S+)", sbatch_output)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Cluster introspection
# ---------------------------------------------------------------------------

def get_server_info(username: Optional[str] = None) -> Dict[str, Any]:
    """
    Query detailed SLURM cluster information: per-partition idle/total CPUs
    and memory, global queue depth, and hostname.

    Args:
        username: HPC username to run the queries as. Required under
                  multi-tenant deployments.

    Returns a dict with keys: success, hostname, partitions (detailed list),
    cluster_totals, queue_status, message.
    """
    try:
        if is_iri_enabled():
            jobs = iri_list_jobs()
            running = pending = 0
            for job in jobs:
                status = job.get("status") if isinstance(job.get("status"), dict) else {}
                state = str(status.get("state", "")).lower()
                if state == "active":
                    running += 1
                elif state in {"new", "queued", "held"}:
                    pending += 1
            return {
                "success": True,
                "hostname": "ifarm.jlab.org (via IRI)",
                "partitions": [{
                    "name": os.environ.get("IRI_DEFAULT_QUEUE", "ifarm"),
                    "available": "up",
                    "nodes": 1,
                    "cpus_allocated": running,
                    "cpus_idle": 0,
                    "cpus_other": 0,
                    "cpus_total": 0,
                    "mem_per_node_mb": 0,
                    "cpus_per_node": 0,
                }],
                "cluster_totals": {
                    "idle_cpus": 0,
                    "total_cpus": 0,
                    "idle_nodes": 0,
                    "total_nodes": 1,
                    "mem_per_node_mb": 0,
                },
                "queue_status": {"running": running, "pending": pending, "total": running + pending},
                "message": "HPC resource info retrieved through IRI",
            }

        hostname = ""
        try:
            r = _run_cmd(["hostname", "-f"], timeout=2, username=username)
            if r.returncode == 0:
                hostname = r.stdout.strip()
        except Exception:
            pass

        # --- Per-partition resource snapshot ---
        # sinfo format: partition | available | nodes | cpus_state(A/I/O/T) | free_mem | cpus_per_node
        partitions: List[Dict[str, Any]] = []
        cluster_idle_cpus = 0
        cluster_total_cpus = 0
        cluster_idle_nodes = 0
        cluster_total_nodes = 0
        cluster_mem_per_node_mb = 0
        try:
            r = _run_cmd(["sinfo", "-h", "-o", "%P %a %D %C %m %c"], timeout=5, username=username)
            if r.returncode == 0:
                seen_partitions: Dict[str, Dict[str, Any]] = {}
                for line in r.stdout.strip().split("\n"):
                    fields = line.split()
                    if len(fields) < 6:
                        continue
                    pname = fields[0].rstrip("*")
                    avail = fields[1]          # up / down
                    node_count = int(fields[2]) if fields[2].isdigit() else 0
                    # CPU state: allocated/idle/other/total
                    cpu_parts = fields[3].split("/")
                    if len(cpu_parts) == 4:
                        cpu_alloc = int(cpu_parts[0])
                        cpu_idle = int(cpu_parts[1])
                        cpu_other = int(cpu_parts[2])
                        cpu_total = int(cpu_parts[3])
                    else:
                        cpu_alloc = cpu_idle = cpu_other = cpu_total = 0
                    mem_mb = int(fields[4]) if fields[4].isdigit() else 0
                    cpus_per_node = int(fields[5]) if fields[5].isdigit() else 0

                    if pname not in seen_partitions:
                        seen_partitions[pname] = {
                            "name": pname,
                            "available": avail,
                            "nodes": 0,
                            "cpus_allocated": 0,
                            "cpus_idle": 0,
                            "cpus_other": 0,
                            "cpus_total": 0,
                            "mem_per_node_mb": mem_mb,
                            "cpus_per_node": cpus_per_node,
                        }
                    p = seen_partitions[pname]
                    p["nodes"] += node_count
                    p["cpus_allocated"] += cpu_alloc
                    p["cpus_idle"] += cpu_idle
                    p["cpus_other"] += cpu_other
                    p["cpus_total"] += cpu_total
                    if mem_mb > p["mem_per_node_mb"]:
                        p["mem_per_node_mb"] = mem_mb
                    if cpus_per_node > p["cpus_per_node"]:
                        p["cpus_per_node"] = cpus_per_node

                for p in seen_partitions.values():
                    if p["available"] == "up":
                        partitions.append(p)
                        cluster_idle_cpus += p["cpus_idle"]
                        cluster_total_cpus += p["cpus_total"]
                        idle_nodes = p["cpus_idle"] // p["cpus_per_node"] if p["cpus_per_node"] else 0
                        cluster_idle_nodes += idle_nodes
                        cluster_total_nodes += p["nodes"]
                        if p["mem_per_node_mb"] > cluster_mem_per_node_mb:
                            cluster_mem_per_node_mb = p["mem_per_node_mb"]
        except Exception:
            pass

        # --- Global queue depth ---
        running = pending = 0
        try:
            r = _run_cmd(["squeue", "-h", "-o", "%t"], timeout=5, username=username)
            if r.returncode == 0:
                statuses = [s.strip() for s in r.stdout.strip().split("\n") if s.strip()]
                running = sum(1 for s in statuses if s == "R")
                pending = sum(1 for s in statuses if s == "PD")
        except Exception:
            pass

        return {
            "success": True,
            "hostname": hostname or "HPC Cluster",
            "partitions": partitions,
            "cluster_totals": {
                "idle_cpus": cluster_idle_cpus,
                "total_cpus": cluster_total_cpus,
                "idle_nodes": cluster_idle_nodes,
                "total_nodes": cluster_total_nodes,
                "mem_per_node_mb": cluster_mem_per_node_mb,
            },
            "queue_status": {"running": running, "pending": pending, "total": running + pending},
            "message": "Server info retrieved successfully",
        }

    except Exception as e:
        return {
            "success": False,
            "hostname": "HPC Cluster",
            "partitions": [],
            "cluster_totals": {
                "idle_cpus": 0, "total_cpus": 0,
                "idle_nodes": 0, "total_nodes": 0,
                "mem_per_node_mb": 0,
            },
            "queue_status": {"running": 0, "pending": 0, "total": 0},
            "message": f"Could not retrieve server info: {e}",
        }


def get_user_usage(username: Optional[str] = None) -> Dict[str, Any]:
    """
    Query the current user's own SLURM resource consumption: running jobs,
    CPUs and memory already allocated.

    Args:
        username: HPC username. If None, queries all of the caller's jobs.

    Returns:
        Dict with running_jobs, running_cpus, pending_jobs, pending_cpus.
    """
    result = {"running_jobs": 0, "running_cpus": 0, "pending_jobs": 0, "pending_cpus": 0}
    try:
        if is_iri_enabled():
            for job in iri_list_jobs():
                status = job.get("status") if isinstance(job.get("status"), dict) else {}
                state = str(status.get("state", "")).lower()
                spec = job.get("job_spec") if isinstance(job.get("job_spec"), dict) else {}
                resources = spec.get("resources") if isinstance(spec.get("resources"), dict) else {}
                try:
                    cpus = int(resources.get("cpu_cores_per_process") or 1) * int(resources.get("process_count") or 1)
                except Exception:
                    cpus = 1
                if state == "active":
                    result["running_jobs"] += 1
                    result["running_cpus"] += cpus
                elif state in {"new", "queued", "held"}:
                    result["pending_jobs"] += 1
                    result["pending_cpus"] += cpus
            return result

        cmd = ["squeue", "-h", "-o", "%t %C"]
        if username:
            cmd.extend(["-u", username])
        r = _run_cmd(cmd, timeout=5, username=username)
        if r.returncode == 0:
            for line in r.stdout.strip().split("\n"):
                parts = line.split()
                if len(parts) < 2:
                    continue
                state = parts[0].strip()
                try:
                    cpus = int(parts[1])
                except ValueError:
                    cpus = 0
                if state == "R":
                    result["running_jobs"] += 1
                    result["running_cpus"] += cpus
                elif state == "PD":
                    result["pending_jobs"] += 1
                    result["pending_cpus"] += cpus
    except Exception:
        pass
    return result


def get_default_slurm_config() -> Dict[str, Any]:
    """
    Build a default SLURM job configuration for ifarm.jlab.org.
    """
    return {
        "account": "",
        "partition": "production",
        "nodes": 1,
        "ntasks": 1,
        "cpus": 4,
        "time": "24:00:00",
        "reservation": "",
        "constraint": "",
        "job_name": "fitpack",
    }


# ---------------------------------------------------------------------------
# Script generation
# ---------------------------------------------------------------------------

def generate_slurm_script(
    input_file: Path,
    output_dir: Path,
    workflow: Dict[str, Any],
    slurm_config: Optional[Dict] = None,
    prior_path: Optional[str] = None,
) -> str:
    """
    Generate an sbatch script for a FitPack PDF fitting job.

    Args:
        input_file:   Path to the parameters YAML file passed to fitlib.
        output_dir:   Path where SLURM logs and fit output will be written.
        workflow:     Workflow metadata dict (currently unused beyond filename).
        slurm_config: Optional overrides for the SLURM header options.

    Returns:
        Complete sbatch script as a string.
    """
    config = get_default_slurm_config()
    if slurm_config:
        config.update(slurm_config)

    input_str = str(input_file)
    output_str = str(output_dir)
    # Root of the ceph mount as seen on the compute node. apptainer does not
    # auto-bind it, and it is NOT reachable as /w/ceph24 on ifarm, so both the
    # bind and the shared-grid lookup must use the real mount point.
    ceph_root = os.environ.get("HPC_CEPH_ROOT", "/w/ceph24")
    # Derive fitpack_dir from the task path: /home/{user}/fitpack_legacy/runs/... → /home/{user}/fitpack_legacy
    fitpack_dir = config.get("fitpack_dir", "")
    if not fitpack_dir:
        # Auto-detect from output_dir: strip /runs/... suffix
        parts = str(output_dir).split("/runs/")
        fitpack_dir = parts[0] if len(parts) > 1 else str(output_dir)

    # Build SBATCH header lines, omitting empty optional fields
    sbatch_lines = ["#!/bin/bash"]
    if config.get("account", "").strip():
        sbatch_lines.append(f"#SBATCH --account={config['account']}")
    sbatch_lines.append(f"#SBATCH --job-name={config['job_name']}")
    if config.get("partition", "").strip():
        sbatch_lines.append(f"#SBATCH --partition={config['partition']}")
    sbatch_lines.append(f"#SBATCH --nodes={config['nodes']}")
    sbatch_lines.append(f"#SBATCH --ntasks={config.get('ntasks', 1)}")
    sbatch_lines.append(f"#SBATCH --cpus-per-task={config['cpus']}")
    sbatch_lines.append(f"#SBATCH --time={config['time']}")
    if config.get("mem", "").strip():
        sbatch_lines.append(f"#SBATCH --mem={config['mem']}")
    if config.get("reservation", "").strip():
        sbatch_lines.append(f"#SBATCH --reservation={config['reservation']}")
    if config.get("constraint", "").strip():
        sbatch_lines.append(f"#SBATCH --constraint={config['constraint']}")
    sbatch_lines.append(f"#SBATCH --output={output_str}/slurm_%j.out")
    sbatch_lines.append(f"#SBATCH --error={output_str}/slurm_%j.err")
    header = "\n".join(sbatch_lines)

    script = f"""{header}

# Job information
echo "=============================================="
echo "Job ID: $SLURM_JOB_ID"
echo "Job Name: $SLURM_JOB_NAME"
echo "Node: $SLURM_NODELIST"
echo "Start time: $(date)"
echo "=============================================="

# Set CPU thread limits
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1

FITPACK_DIR="{fitpack_dir}"
cd "$FITPACK_DIR" || exit 1

# Link shared grids from the JAM area if not already present.
# On ifarm the ceph mount is /w/ceph24 ( /work/JAM is a symlink to it ); a bare
# /w/ceph24 exists but is an empty root-owned stub, so it must not be used here.
CEPH_GRIDS="{ceph_root}/JAM/share/fitpack_grids"
for obs in dy zrap wasym wzrv jet wc pjet dypion pp_hadron; do
    if [ -d "$CEPH_GRIDS/$obs" ] && [ ! -e "$FITPACK_DIR/grids/$obs" ]; then
        # -f replaces a link left pointing at a path that no longer resolves
        # (plain `ln -s` fails with "File exists" and leaves it broken);
        # -n keeps us from descending into an existing directory link.
        ln -sfn "$CEPH_GRIDS/$obs" "$FITPACK_DIR/grids/$obs"
        echo "Linked grids/$obs -> $CEPH_GRIDS/$obs"
    fi
done

echo "Project root: $FITPACK_DIR"
echo "Input file: {input_str}"
echo "Output directory: {output_str}"

# Ensure output directory exists
mkdir -p "{output_str}"

# Run PDF fit via apptainer container
FITPACK_SIF="${{FITPACK_SIF:-/u/scigroup/scicomp/jupyterhub/theory/theory-notebook.sif}}"
apptainer exec \\
    --bind {ceph_root} \\
    "$FITPACK_SIF" \\
    python3 -m fitlib.maxlike_jam \\
    "{input_str}" \\
    "{output_str}" \\
    -n {config['cpus']} \\
    -v 10{f' --prior "{prior_path}"' if prior_path else ''}

EXIT_CODE=$?

echo "=============================================="
echo "End time: $(date)"
echo "Exit code: $EXIT_CODE"
echo "=============================================="

exit $EXIT_CODE
"""
    return script
