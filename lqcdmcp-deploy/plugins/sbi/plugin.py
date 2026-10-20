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
SHIP = ("plugin.py", "dashboard.html", "upstream/backend.py", "upstream/server.py",
        "upstream/static/panel.html", "upstream/data/*.json")
UPSTREAM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upstream")

# False: question and answer only, the model answers in text from the tool
# results, with no panel resources or links. True: the tools also link
# upstream's interactive panel, which draws the conversation's result. Only
# turn it on with an upstream commit whose panel handles chat results (one
# that opens empty otherwise), pinned in the submodule.
PANEL = True

# ChatGPT keys its widget on the legacy skybridge MIME, other MCP Apps hosts
# on the standard one, as for the JAM panel: the same file under two URIs.
UI_URI = "ui://nesap/sbi-panel"
UI_URI_SKYBRIDGE = "ui://nesap/sbi-panel.skybridge"

ROUTING = (
    "NESAP SBI (tools sbi_options, sbi_infer, sbi_validation, sbi_panel): inferring the QCD coupling alpha_s "
    "from fixed Pythia simulation benchmarks (benchmark-1 to benchmark-5) with a neural event "
    "classifier: likelihood, best fit, confidence intervals, comparison with the generating truth, "
    "the method's validation. Questions about simulation-based inference (SBI), the classifier, "
    "NESAP or these benchmarks belong here. It runs inside the gateway and never needs HPC "
    "sign-in, a workspace, campaigns or Slurm. (The alphaS setting of a JAM fit is JAM.)")

# Models choose tools by their descriptions, so these replace upstream's short
# ones here (upstream is not modified). A tool without an entry keeps its own
# description behind the same "NESAP SBI only." marker.
DESCRIPTIONS = {
    "sbi_options": (
        "NESAP SBI only. What the SBI demonstration offers: its one parameter (the QCD coupling "
        "alpha_s), the frozen neural classifier and calibration, the five fixed Pythia benchmark "
        "samples (benchmark-1 to benchmark-5, 80,000 events each) and the limitations. Start here "
        "for an SBI question. Runs inside the gateway: no HPC sign-in, workspace or Slurm."),
    "sbi_infer": (
        "NESAP SBI only. Infer alpha_s for one fixed Pythia benchmark (benchmark_id benchmark-1 to "
        "benchmark-5, default benchmark-3) from archived neural-classifier score counts: best fit, "
        "68% and 95% likelihood-ratio confidence sets (not Bayesian credible intervals), the "
        "likelihood curve, numerical checks, provenance, and the generating truth for comparison. "
        "Computed live inside the gateway in well under a second: no HPC sign-in, workspace or Slurm. "
        "It takes no user data and cannot generate events or train."),
    "sbi_validation": (
        "NESAP SBI only. Archived validation of the fixed SBI method: bias, interval widths and "
        "coverage from 2,560 historical pseudo-experiments at five alpha_s truths, and the scope "
        "of that evidence. Historical results, not a new validation. Runs inside the gateway: no "
        "HPC sign-in, workspace or Slurm."),
}

PANEL_DESCRIPTION = (
    "NESAP SBI only. The browser link of the SBI dashboard on this gateway, like the JAM panel's link: "
    "every benchmark's likelihood profile overlaid (hover values, zoom, 2ΔlnL / likelihood-ratio / "
    "confidence-level axes), best fits with both confidence sets, the generating truths and pulls on "
    "request, the archived validation as charts, sortable tables and CSV/JSON export. With a "
    "benchmark_id the dashboard opens focused on that benchmark. Give the link when the user wants a web page, or when this client "
    "cannot show the SBI panel itself (for example a terminal client). The page asks once for the "
    "user's LQCDMCP dashboard token. No HPC sign-in, workspace or Slurm.")

