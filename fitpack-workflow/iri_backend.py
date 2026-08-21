"""
Minimal IRI REST backend for the FitPack workflow.

This is intentionally a pragmatic adapter, not a full facility integration.
When ``HPC_BACKEND=iri`` the rest of the code can keep using the existing
task database and ssh_runner-shaped helpers while job, file, status, and
cancel operations are sent to the IRI API.
"""
from __future__ import annotations

import fnmatch
import io
import os
import posixpath
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError as exc:  # pragma: no cover - surfaced at runtime with config help
    requests = None  # type: ignore[assignment]
    _REQUESTS_IMPORT_ERROR = exc
else:
    _REQUESTS_IMPORT_ERROR = None


DEFAULT_IRI_BASE_URL = "https://iri-dev.ppg.es.net"
DEFAULT_COMPUTE_RESOURCE_ID = "fb0aafe1-c780-55c0-b635-a7121f1b0ce5"
DEFAULT_STORAGE_RESOURCE_ID = "b6d59ba3-5c2d-57dd-b3b9-108832da0578"
MAX_SMALL_FILE_BYTES = 5_242_880


class IriApiError(RuntimeError):
    """Raised when the IRI API returns an error or an unexpected payload."""


def is_iri_enabled() -> bool:
    return os.environ.get("HPC_BACKEND", "ssh").strip().lower() in {
        "iri",
        "iri-dev",
        "iri_rest",
        "rest",
    }


def iri_token() -> str:
    return (
        os.environ.get("IRI_TOKEN")
        or os.environ.get("IRI_ACCESS_TOKEN")
        or os.environ.get("IRI_BEARER_TOKEN")
        or ""
    ).strip()


def iri_base_url() -> str:
    return os.environ.get("IRI_BASE_URL", DEFAULT_IRI_BASE_URL).rstrip("/")


def iri_compute_resource_id() -> str:
    return (
        os.environ.get("IRI_COMPUTE_RESOURCE_ID")
        or os.environ.get("IRI_RESOURCE_ID")
        or DEFAULT_COMPUTE_RESOURCE_ID
    ).strip()


def iri_storage_resource_id() -> str:
    return (
        os.environ.get("IRI_STORAGE_RESOURCE_ID")
        or os.environ.get("IRI_FILESYSTEM_RESOURCE_ID")
        or os.environ.get("IRI_RESOURCE_ID")
        or DEFAULT_STORAGE_RESOURCE_ID
    ).strip()


def is_iri_ready() -> bool:
    return bool(iri_base_url() and iri_token() and iri_compute_resource_id() and iri_storage_resource_id())


def iri_remote_base_template() -> str:
    return os.environ.get(
        "IRI_REMOTE_BASE_TEMPLATE",
        os.environ.get(
            "HPC_REMOTE_BASE_TEMPLATE",
            os.environ.get("FITPACK_REMOTE_TEMPLATE", "/w/ceph24/JAM/{username}/fitpack_legacy"),
        ),
    )


def get_iri_remote_base(username: str) -> str:
    if not username:
        username = os.environ.get("HPC_DEFAULT_USERNAME", "")
    if not username:
        raise ValueError("HPC username is required to resolve the IRI-backed remote workspace")
    return iri_remote_base_template().format(username=username)


def _require_requests() -> None:
    if requests is None:
        raise IriApiError(
            "Python package 'requests' is required for HPC_BACKEND=iri. "
            "Install fitpack-workflow/requirements.txt."
        ) from _REQUESTS_IMPORT_ERROR


def _clean_dict(value: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in value.items() if v not in (None, "", [], {})}


