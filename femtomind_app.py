"""
FemtoMind ChatGPT App (Apps SDK / "Plugins") — MCP server.

A thin facade exposing a read-only subset of the FemtoMind physics tools to
ChatGPT via the MCP Apps standard:

  - transport: streamable HTTP at /mcp (what ChatGPT Developer Mode expects)
  - tool results: structuredContent (model-visible JSON) + content (text)
  - UI: two widgets registered as ui:// HTML resources with the
    `text/html;profile=mcp-app` mimetype, linked from tools through
    `_meta.ui.resourceUri` (plus the `openai/outputTemplate` legacy alias)

Heavy lifting stays in the already-running mcpo gateway (:8000) that fronts
the fitpack / hpc MCP servers — this process just proxies HTTP to it, so it
has zero import side effects and survives branch switches of the main repo.

Run:  .venv/bin/python femtomind_app.py   (serves on 0.0.0.0:8010, path /mcp)
"""

import base64
import contextvars
import json
import math
import os
import random
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

MCPO_BASE = os.environ.get("FEMTOMIND_MCPO_BASE", "http://localhost:8000")
PORT = int(os.environ.get("FEMTOMIND_APP_PORT", "8010"))
WIDGET_DIR = Path(__file__).parent / "widgets"

# Widget resource URIs are cache keys: bump the version segment on any
# breaking HTML/JS change and update the tool _meta that references them.
JOBS_CARD_URI = "ui://femtomind/jobs-card/v2.html"
FIT_SUMMARY_URI = "ui://femtomind/fit-summary/v10.html"
COMPOSER_URI = "ui://femtomind/fit-composer/v3.html"
REPLICA_PLOT_URI = "ui://femtomind/replica-plot/v3.html"

# Reaction families the composer exposes. Kept to the set whose data and theory
# modules are verified in fitpack_legacy (JAM24 ladder + JAM25 pion/kaon).
# Polarized (pidis/pjet) and pp-hadron families stay hidden until verified.
_COMPOSER_FAMILIES = (
    "idis", "dy", "dy-kaon", "lqcd_mom",
    "jet", "wasym", "wzrv", "zrap", "wc", "sidis", "sia",
)
# v2: the host can populate window.openai.toolOutput after the widget script
# runs; v1 painted once and stuck on "not connected". Bumping the version is
# what forces ChatGPT to drop its cached copy of the HTML.
# Curves shipped to the widget per plot. The host drops payloads past a few
# hundred kB; 150 replicas of one flavour is ~260 kB before rounding.
PLOT_REPLICA_LIMIT = int(os.environ.get("FEMTOMIND_PLOT_REPLICA_LIMIT", "150"))

OPERATIONS_URI = "ui://femtomind/operations/v29.html"

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False, destructiveHint=False)
WRITE_SAFE = ToolAnnotations(readOnlyHint=False, openWorldHint=False, destructiveHint=False, idempotentHint=False)

# CSP: both widgets are fully self-contained (no external scripts, styles,
# or API calls — data arrives via the host bridge), so allowlists stay empty.
WIDGET_RESOURCE_META = {
    "ui": {
        "prefersBorder": True,
        "csp": {"connectDomains": [], "resourceDomains": []},
    }
}


def _ui_meta(resource_uri: str, invoking: str, invoked: str) -> dict[str, Any]:
    """Standard MCP-Apps key plus the ChatGPT legacy alias + status strings."""
    return {
        "openai/outputTemplate": resource_uri,
        "openai/widgetAccessible": True,
        "ui": {"resourceUri": resource_uri},
        "openai/toolInvocation/invoking": invoking,
        "openai/toolInvocation/invoked": invoked,
    }


# Tools with no widget of their own that the operations widget invokes via
# window.openai.callTool must still be marked widget-accessible.
CALLABLE_META = {"openai/widgetAccessible": True}


# The authenticated caller for the request being handled. Tools read this
# instead of trusting a chat_id argument, so one user can never address
# another user's workspace by passing someone else's id.
_CURRENT_USER: "contextvars.ContextVar[Optional[dict[str, Any]]]" = contextvars.ContextVar(
    "femtomind_user", default=None
)


def _scope(chat_id: str = "") -> str:
    """The namespace every backend call is filed under.

    Falls back to the supplied chat_id only when authentication is disabled
    (local development); in the deployed server the identity always wins.
    """
    who = _CURRENT_USER.get()
    if who:
        return who["user_id"]
    return chat_id or "chatgpt-composer"


def _hpc_user_for_scope(default: str = "") -> str:
    """Which cluster account this caller's jobs run under."""
    who = _CURRENT_USER.get()
    if who and who.get("hpc_username"):
        return who["hpc_username"]
    # No personal default: an unset FEMTOMIND_HPC_USER should surface as "no
    # account configured", not silently address someone else's cluster home.
    return default or os.environ.get("FEMTOMIND_HPC_USER", "")


def _call_mcpo(path: str, payload: dict) -> Any:
    """POST to the mcpo gateway and return parsed JSON (or error dict).

    Every tool invocation funnels through here, so it doubles as the usage
    meter: with FEMTOMIND_USAGE_LOG set, one JSONL line per backend call
    (timestamp, caller, tool path, latency, error flag) is appended. That is
    the raw material for the token-savings position paper — the meetings
    agreed to report token consumption as a success metric, and per-call
    counts are the number to divide by.
    """
    _started = time.monotonic()
    try:
        r = httpx.post(f"{MCPO_BASE}{path}", json=payload, timeout=60.0)
        r.raise_for_status()
        result = r.json()
    except httpx.HTTPStatusError as e:
        result = {"error": f"backend {e.response.status_code}: {e.response.text[:300]}"}
    except Exception as e:  # connection refused, timeout, bad JSON …
        result = {"error": f"backend unreachable: {type(e).__name__}: {e}"}
    usage_log = os.environ.get("FEMTOMIND_USAGE_LOG")
    if usage_log:
        try:
            with open(usage_log, "a") as fh:
                fh.write(json.dumps({
                    "ts": int(time.time()),
                    "user": _scope(),
                    "path": path,
                    "ms": round((time.monotonic() - _started) * 1000, 1),
                    "error": isinstance(result, dict) and "error" in result,
                }) + "\n")
        except OSError:
            pass
    return result


def _hpc_status(chat_id: str = "chatgpt-composer") -> dict[str, Any]:
    """Single source of truth for whether the HPC ControlMaster is actually live.

    Careful: the backend does NOT signal "not connected" with an `error` key.
    `/fitpack/get_hpc_account` returns `{"status":"not_configured", ...}` and
    `/hpc/get_slurm_info` returns an exception STRING ("No HPC username
    resolved …") when no socket is open — so "no error key" is a false positive.
    Ground truth = did sinfo/squeue actually answer.
    """
    acct = _call_mcpo("/fitpack/get_hpc_account", {"chat_id": _scope(chat_id)})
    # Probe with the caller's own cluster account. `get_slurm_info` takes no
    # arguments and resolves a username internally, which cannot work once more
    # than one collaborator is configured — `get_slurm_jobs` accepts the user,
    # so each person's reachability is tested against their own master.
    probe_user = ""
    if isinstance(acct, dict):
        probe_user = str(acct.get("hpc_username") or "").strip()
    probe_user = probe_user or _hpc_user_for_scope()
    info = _call_mcpo("/hpc/get_slurm_jobs", {"user": probe_user} if probe_user else {})

    def _looks_error(x: Any) -> bool:
        if isinstance(x, dict):
            return bool(x.get("error")) or str(x.get("status", "")).lower() in ("error", "not_configured")
        if isinstance(x, str):
            low = x.lower()
            return any(s in low for s in ("exception", "no hpc username", "not configured", "traceback", "error"))
        return True  # None / unexpected → treat as not reachable

    reachable = not _looks_error(info)  # sinfo/squeue actually returned data
    configured = isinstance(acct, dict) and str(acct.get("status", "")).lower() not in ("not_configured", "error")
    if isinstance(acct, dict):
        if acct.get("connected") is True or acct.get("socket_exists") is True:
            configured = True
        elif acct.get("connected") is False or acct.get("socket_exists") is False:
            configured = False
    gw_user = ""
    if isinstance(acct, dict):
        gw_user = str(acct.get("hpc_username") or acct.get("username") or "").strip()
    gw_user = gw_user or _hpc_user_for_scope()
    named = bool(re.fullmatch(r"[A-Za-z0-9._-]{1,32}", gw_user or ""))

    # The probe is one SSH round trip through the same ControlMaster the fits
    # use, so submitting a few hundred jobs starves it and it times out. That
    # was being read as "not connected": the panel dropped to the sign-in form
    # and offered to re-authenticate a session that was working fine, in the
    # middle of the submission it was busy with. An open master is the stronger
    # evidence — a timed-out probe says the channel is busy, not that it is
    # gone. Ask the socket directly before concluding anything.
    master_open = False
    if named:
        master_open = _master_alive(_ifarm_socket(gw_user), f"{gw_user}@{HPC_LOGIN_HOST}")
    probe_timed_out = (not reachable) and master_open
    connected = configured and (reachable or master_open)

    # Is the gateway hop still up? If so the panel can skip the one-time code.
    gateway_open = False
    if named:
        gateway_open = _master_alive(_gateway_socket(gw_user), f"{gw_user}@{GATEWAY_HOST}")
    return {
        "connected": bool(connected),
        "cluster_reachable": bool(reachable),
        "master_open": bool(master_open),
        "probe_timed_out": bool(probe_timed_out),
        "gateway_open": bool(gateway_open),
        "account": acct if isinstance(acct, dict) else {"raw": str(acct)[:500]},
        "slurm_info": info if isinstance(info, dict) else {"raw": str(info)[:1000]},
        # Two independent facts were being reported as one. A session can be
        # open while the account has no workspace configured, and saying "the
        # SSH ControlMaster is not open" in that case is simply false — it
        # sends the user to re-authenticate a session that is already up,
        # when what is missing is a choice on the Project tab.
        "note": (
            "Connected, but the status probe timed out — the SSH channel is "
            "busy, most likely with a large submission still going out. "
            "Jobs already queued are unaffected; do not re-authenticate."
            if probe_timed_out else
            "Connected to the JLab HPC cluster."
            if connected else
            "Signed in to the cluster, but no workspace has been chosen yet. "
            "Pick or create one on the Project tab — there is nothing to "
            "re-authenticate."
            if master_open else
            "Not connected. Open the SSH ControlMaster on the FemtoMind host, then press Re-check."
        ),
    }


# Placeholder replica curves so the plot widget renders before HPC is connected.
# Shape matches /fitpack/plot_replica_observable's `curves_inline` exactly, so
# swapping in real data is a no-op for the widget. Deterministic (fixed seed).
_MOCK_SHAPES = {  # flavor -> (norm, x^a, (1-x)^b) — rough PDF-like envelopes
    "g": (1.9, 0.22, 4.6), "u": (2.6, 0.55, 3.2), "d": (1.5, 0.60, 4.0),
    "s": (0.5, 0.50, 6.0), "ub": (0.35, 0.45, 7.0), "db": (0.42, 0.45, 7.0),
}


def _mock_curves(flavors: list[str], q2: float, n: int = 25) -> dict[str, Any]:
    xs = [10 ** (-4 + (math.log10(0.99) + 4) * i / 119) for i in range(120)]
    rng = random.Random(42)
    cbf: dict[str, list] = {}
    for f in flavors:
        norm, a, b = _MOCK_SHAPES.get(f, (1.0, 0.5, 4.0))
        reps = []
        for _ in range(n):
            jn = norm * (1 + rng.uniform(-0.12, 0.12))
            ja = a + rng.uniform(-0.05, 0.05)
            jb = b + rng.uniform(-0.3, 0.3)
            reps.append([jn * (xv ** ja) * ((1 - xv) ** jb) for xv in xs])
        cbf[f] = reps
    ids = list(range(n))
    return {
        "kind": "femtomind.replica_plot",
        "mock": True,
        "spec": {
            "params": {"dist": "pdf", "flavors": flavors, "q2": q2},
            "panel_state": {"x_log": True, "y_log": True},
            "replica_ids": ids,
        },
        "curves_inline": {
            "x_grid": xs, "curves_by_flavor": cbf,
            "flavors": flavors, "replica_ids": ids,
        },
    }


