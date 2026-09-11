"""
SSH runner for remote HPC command execution via ControlMaster.

Multi-user support. Callers should either:
  - pass an explicit ``username=`` to every ssh_* call, or
  - enter the ``set_hpc_user(user)`` context manager once at the top of a
    request so nested helpers pick the user up via the ContextVar.

SSH ControlMaster sockets are expected at:
    ~/.ssh/controlmasters/jlab-ifarm-{username}@ifarm.jlab.org:22

Each user's remote workspace is at:
    /w/ceph24/JAM/{username}/fitpack_legacy   (default; overridable via
                                              HPC_REMOTE_BASE_TEMPLATE)
"""
import contextlib
import contextvars
import logging
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Iterator, List, Optional

from iri_backend import (
    IriApiError,
    get_iri_client,
    get_iri_remote_base,
    iri_find,
    iri_get_slurm_code,
    iri_get_task_status,
    iri_list_jobs,
    iri_submit_script,
    iri_tail_first_matching,
    is_iri_enabled,
    is_iri_ready,
)

logger = logging.getLogger(__name__)

# --- SSH Configuration ---
HPC_HOST = "ifarm.jlab.org"
CONTROL_MASTERS_DIR = os.environ.get(
    "FEMTOMIND_SSH_DIR", os.path.expanduser("~/.ssh/controlmasters")
)
DEFAULT_REMOTE_BASE_TEMPLATE = os.environ.get(
    "HPC_REMOTE_BASE_TEMPLATE", "/w/ceph24/JAM/{username}/fitpack_legacy"
)
REMOTE_BASE = DEFAULT_REMOTE_BASE_TEMPLATE.format(
    username=os.environ.get("HPC_DEFAULT_USERNAME", "{username}")
)

# Per-request username. Request-scoped via ContextVar so concurrent users
# (async tasks / threads) do not bleed into each other.
_current_hpc_user: contextvars.ContextVar[str] = contextvars.ContextVar(
    "fitpack_hpc_user", default=""
)

# Env fallback (single-tenant / local dev). Intentionally has no implicit
# process default — an unset user raises a clear error at the call site.
_ENV_FALLBACK_USER = os.environ.get("HPC_DEFAULT_USERNAME", "")


@contextlib.contextmanager
def set_hpc_user(username: str) -> Iterator[str]:
    """Scope an HPC username to the current context.

    Usage:
        with set_hpc_user("alice"):
            ssh_read_file("/home/alice/.../foo.yaml")  # picks up alice

    Nested scopes restore the outer value on exit, so this is safe to use in
    helpers that are themselves called from a tool entry point.
    """
    if not username:
        raise ValueError("set_hpc_user requires a non-empty username")
    token = _current_hpc_user.set(username)
    try:
        yield username
    finally:
        _current_hpc_user.reset(token)


def _resolve_username(username: Optional[str]) -> str:
    """Resolve the effective HPC username in priority order:
    explicit arg > ContextVar > env fallback.
    Raises if none is set so bugs surface loudly instead of producing
    paths like `jlab-ifarm-@host:22`.
    """
    if username:
        return username
    ctx_user = _current_hpc_user.get("")
    if ctx_user:
        return ctx_user
    if _ENV_FALLBACK_USER:
        return _ENV_FALLBACK_USER
    raise ValueError(
        "No HPC username resolved. Pass username= explicitly, wrap the call "
        "in `with set_hpc_user(...)`, or set HPC_DEFAULT_USERNAME for "
        "single-user dev."
    )


def _resolve_iri_username(username: Optional[str]) -> str:
    if username:
        return username
    ctx_user = _current_hpc_user.get("")
    if ctx_user:
        return ctx_user
    if _ENV_FALLBACK_USER:
        return _ENV_FALLBACK_USER
    raise ValueError(
        "No HPC username resolved for IRI transport. Pass username= explicitly, "
        "wrap the call in `with set_hpc_user(...)`, or set HPC_DEFAULT_USERNAME."
    )


def get_ssh_config(username: Optional[str] = None) -> dict:
    """Build SSH config for a given HPC username."""
    if is_iri_enabled():
        user = _resolve_iri_username(username)
        return {
            "username": user,
            "control_path": f"iri://{user}",
            "user_host": f"{user}@{os.environ.get('HPC_HOST', HPC_HOST)}",
            "remote_base": get_iri_remote_base(user),
        }

    user = _resolve_username(username)
    return {
        "username": user,
        "control_path": os.path.join(
            CONTROL_MASTERS_DIR, f"jlab-ifarm-{user}@{HPC_HOST}:22"
        ),
        "user_host": f"{user}@{HPC_HOST}",
        "remote_base": DEFAULT_REMOTE_BASE_TEMPLATE.format(username=user),
    }


