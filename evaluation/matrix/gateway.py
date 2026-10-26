"""A local gateway for one matrix run: pinned commit, catalog variant, denies.

    python -m evaluation.matrix.gateway start --label ab_full --commit HEAD --variant full --condition C --port 8766
    python -m evaluation.matrix.gateway preflight --port 8766 --condition C --workspace /w/ceph24/JAM/chunyu/bench_eval/mcp/RUN
    python -m evaluation.matrix.gateway stop --port 8766 --archive ~/bench_eval/results/RUN

The gateway runs from a git worktree of the application at the pinned commit
(~/bench_eval/gw/<label>), bound to 127.0.0.1, with its own access directory
(audit log, idempotency file) per port, a per-matrix bearer token
(~/bench_eval/gw_token, 0600; its sha256 goes to the gateway, the token to the
bridge through the environment), the condition's deny list, and no audit
mirror to the farm. A variant 'B' worktree carries the derived condition-B
catalog in place of analyses/JAM24/catalog. Gateways older than the token
mode get the token as JAM_GW_TEST_TOKEN and no server-side deny list; the
preflight says so, and the client-side deny list still applies.

The gateway reaches ifarm through the ControlMaster sockets in ~/fm-cm
(gateway-<user>, ifarm-<user>), as in the pilot; `preflight` reports which
socket they resolve to.
"""
import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from evaluation.common import paths
from evaluation.conditions import deny_lists

GW_DIR = os.path.join(paths.EVAL_ROOT, "gw")
TOKEN_FILE = os.path.join(paths.EVAL_ROOT, "gw_token")
OWNER = os.environ.get("EVAL_GW_OWNER", "chunyu")
CM_DIR = os.path.expanduser("~/fm-cm")


def url(port):
    return "http://127.0.0.1:%d/mcp" % port