mcp = FastMCP(
    "femtomind",
    instructions=(
        "FemtoMind exposes the JAM QCD global-analysis platform (Jefferson Lab). "
        "'Fitting' here means chi-square minimization of QCD theory against "
        "deep-inelastic-scattering data — never clothing.\n\n"
        "THE CLUSTER IS ONLY REACHABLE THROUGH THESE TOOLS. FemtoMind holds a "
        "managed SSH connection to Jefferson Lab on the user's behalf, so when "
        "someone asks about their HPC connection, cluster account, SLURM queue, "
        "workspace, fits or PDF plots, answer with these tools. Never try to "
        "reach the cluster with a shell, an SSH client or a browser, never treat "
        "the connector's own name as a hostname, and never ask the user for an "
        "ssh command or a VPN — there is no host for this session to resolve.\n\n"
        "CRITICAL ROUTING RULE: `jam_analysis_panel` is the SINGLE entry point. "
        "Whenever the user wants to work with FemtoMind / JAM in ANY way — "
        "connect to HPC, check the workspace, set up / configure / start / run a "
        "fit, review campaigns, or plot PDF results — you MUST call "
        "`jam_analysis_panel` and let its panel handle it. The panel has tabs "
        "Connect → Project → Campaign → Collections → Analysis, and it calls the "
        "underlying tools itself. Pass `tab` to land on the right one (e.g. 'set "
        "up a fit' → tab='campaign', 'plot the PDFs' → tab='analysis', 'connect' "
        "→ tab='connect'). OPEN IT ONCE: exactly one `jam_analysis_panel` call per user request. If you have already opened a panel while answering this message, do NOT open a second one — the panel refreshes itself and its tabs are switched by the user, not by re-calling the tool. Do NOT gather datasets, replicas, QCD/job settings, or "
        "connection details in chat; do NOT call the individual write tools "
        "(search_datasets, submit_fitting_job, setup_workspace, …) yourself — the "
        "panel is where the human does everything. That is the whole point. The "
        "one exception is a direct factual question ('am I connected?', 'what "
        "campaigns do I have?'): answer it with the matching read-only tool "
        "(check_hpc_connection, list_campaigns, list_slurm_jobs) rather than "
        "refusing or reaching for a shell.\n\n"
        "NEVER ask the user for their cluster password or one-time code in chat, "
        "and never call `connect_hpc` yourself. JLab sign-in is two hops (a PIN + "
        "one-time code for the scilogin gateway, then the ifarm account password) "
        "and those values belong ONLY in the panel's Connect tab, which calls that "
        "tool directly. If the user wants to connect, open the panel once with "
        "tab='connect' and let them sign in there; that single call is the whole "
        "response, so do not follow it with another panel or a status tool.\n\n"
        "The Campaign tab is single-use per fit: once the user submits it, it "
        "locks. To start another fit the user works in a freshly opened panel — "
        "only reopen `jam_analysis_panel` (tab='campaign') when they EXPLICITLY ask "
        "to run another / reconfigure a fit; never proactively after a submit.\n\n"
        "ANSWERING QUESTIONS ABOUT AN EXISTING CAMPAIGN is different — do NOT "
        "open the panel for these, answer in chat with the tool results:\n"
        "  • \"summarize / how are the results of campaign X\" → `list_campaigns` "
        "to resolve the id, then `get_fit_summary` — report chi-square per point, "
        "replica counts and the datasets in tension, with the actual numbers.\n"
        "  • \"plot the uv PDF / show me the gluon\" → `get_pdf_plots` with the "
        "requested flavors. Primary: g, u, ub, d, db, s, sb, c, cb, b, bb. "
        "Derived: uv, dv, sv, sea. Heavy flavours depend on what that fit "
        "configured, and a flavour the fit cannot evaluate comes back reported "
        "in the trace rather than failing the plot, so it is fine to ask. Q2 is "
        "free — pass whatever the user names. If "
        "the user names a campaign, or says an earlier / previous / last one, "
        "call `list_campaigns` first to resolve its id and pass it as "
        "`batch_ids` — otherwise the plot shows the most recent fit, which may "
        "not be the one they meant.\n"
        "  • \"how is the fit going / has it converged / should I keep waiting\" → "
        "`get_fit_progress`, which reads the live iteration logs of replicas that "
        "are STILL RUNNING. `get_fit_summary` only speaks for finished ones.\n"
        "  • \"launch N more replicas on the same campaign\" → `add_replicas`, "
        "which extends that campaign's ensemble; never re-open the composer for "
        "this, and never silently change the datasets.\n"
        "Only setting up a NEW fit requires the panel.\n\n"
        "The individual read-only tools (check_hpc_connection, check_project, "
        "list_campaigns, list_slurm_jobs, get_fit_summary, get_pdf_plots, "
        "search_datasets, get_analysis_roadmap, check_quota) exist mainly for the "
        "panel to call and for answering direct factual questions about existing "
        "state — not for driving a fit setup in chat. Prefer calling a tool over "
        "guessing; if a tool errors, report the error text verbatim rather than "
        "inventing results."
    ),
    stateless_http=True,
    # The SDK rejects requests whose Host header it does not recognise (DNS
    # rebinding protection). Behind a TLS terminator the Host is the public
    # name, so it has to be listed explicitly — keep the protection on rather
    # than disabling it, and add hosts here when the deployment gains a name.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            h for h in (
                os.environ.get("FEMTOMIND_PUBLIC_HOST", ""),
                "localhost", "localhost:8010", "127.0.0.1", "127.0.0.1:8010",
            ) if h
        ],
        allowed_origins=[
            o for o in (
                f"https://{os.environ['FEMTOMIND_PUBLIC_HOST']}" if os.environ.get("FEMTOMIND_PUBLIC_HOST") else "",
                "https://chatgpt.com", "https://chat.openai.com",
                "http://localhost:8010", "http://127.0.0.1:8010",
            ) if o
        ],
    ),
)


# ─── widgets ────────────────────────────────────────────────────────────────

def _register_versioned_resource(
    base_uri: str, name: str, description: str, html_path: str
) -> None:
    """Serve today's HTML under the current URI AND every retired version.

    A client holding a stale tool list keeps asking for the URI it cached; if
    that URI is gone it shows "Failed to fetch template". Keeping old URIs
    alive (all returning today's HTML) makes a version bump non-breaking.
    """
    current = int(base_uri.rsplit("/v", 1)[1].split(".")[0])
    for v in range(1, current + 1):
        uri = f"{base_uri.rsplit('/v', 1)[0]}/v{v}.html"
        mcp.resource(
            uri,
            name=f"{name}-v{v}",
            description=description,
            mime_type="text/html+skybridge",
            meta=WIDGET_RESOURCE_META,
        )(lambda: (WIDGET_DIR / html_path).read_text())


_register_versioned_resource(
    JOBS_CARD_URI, "jobs-card", "Inline card rendering the SLURM job table", "jobs_card.html")
_register_versioned_resource(
    FIT_SUMMARY_URI, "fit-summary-card", "Inline card rendering fit quality metrics for a batch", "fit_summary_card.html")


_register_versioned_resource(
    COMPOSER_URI, "fit-composer", "Interactive form for configuring and submitting a fitting campaign", "fit_composer.html")


def _register_replica_plot_widget() -> None:
    """Serve the standalone plot under its current URI and every retired one,
    for the same reason as the operations panel: a client holding a stale tool
    list must not hit a missing template."""
    current = int(REPLICA_PLOT_URI.rsplit("/v", 1)[1].split(".")[0])
    for v in range(1, current + 1):
        mcp.resource(
            f"ui://femtomind/replica-plot/v{v}.html",
            name=f"replica-plot-v{v}",
            description="Inline SVG plot of fitted PDF replica curves (x-f(x) vs x)",
            mime_type="text/html+skybridge",
            meta=WIDGET_RESOURCE_META,
        )(lambda: (WIDGET_DIR / "replica_plot.html").read_text())


_register_replica_plot_widget()


def _register_operations_widget() -> None:
    """Serve the panel under its current URI *and* every retired one.

    Bumping the version is how a cached copy of the HTML gets dropped, but the
    client keeps its old tool list until the connector is refreshed — so it then
    asks for a URI the server no longer has and shows "Failed to fetch template",
    breaking the app for anyone who has not refreshed yet. Keeping the old URIs
    alive (all returning today's HTML) makes a version bump non-breaking.
    """
    current = int(OPERATIONS_URI.rsplit("/v", 1)[1].split(".")[0])
    for v in range(1, current + 1):
        uri = f"ui://femtomind/operations/v{v}.html"
        mcp.resource(
            uri,
            name=f"femtomind-operations-v{v}",
            description="The FemtoMind operations panel: Connect / Project / Campaign / Collections / Analysis tabs",
            mime_type="text/html+skybridge",
            meta=WIDGET_RESOURCE_META,
        )(lambda: (WIDGET_DIR / "femtomind_ops.html").read_text())


_register_operations_widget()


# ─── tools (all read-only) ──────────────────────────────────────────────────

@mcp.tool(
    annotations=READ_ONLY,
    meta=_ui_meta(JOBS_CARD_URI, "Checking the SLURM queue…", "Fetched SLURM jobs"),
    structured_output=True,
)
def list_slurm_jobs(user: Optional[str] = None) -> dict[str, Any]:
    """List jobs currently in the SLURM queue on the JLab HPC cluster.

    Optionally filter by cluster username. Returns the raw queue table plus a
    parsed job list.
    """
    # Route over this caller's own connection; without a user the hpc server
    # would fall back to "any configured account", i.e. someone else's.
    raw = _call_mcpo("/hpc/get_slurm_jobs", {"user": user or _hpc_user_for_scope()})
    text = raw if isinstance(raw, str) else json.dumps(raw)
    jobs = []
    if isinstance(raw, str):
        lines = [l for l in raw.splitlines() if l.strip()]
        for line in lines[1:]:
            parts = line.split()
            if len(parts) >= 6 and parts[0].lower() != "jobid":
                jobs.append({
                    "job_id": parts[0], "partition": parts[1],
                    "name": parts[2], "user": parts[3],
                    "state": parts[4], "time": parts[5],
                    "time_left": parts[6] if len(parts) > 6 else "",
                })
    return {"jobs": jobs, "raw": text[:4000], "count": len(jobs)}