def _stored_remote_base(username: Optional[str]) -> Optional[str]:
    """The workspace this user actually set up, if they chose one."""
    if not username:
        return None
    try:
        import sqlite3
        db = os.environ.get("DB_PATH") or str(Path(__file__).parent / "hpc_tasks.db")
        with sqlite3.connect(db, timeout=5) as conn:
            row = conn.execute(
                # Several identities can share one cluster login, so this can
                # match more than one row; take the most recently configured
                # rather than whichever the engine happens to return first.
                # Properly this should be keyed on the caller's identity, but
                # that is not threaded down to this layer yet.
                "SELECT remote_base FROM user_hpc_config WHERE hpc_username = ?"
                " AND remote_base IS NOT NULL AND remote_base != ''"
                " ORDER BY configured_at DESC LIMIT 1",
                (username,),
            ).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def get_remote_base(username: Optional[str] = None) -> str:
    if is_iri_enabled():
        return get_iri_remote_base(_resolve_iri_username(username))
    # A folder the user picked wins over the templated default; otherwise
    # anyone who cloned into a differently named directory ends up with a
    # workspace the submit and plot paths cannot find.
    stored = _stored_remote_base(username)
    if stored:
        return stored
    return get_ssh_config(username)["remote_base"]


def _find_socket(expected_path: str) -> str:
    """Find SSH socket, handling macOS FQDN trailing-dot issue.
    macOS SSH may create 'user@host.org.:22' instead of 'user@host.org:22'."""
    import re
    if Path(expected_path).exists():
        return expected_path
    dotted = re.sub(r'@([^:]+):', r'@\1.:', expected_path)
    if dotted != expected_path and Path(dotted).exists():
        return dotted
    return expected_path


def _check_socket(username: Optional[str] = None) -> bool:
    """Verify the ControlMaster socket file exists for a user."""
    if is_iri_enabled():
        return is_iri_ready()
    cfg = get_ssh_config(username)
    return Path(_find_socket(cfg["control_path"])).exists()


