"""NESAP SBI: live alpha_s likelihood from frozen classifier-score counts.

The science lives in upstream/, a git submodule of
FelixRinger/femtomind-nesap-sbi pinned to one commit; this file only adapts
its tools to the gateway and is the one file of the plugin kept in this
repository. Nothing in upstream/ is modified. Its backend reads only its own
data/ bundle (hash-checked on every call): no cluster, no shell, no Slurm.
"""

import copy
import importlib.util
import os
import sys

NAME = "sbi"
SHIP = ("plugin.py", "upstream/backend.py", "upstream/server.py",
        "upstream/static/panel.html", "upstream/data/*.json")
UPSTREAM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upstream")

# ChatGPT keys its widget on the legacy skybridge MIME, other MCP Apps hosts
# on the standard one, as for the JAM panel: the same file under two URIs.
UI_URI = "ui://nesap/sbi-panel"
UI_URI_SKYBRIDGE = "ui://nesap/sbi-panel.skybridge"

INSTRUCTIONS = (
    "NESAP SBI (tools sbi_options, sbi_infer, sbi_validation) is a separate workflow from the JAM "
    "steps above: it needs no check_hpc_connection, connect_hpc, workspace, campaign or Slurm call.\n"
    "Use sbi_options to discover the supported fixed NESAP SBI workflow. Only alpha_s and "
    "benchmark-1 through benchmark-5 are currently supported. If no sample is specified, state that "
    "benchmark-3 is the default. Call sbi_infer to obtain the numerical result. Do not calculate or "
    "invent a fit. Report the returned best fit, confidence components and numerical checks. If a "
    "tool fails or its checks fail, report that failure rather than presenting an inference result "
    "as successful. State that simulation, classifier training, calibration and scoring were "
    "completed earlier. Only the likelihood calculation runs live. Generating truth is available "
    "for a comparison. It is not used by the estimator. Use sbi_validation for historical "
    "validation evidence. Distinguish this archived cohort from the selected single example and "
    "from numerical checks. These are likelihood-ratio confidence sets, not Bayesian credible "
    "intervals. Do not change nuisance parameters, sample size, calibration or model identity. "
    "Do not call cluster login, shell, Slurm, generation or training tools for this workflow. "
    "Preserve the scientific provenance in exports and acknowledge unsupported requests.")

_backend = _server = None


def _import(name):
    # Unique module names: upstream's generic backend/server must not collide.
    spec = importlib.util.spec_from_file_location("jam_plugin_sbi_" + name,
                                                  os.path.join(UPSTREAM, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load():
    global _backend, _server
    _backend, _server = _import("backend"), _import("server")
    _backend.load_bundle()          # hash-checks the frozen data; raises if it was altered


def _call(name, args):
    # The gateway has already applied `username` (shared mode); upstream
    # rejects any argument it does not define.
    args = {k: v for k, v in (args or {}).items() if k != "username"}
    return _server.invoke(_backend, name, args)


def tools():
    out = {}
    for t in _server.tools_list():
        meta = copy.deepcopy(t.get("_meta") or {})
        if "openai/outputTemplate" in meta:
            meta["openai/outputTemplate"] = UI_URI_SKYBRIDGE
        out[t["name"]] = {"desc": t["description"], "schema": copy.deepcopy(t["inputSchema"]),
                          "annotations": copy.deepcopy(t.get("annotations")), "meta": meta or None,
                          "fn": lambda a, name=t["name"]: _call(name, a)}
    return out


def _panel():
    with open(os.path.join(UPSTREAM, "static", "panel.html"), encoding="utf-8") as fh:
        return fh.read()


def resources():
    meta = {"ui": {"prefersBorder": True}, "openai/widgetPrefersBorder": True}
    return {UI_URI: {"name": "sbi-panel", "mimeType": _server.UI_MIME, "text": _panel, "meta": meta,
                     "description": "NESAP SBI likelihood panel"},
            UI_URI_SKYBRIDGE: {"name": "sbi-panel-chatgpt", "mimeType": "text/html+skybridge",
                               "text": _panel, "meta": meta,
                               "description": "NESAP SBI likelihood panel (ChatGPT widget)"}}
