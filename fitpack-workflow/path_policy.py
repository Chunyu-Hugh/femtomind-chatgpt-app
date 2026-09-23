"""Filesystem permission policy for FemtoMind HPC operations.

Enforces the write-boundary contract at the TOOL layer (not just prompts).

Write tiers:
    OWN      /w/ceph24/JAM/<username>/...   (and /ceph24 alias)   -> allowed
    TMP      /tmp, /var/tmp, $TMPDIR                              -> allowed
    SHARED   /w/ceph24/JAM/share/...        (canonical resources) -> DENIED
    FOREIGN  /w/ceph24/JAM/<someone else>/  ...                    -> DENIED
    UNKNOWN  anything else (relative paths, /u/, /work, apps ...)  -> DENIED

Rationale / canonical failure case (2026-09 grid1 incident): an agent replaced
the shared DY grid1 (/w/ceph24/JAM/share/fitpack_grids/dy/grid1) with an
analysis-specific table set.  check_write_paths() rejects exactly that
operation.  Shared resources are readable and copyable FROM, never writable.

All checks are conservative: on doubt, deny with a clear error.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

TIER_OWN = "own"
TIER_TMP = "tmp"
TIER_SHARED = "shared"
TIER_FOREIGN = "foreign"
TIER_UNKNOWN = "unknown"

WRITABLE_TIERS = {TIER_OWN, TIER_TMP}

# Canonical read-only locations we must never write to.
_READONLY_ROOTS = (
    "/w/ceph24/JAM/share",
    "/ceph24/JAM/share",
    "/u/scigroup",
    "/w/ceph24/JAM/apps",
    "/work/JAM/share",
)


class PolicyError(PermissionError):
    """Raised when an operation would violate the write-boundary policy."""


@dataclass
class PolicyVerdict:
    path: str
    tier: str
    allowed: bool
    reason: str


def _norm(p: str) -> str:
    """Normalize a path string for classification (no filesystem access)."""
    p = str(p).strip()
    if not p:
        return ""
    if p.startswith("~"):
        return ""  # ~ expansion is ambiguous -> UNKNOWN
    # drop trailing slashes, collapse '//'
    p = re.sub(r"/+", "/", p)
    if p != "/":
        p = p.rstrip("/")
    return p


def _jam_user(path: str) -> Optional[str]:
    for root in ("/w/ceph24/JAM/", "/ceph24/JAM/"):
        if path.startswith(root):
            rest = path[len(root):]
            if rest:
                return rest.split("/", 1)[0]
    return None


def classify_path(path: str, username: Optional[str] = None) -> PolicyVerdict:
    """Classify a remote path into a write tier for the given user."""
    n = _norm(path)
    if not n:
        return PolicyVerdict(path, TIER_UNKNOWN, False, "empty or unexpandable path")
    if n == "/":
        return PolicyVerdict(path, TIER_UNKNOWN, False, "filesystem root is not writable")
    if n.startswith("/tmp/") or n == "/tmp" or n.startswith("/var/tmp"):
        return PolicyVerdict(path, TIER_TMP, True, "temporary area")
    if any(n == r or n.startswith(r + "/") for r in _READONLY_ROOTS):
        tier = TIER_SHARED if "/share" in n.split("/JAM/")[-1].split("/", 1)[0] or "JAM/share" in n else TIER_UNKNOWN
        if "/JAM/share" in n or n.startswith(("/w/ceph24/JAM/share", "/ceph24/JAM/share")):
            tier = TIER_SHARED
        return PolicyVerdict(
            path, tier, False,
            "shared/canonical resource is READ-ONLY: may copy FROM, never write, move, replace or rename",
        )
    user = _jam_user(path)
    if user is not None:
        if username and user == username:
            return PolicyVerdict(path, TIER_OWN, True, "inside the user's own JAM area")
        return PolicyVerdict(
            path, TIER_FOREIGN, False,
            f"path belongs to JAM user '{user}', not '{username or '?'}'",
        )
    return PolicyVerdict(
        path, TIER_UNKNOWN, False,
        "path is outside the user's own area and not a known temporary location — write denied by default",
    )


def check_write_path(path: str, username: Optional[str] = None) -> None:
    """Raise PolicyError unless `path` is writable for `username`."""
    v = classify_path(path, username)
    if not v.allowed:
        raise PolicyError(f"write denied for '{path}': {v.reason}")


def check_write_paths(paths: Iterable[str], username: Optional[str] = None) -> None:
    """Raise PolicyError if any of `paths` is not writable."""
    for p in paths:
        check_write_path(p, username)


# --- conservative shell-command scanner -------------------------------------

_WRITE_VERBS = {"cp", "mv", "rm", "ln", "mkdir", "touch", "tee", "install", "dd", "chmod", "chown"}
_INPLACE_EDITORS = {"sed", "perl"}
_REDIRECT_TOKENS = {">", ">>", ">|", "1>", "2>", "&>"}
_SBATCH_PATH_KEYS = ("--output", "--error")


def _is_pathy(tok: str) -> bool:
    return "/" in tok or tok.startswith(("~", "."))


def scan_command_for_writes(cmd: str, username: Optional[str] = None) -> List[PolicyVerdict]:
    """Conservatively scan a shell command string for write destinations.

    Returns the list of DENIED write destinations (empty = no violation found).
    This is a heuristic: it catches direct path arguments of common write
    verbs, in-place editors, redirections and sbatch --output/--error.
    """
    violations: List[PolicyVerdict] = []
    try:
        toks = shlex.split(cmd, posix=True)
    except ValueError:
        # unbalanced quotes -> tokenize loosely and bail if nothing matches
        toks = cmd.split()
    toks = [t for t in toks if t]  # drop empty
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        if t in _REDIRECT_TOKENS and i + 1 < n:
            target = toks[i + 1]
            if not target.startswith("/dev/"):
                v = classify_path(target, username)
                if not v.allowed:
                    violations.append(v)
            i += 2
            continue
        if t in _WRITE_VERBS or t in _INPLACE_EDITORS:
            # collect following non-flag args as candidate destinations;
            # the LAST pathy arg is the destination for cp/mv/ln/tee/sed -i
            j = i + 1
            args: List[str] = []
            while j < n and toks[j] not in ("|", ";", "&&", "||", "#") and not toks[j].startswith("#"):
                a = toks[j]
                if a.startswith("-"):
                    # -sfn style flags; also capture sed -i
                    j += 1
                    continue
                args.append(a)
                j += 1
            # sed/perl only write with -i
            if t in _INPLACE_EDITORS:
                inplace = any(toks[k] == "-i" or toks[k].startswith("-i") for k in range(i + 1, j))
                if not inplace:
                    i = j
                    continue
                # -i means: the file args are edited in place
                candidates = [a for a in args if _is_pathy(a)]
            elif t == "cp":
                candidates = args[-1:] if args else []
            elif t == "mv":
                candidates = args[-1:] if args else []
            elif t == "ln":
                candidates = args[-1:] if args else []
            elif t == "tee":
                candidates = args[-1:] if args else []
            elif t == "rm":
                candidates = [a for a in args if _is_pathy(a)]
            else:  # mkdir/touch/install/dd/chmod/chown
                candidates = [a for a in args if _is_pathy(a)]
            for c in candidates:
                if "$" in c or c.startswith("${"):
                    continue  # variable-expanded path: cannot classify statically
                v = classify_path(c, username)
                if not v.allowed:
                    violations.append(v)
            i = j
            continue
        if t.startswith("--output=") or t.startswith("--error="):
            p = t.split("=", 1)[1]
            if "$" not in p:
                v = classify_path(p, username)
                if not v.allowed:
                    violations.append(v)
            i += 1
            continue
        if t in _SBATCH_PATH_KEYS and i + 1 < n:
            p = toks[i + 1]
            if "$" not in p:
                v = classify_path(p, username)
                if not v.allowed:
                    violations.append(v)
            i += 2
            continue
        i += 1
    return violations


def assert_command_write_safe(cmd: str, username: Optional[str] = None) -> None:
    """Raise PolicyError if the shell command appears to write to a denied path."""
    bad = scan_command_for_writes(cmd, username)
    if bad:
        first = bad[0]
        raise PolicyError(
            f"command appears to write to a protected path '{first.path}' "
            f"({first.reason}); shared/canonical resources are read-only — "
            "propose the change to the user instead"
        )