INSTRUCTIONS = (
    "NESAP SBI (tools sbi_options, sbi_infer, sbi_validation, sbi_panel) is a separate workflow from the JAM "
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
    "Preserve the scientific provenance in exports and acknowledge unsupported requests.\n"
    "Answer in plain text, in the user's language, question by question: first the benchmark and "
    "the best fit, then the 68% and 95% confidence sets (every component, flagging any truncated "
    "one), then whether the numerical checks passed; the generating truth and the validation only "
    "when asked or relevant. Give alpha_s to six decimals, as returned, never recomputed. End with "
    "one line saying that only the likelihood ran live and everything else was produced earlier. "
    + ("The SBI panel shown with sbi_infer's answer draws this result's likelihood curve with both "
       "confidence sets; there the user can show the generating truth, run another benchmark and "
       "download the result JSON. Point the user to it instead of describing the plot."
       if PANEL else
       "If the user wants the curve or the full result, give the returned numbers (curve.alpha and "
       "curve.twice_delta_log_likelihood) rather than describing a plot you cannot show.")
    + "\nWhen the user asks for a web page or a link, or this client shows no SBI panel (a terminal "
    "client such as Claude Code or Codex), call sbi_panel with the same benchmark_id and give the "
    "returned panel_url: the page draws that benchmark's likelihood. The page asks for the LQCDMCP "
    "dashboard token; never ask for the token in chat.")

_backend = _server = None
_page_url = None

# The browser page is upstream's panel. Served on its own (not inside an MCP
# host) the panel calls /api/<kind> with a bearer token and shows its token
# form on HTTP 401. This script, put in front of it, answers those calls with
# the gateway's own tools/call on this URL, as the JAM panel's page does, and
# keeps the token in this browser tab (sessionStorage, the JAM panel's key).
# ?benchmark=<id> preselects that benchmark and runs it once.
PAGE_SHIM = r"""<script>
(()=>{'use strict';
const KEY='jam_gateway_token',realFetch=window.fetch.bind(window),TOOLS={options:'sbi_options',validation:'sbi_validation',infer:'sbi_infer'};
const wanted=new URLSearchParams(location.search).get('benchmark')||'';let sid='',autorun=false;
const reply=(status,data)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
function header(h,name){if(!h)return'';if(typeof h.get==='function')return h.get(name)||'';for(const k in h)if(k.toLowerCase()===name.toLowerCase())return h[k];return''}
function runOnce(){if(autorun)return;autorun=true;let tries=0;const t=setInterval(()=>{const b=document.getElementById('run');if(b&&!b.disabled){clearInterval(t);b.click()}else if(++tries>100)clearInterval(t)},50)}
window.fetch=async(url,opt={})=>{
 const m=typeof url==='string'&&url.match(/^\/api\/(options|validation|infer)$/);if(!m)return realFetch(url,opt);
 let token=header(opt.headers,'Authorization').replace(/^bearer\s+/i,'').trim();
 if(token){try{sessionStorage.setItem(KEY,token)}catch(e){}}else{try{token=sessionStorage.getItem(KEY)||''}catch(e){}}
 if(!token)return reply(401,{error:'The LQCDMCP dashboard token is needed.'});
 const headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream','Authorization':'Bearer '+token};if(sid)headers['Mcp-Session-Id']=sid;
 let d;try{const r=await realFetch(location.pathname,{method:'POST',headers,body:JSON.stringify({jsonrpc:'2.0',id:Date.now()%1e9,method:'tools/call',params:{name:TOOLS[m[1]],arguments:m[1]==='infer'?JSON.parse(opt.body||'{}'):{}}})});
  const s=r.headers.get('Mcp-Session-Id');if(s)sid=s;d=await r.json()}catch(e){return reply(502,{error:'The gateway did not answer.'})}
 if(d.error)return reply(502,{error:d.error.message||'The gateway refused the request.'});
 const res=d.result||{};
 if(res.structuredContent&&res.structuredContent.status==='unauthorized'){try{sessionStorage.removeItem(KEY)}catch(e){}return reply(401,{error:'The gateway did not accept this token.'})}
 if(res.isError)return reply(400,{error:(res.content||[]).map(c=>c.text||'').join(' ').replace(/^error:\s*/,'')||'The tool failed.'});
 const out=res.structuredContent;
 if(m[1]==='options'&&wanted&&out&&(out.benchmarks||[]).some(b=>b.id===wanted)){out.default_benchmark_id=wanted;runOnce()}
 return reply(200,out)};
document.addEventListener('DOMContentLoaded',()=>{const l=document.querySelector('label[for="token"]');if(l)l.textContent='LQCDMCP dashboard token';
 const n=document.querySelector('#auth p');if(n)n.textContent='The same token the JAM panel asks for. Kept in this browser tab only.'});
})();
</script>
"""