def token():
    """The matrix's gateway token; created once."""
    if os.path.isfile(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    paths.eval_dir()
    value = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
    return value


def rpc(port, method, params=None, tok=None, timeout=120):
    body = {"jsonrpc": "2.0", "id": int(time.time() * 1000) % 1000000, "method": method, "params": params or {}}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "Authorization": "Bearer " + (tok or token())}
    req = urllib.request.Request(url(port), data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    if raw.lstrip().startswith("data:") or "\ndata:" in raw:
        data = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
        raw = data[-1] if data else raw
    return json.loads(raw)


def tool(port, name, arguments=None, tok=None, timeout=180):
    """tools/call; returns the structured result (or the parsed text content)."""
    out = rpc(port, "tools/call", {"name": name, "arguments": arguments or {}}, tok, timeout)
    if "error" in out:
        return {"status": "error", "rpc_error": out["error"]}
    res = out.get("result") or {}
    if isinstance(res.get("structuredContent"), dict):
        return res["structuredContent"]
    for c in res.get("content") or []:
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except ValueError:
                return {"text": c["text"], "isError": res.get("isError")}
    return res


def tool_names(port, tok=None):
    out = rpc(port, "tools/list", {}, tok)
    return sorted(t["name"] for t in (out.get("result") or {}).get("tools") or [])


def worktree(label, commit):
    """~/bench_eval/gw/<label> checked out at commit (detached); returns (path, sha)."""
    os.makedirs(GW_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(GW_DIR, label)
    if not os.path.isdir(path):
        subprocess.run(["git", "-C", paths.APP_ROOT, "worktree", "add", "--detach", path, commit], check=True,
                       capture_output=True, text=True)
    sha = subprocess.check_output(["git", "-C", path, "rev-parse", "HEAD"], text=True).strip()
    return path, sha


def overlay_variant_b(tree):
    """Put the derived condition-B catalog into the worktree; returns its report."""
    from evaluation.conditions import derive_catalog
    with tempfile.TemporaryDirectory() as tmp:
        report = derive_catalog.write_tree(tmp)
        src = os.path.join(tmp, "analyses", "JAM24", "catalog")
        dst = os.path.join(tree, "analyses", "JAM24", "catalog")
        for name in os.listdir(src):
            shutil.copyfile(os.path.join(src, name), os.path.join(dst, name))
        shutil.copyfile(os.path.join(tmp, "derived_catalog_B.report.json"), os.path.join(tree, "analyses", "JAM24", "derived_catalog_B.report.json"))
    return {k: report[k] for k in ("variant_sha256", "source_sha256", "leaks")}


def capabilities(tree):
    with open(os.path.join(tree, "lqcdmcp-deploy", "jam_gateway.py"), encoding="utf-8") as fh:
        src = fh.read()
    return {"token_mode": "JAM_GW_TOKEN_SHA256" in src, "tools_deny": "JAM_GW_TOOLS_DENY" in src,
            "audit": "JAM_GW_AUDIT_MIRROR" in src, "wait_for": "wait_for" in src, "jam_guide": "def jam_guide" in src,
            "card_from_catalog": "def campaign_card_from_catalog" in src}


def pidfile(port):
    return os.path.join(paths.EVAL_ROOT, "gw_%d.pid" % port)


def start(label, commit, variant, condition, port, results_dir=None):
    tree, sha = worktree(label, commit)
    caps = capabilities(tree)
    info = {"label": label, "commit": commit, "sha": sha, "worktree": tree, "variant": variant, "condition": condition,
            "port": port, "url": url(port), "capabilities": caps, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if variant == "B":
        info["catalog_variant"] = overlay_variant_b(tree)
    tok = token()
    access = paths.eval_dir("gw_access_%d" % port)
    env = dict(os.environ, JAM_GW_PORT=str(port), JAM_GW_BIND="127.0.0.1", JAM_GW_OWNER=OWNER,
               JAM_GW_NAME="jam-gateway-eval-%d" % port, JAM_GW_ACCESS_DIR=access, JAM_GW_AUDIT_MIRROR="0",
               JAM_GW_DASHBOARD_AUTH="0", JAM_GW_APP_DIR=tree)
    if caps["token_mode"]:
        env["JAM_GW_TOKEN_SHA256"] = hashlib.sha256(tok.encode()).hexdigest()
    else:
        env["JAM_GW_TEST_TOKEN"] = tok
    if caps["tools_deny"]:
        env["JAM_GW_TOOLS_DENY"] = deny_lists.env_value(condition)
    info["server_side_deny"] = caps["tools_deny"]
    results_dir = results_dir or paths.eval_dir("results")
    log = os.path.join(results_dir, "gateway_%d_%s.log" % (port, label))
    with open(log, "ab") as fh:
        proc = subprocess.Popen([sys.executable, "-B", "jam_gateway.py"], cwd=os.path.join(tree, "lqcdmcp-deploy"),
                                env=env, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    with open(pidfile(port), "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))
    info.update(pid=proc.pid, log=log, access_dir=access)
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            info["tools"] = tool_names(port, tok)
            break
        except (urllib.error.URLError, ConnectionError, OSError, ValueError):
            if proc.poll() is not None:
                raise RuntimeError("gateway exited at start; see " + log)
            time.sleep(1.5)
    else:
        raise RuntimeError("gateway did not answer within 90 s; see " + log)
    with open(os.path.join(results_dir, "gateway_%d.json" % port), "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1, sort_keys=True)
    return info


def preflight(port, condition, workspace):
    """Before a run: the hidden tools are absent, the workspace is set, the
    chain is up. Returns a report; 'ok' says whether all held."""
    tok = token()
    names = tool_names(port, tok)
    denied = set(deny_lists.denied(condition))
    visible_denied = sorted(denied & set(names))
    report = {"port": port, "condition": condition, "tools": len(names), "visible_denied": visible_denied,
              "cm": {k: os.path.realpath(os.path.join(CM_DIR, k)) if os.path.lexists(os.path.join(CM_DIR, k)) else None
                     for k in ("gateway-" + OWNER, "ifarm-" + OWNER)}}
    report["use_workspace"] = tool(port, "use_workspace", {"path": workspace}, tok)
    hpc = tool(port, "check_hpc_connection", {}, tok)
    report["check_hpc_connection"] = {k: hpc.get(k) for k in ("connected", "login_open", "cluster_reachable", "auth", "tools_denied",
                                                              "audit", "owner", "shared") if k in hpc}
    report["ok"] = (not visible_denied and bool(report["use_workspace"].get("ok", report["use_workspace"].get("status") == "ok"))
                    and bool(hpc.get("connected")))
    return report


def stop(port, archive_dir=None):
    path = pidfile(port)
    out = {"port": port, "stopped": False}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            pid = int(fh.read().strip() or 0)
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                time.sleep(0.2)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            out["stopped"] = True
        except ProcessLookupError:
            out["stopped"] = "already gone"
        os.remove(path)
    if archive_dir:
        os.makedirs(archive_dir, exist_ok=True)
        access = os.path.join(paths.EVAL_ROOT, "gw_access_%d" % port)
        for name in ("audit.jsonl", "idempotency.json"):
            src = os.path.join(access, name)
            if os.path.isfile(src):
                shutil.copyfile(src, os.path.join(archive_dir, "gateway_%d_%s" % (port, name)))
                out["archived_" + name] = True
        for name in ("audit.jsonl", "idempotency.json"):
            src = os.path.join(access, name)
            if os.path.isfile(src):
                os.remove(src)   # the next run's audit log starts a fresh chain
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evaluation.matrix.gateway")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--label", required=True)
    s.add_argument("--commit", required=True)
    s.add_argument("--variant", default="full", choices=["full", "B"])
    s.add_argument("--condition", default="C", choices=["A", "B", "C"])
    s.add_argument("--port", type=int, default=8766)
    p = sub.add_parser("preflight")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--condition", default="C", choices=["A", "B", "C"])
    p.add_argument("--workspace", required=True)
    t = sub.add_parser("stop")
    t.add_argument("--port", type=int, default=8766)
    t.add_argument("--archive")
    n = sub.add_parser("tools")
    n.add_argument("--port", type=int, default=8766)
    args = ap.parse_args(argv)
    if args.cmd == "start":
        print(json.dumps(start(args.label, args.commit, args.variant, args.condition, args.port), indent=1, sort_keys=True))
    elif args.cmd == "preflight":
        print(json.dumps(preflight(args.port, args.condition, args.workspace), indent=1, sort_keys=True))
    elif args.cmd == "stop":
        print(json.dumps(stop(args.port, args.archive), indent=1))
    elif args.cmd == "tools":
        print("\n".join(tool_names(args.port)))


if __name__ == "__main__":
    main()
