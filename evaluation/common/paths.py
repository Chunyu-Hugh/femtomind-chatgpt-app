"""Where the evaluation finds the application and keeps its own files.

The application is this repository (the parent of evaluation/); the gateway's
Python lives in lqcdmcp-deploy/, which is put on sys.path so that the jamai
package imports as the gateway imports it. Everything the evaluation writes
goes under EVAL_ROOT (default ~/bench_eval), never under the pilot's
~/bench_nomcp, which may be running.
"""
import os
import sys

APP_ROOT = os.path.realpath(os.environ.get("FEMTOMIND_APP_ROOT") or os.path.join(os.path.dirname(__file__), "..", ".."))
DEPLOY = os.path.join(APP_ROOT, "lqcdmcp-deploy")
ANALYSES = os.path.join(APP_ROOT, "analyses")
EVAL_ROOT = os.path.expanduser(os.environ.get("FEMTOMIND_EVAL_ROOT") or "~/bench_eval")


def gateway_on_path():
    """Make `from jamai import ...` work as it does inside the gateway."""
    if DEPLOY not in sys.path:
        sys.path.insert(0, DEPLOY)
    return DEPLOY


def eval_dir(*parts):
    path = os.path.join(EVAL_ROOT, *parts)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path
