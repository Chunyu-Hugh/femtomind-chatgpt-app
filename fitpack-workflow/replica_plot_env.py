"""
Auto-provision a Python environment for running fitpack (fitlib / qcdlib /
tools.config) in a subprocess. Called by the plot_replica_observable MCP
tool before spawning the compute worker.

Strategy (zero-config for end users):

  1. If FITPACK_PYTHON env var points to a valid python with numpy+yaml+
     matplotlib importable → use it. (Escape hatch for devs.)

  2. Else check cache at ~/.local/share/femtomind/env_config.json — if the
     cached python still works and the requirements.txt hash matches →
     reuse.

  3. Else bootstrap:
       a. pip-install `uv` into the backend's own python (if missing).
       b. `uv venv ~/.local/share/femtomind/python_env --python 3.11`
       c. `uv pip install -r <fitpack>/requirements.txt` into that venv.
       d. Validate: new python can import numpy + yaml + matplotlib.
       e. Write cache.

A progress callback emits one event per phase — the caller (replica_plot.py)
forwards these into the message payload's `trace[]` so the UI can show a
Claude-Code-style live feed.

Intentional non-goals:
  * Not hermetic — uv venv inherits system libs (this is fine for fitlib).
  * No offline mode — requires network on first run.
  * No Windows Scripts-dir probing until we actually need it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────
# Paths (override via env vars)
# ──────────────────────────────────────────────────────────────────────────

FITPACK_LOCAL_DIR = Path(os.environ.get(
    "FITPACK_LOCAL_DIR",
    str(Path.home() / "fitpack_legacy_copy"),
))

FEMTOMIND_ENV_HOME = Path(os.environ.get(
    "FEMTOMIND_ENV_HOME",
    str(Path.home() / ".local" / "share" / "femtomind"),
))

PY_ENV_DIR = FEMTOMIND_ENV_HOME / "python_env"
ENV_CONFIG = FEMTOMIND_ENV_HOME / "env_config.json"
PY_VERSION = "3.11"

EXPLICIT_PYTHON = os.environ.get("FITPACK_PYTHON")  # escape hatch

# Validation import list. If any of these fail, the env is considered broken
# and we rebuild. Kept minimal; fitlib-internal imports are checked separately
# by the worker at first real use.
VALIDATE_IMPORTS = "import numpy, yaml, matplotlib; print('ok')"


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────

TraceCallback = Callable[[dict], None]


def ensure_env(on_event: Optional[TraceCallback] = None) -> dict:
    """Make sure a fitpack-capable python interpreter is available.

    Returns {python, fitpack_root, ready: True, trace: [events]} or raises
    RuntimeError. Each event is {icon, text, **extra} — forwarded into the
    plot message's trace[] for live-feed UX.
    """
    trace: list[dict] = []

    def emit(icon: str, text: str, **extra) -> None:
        ev = {"icon": icon, "text": text, **extra}
        if on_event:
            try:
                on_event(ev)
            except Exception:  # callback errors must not break bootstrap
                log.exception("trace callback raised")
        trace.append(ev)

    fitpack_root = _check_fitpack_root()

    # ── 1. Explicit override ─────────────────────────────────────────────
    if EXPLICIT_PYTHON and _validate_env(EXPLICIT_PYTHON):
        emit("🔗", f"using FITPACK_PYTHON=<code>{EXPLICIT_PYTHON}</code>")
        return _ok(EXPLICIT_PYTHON, fitpack_root, trace)

    # ── 2. Cache hit ─────────────────────────────────────────────────────
    cached = _read_cache()
    if cached and _validate_env(cached.get("python")):
        emit("💾", f"python env cache hit → <code>{cached['python']}</code>",
             cached=True)
        return _ok(cached["python"], fitpack_root, trace)

    # ── 3. Bootstrap ─────────────────────────────────────────────────────
    emit("🔧", "setting up fitpack python env (one-time)", section="bootstrap")

    uv_cmd = _ensure_uv(emit)
    _uv_venv(uv_cmd, emit)
    _uv_install_requirements(uv_cmd, emit)

    python_path = str(_python_in_env())
    if not _validate_env(python_path):
        raise RuntimeError(
            f"env bootstrap finished but validation failed at {python_path} — "
            "check the install output above"
        )

    _write_cache({
        "python": python_path,
        "fitpack_root": str(fitpack_root),
        "requirements_hash": _hash_requirements(),
    })
    emit("✅", f"cached at <code>{PY_ENV_DIR}</code>")
    return _ok(python_path, fitpack_root, trace)


# ──────────────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────────────

def _ok(python: str, fitpack_root: Path, trace: list) -> dict:
    return {
        "python": python,
        "fitpack_root": str(fitpack_root),
        "ready": True,
        "trace": trace,
    }


def _check_fitpack_root() -> Path:
    if not FITPACK_LOCAL_DIR.exists():
        raise RuntimeError(
            f"FITPACK_LOCAL_DIR={FITPACK_LOCAL_DIR} does not exist; set the "
            "FITPACK_LOCAL_DIR env var to the fitpack_legacy_copy/ directory."
        )
    if not (FITPACK_LOCAL_DIR / "requirements.txt").exists():
        raise RuntimeError(
            f"no requirements.txt under {FITPACK_LOCAL_DIR}; nothing to install"
        )
    return FITPACK_LOCAL_DIR


def _python_in_env() -> Path:
    # uv venv layout: <env>/bin/python on Linux/macOS, <env>/Scripts/python.exe on Windows.
    candidates = [
        PY_ENV_DIR / "bin" / "python",
        PY_ENV_DIR / "bin" / f"python{PY_VERSION}",
        PY_ENV_DIR / "Scripts" / "python.exe",
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def _hash_requirements() -> str:
    try:
        return hashlib.sha256(
            (FITPACK_LOCAL_DIR / "requirements.txt").read_bytes()
        ).hexdigest()[:16]
    except OSError:
        return "unknown"


def _read_cache() -> Optional[dict]:
    if not ENV_CONFIG.exists():
        return None
    try:
        data = json.loads(ENV_CONFIG.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("env_config.json unreadable, will rebuild")
        return None
    if data.get("requirements_hash") != _hash_requirements():
        log.info("requirements.txt changed since last bootstrap; invalidating cache")
        return None
    return data


def _write_cache(data: dict) -> None:
    FEMTOMIND_ENV_HOME.mkdir(parents=True, exist_ok=True)
    ENV_CONFIG.write_text(json.dumps(data, indent=2))


def _validate_env(python_path: Optional[str]) -> bool:
    if not python_path:
        return False
    p = Path(python_path)
    if not p.exists() or not os.access(p, os.X_OK):
        return False
    try:
        r = subprocess.run(
            [python_path, "-c", VALIDATE_IMPORTS],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0 and "ok" in r.stdout


# ── streaming subprocess runner ───────────────────────────────────────────

import threading
import time as _time


def _run_streaming(
    cmd: list[str],
    emit: TraceCallback,
    phase_id: str,
    timeout: int = 180,
    extra_env: Optional[dict] = None,
) -> int:
    """Run `cmd` and stream its stdout/stderr line-by-line via `emit`.

    Emits the following sequence of trace events for the renderer to group
    into a Claude-Code-style terminal block:

        kind=cmd_start, phase_id=..., cmd=[...], text="$ <cmd>"
        kind=stdout,    phase_id=..., text="<line>"
        kind=stderr,    phase_id=..., text="<line>"
        ...
        kind=cmd_end,   phase_id=..., returncode=N, elapsed_s=T

    Returns the subprocess's returncode (or -1 on timeout). Never raises;
    callers check the returncode and produce a high-level error event.

    PYTHONUNBUFFERED is forced on so child Python processes (pip in
    particular) flush per-line instead of buffering until exit.
    """
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if extra_env:
        env.update(extra_env)

    cmd_str = " ".join(cmd)
    t0 = _time.monotonic()
    emit("⏵", cmd_str, kind="cmd_start", phase_id=phase_id, cmd=list(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered (per Python docs, applies when text=True)
            env=env,
        )
    except OSError as e:
        emit(
            "✗",
            f"failed to spawn: {e}",
            kind="cmd_end",
            phase_id=phase_id,
            returncode=-1,
            elapsed_s=round(_time.monotonic() - t0, 2),
        )
        return -1

    def _drain(stream, label: str):
        for line in stream:
            line = line.rstrip("\n")
            if line:
                emit("·", line, kind=label, phase_id=phase_id)

    t_out = threading.Thread(target=_drain, args=(proc.stdout, "stdout"), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, "stderr"), daemon=True)
    t_out.start()
    t_err.start()

    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        rc = -1

    # Give the drain threads a moment to flush remaining lines after EOF.
    t_out.join(timeout=2)
    t_err.join(timeout=2)

    elapsed = round(_time.monotonic() - t0, 2)
    emit(
        "✓" if rc == 0 else "✗",
        f"exit={rc}",
        kind="cmd_end",
        phase_id=phase_id,
        returncode=rc,
        elapsed_s=elapsed,
    )
    return rc


# ── uv management ─────────────────────────────────────────────────────────

def _ensure_uv(emit: TraceCallback) -> list[str]:
    """Return a command prefix that invokes uv. Install uv if missing.

    Prefers `sys.executable -m uv` because that always works once the uv
    wheel is pip-installed into the backend's own python — no PATH hunting,
    no curl|sh from the network.

    Pip-install strategy adapts to where this code is running:
      - inside a venv (mcpo's, dev's, etc): plain `pip install` into the
        current site-packages. `--user` is rejected by pip inside venvs.
      - outside any venv (system python, Docker base image): `pip install
        --user` so we don't need root and don't pollute /usr.
    """
    # 1. Already usable as a module in current python?
    if _uv_module_works():
        return [sys.executable, "-m", "uv"]

    # 2. Pip-install into current python. Detect venv to pick the right flag.
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    pip_cmd = [sys.executable, "-m", "pip", "install"]
    if not in_venv:
        pip_cmd.append("--user")
    pip_cmd.append("uv")

    emit(
        "📦",
        f"installing uv via pip into {'venv' if in_venv else 'user site'} (~10s)",
        section="uv-install",
    )
    rc = _run_streaming(pip_cmd, emit, phase_id="uv-install", timeout=180)
    if rc != 0:
        raise RuntimeError(
            f"failed to install uv via pip (exit={rc}); see streamed output above"
        )
    if _uv_module_works():
        return [sys.executable, "-m", "uv"]

    # 3. Last resort: maybe pip dropped a binary somewhere on PATH.
    for candidate in ("uv", str(Path.home() / ".local" / "bin" / "uv")):
        try:
            r = subprocess.run(
                [candidate, "--version"], capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return [candidate]
        except (FileNotFoundError, OSError):
            pass

    raise RuntimeError("uv was installed but cannot be invoked — check site-packages")


def _uv_module_works() -> bool:
    try:
        r = subprocess.run(
            [sys.executable, "-m", "uv", "--version"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _uv_venv(uv_cmd: list[str], emit: TraceCallback) -> None:
    emit("🐍", f"creating venv @ python {PY_VERSION} (~5s)", section="venv")
    # `--python 3.11` lets uv download a matching interpreter if absent.
    cmd = [*uv_cmd, "venv", str(PY_ENV_DIR), "--python", PY_VERSION]
    rc = _run_streaming(cmd, emit, phase_id="venv", timeout=180)
    if rc != 0:
        raise RuntimeError(f"uv venv failed (exit={rc}); see streamed output above")


def _uv_install_requirements(uv_cmd: list[str], emit: TraceCallback) -> None:
    req = FITPACK_LOCAL_DIR / "requirements.txt"
    py = _python_in_env()
    emit(
        "📚",
        f"installing fitpack deps from {req.name} (~20-40s)",
        section="deps",
    )
    cmd = [*uv_cmd, "pip", "install", "--python", str(py), "-r", str(req)]
    rc = _run_streaming(cmd, emit, phase_id="deps", timeout=600)
    if rc != 0:
        raise RuntimeError(
            f"uv pip install failed (exit={rc}); see streamed output above"
        )
