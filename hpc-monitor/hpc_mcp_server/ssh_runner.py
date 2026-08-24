"""
SSH runner for remote HPC command execution via ControlMaster.

Multi-user support: all functions accept a `username` parameter.
SSH ControlMaster sockets are expected at:
    ~/.ssh/controlmasters/jlab-ifarm-{username}@ifarm.jlab.org:22

Each user's remote workspace is at:
    /home/{username}/fitpack_legacy
"""
import logging
import os
import subprocess
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# --- SSH Configuration (defaults, overridable per-user) ---
HPC_HOST = "ifarm.jlab.org"
CONTROL_MASTERS_DIR = os.path.expanduser("~/.ssh/controlmasters")
DEFAULT_REMOTE_BASE_TEMPLATE = os.environ.get("HPC_REMOTE_BASE_TEMPLATE", "/ceph24/JAM/{username}/fitpack_legacy")

# Legacy default
_DEFAULT_USERNAME = os.environ.get("HPC_DEFAULT_USERNAME", "")


def get_ssh_config(username: Optional[str] = None) -> dict:
    """Build SSH config for a given HPC username."""
    user = username or _DEFAULT_USERNAME
    return {
        "username": user,
        "control_path": os.path.join(CONTROL_MASTERS_DIR, f"jlab-ifarm-{user}@{HPC_HOST}:22"),
        "user_host": f"{user}@{HPC_HOST}",
        "remote_base": DEFAULT_REMOTE_BASE_TEMPLATE.format(username=user),
    }


def get_remote_base(username: Optional[str] = None) -> str:
    return get_ssh_config(username)["remote_base"]


REMOTE_BASE = get_remote_base()


def _check_socket(username: Optional[str] = None) -> bool:
    cfg = get_ssh_config(username)
    return Path(cfg["control_path"]).exists()


def ssh_run(
    remote_cmd: List[str],
    timeout: int = 10,
    input_data: Optional[str] = None,
    username: Optional[str] = None,
) -> subprocess.CompletedProcess:
    cfg = get_ssh_config(username)
    if not Path(cfg["control_path"]).exists():
        raise ConnectionError(
            f"SSH ControlMaster socket not found for user '{cfg['username']}': {cfg['control_path']}. "
            "Ensure the HPC connection is established."
        )
    cmd = [
        "ssh", "-S", cfg["control_path"], "-o", "BatchMode=yes",
        cfg["user_host"], "--",
    ] + remote_cmd
    kwargs = dict(capture_output=True, text=True, timeout=timeout)
    if input_data is not None:
        kwargs["input"] = input_data
    else:
        kwargs["stdin"] = subprocess.DEVNULL
    return subprocess.run(cmd, **kwargs)


def ssh_read_file(remote_path: str, timeout: int = 10, username: Optional[str] = None) -> str:
    result = ssh_run(["cat", remote_path], timeout=timeout, username=username)
    if result.returncode != 0:
        if "No such file" in result.stderr:
            raise FileNotFoundError(f"Remote file not found: {remote_path}")
        raise RuntimeError(f"Failed to read {remote_path}: {result.stderr}")
    return result.stdout


def ssh_write_file(remote_path: str, content: str, timeout: int = 15, username: Optional[str] = None) -> None:
    parent = str(Path(remote_path).parent)
    ssh_run(["mkdir", "-p", parent], timeout=5, username=username)
    result = ssh_run(
        ["tee", remote_path],
        timeout=timeout,
        input_data=content,
        username=username,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to write {remote_path}: {result.stderr}")


def ssh_file_exists(remote_path: str, username: Optional[str] = None) -> bool:
    result = ssh_run(["test", "-e", remote_path], timeout=5, username=username)
    return result.returncode == 0


def ssh_mkdir(remote_path: str, username: Optional[str] = None) -> None:
    result = ssh_run(["mkdir", "-p", remote_path], timeout=5, username=username)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to mkdir {remote_path}: {result.stderr}")