def _time_to_seconds(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return max(1, int(value))
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(1, int(float(text)))
    except ValueError:
        pass

    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        try:
            days = int(day_text)
        except ValueError:
            days = 0
    parts = [int(p) for p in text.split(":") if p.isdigit()]
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours, minutes, seconds = 0, parts[0], parts[1]
    elif len(parts) == 1:
        hours, minutes, seconds = 0, 0, parts[0]
    else:
        return None
    return max(1, days * 86400 + hours * 3600 + minutes * 60 + seconds)


def _memory_to_bytes(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return max(1, int(value))
    text = str(value).strip()
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([kmgtp]?)(?:i?b)?$", text, re.I)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).lower()
    scale = {
        "": 1,
        "k": 1024,
        "m": 1024**2,
        "g": 1024**3,
        "t": 1024**4,
        "p": 1024**5,
    }[unit]
    return max(1, int(number * scale))


def iri_state_to_task_status(state: str, exit_code: Optional[int] = None) -> str:
    norm = (state or "").strip().lower()
    if norm in {"new", "queued", "held"}:
        return "pending"
    if norm in {"active", "running"}:
        return "running"
    if norm in {"completed", "done", "success", "finished"}:
        return "completed" if exit_code in (None, 0) else "failed"
    if norm in {"canceled", "cancelled"}:
        return "cancelled"
    if norm in {"failed", "error", "timeout"}:
        return "failed"
    return "unknown"


def iri_state_to_slurm_code(state: str, exit_code: Optional[int] = None) -> str:
    status = iri_state_to_task_status(state, exit_code)
    return {
        "pending": "PD",
        "running": "R",
        "completed": "CD",
        "failed": "F",
        "cancelled": "CA",
    }.get(status, "")


def _extract_payload_text(value: Any) -> str:
    """Best-effort extraction from the IRI task result shape.

    Different filesystem operations return different result objects. This keeps
    the adapter tolerant while the dev endpoint is still evolving.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, list):
        return "\n".join(_extract_payload_text(v) for v in value if v is not None)
    if not isinstance(value, dict):
        return str(value)

    for key in ("stdout", "content", "text", "data", "output", "file", "body", "result"):
        if key in value and value[key] is not None:
            nested = value[key]
            if key == "result" and nested is value:
                continue
            text = _extract_payload_text(nested)
            if text:
                return text
    return ""


@dataclass
class IriClient:
    base_url: str
    token: str
    compute_resource_id: str
    storage_resource_id: str
    timeout: int = 30

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers(self, json_body: bool = True) -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        files: Any = None,
        timeout: Optional[int] = None,
    ) -> Any:
        _require_requests()
        headers = self._headers(json_body=files is None)
        response = requests.request(  # type: ignore[union-attr]
            method,
            self._url(path),
            params=params,
            json=json_body,
            files=files,
            headers=headers,
            timeout=timeout or self.timeout,
        )
        if response.status_code >= 400:
            detail = response.text[:1000]
            raise IriApiError(f"IRI {method} {path} failed HTTP {response.status_code}: {detail}")
        if not response.content:
            return {}
        ctype = response.headers.get("content-type", "")
        if "application/json" in ctype:
            return response.json()
        return response.text

    def list_resources(self) -> Any:
        return self.request("GET", "/api/v1/status/resources")

    def launch_job(self, job_spec: Dict[str, Any]) -> Dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/compute/job/{self.compute_resource_id}",
            json_body=job_spec,
            timeout=max(self.timeout, 60),
        )

    def get_job(self, job_id: str, include_spec: bool = False) -> Dict[str, Any]:
        return self.request(
            "GET",
            f"/api/v1/compute/status/{self.compute_resource_id}/{job_id}",
            params={"historical": "true", "include_spec": str(include_spec).lower()},
        )

    def list_jobs(self, filters: Optional[Dict[str, Any]] = None, limit: int = 100) -> Any:
        return self.request(
            "POST",
            f"/api/v1/compute/status/{self.compute_resource_id}",
            params={"historical": "false", "include_spec": "true", "limit": limit},
            json_body=filters or {},
        )

    def cancel_job(self, job_id: str) -> Any:
        return self.request("DELETE", f"/api/v1/compute/cancel/{self.compute_resource_id}/{job_id}")

    def get_task(self, task_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/api/v1/task/{task_id}")

    def wait_task(self, submit_response: Any, timeout: int = 60, poll_interval: float = 0.75) -> Any:
        if not isinstance(submit_response, dict):
            return submit_response
        task_id = submit_response.get("task_id") or submit_response.get("id")
        if not task_id:
            return submit_response
        deadline = time.time() + timeout
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            last = self.get_task(task_id)
            status = str(last.get("status") or "").lower()
            if status in {"done", "completed", "success", "finished"}:
                return last.get("result") if last.get("result") is not None else last
            if status in {"failed", "error", "cancelled", "canceled"}:
                raise IriApiError(f"IRI task {task_id} failed: {last}")
            if last.get("result") is not None and not status:
                return last.get("result")
            time.sleep(poll_interval)
        raise TimeoutError(f"Timed out waiting for IRI task {task_id}: {last}")

    def mkdir(self, path: str) -> Any:
        try:
            response = self.request(
                "POST",
                f"/api/v1/filesystem/mkdir/{self.storage_resource_id}",
                json_body={"path": path, "parent": True},
            )
        except IriApiError as exc:
            if "HTTP 409" in str(exc) or "already" in str(exc).lower():
                return {}
            raise
        return self.wait_task(response)

    def chmod(self, path: str, mode: str) -> Any:
        response = self.request(
            "PUT",
            f"/api/v1/filesystem/chmod/{self.storage_resource_id}",
            json_body={"path": path, "mode": mode},
        )
        return self.wait_task(response)

    def rm(self, path: str) -> Any:
        response = self.request(
            "DELETE",
            f"/api/v1/filesystem/rm/{self.storage_resource_id}",
            params={"path": path},
        )
        return self.wait_task(response)

    def upload_text(self, path: str, content: str) -> Any:
        raw = content.encode("utf-8")
        if len(raw) > MAX_SMALL_FILE_BYTES:
            raise IriApiError(
                f"IRI small-file upload limit is {MAX_SMALL_FILE_BYTES} bytes; "
                f"{path} is {len(raw)} bytes"
            )
        self.mkdir(posixpath.dirname(path) or ".")
        bio = io.BytesIO(raw)
        files = {"file": (posixpath.basename(path), bio, "text/plain; charset=utf-8")}
        response = self.request(
            "POST",
            f"/api/v1/filesystem/upload/{self.storage_resource_id}",
            params={"path": path},
            files=files,
            timeout=max(self.timeout, 60),
        )
        return self.wait_task(response)

    def view_text(self, path: str, size: int = MAX_SMALL_FILE_BYTES, offset: int = 0) -> str:
        response = self.request(
            "GET",
            f"/api/v1/filesystem/view/{self.storage_resource_id}",
            params={"path": path, "size": min(size, MAX_SMALL_FILE_BYTES), "offset": offset},
        )
        return _extract_payload_text(self.wait_task(response))

    def tail_text(self, path: str, lines: int = 120) -> str:
        response = self.request(
            "GET",
            f"/api/v1/filesystem/tail/{self.storage_resource_id}",
            params={"path": path, "lines": max(1, int(lines))},
        )
        return _extract_payload_text(self.wait_task(response))

    def exists(self, path: str) -> bool:
        try:
            response = self.request(
                "GET",
                f"/api/v1/filesystem/stat/{self.storage_resource_id}",
                params={"path": path},
            )
            self.wait_task(response, timeout=20)
            return True
        except Exception:
            return False

    def ls(self, path: str, show_hidden: bool = False) -> List[str]:
        response = self.request(
            "GET",
            f"/api/v1/filesystem/ls/{self.storage_resource_id}",
            params={
                "path": path,
                "showHidden": str(show_hidden).lower(),
                "numericUid": "false",
                "recursive": "false",
                "dereference": "false",
            },
        )
        result = self.wait_task(response)
        if isinstance(result, list):
            return [str(item.get("name", item) if isinstance(item, dict) else item) for item in result]
        if isinstance(result, dict):
            for key in ("files", "entries", "items", "result", "data"):
                val = result.get(key)
                if isinstance(val, list):
                    return [str(item.get("name", item) if isinstance(item, dict) else item) for item in val]
            text = _extract_payload_text(result)
        else:
            text = _extract_payload_text(result)
        names: List[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("total "):
                continue
            parts = line.split()
            names.append(parts[-1] if parts else line)
        return names


def get_iri_client() -> IriClient:
    if not is_iri_ready():
        missing = []
        if not iri_token():
            missing.append("IRI_TOKEN")
        if not iri_compute_resource_id():
            missing.append("IRI_COMPUTE_RESOURCE_ID")
        if not iri_storage_resource_id():
            missing.append("IRI_STORAGE_RESOURCE_ID")
        raise IriApiError(f"IRI backend is not configured; missing {', '.join(missing)}")
    return IriClient(
        base_url=iri_base_url(),
        token=iri_token(),
        compute_resource_id=iri_compute_resource_id(),
        storage_resource_id=iri_storage_resource_id(),
        timeout=int(os.environ.get("IRI_TIMEOUT", "30")),
    )


def build_job_spec(
    script_path: str,
    task_path: Optional[str] = None,
    slurm_config: Optional[Dict[str, Any]] = None,
    name: str = "fitpack",
    directory: Optional[str] = None,
) -> Dict[str, Any]:
    config = dict(slurm_config or {})
    output_dir = task_path or posixpath.dirname(script_path)
    if directory is None:
        parts = output_dir.split("/runs/")
        directory = parts[0] if len(parts) > 1 else posixpath.dirname(script_path)

    resources = _clean_dict({
        "node_count": int(config.get("nodes", 1) or 1),
        "process_count": int(config.get("ntasks", 1) or 1),
        "processes_per_node": int(config.get("ntasks", 1) or 1),
        "cpu_cores_per_process": int(config.get("cpus", 1) or 1),
        "memory": _memory_to_bytes(config.get("mem")),
        "exclusive_node_use": bool(config.get("exclusive_node_use", False)),
    })
    attrs = _clean_dict({
        "duration": _time_to_seconds(config.get("time")),
        "queue_name": os.environ.get("IRI_DEFAULT_QUEUE") or config.get("partition"),
        "account": os.environ.get("IRI_DEFAULT_ACCOUNT") or config.get("account"),
        "reservation_id": config.get("reservation"),
        "custom_attributes": _clean_dict({"constraint": config.get("constraint")}),
    })
    environment = {
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
    }
    environment.update({
        k[len("IRI_JOB_ENV_"):]: v
        for k, v in os.environ.items()
        if k.startswith("IRI_JOB_ENV_")
    })

    job_spec: Dict[str, Any] = _clean_dict({
        "executable": os.environ.get("IRI_JOB_EXECUTABLE", "bash"),
        "arguments": [script_path],
        "directory": directory,
        "name": config.get("job_name") or name,
        "inherit_environment": True,
        "environment": environment,
        "stdout_path": f"{output_dir}/slurm_iri.out",
        "stderr_path": f"{output_dir}/slurm_iri.err",
        "resources": resources,
        "attributes": attrs,
        "launcher": os.environ.get("IRI_JOB_LAUNCHER") or None,
    })
    container_image = os.environ.get("IRI_CONTAINER_IMAGE", "").strip()
    if container_image:
        job_spec["container"] = {"image": container_image}
    return job_spec


def iri_submit_script(
    script_path: str,
    *,
    task_path: Optional[str] = None,
    slurm_config: Optional[Dict[str, Any]] = None,
    username: str = "",
) -> str:
    del username  # Reserved for future per-user resource selection.
    job = get_iri_client().launch_job(
        build_job_spec(script_path, task_path=task_path, slurm_config=slurm_config)
    )
    job_id = job.get("id") if isinstance(job, dict) else None
    if not job_id:
        raise IriApiError(f"IRI job response did not include an id: {job}")
    return str(job_id)


def iri_get_task_status(job_id: str) -> str:
    job = get_iri_client().get_job(job_id)
    status = job.get("status") if isinstance(job, dict) else {}
    state = status.get("state") if isinstance(status, dict) else ""
    exit_code = status.get("exit_code") if isinstance(status, dict) else None
    return iri_state_to_task_status(str(state), exit_code)


def iri_get_slurm_code(job_id: str) -> str:
    job = get_iri_client().get_job(job_id)
    status = job.get("status") if isinstance(job, dict) else {}
    state = status.get("state") if isinstance(status, dict) else ""
    exit_code = status.get("exit_code") if isinstance(status, dict) else None
    return iri_state_to_slurm_code(str(state), exit_code)


def iri_tail_first_matching(directory: str, pattern: str, lines: int = 120) -> str:
    client = get_iri_client()
    for name in client.ls(directory):
        base = posixpath.basename(name)
        full_path = name if name.startswith("/") else f"{directory.rstrip('/')}/{name}"
        if fnmatch.fnmatch(base, pattern):
            return client.tail_text(full_path, lines=lines)
    raise FileNotFoundError(f"No file matching {pattern} found in {directory}")


def iri_find(directory: str, name_pattern: str = "*") -> List[str]:
    """Return files in `directory` whose basename matches `name_pattern`.

    Mirrors `find <dir> -maxdepth 1 -name <pattern>` over the IRI filesystem.
    """
    client = get_iri_client()
    matches: List[str] = []
    for name in client.ls(directory):
        base = posixpath.basename(name)
        if fnmatch.fnmatch(base, name_pattern):
            full = name if name.startswith("/") else f"{directory.rstrip('/')}/{base}"
            matches.append(full)
    return matches


def iri_list_jobs() -> List[Dict[str, Any]]:
    data = get_iri_client().list_jobs()
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("jobs", "items", "data", "result"):
            val = data.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    return []