def _completed(remote_cmd: List[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(remote_cmd, returncode, stdout, stderr)


def _iri_squeue(remote_cmd: List[str]) -> subprocess.CompletedProcess:
    job_filter_flag = next((flag for flag in ("--job", "-j") if flag in remote_cmd), None)
    if job_filter_flag:
        try:
            job_id = remote_cmd[remote_cmd.index(job_filter_flag) + 1]
        except (ValueError, IndexError):
            return _completed(remote_cmd, 1, stderr=f"missing {job_filter_flag} value")
        code = iri_get_slurm_code(job_id)
        # squeue only reports active jobs. Completed/cancelled/failed jobs are
        # intentionally absent so callers fall through to sacct-like history.
        if code in {"PD", "R", "CG", "S", "CF"}:
            return _completed(remote_cmd, 0, stdout=f"{code}\n")
        return _completed(remote_cmd, 0, stdout="")

    lines = []
    for job in iri_list_jobs():
        job_id = str(job.get("id", ""))
        status = job.get("status") if isinstance(job.get("status"), dict) else {}
        state = str(status.get("state", ""))
        exit_code = status.get("exit_code")
        code = {
            "pending": "PD",
            "running": "R",
        }.get(iri_get_task_status(job_id) if job_id else "", iri_get_slurm_code(job_id) if job_id else "")
        if not code:
            code = {
                "queued": "PD",
                "new": "PD",
                "held": "PD",
                "active": "R",
            }.get(state.lower(), "")
        if not code:
            continue
        cpus = 1
        spec = job.get("job_spec") if isinstance(job.get("job_spec"), dict) else {}
        resources = spec.get("resources") if isinstance(spec.get("resources"), dict) else {}
        try:
            cpus = int(resources.get("cpu_cores_per_process") or 1) * int(resources.get("process_count") or 1)
        except Exception:
            cpus = 1
        fmt = " ".join(remote_cmd)
        if "%i %t" in fmt:
            lines.append(f"{job_id} {code}")
        elif "%t %C" in fmt:
            lines.append(f"{code} {cpus}")
        elif "%t" in fmt:
            lines.append(code)
        else:
            name = spec.get("name", "fitpack") if isinstance(spec, dict) else "fitpack"
            lines.append(f"{job_id}|{name}|iri|{code}|0:00|iri|1|{cpus}|0|iri|")
    return _completed(remote_cmd, 0, stdout=("\n".join(lines) + ("\n" if lines else "")))


_SACCT_FIELD_RESOLVERS = {
    "state": lambda s: s["state"],
    "maxrss": lambda s: "",
    "nodelist": lambda s: "",
    "exitcode": lambda s: "",
    "elapsed": lambda s: "",
    "alloccpus": lambda s: "",
    "jobid": lambda s: s["job_id"],
    "jobname": lambda s: "fitpack",
}


def _iri_sacct(remote_cmd: List[str]) -> subprocess.CompletedProcess:
    try:
        job_id = remote_cmd[remote_cmd.index("-j") + 1]
    except (ValueError, IndexError):
        return _completed(remote_cmd, 1, stderr="missing -j value")
    status = iri_get_task_status(job_id)
    state = {
        "completed": "COMPLETED",
        "failed": "FAILED",
        "cancelled": "CANCELLED",
        "pending": "PENDING",
        "running": "RUNNING",
    }.get(status, "UNKNOWN")

    fields: List[str] = ["State"]
    for arg in remote_cmd:
        if arg.startswith("--format="):
            fields = arg.split("=", 1)[1].split(",")
            break
    else:
        for flag in ("--format", "-o"):
            if flag in remote_cmd:
                idx = remote_cmd.index(flag)
                if idx + 1 < len(remote_cmd):
                    fields = remote_cmd[idx + 1].split(",")
                    break

    ctx = {"state": state, "job_id": str(job_id)}
    line = "|".join(_SACCT_FIELD_RESOLVERS.get(f.strip().lower(), lambda _s: "")(ctx) for f in fields)
    return _completed(remote_cmd, 0, stdout=line + "\n")


def _iri_shell(remote_cmd: List[str]) -> subprocess.CompletedProcess:
    shell_cmd = remote_cmd[2] if len(remote_cmd) >= 3 else ""
    match = re.search(r"ls -1\s+(.+?)/slurm_\*\.out", shell_cmd)
    if match:
        try:
            directory = shlex.split(match.group(1))[0]
        except Exception:
            directory = match.group(1).strip("'\"")
        try:
            return _completed(remote_cmd, 0, stdout=iri_tail_first_matching(directory, "slurm_*.out", lines=120))
        except FileNotFoundError:
            return _completed(remote_cmd, 0, stdout="__NO_SLURM_FILE__\n")
        except Exception as exc:
            return _completed(remote_cmd, 1, stderr=str(exc))
    return _completed(remote_cmd, 127, stderr=f"IRI backend cannot run arbitrary shell command: {shell_cmd}")


def _iri_run(remote_cmd: List[str], timeout: int, input_data: Optional[str], username: Optional[str]) -> subprocess.CompletedProcess:
    del timeout
    user = _resolve_iri_username(username)
    client = get_iri_client()
    if not remote_cmd:
        return _completed(remote_cmd, 0)

    op = remote_cmd[0]
    try:
        if op == "echo":
            return _completed(remote_cmd, 0, stdout=(" ".join(remote_cmd[1:]) + "\n"))
        if op == "hostname":
            return _completed(remote_cmd, 0, stdout=f"{os.environ.get('HPC_HOST', HPC_HOST)}\n")
        if op == "mkdir":
            paths = [p for p in remote_cmd[1:] if not p.startswith("-")]
            for path in paths:
                client.mkdir(path)
            return _completed(remote_cmd, 0)
        if op == "tee" and len(remote_cmd) >= 2:
            path = remote_cmd[-1]
            content = input_data or ""
            client.upload_text(path, content)
            return _completed(remote_cmd, 0, stdout=content)
        if op == "cat" and len(remote_cmd) >= 2:
            return _completed(remote_cmd, 0, stdout=client.view_text(remote_cmd[1]))
        if op == "test" and "-e" in remote_cmd and len(remote_cmd) >= 3:
            path = remote_cmd[-1]
            return _completed(remote_cmd, 0 if client.exists(path) else 1)
        if op == "rm":
            ignore_missing = any(flag in {"-f", "-rf", "-fr"} for flag in remote_cmd[1:] if flag.startswith("-"))
            paths = [p for p in remote_cmd[1:] if not p.startswith("-")]
            for path in paths:
                try:
                    client.rm(path)
                except Exception:
                    if not ignore_missing:
                        raise
            return _completed(remote_cmd, 0)
        if op == "chmod" and len(remote_cmd) >= 3:
            mode = remote_cmd[1].lstrip("+")
            if mode == "x":
                mode = "755"
            client.chmod(remote_cmd[-1], mode)
            return _completed(remote_cmd, 0)
        if op == "sbatch" and len(remote_cmd) >= 2:
            job_id = iri_submit_script(remote_cmd[-1], username=user)
            return _completed(remote_cmd, 0, stdout=f"Submitted batch job {job_id}\n")
        if op == "bash" and len(remote_cmd) >= 2:
            job_id = iri_submit_script(remote_cmd[1], username=user)
            return _completed(remote_cmd, 0, stdout=f"Submitted IRI job {job_id}\n")
        if op == "scancel" and len(remote_cmd) >= 2:
            client.cancel_job(remote_cmd[1])
            return _completed(remote_cmd, 0)
        if op == "squeue":
            return _iri_squeue(remote_cmd)
        if op == "sacct":
            return _iri_sacct(remote_cmd)
        if op == "sinfo":
            return _completed(remote_cmd, 0, stdout="")
        if op == "sh" and len(remote_cmd) >= 3 and remote_cmd[1] == "-c":
            return _iri_shell(remote_cmd)
        if op == "find" and len(remote_cmd) >= 2:
            directory = remote_cmd[1]
            name_pattern = "*"
            args = remote_cmd[2:]
            i = 0
            while i < len(args):
                if args[i] == "-name" and i + 1 < len(args):
                    name_pattern = args[i + 1]
                    i += 2
                else:
                    i += 1
            try:
                matches = iri_find(directory, name_pattern)
            except FileNotFoundError:
                return _completed(remote_cmd, 0, stdout="")
            return _completed(remote_cmd, 0, stdout=("\n".join(matches) + ("\n" if matches else "")))
    except FileNotFoundError as exc:
        return _completed(remote_cmd, 1, stderr=f"No such file or directory: {exc}")
    except (IriApiError, TimeoutError, RuntimeError, ValueError) as exc:
        return _completed(remote_cmd, 1, stderr=str(exc))

    return _completed(remote_cmd, 127, stderr=f"IRI backend cannot run command: {remote_cmd!r}")


def ssh_run(
    remote_cmd: List[str],
    timeout: int = 10,
    input_data: Optional[str] = None,
    username: Optional[str] = None,
    quote: bool = True,
) -> subprocess.CompletedProcess:
    """Execute a command on the remote HPC via ControlMaster.

    ssh concatenates the argv elements after ``--`` into ONE command line that
    the remote login shell re-parses. Unquoted elements are therefore shell
    words on the remote side: a path or job name containing spaces or shell
    metacharacters would be split or executed. ``quote=True`` (the default)
    shell-quotes every element so each argv entry arrives as exactly one
    remote word; callers that deliberately pass a pre-quoted whole-script
    element (e.g. ``["sh", "-c", shlex.quote(script)]``) set ``quote=False``
    to keep it verbatim.
    """
    if is_iri_enabled():
        return _iri_run(remote_cmd, timeout=timeout, input_data=input_data, username=username)

    cfg = get_ssh_config(username)
    socket_path = _find_socket(cfg["control_path"])
    if not Path(socket_path).exists():
        raise ConnectionError(
            f"SSH ControlMaster socket not found for user '{cfg['username']}': {cfg['control_path']}. "
            "Ensure the HPC connection is established."
        )
    remote_argv = [shlex.quote(str(a)) for a in remote_cmd] if quote else [str(a) for a in remote_cmd]
    cmd = [
        "ssh", "-S", socket_path, "-o", "BatchMode=yes",
        cfg["user_host"], "--",
    ] + remote_argv
    kwargs = dict(capture_output=True, text=True, timeout=timeout)
    if input_data is not None:
        kwargs["input"] = input_data
    else:
        kwargs["stdin"] = subprocess.DEVNULL
    return subprocess.run(cmd, **kwargs)


def ssh_read_file(remote_path: str, timeout: int = 10, username: Optional[str] = None) -> str:
    """Read a file from the remote HPC via cat."""
    result = ssh_run(["cat", remote_path], timeout=timeout, username=username)
    if result.returncode != 0:
        if "No such file" in result.stderr:
            raise FileNotFoundError(f"Remote file not found: {remote_path}")
        raise RuntimeError(f"Failed to read {remote_path}: {result.stderr}")
    return result.stdout


def ssh_write_file(remote_path: str, content: str, timeout: int = 15, username: Optional[str] = None) -> None:
    """Write content to a file on the remote HPC via stdin pipe."""
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
    """Check whether a file exists on the remote HPC."""
    result = ssh_run(["test", "-e", remote_path], timeout=5, username=username)
    return result.returncode == 0


def ssh_mkdir(remote_path: str, username: Optional[str] = None) -> None:
    """Create a directory (and parents) on the remote HPC."""
    result = ssh_run(["mkdir", "-p", remote_path], timeout=5, username=username)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to mkdir {remote_path}: {result.stderr}")