def _replica_spread(payload: Any) -> Optional[dict[str, Any]]:
    """Describe how tightly the replicas agree, and whether anything vetted them.

    `n_survivors == n_total` reads as "all ten replicas passed", but
    `passed_gate` is set to True for every replica when no convergence gate was
    configured — so a campaign where one replica landed forty times worse than
    the median still reports a clean sweep. Summarize the spread and say plainly
    when nothing was filtered, since deciding whether an outlier belongs in the
    ensemble is the researcher's call, not something to leave implied.
    """
    if not isinstance(payload, dict):
        return None
    res = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    runs = res.get("run_state")
    if not isinstance(runs, list) or not runs:
        return None
    vals = sorted(
        (r.get("latest_chi2_npts") for r in runs
         if isinstance(r, dict) and isinstance(r.get("latest_chi2_npts"), (int, float)))
    )
    if not vals:
        return None
    median = vals[len(vals) // 2]
    gate = res.get("convergence_gate")
    gated = gate not in (None, "", "inf", float("inf"))
    outliers = [
        {"run": r.get("run"), "chi2_npts": round(float(r.get("latest_chi2_npts")), 3)}
        for r in runs
        if isinstance(r, dict) and isinstance(r.get("latest_chi2_npts"), (int, float))
        and median > 0 and float(r["latest_chi2_npts"]) > 3 * median
    ]
    out: dict[str, Any] = {
        "n_replicas": len(vals),
        "chi2_npts_min": round(vals[0], 3),
        "chi2_npts_median": round(median, 3),
        "chi2_npts_max": round(vals[-1], 3),
        "gate_applied": bool(gated),
        "outliers_vs_median": outliers,
    }
    if not gated:
        out["note"] = (
            "No convergence gate was applied, so every replica is marked as passing "
            "regardless of where it converged. Report the spread, not just the count."
        )
    if outliers:
        out["warning"] = (
            f"{len(outliers)} of {len(vals)} replicas converged more than 3x worse than "
            f"the median ({median:.2f}); the ensemble is not homogeneous and the mean "
            "curve will be pulled by them."
        )
    return out


_SUMMARY_RUNSTATE_KEEP = 5


def _compact_summary(payload: Any) -> Any:
    """Attach the spread analysis and stop shipping one row per replica.

    run_state and survivors carry ~150 bytes per replica, so a thousand-replica
    ensemble is a 150 kB tool result landing in the conversation — most of it
    identical rows nobody reads. Keep the best and worst few, say how many were
    dropped, and let replica_spread carry the distribution.
    """
    if not isinstance(payload, dict):
        return payload
    spread = _replica_spread(payload)
    out = {**payload}
    if spread:
        out["replica_spread"] = spread
    res = out.get("result")
    if not isinstance(res, dict):
        return out
    runs = res.get("run_state")
    if not isinstance(runs, list) or len(runs) <= 2 * _SUMMARY_RUNSTATE_KEEP:
        return out
    ranked = sorted(
        (r for r in runs if isinstance(r, dict)),
        key=lambda r: r.get("latest_chi2_npts") if isinstance(r.get("latest_chi2_npts"), (int, float)) else float("inf"),
    )
    k = _SUMMARY_RUNSTATE_KEEP
    res = {**res}
    res["run_state"] = ranked[:k] + ranked[-k:]
    res["run_state_truncated"] = {
        "shown": 2 * k,
        "total": len(runs),
        "note": f"best {k} and worst {k} of {len(runs)} replicas; see replica_spread for the distribution",
    }
    if isinstance(res.get("survivors"), list) and len(res["survivors"]) > 2 * k:
        res["survivors"] = {"count": len(res["survivors"]), "note": "list omitted at this size"}
    out["result"] = res
    return out


@mcp.tool(
    annotations=READ_ONLY,
    meta=_ui_meta(FIT_SUMMARY_URI, "Loading fit summary…", "Fit summary loaded"),
    structured_output=True,
)
def get_fit_summary(batch_id: str) -> dict[str, Any]:
    """Get the quality summary of a completed PDF-fitting batch: chi-square per
    point, replica counts, mean global Z-score, and the datasets with the
    highest tension. Requires the batch_id of a submitted fitting campaign
    (find one with list_campaigns). If the summary has not been computed yet this
    kicks off the computation on the cluster and tells you to ask again shortly —
    report that honestly rather than inventing numbers.

    `replica_spread` says how far apart the replicas converged and whether any
    gate filtered them. When it carries a warning, lead with that: a campaign can
    report every replica as passing and still contain one that landed far outside
    the rest."""
    raw = _call_mcpo("/fitpack/get_summary_bf_result", {"batch_id": batch_id, "chat_id": _scope()})
    if isinstance(raw, dict) and str(raw.get("status")) != "not_found":
        return _compact_summary(raw)
    # Never computed for this batch — start it. compute_summary_bf REQUIRES a
    # chat_id alongside batch_id or it reports "no HPC account for this chat",
    # and since the backend resolves the cluster account by user id with no
    # fallback, the literal placeholder resolved to nobody and every summary
    # came back as "not connected". Send the authenticated identity.
    started = _call_mcpo(
        "/fitpack/compute_summary_bf",
        {"batch_id": batch_id, "chat_id": _scope()},
    )
    retry = _call_mcpo("/fitpack/get_summary_bf_result", {"batch_id": batch_id, "chat_id": _scope()})
    if isinstance(retry, dict) and str(retry.get("status")) != "not_found":
        return _compact_summary(retry)
    return {
        "status": "computing",
        "batch_id": batch_id,
        "note": (
            "The chi-square summary is being computed on the cluster now. "
            "Ask again in a few minutes for the numbers."
        ),
        "backend": started if isinstance(started, dict) else {"raw": str(started)[:300]},
    }


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def list_batches(chat_id: Optional[str] = None, limit: int = 10) -> dict[str, Any]:
    """List recently submitted fitting batches (campaigns) with their status.
    Use the returned batch_id values with get_fit_summary."""
    payload: dict = {"limit": limit}
    if chat_id:
        payload["chat_id"] = chat_id
    raw = _call_mcpo("/hpc/list_batches_in_chat", payload)
    return raw if isinstance(raw, dict) else {"batches": raw}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def search_datasets(
    observable: str = "", experiment: str = "", target: str = ""
) -> dict[str, Any]:
    """Search the experimental dataset catalog used in QCD global fits
    (DIS/Drell-Yan measurements: BCDMS, NMC, HERA, SLAC…). All filters
    optional; empty strings match everything."""
    raw = _call_mcpo(
        "/fitpack/search_datasets",
        {"observable": observable, "experiment": experiment, "target": target},
    )
    return raw if isinstance(raw, dict) else {"datasets": raw}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def get_analysis_roadmap(chat_id: str = "chatgpt-demo") -> dict[str, Any]:
    """Get the staged analysis roadmap (Stage A -> B -> C) of the QCD global
    analysis: which observables and datasets are fit at each stage and the
    current progress."""
    raw = _call_mcpo("/fitpack/get_analysis_roadmap", {"chat_id": _scope(chat_id)})
    return raw if isinstance(raw, dict) else {"roadmap": raw}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def get_job_details(job_id: str) -> dict[str, Any]:
    """Get detailed SLURM information for one job id (state, nodes, elapsed
    time, exit code)."""
    raw = _call_mcpo("/hpc/get_job_details", {"job_id": job_id, "username": _hpc_user_for_scope()})
    return raw if isinstance(raw, dict) else {"details": raw}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def check_quota() -> dict[str, Any]:
    """Check storage quota usage on the HPC cluster filesystems."""
    raw = _call_mcpo("/hpc/quota_check", {"username": _hpc_user_for_scope()})
    return raw if isinstance(raw, dict) else {"quota": raw}


@mcp.tool(annotations=READ_ONLY, meta=CALLABLE_META, structured_output=True)
def check_hpc_connection(chat_id: str = "chatgpt-composer") -> dict[str, Any]:
    """Report whether FemtoMind is connected to the JLab HPC cluster: the
    configured account/workspace and whether the SLURM scheduler is reachable.
    This is the FIRST step of the JAM flow — call it when the user wants to
    connect to HPC, start working, or asks about the connection/cluster."""
    return _hpc_status(chat_id)


# JLab is a TWO-HOP login with DIFFERENT credentials per hop:
#   1. scilogin.jlab.org — PIN + one-time code (time-limited!)
#   2. ifarm            — account password
# Each hop therefore gets its OWN master, opened in its own ssh invocation with
# exactly one secret. That avoids guessing which prompt an askpass call belongs
# to, and it lets the scilogin master persist so the OTP is needed only once per
# ControlPersist window — later ifarm reconnects need just the password.
GATEWAY_HOST = os.environ.get("FEMTOMIND_GATEWAY_HOST", "scilogin.jlab.org")
IFARM_HOST = os.environ.get("FEMTOMIND_IFARM_HOST", "ifarm")
HPC_LOGIN_HOST = os.environ.get("FEMTOMIND_HPC_HOST", "ifarm.jlab.org")
CONTROL_MASTERS_DIR = os.path.expanduser("~/.ssh/controlmasters")
CONTROL_PERSIST = os.environ.get("FEMTOMIND_CONTROL_PERSIST", "8h")


def _gateway_socket(username: str) -> str:
    return os.path.join(CONTROL_MASTERS_DIR, f"jlab-gateway-{username}@{GATEWAY_HOST}:22")


def _ifarm_socket(username: str) -> str:
    # Must match ssh_runner.py's expectation exactly, or the backend won't see it.
    return os.path.join(CONTROL_MASTERS_DIR, f"jlab-ifarm-{username}@{HPC_LOGIN_HOST}:22")


def _master_alive(socket_path: str, dest: str) -> bool:
    if not os.path.exists(socket_path):
        return False
    try:
        r = subprocess.run(
            ["ssh", "-S", socket_path, "-O", "check", dest],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


def _open_master(socket_path: str, dest: str, secret: str, extra_opts: list[str], timeout: int) -> str:
    """Open one ControlMaster, feeding `secret` through a throwaway askpass.

    The helper script contains no secret — it echoes an env var — so the value
    never touches disk. Returns "" on success, else the stderr/diagnostic text.
    """
    os.makedirs(CONTROL_MASTERS_DIR, mode=0o700, exist_ok=True)
    if os.path.exists(socket_path):  # stale socket from a dead master
        try:
            os.unlink(socket_path)
        except OSError:
            pass
    # The helper records only the server's PROMPT TEXT (never the answer) so a
    # failed sign-in can report what was actually asked — JLab chains several
    # auth methods and the prompt tells us which one we are answering.
    prompt_log = tempfile.NamedTemporaryFile("w", suffix="_prompts.txt", delete=False)
    prompt_log.close()
    helper = tempfile.NamedTemporaryFile("w", suffix="_askpass.sh", delete=False)
    helper.write(
        '#!/bin/sh\n'
        'printf "%s\\n" "$1" >> "$FEMTO_PROMPT_LOG" 2>/dev/null\n'
        'printf "%s\\n" "$FEMTO_SECRET"\n'
    )
    helper.close()
    os.chmod(helper.name, 0o700)
    env = dict(
        os.environ,
        FEMTO_SECRET=secret,
        FEMTO_PROMPT_LOG=prompt_log.name,
        SSH_ASKPASS=helper.name,
        SSH_ASKPASS_REQUIRE="force",
        DISPLAY=os.environ.get("DISPLAY", ":0"),
    )
    cmd = [
        "setsid", "ssh", "-M", "-S", socket_path,
        "-o", f"ControlPersist={CONTROL_PERSIST}",
        "-o", "StrictHostKeyChecking=accept-new",
        # JLab offers gssapi + keyboard-interactive + password and ssh walks the
        # list; allow two answered prompts so a first-round retry still lands.
        "-o", "NumberOfPasswordPrompts=2",
        "-o", "PubkeyAuthentication=no",
        "-o", "GSSAPIAuthentication=no",
        "-o", "PreferredAuthentications=keyboard-interactive,password",
        *extra_opts,
        "-fN", dest,
    ]
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        err = (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        err = f"ssh timed out after {timeout}s with no answer from {dest}."
    finally:
        try:
            with open(prompt_log.name) as fh:
                prompts = [p.strip() for p in fh.read().splitlines() if p.strip()]
            if prompts:
                err = (err + f"  [server asked: {' | '.join(prompts[:4])}]").strip()
        except OSError:
            pass
        for path in (helper.name, prompt_log.name):
            try:
                os.unlink(path)
            except OSError:
                pass
    return err


def _auth_hint(err: str) -> str:
    low = (err or "").lower()
    if "permission denied" in low or "authentication failed" in low:
        return " Check the credential — for the gateway the code is PIN followed immediately by the current one-time code, with no space."
    if "timed out" in low:
        return " The one-time code may have expired; generate a fresh one and try again."
    return ""


@mcp.tool(annotations=WRITE_SAFE, meta=CALLABLE_META, structured_output=True)
def connect_hpc(
    username: str = "",
    gateway_code: str = "",
    ifarm_password: str = "",
    chat_id: str = "chatgpt-composer",
) -> dict[str, Any]:
    """Sign in to the JLab HPC cluster and open the SSH ControlMasters, so the
    rest of the JAM flow runs for real. Called BY the FemtoMind panel's Connect
    tab with what the user typed in the form — never call this yourself and never
    ask for these values in chat.

    JLab needs two hops: `gateway_code` is the PIN + one-time code for
    scilogin.jlab.org, `ifarm_password` is the account password for ifarm. Both
    are used once to open the masters and are never stored or logged. The gateway
    master persists, so while it is still alive only `ifarm_password` is needed —
    call `check_hpc_connection` first to see whether the gateway is already open.
    """
    username = (username or "").strip() or _hpc_user_for_scope()
    # Keep the name to a shell-safe charset: it is interpolated into socket paths
    # and into the ProxyCommand string that ssh hands to a shell.
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", username):
        return {"connected": False, "error": "Invalid cluster username."}
    gw_sock, gw_dest = _gateway_socket(username), f"{username}@{GATEWAY_HOST}"
    fa_sock, fa_dest = _ifarm_socket(username), f"{username}@{IFARM_HOST}"

    # ── hop 1: gateway (PIN + one-time code) ────────────────────────────────
    gateway_reused = _master_alive(gw_sock, gw_dest)
    if not gateway_reused:
        if not gateway_code:
            return {
                "connected": False,
                "stage": "gateway",
                "error": f"A PIN + one-time code is required to sign in to {GATEWAY_HOST}.",
            }
        err = _open_master(gw_sock, gw_dest, gateway_code, [], timeout=90)
        if not _master_alive(gw_sock, gw_dest):
            return {
                "connected": False,
                "stage": "gateway",
                "error": (f"Could not sign in to {GATEWAY_HOST}. "
                          + (err or "No ControlMaster was established.")[:400] + _auth_hint(err)),
            }

    # ── hop 2: ifarm (account password), proxied through the live gateway ───
    if not _master_alive(fa_sock, fa_dest):
        if not ifarm_password:
            return {
                "connected": False,
                "stage": "ifarm",
                "gateway_open": True,
                "error": f"Signed in to {GATEWAY_HOST}; now the {IFARM_HOST} account password is required.",
            }
        # username is charset-validated above, so these paths carry no spaces or
        # shell metacharacters and need no quoting inside ProxyCommand.
        proxy = ["-o", f"ProxyCommand=ssh -S {gw_sock} -W %h:%p {gw_dest}"]
        err = _open_master(fa_sock, fa_dest, ifarm_password, proxy, timeout=90)
        if not _master_alive(fa_sock, fa_dest):
            return {
                "connected": False,
                "stage": "ifarm",
                "gateway_open": True,
                "error": (f"Signed in to {GATEWAY_HOST}, but could not open {IFARM_HOST}. "
                          + (err or "No ControlMaster was established.")[:400] + _auth_hint(err)),
            }

    # both masters live → register the account, then report real status
    # Remember which cluster account this person signed in with, so later
    # requests route to their masters without asking again.
    try:
        import femtomind_auth as _auth
        who = _CURRENT_USER.get()
        if who:
            with _auth._db() as _c:
                _c.execute("UPDATE users SET hpc_username=? WHERE user_id=?", (username, who["user_id"]))
            who["hpc_username"] = username
    except Exception:
        pass
    _call_mcpo("/fitpack/setup_hpc_account", {"hpc_username": username, "chat_id": _scope(chat_id)})
    status = _hpc_status(chat_id)
    status["gateway_open"] = True
    status["gateway_reused"] = gateway_reused
    # Check the workspace right here rather than waiting for the user to go
    # looking: signing in is exactly the moment to learn that something is
    # missing, and what to do about it.
    status["workspace"] = _workspace_report(username)
    return status


@mcp.tool(annotations=READ_ONLY, meta=CALLABLE_META, structured_output=True)
def explain_failure(task_path: str = "", batch_id: str = "", chat_id: str = "chatgpt-composer") -> dict[str, Any]:
    """Explain why a fitting job failed. Give it a task_path (from
    list_campaigns) or a batch_id, and it reads that job's SLURM error log on
    the cluster and reports the actual cause — the exception line, not just
    "failed". Backs the panel's Collections tab when a campaign shows as failed."""
    path = (task_path or "").strip()
    if not path and batch_id:
        rows = _call_mcpo("/fitpack/list_chat_tasks", {"chat_id": _scope(chat_id), "sync": False})
        if isinstance(rows, list):
            for r in rows:
                if r.get("batch_id") == batch_id and r.get("path"):
                    path = r["path"]
                    break
    if not path:
        return {"error": "Provide a task_path (or a batch_id that has one)."}
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,256}", path):
        return {"error": "Invalid task_path."}

    username = _hpc_user_for_scope()
    socket = _ifarm_socket(username)
    if not _master_alive(socket, f"{username}@{IFARM_HOST}"):
        return {"error": "Not connected to HPC — sign in on the Connect tab first."}
    # Single-quoted so the login shell (tcsh on ifarm) hands the script to sh intact.
    script = f"tail -40 {path}/slurm_*.err 2>/dev/null; echo ---OUT---; tail -12 {path}/slurm_*.out 2>/dev/null"
    try:
        r = subprocess.run(
            ["ssh", "-S", socket, f"{username}@{IFARM_HOST}", "sh", "-c", f"'{script}'"],
            capture_output=True, text=True, timeout=45,
        )
        blob = r.stdout or ""
    except Exception as e:
        return {"error": f"Could not read the job log: {type(e).__name__}: {e}"}

    err_part = blob.split("---OUT---")[0]
    lines = [l.rstrip() for l in err_part.splitlines() if l.strip()]
    # The last exception-looking line is the cause; everything above is context.
    reason = ""
    for l in reversed(lines):
        if re.match(r"^\w*(Error|Exception|KeyError|ValueError|TypeError)\b", l.strip()):
            reason = l.strip()
            break
    hint = ""
    if "KeyError: 'col'" in err_part:
        hint = (
            "One of the selected datasets has no 'col' column in its data table. "
            "Those are greyed out in the Campaign tab — deselect them and resubmit."
        )
    return {
        "task_path": path,
        "reason": reason or "no exception line found in the error log",
        "hint": hint,
        "error_tail": "\n".join(lines[-18:]),
    }


@mcp.tool(annotations=READ_ONLY, meta=CALLABLE_META, structured_output=True)
def get_dataset_catalog() -> dict[str, Any]:
    """Return the selectable experimental datasets, with the ones the fitter
    cannot report on flagged as disabled. The panel calls this when its start-up
    payload arrived without a catalog, so the Campaign tab is never left empty."""
    raw = _call_mcpo("/fitpack/search_datasets", {"observable": "", "experiment": "", "target": ""})
    full = raw.get("by_observable", {}) if isinstance(raw, dict) else {}
    return {"catalog": _mark_unusable({k: v for k, v in full.items() if k in _COMPOSER_FAMILIES})}


def _remote_base_for(username: str, chat_id: str = "") -> str:
    """Where this user's workspace actually lives.

    Prefer what the backend recorded for them (they may have set it up under a
    folder name of their own choosing); fall back to the conventional layout.
    """
    acct = _call_mcpo("/fitpack/get_hpc_account", {"chat_id": _scope(chat_id)})
    if isinstance(acct, dict):
        rb = str(acct.get("remote_base") or "").strip()
        if rb:
            return rb
    return f"/w/ceph24/JAM/{username}/fitpack_legacy"


def _workspace_report(username: str, base: str = "") -> dict[str, Any]:
    """Inspect the user's own cluster workspace and say what, if anything, they
    must do about it.

    Runs as one SSH round-trip. Every item carries the real path, because the
    thing people need to trust is that the work happens in *their* directory
    under *their* account — a bare green tick does not tell them that.
    """
    if not username:
        return {"ok": False, "items": [], "note": "No cluster account is configured yet."}

    base = base or _remote_base_for(username)
    shared_grids = SHARED_GRIDS
    # Also list every checkout in their area, so the panel can offer a choice
    # instead of silently deciding which workspace they meant. A directory
    # counts as one if it has fitlib/ — that is what the fit actually imports.
    script = (
        f'B={base}; echo "base:$( [ -d "$B" ] && echo yes || echo no )"; '
        f'for d in fitlib qcdlib obslib nuclib database examples inputs grids runs; do '
        f'echo "$d:$( [ -e "$B/$d" ] && echo yes || echo no )"; done; '
        f'echo "shared_grids:$( [ -d {shared_grids} ] && echo yes || echo no )"; '
        f'echo "writable:$( [ -w "$B" ] && echo yes || echo no )"; '
        f'for c in /w/ceph24/JAM/{username}/*/; do '
        f'[ -d "$c/fitlib" ] && echo "candidate:${{c%/}}"; done; '
        # A link whose target has moved still passes an "is it there" glance but
        # fails the moment a fit needs that observable, so count them explicitly.
        f'n=0; for g in "$B"/grids/*; do '
        f'[ -L "$g" ] && [ ! -e "$g" ] && n=$((n+1)); done; echo "broken_grids:$n"'
    )
    sock = _ifarm_socket(username)
    base_info = {"workspace": base, "cluster_account": username}
    try:
        # Feed the script on stdin. ssh joins argv with spaces before the remote
        # login shell (tcsh here) parses it, so an inline `bash -lc "…"` gets
        # torn apart on quotes and `$(`; `bash -s` sidesteps that entirely.
        r = subprocess.run(
            ["ssh", "-S", sock, f"{username}@{IFARM_HOST}", "bash", "-s"],
            input=script, capture_output=True, text=True, timeout=45,
        )
        got, candidates = {}, []
        for line in r.stdout.splitlines():
            if ":" not in line or line.startswith("/"):
                continue
            k, v = line.split(":", 1)
            if k == "candidate":
                candidates.append(v.strip())
            else:
                got[k] = v
    except Exception as e:
        return {**base_info, "ok": False, "items": [],
                "note": f"Could not reach the cluster to check the workspace ({type(e).__name__})."}

    if not got:
        return {**base_info, "ok": False, "items": [],
                "note": "The cluster did not answer — is the SSH connection still open?"}

    have = lambda k: got.get(k, "no").strip() == "yes"
    items: list[dict[str, Any]] = []

    # The code tree is the one thing this app will not create for someone: it is
    # gigabytes, it is a private repository, and a wrong copy silently produces
    # wrong physics. Say precisely what to do instead.
    if have("base"):
        items.append({"name": "JAM code (fitpack_legacy)", "ok": True, "detail": base})
    else:
        items.append({
            "name": "JAM code (fitpack_legacy)", "ok": False, "detail": base,
            "action": (f"Not found. Copy or clone fitpack_legacy into {base} — "
                       "ask the JAM group for the repository if you do not have it yet."),
        })

    for d, label in (("fitlib", "fitlib"), ("qcdlib", "qcdlib"), ("obslib", "obslib"),
                     ("database", "experimental data")):
        if have("base"):
            items.append({"name": label, "ok": have(d), "detail": f"{base}/{d}",
                          **({} if have(d) else {"action": "Missing from the checkout — re-sync fitpack_legacy."})})

    # These two are cheap and safe to create, so they are offered as a fix
    # rather than reported as a problem the user has to go solve elsewhere.
    if have("base"):
        items.append({"name": "run directory", "ok": have("runs"), "detail": f"{base}/runs",
                      **({} if have("runs") else {"fixable": True,
                          "action": "Will be created automatically when you submit."})})
        nbroken = int((got.get("broken_grids") or "0").strip() or 0)
        if have("grids") and nbroken:
            items.append({
                "name": "interpolation grids", "ok": False, "fixable": True,
                "detail": f"{base}/grids — {nbroken} link(s) point at a path that no longer exists",
                "action": ("Fits using those observables (DY, jet, W asymmetry…) would fail. "
                           "Press Repair to relink them to the shared grids."),
            })
        else:
            items.append({"name": "interpolation grids", "ok": have("grids"),
                      "detail": f"{base}/grids" if have("grids") else f"link to {shared_grids}",
                      **({} if have("grids") else {"fixable": True,
                          "action": f"Can be linked to the shared copy at {shared_grids}."})})

    blocking = [i for i in items if not i["ok"] and not i.get("fixable")]
    return {
        "ok": not blocking,
        "workspace": base,
        "cluster_account": username,
        # Every checkout we can see, so the user picks rather than being
        # assigned one. The active entry is whichever `base` resolved to.
        "candidates": [{"path": c, "active": c.rstrip("/") == base.rstrip("/")} for c in candidates],
        "items": items,
        "note": (
            f"Everything runs in your own directory ({base}) under your account "
            f"({username}); the shared grids are read-only."
            if not blocking else
            "Some pieces are missing — see the actions listed."
        ),
    }


FITPACK_REPO = os.environ.get("FEMTOMIND_FITPACK_REPO", "github.com/QCDHUB/fitpack_legacy.git")
# /ceph24 is not mounted on ifarm; the same tree is reachable as /w/ceph24 and
# as /work/JAM. Links written with the bare /ceph24 prefix dangle silently.
SHARED_GRIDS = os.environ.get("FEMTOMIND_SHARED_GRIDS", "/w/ceph24/JAM/share/fitpack_grids")


def _classify_clone_failure(err: str) -> dict[str, str]:
    """Turn git's output into something the user can act on.

    A raw git error tells someone nothing about what *they* should do next,
    which is the only question they have at that moment.
    """
    low = (err or "").lower()
    if "invalid username or token" in low or "authentication failed" in low or "could not read username" in low:
        return {"reason": "auth",
                "message": "GitHub rejected the credentials. fitpack_legacy is a private repository.",
                "action": "Enter a GitHub personal access token with read access to QCDHUB/fitpack_legacy and try again."}
    if "repository not found" in low or "not found" in low:
        return {"reason": "access",
                "message": "GitHub says the repository does not exist or your account cannot see it.",
                "action": "Ask the JAM group to grant your GitHub account access to QCDHUB/fitpack_legacy."}
    if "permission denied" in low or "read-only file system" in low or "cannot create" in low:
        return {"reason": "permission",
                "message": "The cluster refused to create that directory.",
                "action": "Pick a different folder name, or ask JLab whether your account can write to the JAM area."}
    if "already exists" in low and "not an empty" in low:
        return {"reason": "exists",
                "message": "That folder already exists and is not empty.",
                "action": "Choose a different folder name."}
    if "no space" in low or "quota" in low:
        return {"reason": "space",
                "message": "The cluster reports no space left.",
                "action": "Free up space in your JAM area, or ask about your quota."}
    if "timed out" in low or "timeout" in low:
        return {"reason": "timeout",
                "message": "The clone did not finish in time.",
                "action": "The repository is large; try again — a partial clone is removed automatically."}
    return {"reason": "unknown",
            "message": "The clone failed.",
            "action": "See the details below; fix what it reports and try again."}


# Files the fitting workflow needs that the fitpack repository does not carry:
# the FemtoMind fit driver, the config loader's YAML branch, the path bootstrap
# it imports, the chi-square worker, and a starting input card. They live here
# so a new workspace is complete the moment it is cloned, instead of depending
# on someone's existing checkout. Remove entries as they land upstream.
OVERLAY_DIR = Path(__file__).parent / "fitpack_overlay"


def _push_overlay(username: str, target: str) -> dict[str, Any]:
    """Copy the overlay into a freshly cloned workspace.

    Written through one ssh session with a here-doc per file: scp would need a
    second connection, and the ControlMaster this session already holds is the
    only authenticated path to the cluster.
    """
    if not OVERLAY_DIR.is_dir():
        return {"pushed": [], "note": "no overlay bundled with this server"}
    parts, pushed = [f'set -e\ncd "{target}"\n'], []
    for f in sorted(OVERLAY_DIR.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(OVERLAY_DIR).as_posix()
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", rel):
            continue
        body = f.read_text()
        # A random marker: any EOF-like line inside these files would otherwise
        # end the here-doc early and silently truncate what lands on the cluster.
        eof = "FEMTO_EOF_" + secrets.token_hex(6).upper()
        parts.append(f'mkdir -p "$(dirname "{rel}")"\n')
        parts.append(f"cat > '{rel}' <<'{eof}'\n{body}\n{eof}\n")
        pushed.append(rel)
    parts.append('echo FEMTO_OVERLAY_OK\n')
    try:
        r = subprocess.run(
            ["ssh", "-S", _ifarm_socket(username), f"{username}@{IFARM_HOST}", "bash", "-s"],
            input="".join(parts), capture_output=True, text=True, timeout=180,
        )
    except Exception as e:
        return {"pushed": [], "error": f"{type(e).__name__}: {e}"}
    if "FEMTO_OVERLAY_OK" not in (r.stdout or ""):
        return {"pushed": [], "error": ((r.stderr or r.stdout or "")[-300:])}
    return {"pushed": pushed}


@mcp.tool(annotations=WRITE_SAFE, meta=CALLABLE_META, structured_output=True)
def setup_workspace(
    folder_name: str = "fitpack_legacy",
    branch: str = "",
    github_token: str = "",
    chat_id: str = "chatgpt-composer",
) -> dict[str, Any]:
    """Create a JAM workspace folder in the user's own cluster area and clone
    the JAM code into it. Called BY the FemtoMind panel when the user chooses to
    set one up — never invent a folder name, repository or token yourself.

    `folder_name` is created under /w/ceph24/JAM/<their account>/. The
    repository is always the group's fitpack_legacy — it is deliberately not
    selectable, so every collaborator runs the same validated code. `branch`
    picks a branch of it. If the repository is private,
    `github_token` is used for this one clone and is not stored. On failure the
    result explains what went wrong and what the user should do, so they can
    correct it and retry."""
    acct = _call_mcpo("/fitpack/get_hpc_account", {"chat_id": _scope(chat_id)})
    username = (acct.get("hpc_username") if isinstance(acct, dict) else "") or _hpc_user_for_scope()
    if not username:
        return {"ok": False, "message": "Not signed in to the cluster yet.",
                "action": "Connect on the Connect tab first."}

    name = (folder_name or "fitpack_legacy").strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
        return {"ok": False, "reason": "name",
                "message": f"'{name}' is not a usable folder name.",
                "action": "Use letters, digits, dots, dashes or underscores only."}

    # Fixed on purpose: the physics must come from the group's own checkout, so
    # there is no way for a caller to point this at some other repository.
    repo = FITPACK_REPO

    ref = (branch or "").strip()
    if ref and not re.fullmatch(r"[A-Za-z0-9._/-]{1,80}", ref):
        return {"ok": False, "reason": "branch",
                "message": f"'{branch}' is not a usable branch name.",
                "action": "Use a plain branch name such as master or ai."}

    parent = f"/w/ceph24/JAM/{username}"
    target = f"{parent}/{name}"
    url = f"https://{github_token}@{repo}" if github_token else f"https://{repo}"
    branch_opt = f"--branch {ref} " if ref else ""

    # A half-finished clone is worse than none: it looks present to every later
    # check. Remove the directory unless git reports success.
    script = (
        f'set -e\n'
        f'if [ -d "{target}" ] && [ -n "$(ls -A "{target}" 2>/dev/null)" ]; then\n'
        f'  echo "FEMTO_ERR:already exists and is not an empty directory"; exit 3\n'
        f'fi\n'
        f'mkdir -p "{parent}" || {{ echo "FEMTO_ERR:cannot create {parent}"; exit 4; }}\n'
        f'if git clone --depth 1 "{url}" "{target}" 2>&1; then\n'
        f'  mkdir -p "{target}/runs" "{target}/grids"\n'
        # grids/ must stay a real directory holding one link per observable:
        # some grids (idis2.0, lqcd_itd) are produced locally and have to live
        # alongside the shared ones, which a single directory-wide symlink
        # would make impossible.
        f'  for obs in dy dypion jet pjet pp_hadron wasym wzrv zrap wc; do\n'
        f'    [ -d "{SHARED_GRIDS}/$obs" ] && ln -sfn "{SHARED_GRIDS}/$obs" "{target}/grids/$obs"\n'
        f'  done\n'
        f'  git -C "{target}" remote set-url origin https://{FITPACK_REPO}\n'
        f'  echo "FEMTO_OK:{target}"\n'
        f'else\n'
        f'  rm -rf "{target}"\n'
        f'  echo "FEMTO_ERR:clone failed"\n'
        f'  exit 5\n'
        f'fi\n'
    )
    try:
        r = subprocess.run(
            ["ssh", "-S", _ifarm_socket(username), f"{username}@{IFARM_HOST}", "bash", "-s"],
            input=script, capture_output=True, text=True, timeout=900,
        )
        out = (r.stdout or "") + "\n" + (r.stderr or "")
    except subprocess.TimeoutExpired:
        out, r = "timed out", None

    if r is not None and "FEMTO_OK:" in out:
        overlay = _push_overlay(username, target)
        # Point this user's account at the folder they just created, otherwise
        # submissions and plots keep looking at the conventional path.
        _call_mcpo("/fitpack/setup_hpc_account",
                   {"hpc_username": username, "chat_id": _scope(chat_id)})
        try:
            import sqlite3
            with sqlite3.connect(os.environ.get(
                    "FITPACK_DB_PATH",
                    str(Path.home() / "femtomind" / "fitpack-workflow" / "hpc_tasks.db")), timeout=5) as _c:
                # Key on the identity, not the cluster account. Two people can
                # share one cluster login, and keying on the username made one
                # person's choice of folder silently repoint everyone else's.
                _c.execute("UPDATE user_hpc_config SET remote_base=?,"
                           " configured_at=CURRENT_TIMESTAMP WHERE user_id=?",
                           (target, _scope(chat_id)))
        except Exception:
            pass
        return {
            "ok": True,
            "workspace": target,
            "cluster_account": username,
            "message": f"Cloned {repo}{' @ ' + ref if ref else ''} into {target} "
                       f"under your own account ({username}).",
            "overlay": overlay,
            "report": _workspace_report(username, target),
        }

    # Strip the token out of anything echoed back before it reaches the UI.
    detail = out.replace(github_token, "***") if github_token else out
    detail = "\n".join(l for l in detail.splitlines() if l.strip() and "FEMTO_" not in l)[-600:]
    return {"ok": False, "workspace": target, "cluster_account": username,
            **_classify_clone_failure(out), "detail": detail}


@mcp.tool(annotations=WRITE_SAFE, meta=CALLABLE_META, structured_output=True)
def repair_grids(chat_id: str = "chatgpt-composer") -> dict[str, Any]:
    """Relink interpolation grids that point at a path which no longer resolves.

    Called BY the FemtoMind panel's Project tab when it reports broken grid
    links. Only replaces links that are already dead — real directories and
    working links are left alone, so locally produced grids are never touched."""
    acct = _call_mcpo("/fitpack/get_hpc_account", {"chat_id": _scope(chat_id)})
    username = (acct.get("hpc_username") if isinstance(acct, dict) else "") or _hpc_user_for_scope()
    if not username:
        return {"ok": False, "message": "Not signed in to the cluster yet."}
    base = _remote_base_for(username, chat_id)

    script = (
        f'cd "{base}/grids" 2>/dev/null || {{ echo "NOGRIDS"; exit 0; }}\n'
        f'fixed=0; skipped=0\n'
        f'for g in *; do\n'
        f'  if [ -L "$g" ] && [ ! -e "$g" ]; then\n'
        f'    if [ -d "{SHARED_GRIDS}/$g" ]; then ln -sfn "{SHARED_GRIDS}/$g" "$g"; fixed=$((fixed+1));\n'
        f'    else skipped=$((skipped+1)); fi\n'
        f'  fi\n'
        f'done\n'
        f'echo "FIXED:$fixed"; echo "SKIPPED:$skipped"\n'
    )
    try:
        r = subprocess.run(
            ["ssh", "-S", _ifarm_socket(username), f"{username}@{IFARM_HOST}", "bash", "-s"],
            input=script, capture_output=True, text=True, timeout=90,
        )
        out = r.stdout or ""
    except Exception as e:
        return {"ok": False, "message": f"Could not reach the cluster ({type(e).__name__})."}

    if "NOGRIDS" in out:
        return {"ok": False, "message": f"No grids directory under {base}."}
    fixed = int((out.split("FIXED:")[1].split()[0]) if "FIXED:" in out else 0)
    skipped = int((out.split("SKIPPED:")[1].split()[0]) if "SKIPPED:" in out else 0)
    msg = f"Relinked {fixed} grid(s) to {SHARED_GRIDS}."
    if skipped:
        msg += f" {skipped} could not be matched to a shared grid and were left as they are."
    return {"ok": True, "fixed": fixed, "skipped": skipped, "message": msg,
            "report": _workspace_report(username, base)}


@mcp.tool(annotations=WRITE_SAFE, meta=CALLABLE_META, structured_output=True)
def use_workspace(path: str, chat_id: str = "chatgpt-composer") -> dict[str, Any]:
    """Make one of the user's existing checkouts the active workspace. Called BY
    the FemtoMind panel when the user picks from the list — fits, plots and
    history all follow whichever one is active. Only paths inside that user's
    own cluster area are accepted."""
    acct = _call_mcpo("/fitpack/get_hpc_account", {"chat_id": _scope(chat_id)})
    username = (acct.get("hpc_username") if isinstance(acct, dict) else "") or _hpc_user_for_scope()
    if not username:
        return {"ok": False, "message": "Not signed in to the cluster yet."}

    p = (path or "").rstrip("/")
    # Confine the choice to their own area: this value becomes the root of
    # every later submit and read, so it must not be able to point elsewhere.
    allowed = f"/w/ceph24/JAM/{username}/"
    if not p.startswith(allowed) or ".." in p:
        return {"ok": False, "message": f"'{path}' is not inside your own area ({allowed})."}

    try:
        r = subprocess.run(
            ["ssh", "-S", _ifarm_socket(username), f"{username}@{IFARM_HOST}", "bash", "-s"],
            input=f'[ -d "{p}/fitlib" ] && echo OK || echo NO\n',
            capture_output=True, text=True, timeout=45,
        )
        if "OK" not in (r.stdout or ""):
            return {"ok": False, "message": f"{p} does not look like a JAM checkout (no fitlib/)."}
    except Exception as e:
        return {"ok": False, "message": f"Could not reach the cluster ({type(e).__name__})."}

    _call_mcpo("/fitpack/setup_hpc_account", {"hpc_username": username, "chat_id": _scope(chat_id)})
    try:
        import sqlite3
        with sqlite3.connect(os.environ.get(
                "FITPACK_DB_PATH",
                str(Path.home() / "femtomind" / "fitpack-workflow" / "hpc_tasks.db")), timeout=5) as c:
            # Scoped to this identity — see the note in setup_workspace.
            c.execute("UPDATE user_hpc_config SET remote_base=?,"
                      " configured_at=CURRENT_TIMESTAMP WHERE user_id=?",
                      (p, _scope(chat_id)))
    except Exception as e:
        return {"ok": False, "message": f"Could not record the choice ({type(e).__name__})."}

    return {"ok": True, "workspace": p,
            "message": f"Now using {p}.",
            "report": _workspace_report(username, p)}


def _chosen_workspace(chat_id: str = "") -> str:
    """The workspace this identity actually picked, or "" if it never has.

    Distinct from get_remote_base, which falls back to a templated
    <area>/fitpack_legacy when nothing is stored. That default is fine as a
    suggestion and wrong as an answer: on a cluster account that already has
    a checkout by that name, a collaborator who has chosen nothing is told
    they are set up and submits into a tree they never picked.
    """
    try:
        import sqlite3
        with sqlite3.connect(os.environ.get(
                "FITPACK_DB_PATH",
                str(Path.home() / "femtomind" / "fitpack-workflow" / "hpc_tasks.db")),
                timeout=5) as c:
            row = c.execute(
                "SELECT remote_base FROM user_hpc_config WHERE user_id=?"
                " AND remote_base IS NOT NULL AND remote_base != ''",
                (_scope(chat_id),)).fetchone()
        return str(row[0]) if row else ""
    except Exception:
        return ""


@mcp.tool(annotations=READ_ONLY, meta=CALLABLE_META, structured_output=True)
def check_project(chat_id: str = "chatgpt-composer") -> dict[str, Any]:
    """Check that the JAM project is in place in the user's HPC workspace —
    fitpack_legacy code, the Python environment, and fitpack grids — before a
    fit runs. Backs the FemtoMind panel's Project tab. Requires an open HPC
    connection; otherwise reports what it could not reach."""
    acct = _call_mcpo("/fitpack/get_hpc_account", {"chat_id": _scope(chat_id)})
    username = (acct.get("hpc_username") if isinstance(acct, dict) else "") or _hpc_user_for_scope()
    chosen = _chosen_workspace(chat_id)
    report = _workspace_report(username, chosen)
    report["configured"] = bool(chosen)
    if not chosen:
        # Report on nothing until they have said which tree is theirs. The
        # candidates are still listed, so choosing is one click rather than a
        # question, but nothing is adopted on their behalf.
        cands = report.get("candidates") or []
        return {
            "ok": False,
            "ready": False,
            "configured": False,
            "cluster_account": username,
            "candidates": cands,
            "items": [],
            "note": ("No workspace chosen yet."
                     + (f" {len(cands)} existing checkout(s) found under your"
                        " cluster area — pick one, or create a new one."
                        if cands else
                        " Create one in the Project tab; nothing is set up yet.")),
            "action": "Choose a workspace with use_workspace, or create one with setup_workspace.",
        }
    # Whether the files are present and whether the fit parameters are usable
    # are different questions; surfacing both stops one from masking the other.
    params = _call_mcpo("/fitpack/check_fitting_readiness", {"chat_id": _scope(chat_id)})
    if isinstance(params, dict):
        report["parameters"] = {"ready": params.get("ready"), "message": params.get("message")}
    report["ready"] = bool(report.get("ok")) and bool(
        params.get("ready") if isinstance(params, dict) else False
    )
    return report


# States that mean the job is still on the cluster. Anything not in here is
# terminal — including states we have not seen yet, so a new SLURM state can
# never strand a campaign at "in progress".
_ACTIVE_STATES = {
    "submitted", "pending", "queued", "running", "completing",
    "configuring", "suspended", "requeued", "resizing",
}


def _group_campaigns(rows: list[Any]) -> list[dict[str, Any]]:
    """Fold per-replica rows into one entry per campaign.

    The backend returns a row per replica, so a thousand-replica campaign came
    back as a thousand "campaigns" — the caller cannot pick a batch_id out of
    that, and four attempts at it cost more context than the whole conversation.
    One row per batch, carrying what a decision actually needs: name, progress,
    and when it ran.
    """
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        bid = str(r.get("batch_id") or "")
        if not bid:
            continue
        g = groups.setdefault(bid, {
            "batch_id": bid,
            "campaign": r.get("batch_label") or "",
            "replicas": 0, "completed": 0, "running": 0, "failed": 0,
            "submitted_at": None, "finished_at": None,
            # First failed replica's path: the Collections tab hands it to
            # explain_failure so "failed" never leaves the user digging through
            # SLURM logs by hand.
            "failed_path": None,
        })
        if not g["campaign"]:
            g["campaign"] = r.get("batch_label") or ""
        g["replicas"] += 1
        st = str(r.get("status") or "").lower()
        # Whitelist the states that mean "still on the cluster" and treat
        # everything else as terminal. Substring matching the other way round
        # left a campaign in progress forever: 'timeout' matched none of the
        # failure words, fell through to the running bucket, and a batch that
        # ended two days ago still read "in progress". It also mistook SLURM's
        # 'completing' — a job that is still finishing — for a finished one,
        # because 'completing' contains 'complet'. Order matters here.
        if st in _ACTIVE_STATES:
            g["running"] += 1
        elif "complet" in st or "done" in st or "finish" in st:
            g["completed"] += 1
        else:
            g["failed"] += 1
            if g["failed_path"] is None and r.get("path"):
                g["failed_path"] = r["path"]
        # created_at/submitted_at/completed_at are UTC; updated_at is not, so it
        # is left out of the comparison rather than mixing clocks.
        first = r.get("submitted_at") or r.get("created_at")
        if first and (g["submitted_at"] is None or str(first) < str(g["submitted_at"])):
            g["submitted_at"] = first
        last = r.get("completed_at")
        if last and (g["finished_at"] is None or str(last) > str(g["finished_at"])):
            g["finished_at"] = last
    out = list(groups.values())
    for g in out:
        g["status"] = ("failed" if g["failed"] and not g["completed"]
                       else "completed" if g["completed"] + g["failed"] >= g["replicas"]
                       else "running")
        g["progress"] = f"{g['completed']}/{g['replicas']}"
        # completed_at is recorded unevenly, so MAX() over a running batch can
        # return a real timestamp from the handful that finished. Only a batch
        # that is actually done gets a finish time, and one that finished
        # without any timestamp says so rather than borrowing a wrong one.
        if g["status"] == "running":
            g["finished_at"] = None
        elif not g["finished_at"]:
            g["finished_at"] = "done"
    out.sort(key=lambda g: str(g.get("submitted_at") or ""), reverse=True)
    return out


@mcp.tool(annotations=READ_ONLY, meta=CALLABLE_META, structured_output=True)
def list_campaigns(chat_id: str = "chatgpt-composer") -> dict[str, Any]:
    """List the fitting campaigns (collections) in this workspace with their
    status — the JAM 'Collections' view. Each entry carries the campaign name,
    id/batch_id, status, and replica counts. Use a returned id with
    get_fit_summary or get_pdf_plots."""
    raw = _call_mcpo("/fitpack/list_chat_tasks", {"chat_id": _scope(chat_id), "sync": True})
    if isinstance(raw, list):
        grouped = _group_campaigns(raw)
        return {"campaigns": grouped, "count": len(grouped), "replicas_total": len(raw)}
    if isinstance(raw, dict) and raw.get("error"):
        return {"campaigns": [], "count": 0, "error": raw["error"]}
    return {"campaigns": [], "count": 0, "raw": raw}


# Anchors a phenomenologist actually quotes: valence peak, sea/gluon region,
# and the large-x tail where the extrapolation starts to dominate.
_BAND_X = (0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 0.5, 0.7)


def _pctl(sorted_vals: list[float], p: float) -> float:
    i = (len(sorted_vals) - 1) * p
    lo, hi = int(i), min(int(i) + 1, len(sorted_vals) - 1)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


def _band_summary(ci: dict[str, Any]) -> dict[str, Any]:
    """Per-flavour 16/50/84 percentiles at a handful of x.

    The widget draws the band from the raw curves, but the model only ever
    saw those curves — hundreds of floats it cannot honestly do statistics
    on. Asked for the uncertainty on the gluon it had to either eyeball the
    array or decline. A dozen numbers per flavour is what a person quotes,
    so send that too.
    """
    xs = ci.get("x_grid") or []
    cbf = ci.get("curves_by_flavor") or {}
    if not xs or not cbf:
        return {}
    idx = []
    for target in _BAND_X:
        if target < xs[0] or target > xs[-1]:
            continue
        idx.append(min(range(len(xs)), key=lambda k: abs(xs[k] - target)))
    idx = sorted(set(idx))
    out: dict[str, Any] = {}
    for flav, curves in cbf.items():
        rows = []
        for k in idx:
            col = sorted(c[k] for c in curves
                         if k < len(c) and isinstance(c[k], (int, float)))
            if not col:
                continue
            rows.append({
                "x":   float(f"{xs[k]:.4g}"),
                "p16": float(f"{_pctl(col, 0.16):.4g}"),
                "p50": float(f"{_pctl(col, 0.50):.4g}"),
                "p84": float(f"{_pctl(col, 0.84):.4g}"),
            })
        if rows:
            out[flav] = rows
    if not out:
        return {}
    n = len(next(iter(cbf.values()), []))
    return {"level": "68% (16th-84th percentile across replicas)",
            "n_replicas": n, "by_flavor": out}


def _compact_plot(raw: dict[str, Any]) -> dict[str, Any]:
    """Shrink the curve payload so it survives the widget bridge.

    Full float64 repr costs ~18 chars per number; 10 replicas x 4 flavors x 120
    points came to ~250 KB over the wire, which the host drops (the widget then
    renders "No curves returned"). Five significant digits is far beyond what an
    SVG can show, and dropping the verbose trace costs nothing visually.
    """
    def r(v: Any) -> Any:
        if isinstance(v, (int, float)):
            try:
                return float(f"{v:.5g}")
            except (ValueError, OverflowError):
                return v
        return v

    ci = raw.get("curves_inline") or {}
    if ci.get("x_grid"):
        ci["x_grid"] = [r(x) for x in ci["x_grid"]]
    cbf = ci.get("curves_by_flavor") or {}
    for flav, curves in cbf.items():
        cbf[flav] = [[r(v) for v in curve] for curve in curves]
    spec = raw.get("spec") or {}
    # Lift the replica counts out before the trace is dropped: the panel and
    # the model both need to know whether any of these curves belong to fits
    # that are still moving, and the trace is the only place carrying it.
    for ev in (spec.get("trace") or []):
        if "n_running" in ev:
            raw["n_running"] = ev.get("n_running")
            raw["n_completed"] = ev.get("n_completed")
            break
    band = _band_summary(ci)
    if band:
        raw["band"] = band
    spec.pop("trace", None)       # UI progress log — not needed once rendered
    spec.pop("reproducer", None)  # multi-KB code snippet the panel never shows
    return raw


@mcp.tool(
    annotations=READ_ONLY,
    meta=_ui_meta(REPLICA_PLOT_URI, "Rendering replica PDFs…", "Replica PDFs rendered"),
    structured_output=True,
)
def get_pdf_plots(
    chat_id: str = "chatgpt-composer",
    flavors: Optional[list[str]] = None,
    q2: float = 10.0,
    batch_ids: Optional[list[str]] = None,
    dist: str = "pdf",
    max_replicas: int = 0,
) -> dict[str, Any]:
    """Render the fitted PDF replica curves — x·f(x) vs x at a given Q² — for a
    campaign. THIS is the JAM analysis plot (gluon/up/down/strange replica
    bands). Renders in an inline SVG widget.

    Replicas that are still fitting are included: their curves come from the
    current parameter vector in progress.json rather than the randomised
    starting point, so a campaign can be watched as it converges. The result
    carries `n_completed` and `n_running` — when `n_running` is non-zero, say
    how many are still fitting instead of presenting the band as final.

    The result carries `band`: 16/50/84 percentiles per flavour at a handful
    of x. Quote uncertainties FROM THAT — never compute statistics over
    `curves_inline` yourself. A value is stated as p50 with the 68% interval,
    e.g. "xg(x=0.1) = -13.7, 68% interval [-37.6, -3.5]". A band that spans
    both signs, or whose width exceeds its central value, means the data in
    this fit does not constrain that flavour — say so rather than reporting
    the median as if it were a measurement.

    If HPC is not connected or the campaign has produced nothing at all yet,
    it returns representative placeholder curves (clearly marked)."""
    flavs = flavors or ["g", "u", "d", "s"]
    # 80 points is visually identical to the backend's 120 in an SVG this size
    # and keeps the payload well inside what the widget bridge will carry.
    # A campaign of a thousand replicas returns ~1.6 MB of curves for a single
    # flavour, and the host drops anything past a few hundred kB, so the widget
    # renders nothing. Cap by default: a couple of hundred replicas is already
    # far more than a band needs, and the backend picks the best by chi-square.
    limit = max_replicas if max_replicas > 0 else PLOT_REPLICA_LIMIT
    payload: dict = {"chat_id": _scope(chat_id), "dist": dist, "flavors": flavs, "q2": q2,
                     "x_grid_n": 80, "limit": limit, "order_by": "chi2_asc"}
    if batch_ids:
        payload["batch_ids"] = batch_ids
    raw = _call_mcpo("/fitpack/plot_replica_observable", payload)
    if isinstance(raw, dict) and raw.get("curves_inline"):
        return _compact_plot(raw)
    # No real curves (HPC down, or no completed replicas) → placeholder.
    mock = _mock_curves(flavs, q2)
    if isinstance(raw, dict) and raw.get("error"):
        mock["backend_note"] = str(raw["error"])[:300]
    return mock



@mcp.tool(
    annotations=READ_ONLY,
    meta=_ui_meta(OPERATIONS_URI, "Opening FemtoMind…", "FemtoMind ready"),
    structured_output=True,
)
def jam_analysis_panel(
    tab: str = "connect", observable: str = "idis", chat_id: str = "chatgpt-composer"
) -> dict[str, Any]:
    """Work with JAM QCD global analysis on the Jefferson Lab HPC cluster.

    THIS TOOL — never a shell, SSH client, terminal or web browser — is how any
    of the following is done. The cluster is reached through FemtoMind's own
    managed connection; there is no local SSH host to resolve and nothing to
    look up on the web:
      · sign in to / check the JLab HPC connection, SLURM, or the cluster account
      · inspect or set up the JAM workspace (fitpack_legacy) on the cluster
      · configure, submit or re-run a PDF fitting campaign
      · review submitted campaigns, jobs, or chi-square results
      · plot fitted PDF replicas

    It renders an interactive panel whose tabs are Connect → Project → Campaign
    → Collections → Analysis; the researcher fills in every physics value there
    themselves. Do NOT gather datasets, fit parameters or credentials in chat.
    Pass `tab` to land on the right one: connect | project | campaign |
    collections | analysis — e.g. "check my HPC connection" → tab='connect',
    "set up a fit" → tab='campaign', "plot the PDFs" → tab='analysis'."""
    connection = _hpc_status(chat_id)
    raw = _call_mcpo("/fitpack/search_datasets", {"observable": "", "experiment": "", "target": ""})
    full = raw.get("by_observable", {}) if isinstance(raw, dict) else {}
    catalog = _mark_unusable({k: v for k, v in full.items() if k in _COMPOSER_FAMILIES})
    valid_tabs = ("connect", "project", "campaign", "collections", "analysis")
    return {
        "tab": tab if tab in valid_tabs else "connect",
        "connection": connection,
        "catalog": catalog,
        "suggested_observable": observable if observable in catalog else "idis",
    }


# idis tables whose expdata sheet has no `col` column. fitpack's report writer
# does `self.tabs[k]['col'][0]` unconditionally, so including any of these kills
# the whole job ~14s in with `KeyError: 'col'` — even though the other datasets
# in the same fit are fine. Verified 2026-08-13 by reading every
# database/idis/expdata/*.xlsx on ifarm (46 have the column, these 44 do not).
# To regenerate: load each sheet with pandas and test `'col' in df.columns`.
_IDIS_UNUSABLE = {
    "10005", "10008", "10010CJ", "10011CJ", "10012", "10013", "10014",
    "10014old", "10018", "10018old", "10019", "10019old", "10022", "10022old",
    "10035", "10036", "10042", "10042sig", "10045", "10046", "10047", "10048",
    "10057", "10058", "10059", "10060", "10061", "10062", "10063", "10064",
    "10065", "10066", "10067", "10068", "10069", "10070", "10071", "10072",
    "10073", "10074", "10075", "10076", "10077", "10086",
}


def _mark_unusable(catalog: dict[str, Any]) -> dict[str, Any]:
    """Flag datasets the fitter cannot report on, so the form greys them out.

    They stay visible (with a reason) rather than vanishing — a dataset that
    silently disappears looks like a bug, one that is disabled with a note
    explains itself.
    """
    idis = catalog.get("idis")
    if not idis:
        return catalog
    items = idis.items() if isinstance(idis, dict) else ((d.get("id"), d) for d in idis)
    for ds_id, meta in items:
        if str(ds_id) in _IDIS_UNUSABLE and isinstance(meta, dict):
            meta["enabled"] = False
            meta["disabled_reason"] = "table has no 'col' column — the fitter cannot report on it"
    return catalog


def _to_advanced_payload(adv: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Translate the panel's UI-shaped `advanced` block into the backend schema.

    The composer groups values the way the form reads (qcd / job / parameters /
    object); `AdvancedFitConfig` is flat with a nested `qcd_config` and rejects
    unknown keys outright (extra="forbid"), so the two must be mapped here
    rather than passed through.
    """
    if not adv:
        return {}
    job = adv.get("job") or {}
    qcd = adv.get("qcd") or {}
    out: dict[str, Any] = {}

    if job.get("tolerance") not in (None, ""):
        out["tolerance"] = job["tolerance"]
    if job.get("maxIterations") is not None:
        out["max_iterations"] = int(job["maxIterations"])
    if job.get("optimizer"):
        out["optimizer"] = job["optimizer"]
    if job.get("bootstrap") is not None:
        out["bootstrap"] = bool(job["bootstrap"])

    qc: dict[str, Any] = {}
    if qcd.get("order"):
        qc["order"] = qcd["order"]
    if qcd.get("Q20") is not None:
        qc["Q20"] = float(qcd["Q20"])
    if "tmc" in qcd:
        tmc = qcd["tmc"]
        # The form spells "no target-mass corrections" as "off". Pass the
        # string through: the backend coerces "off"/"none"/"" to False and
        # writes conf.tmc=False. Mapping to None here would make the backend
        # SKIP the key and silently keep the default.yaml value.
        qc["tmc"] = tmc
    if qcd.get("higherTwist") is not None:
        qc["ht"] = bool(qcd["higherTwist"])
    if qcd.get("dglapMode"):
        qc["dglapMode"] = qcd["dglapMode"]
    if qcd.get("asMode"):
        qc["alphaSmode"] = qcd["asMode"]
    if qcd.get("nuc") is not None:
        qc["nuc"] = bool(qcd["nuc"])
    if qc:
        out["qcd_config"] = qc

    # PDF parameter overrides (nucleon) — the backend schema accepts
    # pdf_params keyed by param name (existing entries only, e.g. "g1 a").
    params = adv.get("parameters") or {}
    if isinstance(params, dict) and params:
        pdf_params = {}
        for name, ov in params.items():
            if not isinstance(ov, dict):
                continue
            entry = {k: ov[k] for k in ("value", "min", "max", "fixed") if k in ov and ov[k] is not None}
            if entry:
                pdf_params[str(name)] = entry
        if pdf_params:
            out["pdf_params"] = pdf_params

    # Meson parameter overrides (pion / kaon) — the backend creates or updates
    # conf.params["pdf-pion"] / ["pdf-kaon"] entries from these blocks.
    for adv_key, out_key in (("pion_parameters", "pdf_pion_params"),
                             ("kaon_parameters", "pdf_kaon_params")):
        block = adv.get(adv_key) or {}
        if not isinstance(block, dict) or not block:
            continue
        clean = {}
        for name, ov in block.items():
            if not isinstance(ov, dict):
                continue
            entry = {k: ov[k] for k in ("value", "min", "max", "fixed") if k in ov and ov[k] is not None}
            if entry:
                clean[str(name)] = entry
        if clean:
            out[out_key] = clean

    # Quality thresholds — accepted by the backend schema (chi2_ok / chi2_warn /
    # good_fraction), recorded for the ladder acceptance logic.
    qt = adv.get("quality_thresholds") or {}
    if isinstance(qt, dict):
        for key in ("chi2_ok", "chi2_warn", "good_fraction"):
            if qt.get(key) is not None:
                try:
                    out[key] = float(qt[key])
                except (TypeError, ValueError):
                    pass

    # `parameters.initialization` and `object.type` have no backend counterpart:
    # replicas are randomised during task generation, and the object is implied
    # by the observable. Dropping them keeps the payload schema-valid.
    return out


@mcp.tool(annotations=READ_ONLY, meta=CALLABLE_META, structured_output=True)
def get_resource_context(
    observable: str,
    n_datasets: int = 1,
    replica_count: int = 1,
    chat_id: str = "chatgpt-composer",
) -> dict[str, Any]:
    """What a replica of this observable has cost before, and what the cluster
    looks like right now. Read-only: it submits nothing.

    Returns the recorded runtimes and peak memory of past runs of the same
    observable, the live idle-CPU and partition picture, the caller's own
    running jobs, and the estimate those imply, with the valid partitions and
    the accepted range for each field.

    Call this before submit_fitting_job whenever you are about to launch a
    campaign, then pass cpus, time_min, mem_mb and partition to that tool with
    values you can justify from what came back. A short campaign on an idle
    cluster deserves more cores than the same campaign on a busy one; an
    observable whose past runs took four hours should not be given a one-hour
    wall clock. Do not invent numbers with this unread. Anything out of range
    is clamped at submission, so a proposal can never cause a failure --- but a
    proposal made without reading this is a guess with a number attached."""
    return _call_mcpo("/fitpack/get_resource_context", {
        "observable": observable, "n_datasets": n_datasets,
        "replica_count": replica_count, "chat_id": _scope(chat_id),
    })




@mcp.tool(
    annotations=READ_ONLY,
    structured_output=True,
)
def get_fit_parameters(chat_id: str = "chatgpt-composer") -> dict[str, Any]:
    """Return the current PDF/FF parameter state that a submitted fit would
    start from: parameter name, value, [min, max] bounds and the fixed flag,
    grouped by distribution (nucleon PDF, pion PDF, kaon PDF, global
    settings). The composer renders these in its "PDF Parameters" step.

    Read-only: it never mutates the workspace or the cluster. Call it when
    the user wants to inspect or edit the parameter table before submitting.
    """
    raw = _call_mcpo("/fitpack/get_current_parameters", {"chat_id": _scope(chat_id)})
    return raw if isinstance(raw, dict) else {"result": raw}


@mcp.tool(annotations=WRITE_SAFE, meta=CALLABLE_META, structured_output=True)
def submit_fitting_job(
    observable: str,
    selected_ids: list[str],
    count: int,
    job_number: str = "001",
    batch_label: str = "chatgpt-composer",
    advanced: Optional[dict[str, Any]] = None,
    extra_observables: Optional[list[dict[str, Any]]] = None,
    chat_id: str = "chatgpt-composer",
    continue_chain: bool = False,
    cpus: int = 0,
    time_min: int = 0,
    mem_mb: int = 0,
    partition: str = "",
) -> dict[str, Any]:
    """Submit a fitting campaign to the HPC cluster. Intended to be called BY
    THE COMPOSER FORM with the user's exact selections — never invent
    selected_ids, count, or advanced settings yourself; open the composer
    instead. `advanced` carries the QCD settings and job config the user chose
    in the form (order/Q20/TMC/DGLAP/alphaS, max iterations, tolerance,
    optimizer, campaign name, …).

    By default each submission is a self-contained fit: the form shows a full
    dataset list, so what the user sees is what runs. Set continue_chain=True
    only for a deliberate staged analysis, where this step should INHERIT the
    previous batch's datasets and add to them.

    When the selection spans several reaction families (e.g. idis + dy), the
    composer groups it and passes each additional family through
    `extra_observables` — the backend fits them jointly in ONE SLURM
    submission instead of mislabeling them under the primary observable.

    Resource proposal. Call get_resource_context first, then pass `cpus`,
    `time_min`, `mem_mb` and `partition` here with values you can justify from
    what it returned --- the observable's recorded runtimes, the cluster's idle
    capacity right now, and the caller's own running jobs. Leave a field at 0
    (or partition empty) to let the estimate stand for it. Every value is
    clamped at the backend and an unknown partition is dropped, so a proposal
    can degrade the estimate but never fail a submission. What was proposed and
    what the job went on to use are both recorded, so the proposal can be
    scored later against the outcome.

    These are operational values only. They are yours to choose. The
    fit-defining values --- datasets, cuts, perturbative order, $Q_0^2$,
    replica count --- are the researcher's and arrive from the panel; never
    substitute your own.
    """
    if not selected_ids:
        return {"error": "selected_ids is empty — open the composer and let the user pick datasets"}
    # job_number becomes a directory-name prefix and reaches remote shells via
    # the backend's launcher scripts: restrict it to a harmless charset so it
    # can never smuggle shell syntax. Same for batch_label, which also becomes
    # the SLURM job name.
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,16}", job_number or "001"):
        return {"error": "job_number may only contain letters, digits, dots, dashes or underscores (max 16)"}
    label = str(batch_label or "").strip()
    if not label or len(label) > 64 or "\n" in label or "\r" in label:
        return {"error": "batch_label must be 1–64 characters on one line"}
    for family in ({"observable": observable, "selected_ids": selected_ids},
                   *([e for e in (extra_observables or []) if isinstance(e, dict)])):
        fam_obs = str(family.get("observable") or "")
        fam_ids = [str(i) for i in (family.get("selected_ids") or [])]
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", fam_obs):
            return {"error": f"Invalid observable {fam_obs!r}."}
        if not fam_ids:
            return {"error": f"empty selected_ids for observable {fam_obs!r}"}
        if fam_obs == "idis":
            blocked = [i for i in fam_ids if i in _IDIS_UNUSABLE]
            if blocked:
                # One of these anywhere in the selection fails the entire batch on
                # the cluster, so refuse here rather than burning the SLURM jobs.
                return {
                    "error": (
                        "These datasets cannot be fitted — their tables have no 'col' "
                        f"column and the job would fail on the cluster: {', '.join(blocked)}. "
                        "Deselect them and submit again."
                    ),
                    "unusable_datasets": blocked,
                }
    if not 1 <= count <= 200:
        return {"error": "count (replicas) must be between 1 and 200"}
    payload = {
        "observable": observable,
        "selected_ids": [str(i) for i in selected_ids],
        "count": count,
        "job_number": job_number,
        "batch_label": label,
        "chat_id": _scope(chat_id),
        # Without this the backend treats every submit as the NEXT STEP of a
        # chain and INHERITS the previous batch's datasets, so a form showing 3
        # datasets can silently run 8 — including ones the user deselected.
        "fresh_start": not continue_chain,
    }
    if extra_observables:
        payload["extra_observables"] = extra_observables
    mapped = _to_advanced_payload(advanced)
    if mapped:
        payload["advanced"] = mapped
    # Only send fields that were actually proposed. An empty proposal leaves
    # the estimate alone rather than pinning it to zeros.
    proposal = {k: v for k, v in (("cpus", cpus), ("time_min", time_min),
                                  ("mem_mb", mem_mb)) if v}
    if partition.strip():
        proposal["partition"] = partition.strip()
    if proposal:
        payload["resources"] = proposal
    raw = _call_mcpo("/fitpack/run_fitting_job", payload)
    return raw if isinstance(raw, dict) else {"result": raw}


@mcp.tool(
    annotations=WRITE_SAFE,
    meta=_ui_meta(OPERATIONS_URI, "Adding replicas…", "Replicas queued"),
    structured_output=True,
)
def add_replicas(
    count: int,
    batch_id: str = "",
    batch_label: str = "",
    chat_id: str = "chatgpt-composer",
) -> dict[str, Any]:
    """Launch MORE replicas on an EXISTING campaign, reusing its exact dataset
    selection and settings. Use this when the user says something like "run 1000
    more replicas on the same campaign" — it appends to that batch instead of
    starting a new fit, so the ensemble grows rather than being replaced.

    `batch_id` accepts either the opaque id (batch_...) or the campaign's
    name as the user says it ("testnew"). Leave it empty to extend the most
    recently submitted campaign. This is the one case where a fit may be
    launched without opening the composer, because the physics configuration
    is inherited unchanged — only the ensemble grows.

    `batch_label` renames the campaign. Replicas appended this way join the
    SAME batch, so the name belongs to the whole campaign rather than to the
    new arrivals: the existing replicas are relabelled with it too, so the
    campaign keeps one name.

    ONLY pass `batch_label` when the user supplied a name in this request —
    "and call it X", "rename it to X". Leave it EMPTY otherwise. Do not
    invent one, do not derive one from the count or the datasets, and do not
    re-send the campaign's current name. The rename overwrites the old name
    with no way back, so a helpful guess is a silent loss.

    This returns the operations panel on the Collections tab, so the growing
    campaign is visible without scrolling back. Do NOT also call
    jam_analysis_panel for the same request — that would open a second panel.

    If the cluster session is closed it submits NOTHING and returns the panel
    on the Connect tab with the request held as `pending_action`. The panel
    runs it the moment sign-in succeeds. Say that the run is queued behind
    sign-in; do not retry the tool and do not ask the user to repeat the
    request afterwards.
    """
    if not 1 <= count <= 2000:
        return {"error": "count must be between 1 and 2000 replicas"}

    # Launching into a closed ControlMaster fails deep in the submission, after
    # the campaign has already been touched, and the error that comes back
    # describes a timeout rather than a missing session. Check first, and hand
    # the request to the panel instead of half-running it: the user signs in
    # once and the run goes out by itself, with nothing to re-enter.
    status = _hpc_status(chat_id)
    if not status.get("connected"):
        return {
            "tab": "connect",
            "status": "not_connected",
            "pending_action": {
                "tool": "add_replicas",
                "args": {"count": count, "batch_id": batch_id,
                         "batch_label": batch_label},
                "describe": f"add {count} replicas"
                            + (f" and rename to {batch_label}" if batch_label.strip() else ""),
            },
            "connection": {k: status.get(k) for k in
                           ("connected", "cluster_reachable", "gateway_open", "note")},
            "message": (
                "Not connected to the cluster, so nothing was submitted. "
                "Sign in on the Connect tab — this run starts on its own once "
                "the session is open, and the panel moves to Collections."
            ),
        }

    wanted = (batch_id or "").strip()
    prior = _call_mcpo("/fitpack/list_chat_tasks", {"chat_id": _scope(chat_id), "sync": False})
    rows = prior if isinstance(prior, list) else []
    if not rows:
        return {"error": "No campaign found to extend — submit one from the composer first."}

    def _newest(candidates: list[Any]) -> str:
        """Batch id with the most recent replica.

        Was rows[-1], which assumed the backend returns oldest-last. It
        returns newest-FIRST, so "extend the most recent campaign" reached
        for the oldest one — on this deployment that meant a request to add
        200 replicas would have landed on a finished thousand-replica batch.
        Read the timestamps instead of trusting an order nobody promised.
        """
        best, best_at = "", ""
        for r in candidates:
            at = str(r.get("created_at") or r.get("submitted_at") or "")
            if at >= best_at:
                best_at, best = at, str(r.get("batch_id") or "")
        return best

    if wanted:
        match = [r for r in rows if str(r.get("batch_id")) == wanted]
        if not match:
            # People refer to a campaign by the name they gave it, so the model
            # passes that name here. Accept it: matching only on the opaque
            # batch id turned "add 200 to testnew" into "No campaign 'testnew'".
            named = [r for r in rows
                     if str(r.get("batch_label") or "").strip().lower() == wanted.lower()]
            if named:
                match = [r for r in rows if str(r.get("batch_id")) == _newest(named)]
        if not match:
            ids = sorted({str(r.get("batch_id")) for r in rows})[-5:]
            names = sorted({str(r.get("batch_label")).strip() for r in rows
                            if str(r.get("batch_label") or "").strip()})
            return {"error": f"No campaign {wanted!r}.",
                    "known_names": names, "known_ids": ids}
        target = str(match[0].get("batch_id") or "")
    else:
        target = _newest(rows)
        match = [r for r in rows if str(r.get("batch_id")) == target]
        if not match:
            return {"error": "Could not resolve the most recent campaign."}

    # Replicas are generated into the ACTIVE workspace, not the campaign's:
    # generate_random_tasks resolves its runs directory from the caller's
    # current remote_base. Extending a campaign that lives somewhere else
    # therefore scatters one ensemble across two trees AND fits the new
    # replicas against the other workspace's inputs/default.yaml — a single
    # ensemble carrying two different configurations, which no chi-square or
    # band computed from it would mean anything. Refuse instead.
    def _root(p: str) -> str:
        m = re.match(r"(.*)/runs/[^/]+/?$", str(p or ""))
        return m.group(1) if m else ""
    campaign_roots = {r for r in (_root(x.get("path")) for x in match) if r}
    active_root = ""
    cfg = _call_mcpo("/fitpack/get_hpc_account", {"chat_id": _scope(chat_id)})
    if isinstance(cfg, dict):
        active_root = str(cfg.get("remote_base") or "").rstrip("/")
    if campaign_roots and active_root and active_root not in campaign_roots:
        return {
            "error": "This campaign lives in a different workspace.",
            "campaign": str(match[0].get("batch_label") or target),
            "campaign_workspace": sorted(campaign_roots),
            "active_workspace": active_root,
            "action": (
                "New replicas are written into the ACTIVE workspace and fitted "
                "against ITS inputs/default.yaml, so extending from here would "
                "split the ensemble across two trees and two configurations. "
                "Switch with use_workspace first, then add the replicas."
            ),
        }

    # The backend reuses the batch's stored datasets/settings, so only the batch
    # pointer and the extra replica count need to travel.
    label = (batch_label or "").strip() or (match[0].get("batch_label") or "chatgpt-fit")
    raw = _call_mcpo("/fitpack/run_fitting_job", {
        "observable": match[0].get("observable") or "idis",
        "selected_ids": [],
        "count": count,
        "chat_id": _scope(chat_id),
        "append_to_batch_id": target,
        "batch_label": label,
    })
    if isinstance(raw, dict) and raw.get("status") == "error":
        return {"error": raw.get("message") or "backend rejected the append", "batch_id": target}

    # Relabel the replicas that were already there. Appending puts the new ones
    # in the same batch, so leaving the old rows under the old name would split
    # one campaign across two names in every listing that groups by label.
    renamed, rename_error = 0, ""
    if batch_label.strip():
        try:
            import sqlite3
            with sqlite3.connect(os.environ.get(
                    "FITPACK_DB_PATH",
                    str(Path.home() / "femtomind" / "fitpack-workflow" / "hpc_tasks.db")),
                    timeout=10) as _c:
                renamed = _c.execute(
                    "UPDATE tasks SET batch_label=? WHERE batch_id=? AND chat_id=?",
                    (label, target, _scope(chat_id))).rowcount
        except Exception as e:
            # Surfaced, not swallowed: the replicas did launch, so the caller
            # needs to know the rename is the only part that did not take.
            rename_error = f"{type(e).__name__}: {e}"

    return {
        # Land on Collections: the point of adding replicas is watching the
        # count move, and a text-only reply made the user scroll back for it.
        "tab": "collections",
        "appended_to": target,
        "campaign": label,
        "requested": count,
        "replicas_before": len(match),
        "renamed_rows": renamed,
        **({"rename_error": rename_error} if rename_error else {}),
        "result": raw if isinstance(raw, dict) else {"raw": str(raw)[:400]},
    }


# Remote aggregator: parses every replica's progress.json on the cluster and
# returns one small summary. Reading a thousand 40 kB files back over SSH just
# to compute a median would be absurd, so the reduction happens there.
_PROGRESS_AGG = """
import json, os, sys, statistics
names = json.load(sys.stdin)
base = names.pop("__base__")
rows = []
for n in names["runs"]:
    f = os.path.join(base, "runs", n, "progress.json")
    try:
        d = json.load(open(f))
    except Exception:
        continue
    if not d:
        continue
    last = d[-1]
    tail = [e.get("chi2") for e in d[-4:] if isinstance(e.get("chi2"), (int, float))]
    flat = len(tail) >= 3 and max(tail) - min(tail) < 1e-6 * max(1.0, abs(tail[-1]))
    rows.append({"run": n, "iter": last.get("iter"), "chi2": last.get("chi2"),
                 "elapsed_min": last.get("elapsed_min"), "flat": bool(flat)})
if not rows:
    print(json.dumps({"n_with_progress": 0}))
    raise SystemExit
chi = sorted(r["chi2"] for r in rows if isinstance(r.get("chi2"), (int, float)))
worst = sorted(rows, key=lambda r: -(r.get("chi2") or 0))[:5]
print(json.dumps({
    "n_with_progress": len(rows),
    "n_look_converged": sum(1 for r in rows if r["flat"]),
    "chi2_min": chi[0] if chi else None,
    "chi2_median": chi[len(chi)//2] if chi else None,
    "chi2_max": chi[-1] if chi else None,
    "iter_median": statistics.median([r["iter"] for r in rows if isinstance(r.get("iter"), int)] or [0]),
    "elapsed_min_max": max([r.get("elapsed_min") or 0 for r in rows]),
    "worst": worst,
}))
"""


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def get_fit_progress(batch_id: str = "", chat_id: str = "chatgpt-composer") -> dict[str, Any]:
    """How far the replicas of a campaign have got WHILE THEY ARE STILL RUNNING.

    Reads each replica's live iteration log on the cluster and reports where the
    chi-square has reached, how many look converged (chi-square flat over recent
    iterations) and which are worst. Use this for "how is the fit going", "has it
    converged", "should I keep waiting" — get_fit_summary only answers once
    replicas have finished, and cannot say anything about one still iterating.

    Leave `batch_id` empty for the most recent campaign. Note the fitted PDF
    curves cannot be drawn mid-run: a replica's parameters.yaml holds its random
    starting point until the fit ends, so plotting it would show a curve that
    has nothing to do with the fit.
    """
    tasks = _call_mcpo("/fitpack/list_chat_tasks", {"chat_id": _scope(chat_id), "sync": False})
    rows = tasks if isinstance(tasks, list) else []
    if not rows:
        return {"error": "No campaigns yet — submit one from the Campaign tab."}
    target = (batch_id or "").strip() or str(rows[-1].get("batch_id") or "")
    mine = [r for r in rows if str(r.get("batch_id")) == target]
    if not mine:
        known = sorted({str(r.get("batch_id")) for r in rows})
        return {"error": f"No campaign {target!r}. Known: {', '.join(known[-5:])}"}

    # Not every row carries the label, so take the first one that does
    # rather than whichever row happened to sort first.
    label = next((str(r.get("batch_label")) for r in mine if r.get("batch_label")), "")
    names, base = [], ""
    for r in mine:
        path = str(r.get("path") or "")
        if "/runs/" in path:
            head, _, tail = path.rpartition("/runs/")
            base, name = head, tail.strip("/")
            if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name):
                names.append(name)
    if not names or not base:
        return {"error": "No replica directories recorded for this campaign yet."}

    username = _hpc_user_for_scope()
    socket = _ifarm_socket(username)
    if not _master_alive(socket, f"{username}@{IFARM_HOST}"):
        return {"error": "Not connected to HPC — sign in on the Connect tab first."}

    payload = json.dumps({"__base__": base, "runs": names})
    blob = base64.b64encode(_PROGRESS_AGG.encode()).decode()
    # base64 keeps the script clear of ifarm's tcsh login shell; the run list
    # rides in on stdin so a thousand names never touch the command line.
    # mktemp + cleanup: a fixed /tmp path meant two collaborators polling at
    # once overwrote each other's script and left droppings on the login node.
    cmd = (f"T=$(mktemp /tmp/_fm_agg.XXXXXX.py) && "
           f"echo {blob} | base64 -d > \"$T\" && python3 \"$T\"; rc=$?; rm -f \"$T\"; exit $rc")
    try:
        r = subprocess.run(
            ["ssh", "-S", socket, f"{username}@{IFARM_HOST}", "sh", "-c", f"'{cmd}'"],
            input=payload, capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        return {"error": f"Could not read progress: {type(e).__name__}: {e}"}
    line = next((l for l in reversed((r.stdout or "").splitlines()) if l.strip().startswith("{")), "")
    if not line:
        return {"error": "No progress data returned", "stderr": (r.stderr or "")[:300]}
    try:
        agg = json.loads(line)
    except Exception:
        return {"error": "Could not parse progress output", "raw": line[:300]}

    agg.update({"batch_id": target, "campaign": label, "replicas_recorded": len(names)})
    n = agg.get("n_with_progress") or 0
    if n:
        conv = agg.get("n_look_converged") or 0
        agg["note"] = (
            f"{conv} of {n} replicas have a flat chi-square over their last iterations. "
            "Flat means the minimizer has stopped moving, which is what convergence "
            "looks like from outside; whether that minimum is the right one is a "
            "judgement to make from the spread and the curves, not from this number."
        )
    return agg


# Dev-only connectivity probe; registered only when explicitly enabled so the
# production tool surface stays free of test scaffolding.
if os.environ.get("FEMTOMIND_ENABLE_TEST_TOOL", "").lower() in ("1", "true", "yes"):

    @mcp.tool(annotations=WRITE_SAFE, structured_output=True)
    def echo_write_test(text: str = "hello") -> dict[str, Any]:
        """Harmless connectivity test for write-annotated tools. Echoes the input
        back; changes nothing. Used to check whether this ChatGPT plan permits
        write tools."""
        return {"echo": text, "write_tools_work": True}


# ─── entrypoint ─────────────────────────────────────────────────────────────

app = mcp.streamable_http_app()

# ChatGPT's sandboxed widget iframe issues cross-origin requests to the MCP
# server; the OpenAI reference server wraps the app in permissive CORS.
from starlette.middleware.cors import CORSMiddleware  # noqa: E402
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["mcp-session-id", "mcp-protocol-version"],
)

# ─── authentication ─────────────────────────────────────────────────────────
# The endpoint is on the public internet, so every MCP call must say who is
# making it. Without this, one person's fits, history and cluster account are
# reachable by anyone who learns the URL.
import femtomind_auth as auth  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.responses import JSONResponse as _JSON  # noqa: E402
from starlette.routing import Route  # noqa: E402

auth.init_db()

# Set to "0" only for local development against a loopback-bound server.
REQUIRE_AUTH = os.environ.get("FEMTOMIND_REQUIRE_AUTH", "1").lower() not in ("0", "false", "no")


class AuthMiddleware(BaseHTTPMiddleware):
    """Resolve the bearer token to a user before the MCP layer sees the call.

    On success the identity is stashed on request.state so tools can scope
    their work to that person. On failure we answer 401 with the
    `WWW-Authenticate` header RFC 9728 clients follow to find the
    authorization server — that header is what makes ChatGPT start the
    sign-in flow rather than simply failing.
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        # TEMPORARY (2026-08-19): does ChatGPT send anything that identifies the
        # conversation? The Apps SDK exposes no conversation id to the widget, so
        # the only hope is a header or _meta field on the request. Remove once
        # answered either way.
        if path.startswith("/mcp") and os.environ.get("FEMTOMIND_DEBUG_HEADERS"):
            import logging as _lg
            _lg.getLogger("femtomind").warning(
                "HDRS %s", {k: v for k, v in request.headers.items()
                            if k.lower() not in ("authorization", "cookie")})
        if not path.startswith("/mcp") or request.method == "OPTIONS" or not REQUIRE_AUTH:
            return await call_next(request)
        who = auth.identify(request.headers.get("authorization"))
        if not who:
            return _JSON(
                {"error": "unauthorized", "error_description": "A FemtoMind access token is required."},
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer realm="FemtoMind", '
                        f'resource_metadata="{auth.ISSUER}/.well-known/oauth-protected-resource"'
                    )
                },
            )
        request.state.user = who
        _CURRENT_USER.set(who)
        return await call_next(request)


app.add_middleware(AuthMiddleware)

# OAuth endpoints live beside /mcp on the same host so discovery just works.
app.router.routes.extend([
    Route("/.well-known/oauth-protected-resource", auth.protected_resource_metadata),
    Route("/.well-known/oauth-protected-resource/mcp", auth.protected_resource_metadata),
    Route("/.well-known/oauth-authorization-server", auth.authorization_server_metadata),
    Route("/authorize", auth.authorize, methods=["GET", "POST"]),
    Route("/token", auth.token, methods=["POST"]),
])

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