def configure(page_url):
    global _page_url
    _page_url = page_url


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
        meta = copy.deepcopy(t.get("_meta") or {}) if PANEL else {}
        if "openai/outputTemplate" in meta:
            meta["openai/outputTemplate"] = UI_URI_SKYBRIDGE
        desc = DESCRIPTIONS.get(t["name"]) or "NESAP SBI only. " + t["description"]
        out[t["name"]] = {"desc": desc, "schema": copy.deepcopy(t["inputSchema"]),
                          "annotations": copy.deepcopy(t.get("annotations")), "meta": meta or None,
                          "fn": lambda a, name=t["name"]: _call(name, a)}
    if _page_url:
        out["sbi_panel"] = {
            "desc": PANEL_DESCRIPTION,
            "schema": {"type": "object", "additionalProperties": False,
                       "properties": {"benchmark_id": {"type": "string"}}},
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
            "meta": None, "fn": _panel_link}
    return out


def _panel_link(args):
    args = {k: v for k, v in (args or {}).items() if k != "username"}
    if set(args) - {"benchmark_id"}:
        raise ValueError("Unsupported arguments")
    benchmark = args.get("benchmark_id") or ""
    if benchmark and benchmark not in [b["id"] for b in _backend.get_options()["benchmarks"]]:
        raise ValueError("Unknown benchmark_id. Choose benchmark-1 through benchmark-5.")
    return {"panel_url": _page_url(benchmark=benchmark), "benchmark_id": benchmark or None,
            "guidance": "Give the user this link to open in a browser. The page asks once for the "
                        "LQCDMCP dashboard token (the JAM panel's token); never ask for it in chat. "
                        + ("It opens on " + benchmark + " and draws its likelihood." if benchmark else
                           "The user picks a benchmark there and presses Run inference.")}


# The dashboard (dashboard.html, this plugin's own file) draws every benchmark,
# the validation and their comparisons from the same tools, interactively; it
# is the browser page and the chat widget. Upstream's panel stays reachable as
# the classic view (?app=sbi&view=classic). WIDGET = "classic" puts it back in chat.
WIDGET = "dashboard"


# The gateway's shared chart code, inlined where the dashboard marks it.
CHARTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "jamai", "static", "jamcharts.js")


def _dashboard():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "dashboard.html"), encoding="utf-8") as fh:
        html = fh.read()
    with open(CHARTS, encoding="utf-8") as fh:
        return html.replace("/*@jamcharts*/", fh.read(), 1)


def _widget():
    return _dashboard() if WIDGET == "dashboard" else _panel()


def page(query=None):
    if (query or {}).get("view") != "classic":
        return _dashboard()
    html = _panel()
    head = '<meta charset="utf-8">'
    if head not in html:
        raise ValueError("unexpected panel.html: no charset meta")
    return html.replace(head, head + "\n" + PAGE_SHIM, 1)


def _panel():
    with open(os.path.join(UPSTREAM, "static", "panel.html"), encoding="utf-8") as fh:
        return fh.read()


def resources():
    if not PANEL:
        return {}
    meta = {"ui": {"prefersBorder": True}, "openai/widgetPrefersBorder": True}
    return {UI_URI: {"name": "sbi-panel", "mimeType": _server.UI_MIME, "text": _widget, "meta": meta,
                     "description": "NESAP SBI likelihood panel"},
            UI_URI_SKYBRIDGE: {"name": "sbi-panel-chatgpt", "mimeType": "text/html+skybridge",
                               "text": _widget, "meta": meta,
                               "description": "NESAP SBI likelihood panel (ChatGPT widget)"}}
