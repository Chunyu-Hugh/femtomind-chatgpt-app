"""
Lightweight helpers for keeping FITPACK paths configurable.

The code base historically relied on hardcoded absolute paths on the lab HPC.
This module provides a single place to resolve the repository root, push it to
``sys.path``, and populate the ``FITPACK`` environment variable for downstream
consumers such as ``nuclib`` tables and grid loaders.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def bootstrap_fitpack_path(path_hint: Optional[str] = None) -> Path:
    """
    Resolve the fitpack root and ensure it is on ``sys.path`` while setting the
    ``FITPACK`` environment variable.

    Resolution order:
    1) Explicit ``path_hint`` argument.
    2) ``FITPACK_PATH`` environment variable (kept for readability).
    3) Parent of this file's parent (the repository root).
    """
    fallback_root = Path(__file__).resolve().parents[1]
    root = Path(path_hint or os.environ.get("FITPACK_PATH") or fallback_root).resolve()

    # Keep the path on sys.path so scripts run from arbitrary working dirs still
    # find local imports without relying on cluster-specific absolute paths.
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    # Many utilities rely on FITPACK for locating grids/tables; default it here.
    os.environ.setdefault("FITPACK", str(root))

    return root
